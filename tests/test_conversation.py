"""An Assist turn through the real conversation entity, `/ws/chat` faked.

Covers what only shows up end to end: the reply streamed through the
chat log becomes the spoken answer, and every turn of one conversation —
the first included — reaches ZeroClaw under the same session id.
"""

from unittest.mock import patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er, intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeroclaw_conversation.api import ZeroClawError
from custom_components.zeroclaw_conversation.const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HOST,
    DOMAIN,
)

MODULE = "custom_components.zeroclaw_conversation.conversation"


class _FakeGateway:
    def __init__(self, chunks: list[str] | None = None, error: str | None = None):
        self.chunks = chunks if chunks is not None else ["Fatto, ", "luci spente."]
        self.error = error
        self.session_ids: list[str] = []

    async def stream(self, _hass, _host, _token, _message, session_id, **_kwargs):
        self.session_ids.append(session_id)
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise ZeroClawError(self.error)


@pytest.fixture
async def agent_id(hass: HomeAssistant):
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ZeroClaw (mario)",
        data={CONF_HOST: "http://local-zeroclaw:42617", CONF_API_TOKEN: "tok", CONF_AGENT: "mario"},
    )
    entry.add_to_hass(hass)
    # Kept patched for the whole test: unloading at teardown reads it too.
    with patch("custom_components.zeroclaw_conversation.PLATFORMS", ["conversation"]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        [entity] = [
            e.entity_id
            for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
            if e.domain == "conversation"
        ]
        yield entity


async def _converse(hass, agent_id, text, conversation_id=None):
    return await conversation.async_converse(
        hass, text, conversation_id, Context(), agent_id=agent_id
    )


async def test_streamed_reply_becomes_the_spoken_answer(hass: HomeAssistant, agent_id):
    gateway = _FakeGateway()
    with patch(f"{MODULE}.async_stream_ws_chat", gateway.stream):
        result = await _converse(hass, agent_id, "spegni le luci")

    assert result.response.speech["plain"]["speech"] == "Fatto, luci spente."


async def test_every_turn_uses_the_same_session(hass: HomeAssistant, agent_id):
    """The first turn used to go out under an id minted by the entity,
    and every later turn under Home Assistant's own — two ZeroClaw
    sessions for one conversation."""
    gateway = _FakeGateway()
    with patch(f"{MODULE}.async_stream_ws_chat", gateway.stream):
        first = await _converse(hass, agent_id, "ciao")
        await _converse(hass, agent_id, "e adesso?", first.conversation_id)

    assert gateway.session_ids == [first.conversation_id, first.conversation_id]


async def test_gateway_error_is_a_readable_error(hass: HomeAssistant, agent_id):
    gateway = _FakeGateway(chunks=[], error="ZeroClaw aborted the reply")
    with patch(f"{MODULE}.async_stream_ws_chat", gateway.stream):
        result = await _converse(hass, agent_id, "ciao")

    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert "aborted" in result.response.speech["plain"]["speech"]


async def test_empty_reply_is_a_readable_error(hass: HomeAssistant, agent_id):
    gateway = _FakeGateway(chunks=[])
    with patch(f"{MODULE}.async_stream_ws_chat", gateway.stream):
        result = await _converse(hass, agent_id, "ciao")

    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert "empty reply" in result.response.speech["plain"]["speech"]
