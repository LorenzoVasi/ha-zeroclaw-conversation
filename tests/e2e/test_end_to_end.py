"""End-to-end tests spanning both repositories.

Everything in the path is real except the model: Home Assistant core (the
same harness the rest of the suite uses), this integration, and a real
ZeroClaw daemon built from the `addon-zeroclaw` repository's own
Dockerfile — talking to `fake_llm.py` instead of a paid, non-deterministic
provider. See `tests/e2e/README.md` for how the stack is brought up.

Skipped unless `ZEROCLAW_E2E_HOST` points at a running gateway, so the
ordinary `pytest` run stays hermetic and fast.

Why this exists: this project's entire bug history is assumptions about
ZeroClaw's contract that only a running instance disproved — a header
that was never read, an endpoint that had no session concept, a config
write that silently did nothing. Unit tests cannot see any of that.
"""

from __future__ import annotations

import json
import os
import uuid

import aiohttp
import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeroclaw_conversation.const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HA_URL,
    CONF_HOST,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

HOST = os.environ.get("ZEROCLAW_E2E_HOST", "")
TOKEN = os.environ.get("ZEROCLAW_E2E_TOKEN", "e2e-token")
AGENT = os.environ.get("ZEROCLAW_E2E_AGENT", "default")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not HOST, reason="ZEROCLAW_E2E_HOST not set; see tests/e2e/README.md"
    ),
]


async def test_webhook_returns_the_canned_reply():
    """The contract `ai_task`, `notify_agent` and a fired watch depend on."""
    async with aiohttp.ClientSession() as session, session.post(
        f"{HOST}/webhook",
        json={"message": "ping"},
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        assert resp.status == 200
        assert (await resp.json())["response"] == "pong"


async def test_ws_chat_resumes_the_same_session():
    """Assist's own path, and the reason it was rewritten: an earlier
    version threaded conversation history through a header `/webhook`
    never read, so every turn started from scratch. `resumed` on the
    second connect is the only real proof continuity works."""
    session_id = f"e2e-{uuid.uuid4().hex}"
    ws_host = HOST.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

    async def _turn(text: str) -> tuple[str, dict]:
        start: dict = {}
        async with aiohttp.ClientSession() as session, session.ws_connect(
            f"{ws_host}/ws/chat",
            params={"session_id": session_id, "agent": AGENT},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as ws:
            await ws.send_json({"type": "message", "content": text})
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "session_start":
                    start = data
                elif data.get("type") == "done":
                    return data.get("full_response", ""), start
                elif data.get("type") == "error":
                    pytest.fail(f"ZeroClaw returned an error frame: {data}")
        pytest.fail("connection closed without a reply")

    first_reply, first_start = await _turn("ping")
    assert first_reply == "pong"
    assert first_start.get("resumed") is False

    _second_reply, second_start = await _turn("ping")
    assert second_start.get("resumed") is True
    assert second_start.get("message_count", 0) > first_start.get("message_count", 0)


async def _setup_agent_entity(hass: HomeAssistant) -> str:
    """Configure the integration against the live gateway and return the
    conversation entity's id.

    Looked up from the entity registry rather than constructed by hand:
    the id is derived from the entry title, so hardcoding it makes the
    test fail for a reason that has nothing to do with what it's testing.
    """
    assert await async_setup_component(hass, "conversation", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"ZeroClaw ({AGENT})",
        data={
            CONF_HOST: HOST,
            CONF_API_TOKEN: TOKEN,
            CONF_AGENT: AGENT,
            CONF_HA_URL: "",
            CONF_WEBHOOK_ID: "",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    conversation_entities = [e for e in entities if e.domain == "conversation"]
    assert len(conversation_entities) == 1, conversation_entities
    return conversation_entities[0].entity_id


async def test_assist_turn_through_real_home_assistant(
    hass: HomeAssistant, real_client_session
):
    """The actual cross-repo test: a real Assist conversation, handled by
    this integration's conversation entity, answered by a real ZeroClaw."""
    agent_id = await _setup_agent_entity(hass)

    result = await conversation.async_converse(
        hass, "ping", conversation_id=None, context=Context(), agent_id=agent_id
    )

    assert result.response.speech["plain"]["speech"] == "pong"


async def test_assist_reply_reflects_the_prompt(
    hass: HomeAssistant, real_client_session
):
    """A second phrase, so the test can't pass on a hardcoded "pong"
    coming from anywhere other than the fake model's lookup table."""
    agent_id = await _setup_agent_entity(hass)

    result = await conversation.async_converse(
        hass,
        "la lavatrice ha finito?",
        conversation_id=None,
        context=Context(),
        agent_id=agent_id,
    )

    assert "asciugatrice" in result.response.speech["plain"]["speech"]
