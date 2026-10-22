"""ZeroClaw conversation agent platform.

Forwards each Assist turn to a ZeroClaw gateway over `GET /ws/chat` and
speaks back whatever ZeroClaw replies. Multi-turn context lives on
ZeroClaw's side, keyed by `session_id` (mirrored from Home Assistant's own
`conversation_id`, which Home Assistant's Assist chat UI keeps stable for
as long as that chat window stays open — see `ha-assist-chat.ts` in
home-assistant/frontend — and mints fresh only when the window is closed
and reopened) rather than being re-sent from Home Assistant's chat log.

Not `/webhook`: that endpoint is stateless — confirmed by reading
ZeroClaw's own REST API reference and its Rust handler source, neither of
which has any session/thread concept at all. An earlier version of this
file sent a `X-Session-Id` header to `/webhook` believing it threaded
conversation history; it did not exist anywhere in ZeroClaw and was
silently ignored, so every Assist turn was landing as a brand-new,
context-free conversation on ZeroClaw's side regardless of Home Assistant's
own `conversation_id` — see docs/DECISIONS.md for the full read of
`crates/zeroclaw-gateway/src/{api_webhook.rs,ws.rs}` that found this.
"""

from __future__ import annotations

import logging
import uuid

import voluptuous as vol
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform, intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    ZeroClawError,
    api_token_for,
    async_call_webhook,
    async_stream_ws_chat,
    webhook_secret_for,
)
from .approval import async_ask_on_phone
from .const import (
    CONF_AGENT,
    CONF_CONTINUOUS_CONVERSATION,
    CONF_HOST,
    DATA_LAST_USER_ID,
    DOMAIN,
)
from .person_notify import async_resolve_person_name
from .session_cleanup import SessionRegistry, async_setup_cleanup

_LOGGER = logging.getLogger(__name__)

SERVICE_NOTIFY_AGENT = "notify_agent"

