"""Config flow, against real Home Assistant core.

Worth having as more than a smoke test: this repo has shipped a config
flow that looked right and only failed once actually loaded by HA (see
docs/DECISIONS.md — `supported_languages` turning out to be an abstract
property, the default host guess being wrong twice). Running the flow
through the real harness is the cheapest way to keep catching that class
of thing.
"""

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.zeroclaw_conversation.const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HA_URL,
    CONF_HOST,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    DOMAIN,
)

HOST = "http://local-zeroclaw:42617"

_VALIDATE = "custom_components.zeroclaw_conversation.config_flow._validate_host"
_FETCH_AGENTS = "custom_components.zeroclaw_conversation.config_flow.async_fetch_agents"


async def _run_flow(hass: HomeAssistant, user_step: dict, agent: str = "mario"):
    """Drive both steps of the happy path and return the final result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with (
        patch(_VALIDATE, return_value=None),
        patch(_FETCH_AGENTS, return_value=["mario", "other"]),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_step
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "agent"

        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AGENT: agent}
        )


async def test_happy_path_without_a_webhook_secret(hass: HomeAssistant):
    """The shape every existing install has: no secret configured."""
    result = await _run_flow(
        hass,
        {CONF_HOST: HOST, CONF_API_TOKEN: "tok", CONF_HA_URL: ""},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ZeroClaw (mario)"
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_API_TOKEN] == "tok"
    assert result["data"][CONF_AGENT] == "mario"
    assert result["data"][CONF_WEBHOOK_SECRET] == ""


async def test_webhook_secret_is_stored_when_given(hass: HomeAssistant):
    result = await _run_flow(
        hass,
        {
            CONF_HOST: HOST,
            CONF_API_TOKEN: "tok",
            CONF_WEBHOOK_SECRET: "s3cr3t",
            CONF_HA_URL: "",
        },
    )
    assert result["data"][CONF_WEBHOOK_SECRET] == "s3cr3t"


async def test_blank_ha_url_means_no_webhook_id(hass: HomeAssistant):
    """Leaving Home Assistant's URL blank switches the notify/watch
    feature off entirely — no inbound webhook is registered for it."""
    result = await _run_flow(
        hass, {CONF_HOST: HOST, CONF_API_TOKEN: "", CONF_HA_URL: ""}
    )
    assert result["data"][CONF_WEBHOOK_ID] == ""


async def test_ha_url_generates_a_webhook_id(hass: HomeAssistant):
    result = await _run_flow(
        hass,
        {
            CONF_HOST: HOST,
            CONF_API_TOKEN: "",
            CONF_HA_URL: "http://homeassistant:8123",
        },
    )
    assert result["data"][CONF_WEBHOOK_ID]


async def test_trailing_slash_is_stripped_from_the_host(hass: HomeAssistant):
    """`api.py` builds every URL as f"{host}/…", so a trailing slash
    would produce `//webhook` on every call."""
    result = await _run_flow(
        hass, {CONF_HOST: f"{HOST}/", CONF_API_TOKEN: "", CONF_HA_URL: ""}
    )
    assert result["data"][CONF_HOST] == HOST


async def test_unreachable_gateway_shows_an_error_not_a_traceback(
    hass: HomeAssistant,
):
    from custom_components.zeroclaw_conversation.config_flow import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(_VALIDATE, side_effect=CannotConnect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: HOST, CONF_API_TOKEN: "", CONF_HA_URL: ""},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_same_host_with_a_different_agent_is_allowed(hass: HomeAssistant):
    """Uniqueness is (host, agent), not host alone — one gateway can host
    several agents, each worth its own entry. Regression guard for the
    earlier `_async_abort_entries_match({CONF_HOST: host})` that blocked
    the second one outright."""
    first = await _run_flow(
        hass, {CONF_HOST: HOST, CONF_API_TOKEN: "", CONF_HA_URL: ""}, agent="mario"
    )
    assert first["type"] is FlowResultType.CREATE_ENTRY

    second = await _run_flow(
        hass, {CONF_HOST: HOST, CONF_API_TOKEN: "", CONF_HA_URL: ""}, agent="other"
    )
    assert second["type"] is FlowResultType.CREATE_ENTRY


async def test_same_host_and_agent_is_rejected(hass: HomeAssistant):
    await _run_flow(
        hass, {CONF_HOST: HOST, CONF_API_TOKEN: "", CONF_HA_URL: ""}, agent="mario"
    )
    duplicate = await _run_flow(
        hass, {CONF_HOST: HOST, CONF_API_TOKEN: "", CONF_HA_URL: ""}, agent="mario"
    )
    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
