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

from .api import ZeroClawError, async_call_webhook, async_call_ws_chat
from .const import CONF_AGENT, CONF_API_TOKEN, CONF_HOST, DATA_LAST_USER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_NOTIFY_AGENT = "notify_agent"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ZeroClaw conversation entity, and the `notify_agent`
    entity service — the inbound half of "trigger the agent from a Home
    Assistant automation instead of a token-costing heartbeat poll" (see
    docs/DECISIONS.md): an automation whose trigger is a state change (a
    washing machine finishing, say) calls this service, targeting this
    entity, as its action. `entity_platform.async_register_entity_service`
    handles the usual `entity_id`/`device_id`/`area_id` target resolution
    and dispatches to `async_notify_agent` on each matching entity — the
    same pattern `services.yaml` documents for the UI/automation editor.
    """
    async_add_entities([ZeroClawConversationEntity(entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_NOTIFY_AGENT,
        {vol.Required("message"): str},
        "async_notify_agent",
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

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._host: str = entry.data[CONF_HOST].rstrip("/")
        self._token: str = entry.data.get(CONF_API_TOKEN) or ""
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
        conversation_id = user_input.conversation_id or uuid.uuid4().hex
        response = intent.IntentResponse(language=user_input.language)

        # Remember who's talking, per config entry — read back by
        # `webhook.py`'s notify handler (`person_notify.py`) to resolve
        # *whose* phone to push a later proactive notification to, instead
        # of a fixed notify target configured once at setup. `context.
        # user_id` is `None` for some non-interactive contexts; leaving the
        # previous value in place then (not clearing it) means a stale-but-
        # real "last known user" beats notifying nobody.
        if user_input.context.user_id:
            self.hass.data.setdefault(DOMAIN, {}).setdefault(DATA_LAST_USER_ID, {})[
                self._entry.entry_id
            ] = user_input.context.user_id

        try:
            reply = await async_call_ws_chat(
                self.hass,
                self._host,
                self._token,
                user_input.text,
                conversation_id,
                agent=self._agent,
            )
        except ZeroClawError as err:
            _LOGGER.warning("ZeroClaw call failed: %s", err)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"ZeroClaw error: {err}",
            )
            return conversation.ConversationResult(
                response=response, conversation_id=conversation_id
            )

        response.async_set_speech(reply)
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id
        )

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
            )
        except ZeroClawError as err:
            _LOGGER.warning("notify_agent service call failed: %s", err)
            raise HomeAssistantError(str(err)) from err