_SPEAKER_CONTEXT_TEMPLATE = (
    "[Home Assistant context, not written by the household — this is the "
    "first message of a brand-new conversation, and Home Assistant has "
    "identified the speaker as \"{name}\". Greet them by name naturally as "
    "part of your reply, then respond to their actual message below. Never "
    "quote or mention this note itself.]\n\n{text}"
)
"""Prepended to `user_input.text` only on a conversation's first turn (see
`_async_handle_message`), only when the speaker could be identified — see
docs/DECISIONS.md, "Greeting the speaker by name". English regardless of
the household's own language: SOUL.md's per-language "Always respond in
<language>" directive (`personality.py`) already anchors the reply
language, so this note doesn't need translating into all 64 supported
languages to be understood the same way English personality-file content
already is for every non-en/it language.
"""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ZeroClaw conversation entity, the `notify_agent` entity
    service — the inbound half of "trigger the agent from a Home Assistant
    automation instead of a token-costing heartbeat poll" (see
    docs/DECISIONS.md): an automation whose trigger is a state change (a
    washing machine finishing, say) calls this service, targeting this
    entity, as its action. `entity_platform.async_register_entity_service`
    handles the usual `entity_id`/`device_id`/`area_id` target resolution
    and dispatches to `async_notify_agent` on each matching entity — the
    same pattern `services.yaml` documents for the UI/automation editor —
    and periodic zombie-session cleanup (`session_cleanup.py`): every
    Assist chat window mints a fresh ZeroClaw session when reopened (see
    this module's own docstring), and nothing on ZeroClaw's own side ever
    expires the previous one. Cleanup only deletes sessions recorded in this
    entry's `SessionRegistry`, i.e. ones this entity itself opened — and
    only exists for an entry with a named agent (see `_async_cleanup`), so
    an "auto" entry records nothing it would never clean up.
    """
    registry: SessionRegistry | None = None
    if entry.data.get(CONF_AGENT):
        registry = SessionRegistry(hass, entry.entry_id)
        await registry.async_load()

    async_add_entities([ZeroClawConversationEntity(entry, registry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_NOTIFY_AGENT,
        {vol.Required("message"): str},
        "async_notify_agent",
    )

    if registry is not None:
        async_setup_cleanup(hass, entry, registry)


def _error_result(
    user_input: conversation.ConversationInput, conversation_id: str, message: str
) -> conversation.ConversationResult:
    response = intent.IntentResponse(language=user_input.language)
    response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN, message)
    return conversation.ConversationResult(
        response=response, conversation_id=conversation_id
    )


class ZeroClawConversationEntity(conversation.ConversationEntity):
    """A conversation agent that delegates to a ZeroClaw gateway.

    On this HA version, `ConversationEntity.supported_languages` is an
    abstract `@property` — confirmed by a real setup failure ("Can't
    instantiate abstract class ZeroClawConversationEntity without an
    implementation for abstract method 'supported_languages'"). Setting
    `_attr_supported_languages` as a class attribute alone does not satisfy
    it; it must be a concrete property override (below).
    """

    _attr_has_entity_name = True
    # Declares that this agent handles device control itself (via its own
    # MCP connection back into Home Assistant), not through HA's local
    # exposed-entity intent matching. Every LLM-backed conversation agent in
    # home-assistant/core declares this (Anthropic, OpenAI, Ollama, etc.) —
    # confirmed by reading assist_pipeline/pipeline.py: it only changes how
    # HA pre-filters sentences before reaching this entity (skips some local
    # intent matching when `prefer_local_intents` is on), nothing this
    # entity needs to implement itself. Without it, Assist shows "This
    # assistant can't control your home" even though ZeroClaw actually can,
    # just via its own MCP tool calls rather than HA's local intent system.
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL
    # The reply is fed into the chat log as `/ws/chat` streams it (see
    # `_async_handle_message`), so a voice pipeline starts speaking on the
    # first sentence instead of after the whole turn, tool calls included.
    # Same mechanism Ollama/OpenAI/Anthropic use in home-assistant/core
    # (`chat_log.async_add_delta_content_stream` +
    # `async_get_result_from_chat_log`, read at the 2026.10.2 tag).
    _attr_supports_streaming = True

    def __init__(
        self, entry: ConfigEntry, registry: SessionRegistry | None = None
    ) -> None:
        self._entry = entry
        self._registry = registry
        self._attr_unique_id = entry.entry_id
        self._host: str = entry.data[CONF_HOST].rstrip("/")
        self._token: str = api_token_for(entry)
        self._agent: str = entry.data.get(CONF_AGENT) or ""
        # Disambiguates multiple config entries against the same gateway,
        # each targeting a different ZeroClaw agent — without this, every
        # such entry would show up identically as "ZeroClaw" everywhere
        # (device list, Assist agent picker, etc.), making them impossible
        # to tell apart.
        self._attr_name = f"ZeroClaw ({self._agent})" if self._agent else "ZeroClaw"

    @property
    def supported_languages(self) -> list[str] | str:
        """ZeroClaw's own LLM handles language detection, not HA."""
        return "*"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._attr_name,
            "manufacturer": "ZeroClaw Labs",
            "entry_type": "service",
        }

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log,
    ) -> conversation.ConversationResult:
        """Send one turn to ZeroClaw and return its reply."""
        # Home Assistant leaves `conversation_id` unset on the very first
        # turn of a new conversation and expects the entity to mint one
        # (below) — the same signal used here to decide whether to greet
        # the speaker by name, checked before it's overwritten.
        is_new_conversation = not user_input.conversation_id
        # The chat log's id, not one minted here: Home Assistant already
        # minted it (a ULID, `helpers/chat_session.py`) and is what
        # `async_get_result_from_chat_log` hands back to the frontend, so
        # the next turn arrives with exactly this id. Minting a separate one
        # here, as this used to, gave the first turn of every conversation a
        # different ZeroClaw session from all the turns after it.
        conversation_id = chat_log.conversation_id

        # Remember who's talking, per config entry — read back by
        # `webhook.py`'s notify handler (`person_notify.py`) to resolve
        # *whose* phone to push a later proactive notification to, instead
        # of a fixed notify target configured once at setup. `context.
        # user_id` is `None` for some non-interactive contexts; leaving the
        # previous value in place then (not clearing it) means a stale-but-
        # real "last known user" beats notifying nobody.
        speaker_name: str | None = None
        if user_input.context.user_id:
            self.hass.data.setdefault(DOMAIN, {}).setdefault(DATA_LAST_USER_ID, {})[
                self._entry.entry_id
            ] = user_input.context.user_id
            speaker_name = async_resolve_person_name(
                self.hass, user_input.context.user_id
            )

        outgoing_text = user_input.text
        if is_new_conversation and speaker_name:
            outgoing_text = _SPEAKER_CONTEXT_TEMPLATE.format(
                name=speaker_name, text=user_input.text
            )
        _LOGGER.debug(
            "ZeroClaw speaker context: user_id=%s speaker_name=%s "
            "is_new_conversation=%s (annotation %s)",
            user_input.context.user_id,
            speaker_name,
            is_new_conversation,
            "added" if outgoing_text != user_input.text else "skipped",
        )

        # Recorded before the call, so a session ZeroClaw persists is
        # always one `session_cleanup.py` is allowed to delete later — and
        # nothing it didn't get from here ever is.
        if self._registry is not None:
            self._registry.async_add(conversation_id)

        async def _ask(tool: str, summary: str, timeout_secs: int) -> bool | None:
            # A gated tool (e.g. opening a cover) is confirmed on the
            # speaker's phone instead of being refused outright — see
            # approval.py.
            return await async_ask_on_phone(
                self.hass,
                self._entry,
                user_input.context.user_id,
                tool,
                summary,
                timeout_secs,
            )

        async def _deltas():
            yield {"role": "assistant"}
            async for chunk in async_stream_ws_chat(
                self.hass,
                self._host,
                self._token,
                outgoing_text,
                conversation_id,
                agent=self._agent,
                approval_handler=_ask,
            ):
                yield {"content": chunk}

        try:
            async for _content in chat_log.async_add_delta_content_stream(
                self.entity_id, _deltas()
            ):
                pass
        except ZeroClawError as err:
            _LOGGER.warning("ZeroClaw call failed: %s", err)
            return _error_result(user_input, conversation_id, f"ZeroClaw error: {err}")

        # An empty reply adds no assistant content at all, and
        # `async_get_result_from_chat_log` then raises on the user's own
        # message being last — answer with a readable error instead.
        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            return _error_result(
                user_input, conversation_id, "ZeroClaw error: empty reply"
            )
        result = conversation.async_get_result_from_chat_log(user_input, chat_log)
        # Read at call time, not cached at setup: an options change reloads
        # the entry anyway, but this keeps the source of truth in one place.
        if self._entry.options.get(CONF_CONTINUOUS_CONVERSATION):
            result.continue_conversation = True
        return result

    async def async_notify_agent(self, message: str) -> None:
        """Send `message` to this agent as one stateless turn — the
        `notify_agent` service handler (see `async_setup_entry`), the entry
        point for a Home Assistant automation that wants to tell the agent
        about something (e.g. "the washing machine just finished") instead
        of the agent finding out by polling.

        Uses the stateless `/webhook` call (`async_call_webhook`, same as
        `ai_task.py`), not `/ws/chat`: an automation-triggered event isn't
        part of an ongoing Assist conversation, so there is no
        `conversation_id` to thread continuity through. If the agent's own
        reply matters to whoever fired the automation, that's what the
        notify-webhook (`webhook.py`) is for — this call's return value
        (ZeroClaw's reply text) is intentionally discarded, matching how a
        "tell the agent something happened" action isn't expected to hand
        anything back to the automation that called it.
        """
        try:
            await async_call_webhook(
                self.hass,
                self._host,
                self._token,
                message,
                uuid.uuid4().hex,
                agent=self._agent,
                webhook_secret=webhook_secret_for(self._entry),
            )
        except ZeroClawError as err:
            _LOGGER.warning("notify_agent service call failed: %s", err)
            raise HomeAssistantError(str(err)) from err
