"""Config flow for ZeroClaw Conversation.

Steps: (1) host + API token, validated by reachability; (2) pick an agent
alias from a live-fetched list (falls back to a free-text field if the
fetch fails — a wrong/missing token on a paired gateway, or an older
ZeroClaw without the endpoint this relies on, shouldn't block setup
entirely), or create one; (3, only when creating) name it and pick which
*already-configured* model provider it should use.

Provider credentials themselves are NOT collected here — they live in the
companion `zeroclaw` add-on's own `providers` option, seeded before
ZeroClaw's daemon starts. Writing a fresh provider's credentials through
this integration's own config flow (via ZeroClaw's live HTTP API) used to
be possible, but turned out not to reliably take effect for actual LLM
calls even though the write itself persisted — see docs/DECISIONS.md.

Step 1 also collects `CONF_HA_URL` and generates this entry's webhook ID
(`webhook.async_generate_id()`) — the notify/watch feature's setup, see
`webhook.py`/`watch.py`/`person_notify.py`/docs/DECISIONS.md ("Scheduling
and event-driven triggers"). Optional; leaving it blank skips the feature
entirely for this entry. *Who* gets notified isn't configured here at
all — resolved dynamically from whoever most recently talked to the agent
(see `person_notify.py`), not a fixed target picked at setup time.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ZeroClawError,
    async_apply_quickstart,
    async_fetch_agents,
    async_fetch_configured_providers,
    async_fetch_personality_templates,
    async_grant_mcp_bundle,
    async_write_personality_file,
)
from .const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HA_URL,
    CONF_HOST,
    CONF_WEBHOOK_ID,
    DEFAULT_HA_URL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .personality import build_personality_files, default_system_prompt, sanitize_agent_alias

HOME_ASSISTANT_MCP_BUNDLE = "home_assistant"
"""The MCP bundle name the companion `zeroclaw` add-on seeds (server +
bundle, both named "home_assistant") when its `home_assistant_token`
option is set — see that repo's `run.sh` / docs/DECISIONS.md. Hardcoded
here rather than discovered live: there is currently no ZeroClaw endpoint
that lists configured MCP bundles the way `/api/quickstart/state` lists
agents and providers, and the add-on always uses this exact name."""

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "http://local-zeroclaw:42617"
"""Internal DNS name for a *locally installed* `zeroclaw` add-on
(config.yaml slug `zeroclaw`, installed from the HA add-on store's "Local
add-ons" section — not from a published repository). Confirmed by running
`hostname` inside the actual running container on a real Home Assistant
instance: `local-zeroclaw` (hyphenated). Portainer displays the Docker
*container name* as `app_local_zeroclaw` (underscored) — a different,
easily-confused value; Docker/Supervisor sanitizes container names into
RFC-1123-valid hostnames by swapping `_` for `-`, so the container name is
NOT the same string as its resolvable hostname. Trust `hostname` run
inside the container, not the Portainer container-list name, if this ever
needs re-confirming. If your `zeroclaw` add-on came from a published
repository instead, or you renamed the slug, this will differ — check by
running `hostname` inside that container (Portainer's console, or the SSH
add-on).
"""

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Optional(CONF_API_TOKEN, default=""): str,
        # Enables the agent proactively notifying/watching (see
        # docs/DECISIONS.md, "Scheduling and event-driven triggers"): leave
        # blank to skip teaching the agent a notify-webhook URL entirely,
        # rather than forcing every setup through an extra required field
        # for a feature not everyone wants. *Who* gets pushed a
        # notification isn't configured here at all — resolved dynamically
        # from Home Assistant's own person/device linkage, whoever most
        # recently talked to this agent (see `person_notify.py`).
        vol.Optional(CONF_HA_URL, default=DEFAULT_HA_URL): str,
    }
)

AUTO_AGENT_VALUE = ""
"""Sentinel stored/selected when the user wants ZeroClaw's own automatic
agent pick rather than naming one explicitly — kept as the empty string so
it round-trips unchanged through `CONF_AGENT` and `api.py`'s `agent=`
handling (falsy → omitted from the request), no separate translation
needed on the read side.
"""

CREATE_NEW_AGENT_VALUE = "__create_new__"
"""Sentinel for the "create a new agent" option in the step-2 dropdown —
distinct from AUTO_AGENT_VALUE and from any real agent alias (ZeroClaw
alias syntax doesn't allow leading/trailing underscores this dense, but the
real guarantee is just that this exact string is never returned by
`async_fetch_agents`)."""


class CannotConnect(Exception):
    """Raised when the ZeroClaw gateway can't be reached."""


async def _validate_host(hass, host: str) -> None:
    """Confirm a ZeroClaw gateway answers at `host`.

    Only checks reachability via the gateway's `/health` endpoint — it does
    NOT validate `api_token`, since doing that would mean POSTing a real
    message to `/webhook` during setup (an unauthenticated probe would either
    pointlessly consume an LLM call or, without a `message`, exercise
    undocumented edge-case behavior). A wrong token instead surfaces the
    first time Assist actually talks to ZeroClaw, with a clear error.
    """
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host.rstrip('/')}/health",
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status >= 500:
                raise CannotConnect(f"ZeroClaw returned HTTP {resp.status}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise CannotConnect(str(err)) from err


class ZeroClawConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ZeroClaw Conversation."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._token: str = ""
        self._ha_url: str = ""
        self._webhook_id: str = ""
        self._new_agent_display_name: str = ""
        self._new_agent_alias: str = ""

    def _notify_webhook_url(self) -> str | None:
        """The full URL this entry's agent should POST to for notify/watch
        requests (see `webhook.py`), or `None` if the feature is off
        (`CONF_HA_URL` left blank) — in which case `TOOLS.md` simply
        doesn't get the notify section (see `personality.py`)."""
        if not self._ha_url or not self._webhook_id:
            return None
        return f"{self._ha_url}{webhook.async_generate_path(self._webhook_id)}"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: host + API token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            # Uniqueness is (host, agent), not host alone — a single
            # ZeroClaw gateway can host multiple agents, each worth its own
            # config entry (e.g. its own name/area in HA). The duplicate
            # check happens in step 2, once the agent is actually known.
            try:
                await _validate_host(self.hass, host)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self._host = host
                self._token = user_input.get(CONF_API_TOKEN, "")
                self._ha_url = user_input.get(CONF_HA_URL, "").rstrip("/")
                if self._ha_url:
                    self._webhook_id = webhook.async_generate_id()
                return await self.async_step_agent()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_agent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: pick an agent alias from a live-fetched list, or create one."""
        if user_input is not None:
            agent = user_input.get(CONF_AGENT, AUTO_AGENT_VALUE)
            if agent == CREATE_NEW_AGENT_VALUE:
                return await self.async_step_new_agent()
            return self._finish(agent)

        try:
            agents = await async_fetch_agents(self.hass, self._host, self._token)
        except ZeroClawError as err:
            _LOGGER.debug("Could not fetch ZeroClaw agent list: %s", err)
            agents = None  # fall back to free text below

        auto_label = "Auto (let ZeroClaw pick)"
        if agents is not None:
            schema = vol.Schema(
                {
                    vol.Optional(CONF_AGENT, default=AUTO_AGENT_VALUE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=AUTO_AGENT_VALUE, label=auto_label
                                ),
                                *(
                                    selector.SelectOptionDict(value=a, label=a)
                                    for a in agents
                                ),
                                selector.SelectOptionDict(
                                    value=CREATE_NEW_AGENT_VALUE,
                                    label="+ Create a new agent",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            )
            fallback_note = ""
        else:
            # Fetch failed (wrong/missing token on a paired gateway, older
            # ZeroClaw without this endpoint, etc.) — don't block setup,
            # fall back to a plain text field. "Create a new agent" isn't
            # offered here either, since it needs the same live API.
            schema = vol.Schema({vol.Optional(CONF_AGENT, default=AUTO_AGENT_VALUE): str})
            fallback_note = (
                " Could not fetch the agent list from ZeroClaw — leave "
                "blank for automatic selection, or type an exact "
                "[agents.<alias>] name from ZeroClaw's own config."
            )

        return self.async_show_form(
            step_id="agent",
            data_schema=schema,
            description_placeholders={"fallback_note": fallback_note},
        )

    async def async_step_new_agent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: name the new agent and pick an already-configured provider.

        No credentials collected here — see this file's module docstring
        and docs/DECISIONS.md for why. If nothing is configured yet, aborts
        with a clear reason instead of showing a form with an empty,
        unusable dropdown.
        """
        try:
            providers = await async_fetch_configured_providers(
                self.hass, self._host, self._token
            )
        except ZeroClawError as err:
            _LOGGER.warning("Could not fetch ZeroClaw's configured providers: %s", err)
            providers = []

        if not providers:
            return self.async_abort(reason="no_providers_configured")

        errors: dict[str, str] = {}

        if user_input is not None:
            self._new_agent_display_name = user_input["name"].strip()
            # ZeroClaw's agent alias is a strict identifier — confirmed
            # against a real running gateway (its own rejection messages):
            # lowercase ASCII letters/digits only, single underscores,
            # must start/end with a letter or digit, no accented/unicode
            # characters at all. The alias is what ZeroClaw actually stores
            # and matches on; the display name the user typed is preserved
            # as-is for the system prompt and IDENTITY.md instead, where
            # it's just text a human (or the model) reads, not an
            # identifier — see docs/DECISIONS.md.
            self._new_agent_alias = sanitize_agent_alias(self._new_agent_display_name)
            if not self._new_agent_alias:
                errors["base"] = "agent_name_required"
            else:
                try:
                    await async_apply_quickstart(
                        self.hass,
                        self._host,
                        self._token,
                        agent_name=self._new_agent_alias,
                        system_prompt=default_system_prompt(
                            self._new_agent_display_name, self.hass.config.language
                        ),
                        model_provider_alias=user_input["model_provider"],
                    )
                except ZeroClawError as err:
                    _LOGGER.warning("Could not create ZeroClaw agent: %s", err)
                    errors["base"] = "create_agent_failed"
                else:
                    await self._async_write_home_helper_personality(
                        self._new_agent_alias, self._new_agent_display_name
                    )
                    await self._async_grant_home_assistant_mcp(self._new_agent_alias)
                    return self._finish(self._new_agent_alias)

        schema = vol.Schema(
            {
                vol.Required("name"): str,
                vol.Required("model_provider"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=p, label=p)
                            for p in providers
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="new_agent", data_schema=schema, errors=errors
        )

    async def _async_write_home_helper_personality(
        self, agent: str, display_name: str
    ) -> None:
        """Best-effort: write the home-helper personality files for a newly
        created agent. Failure here doesn't block agent creation — the
        agent already exists and works, just with ZeroClaw's own bare
        default personality until the user (or a retry) sets it up. See
        docs/DECISIONS.md.
        """
        try:
            templates = await async_fetch_personality_templates(
                self.hass, self._host, self._token
            )
            for f in build_personality_files(
                templates,
                display_name,
                self.hass.config.language,
                self._notify_webhook_url(),
            ):
                await async_write_personality_file(
                    self.hass,
                    self._host,
                    self._token,
                    agent,
                    f["filename"],
                    f["content"],
                )
        except ZeroClawError as err:
            _LOGGER.warning(
                "Agent '%s' was created, but writing its personality files failed: %s",
                agent,
                err,
            )

    async def _async_grant_home_assistant_mcp(self, agent: str) -> None:
        """Best-effort: grant the newly created agent the add-on's
        `home_assistant` MCP bundle immediately, rather than waiting for
        the add-on's own next restart (its `run.sh` reconciles this for
        every *existing* agent on every boot, but a just-created agent
        obviously can't wait for that). Silently a no-op if the add-on
        never configured Home Assistant integration (no such bundle
        exists) — logged, not fatal, same as the personality-file write.
        """
        try:
            await async_grant_mcp_bundle(
                self.hass, self._host, self._token, agent, HOME_ASSISTANT_MCP_BUNDLE
            )
        except ZeroClawError as err:
            _LOGGER.warning(
                "Agent '%s' was created, but granting it the '%s' MCP bundle failed "
                "(this is expected if the zeroclaw add-on hasn't configured Home "
                "Assistant integration): %s",
                agent,
                HOME_ASSISTANT_MCP_BUNDLE,
                err,
            )

    def _finish(self, agent: str) -> ConfigFlowResult:
        """Common tail for both the "pick existing" and "create new" paths."""
        # (host, agent) together identify a distinct service — this is
        # the actual uniqueness key, not host alone (see async_step_user).
        self._async_abort_entries_match({CONF_HOST: self._host, CONF_AGENT: agent})
        title = f"ZeroClaw ({agent})" if agent else "ZeroClaw"
        return self.async_create_entry(
            title=title,
            data={
                CONF_HOST: self._host,
                CONF_API_TOKEN: self._token,
                CONF_AGENT: agent,
                CONF_HA_URL: self._ha_url,
                CONF_WEBHOOK_ID: self._webhook_id,
            },
        )
