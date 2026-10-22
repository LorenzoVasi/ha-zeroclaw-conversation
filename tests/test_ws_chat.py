"""`/ws/chat` frame handling: streaming, approvals, and failing fast.

An `approval_request` (ZeroClaw's `supervised` level, for any tool not in
`auto_approve`) used to sit unanswered until the 30s timeout, surfacing as
"Timed out waiting for ZeroClaw's reply" — see docs/DECISIONS.md.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.zeroclaw_conversation.api import (
    ZeroClawError,
    async_call_ws_chat,
    async_stream_ws_chat,
)

HOST = "http://local-zeroclaw:42617"
APPROVAL = {
    "type": "approval_request",
    "request_id": "req-1",
    "tool": "home_assistant__cover__HassOpenCover",
    "arguments_summary": "name: Cancello",
    "timeout_secs": 120,
}


class _FakeWs:
    """Hands out `frames` one `receive()` at a time, then reports closed.
    A frame of `None` never arrives (for the idle timeout)."""

    def __init__(self, frames: list[dict | None]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive(self):
        if not self._frames:
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
        frame = self._frames.pop(0)
        if frame is None:
            await asyncio.Event().wait()
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))


def _patched_session(ws: _FakeWs):
    async def ws_connect(*_args, **_kwargs):
        return ws

    return patch(
        "custom_components.zeroclaw_conversation.api.async_get_clientsession",
        return_value=SimpleNamespace(ws_connect=ws_connect),
    )


async def _collect(hass, ws, **kwargs) -> list[str]:
    with _patched_session(ws):
        return [
            chunk
            async for chunk in async_stream_ws_chat(
                hass, HOST, "tok", "ciao", "s1", **kwargs
            )
        ]


async def test_chunks_are_yielded_as_they_arrive(hass: HomeAssistant):
    ws = _FakeWs(
        [
            {"type": "session_start"},
            {"type": "chunk", "content": "Spengo "},
            {"type": "tool_call", "id": "1", "name": "x", "args": {}},
            {"type": "chunk", "content": "le luci."},
            {"type": "done", "full_response": "Spengo le luci."},
        ]
    )
    assert await _collect(hass, ws) == ["Spengo ", "le luci."]
    assert ws.sent == [{"type": "message", "content": "ciao"}]


async def test_full_response_is_used_when_nothing_streamed(hass: HomeAssistant):
    ws = _FakeWs([{"type": "done", "full_response": "Fatto."}])
    assert await _collect(hass, ws) == ["Fatto."]


async def test_collected_call_joins_the_stream(hass: HomeAssistant):
    ws = _FakeWs(
        [
            {"type": "chunk", "content": "Fat"},
            {"type": "chunk", "content": "to."},
            {"type": "done", "full_response": "Fatto."},
        ]
    )
    with _patched_session(ws):
        assert await async_call_ws_chat(hass, HOST, "tok", "ciao", "s1") == "Fatto."


async def test_aborted_turn_raises(hass: HomeAssistant):
    with pytest.raises(ZeroClawError, match="aborted"):
        await _collect(hass, _FakeWs([{"type": "aborted"}]))


async def test_connection_closed_without_done_raises(hass: HomeAssistant):
    with pytest.raises(ZeroClawError, match="without a reply"):
        await _collect(hass, _FakeWs([{"type": "chunk", "content": "a"}]))


async def test_silence_times_out(hass: HomeAssistant):
    with (
        patch("custom_components.zeroclaw_conversation.api.DEFAULT_TIMEOUT", 0.01),
        pytest.raises(ZeroClawError, match="Timed out"),
    ):
        await _collect(hass, _FakeWs([{"type": "session_start"}, None]))


async def test_approval_without_handler_is_denied_and_names_the_tool(
    hass: HomeAssistant,
):
    ws = _FakeWs([APPROVAL, {"type": "done", "full_response": "unreached"}])
    with pytest.raises(ZeroClawError) as err:
        await _collect(hass, ws)

    assert "home_assistant__cover__HassOpenCover" in str(err.value)
    assert "auto_approve" in str(err.value)
    assert ws.sent[-1] == {
        "type": "approval_response",
        "request_id": "req-1",
        "decision": "deny",
    }


async def test_approval_handler_that_cannot_ask_behaves_like_no_handler(
    hass: HomeAssistant,
):
    async def nobody(*_args):
        return None

    ws = _FakeWs([APPROVAL])
    with pytest.raises(ZeroClawError, match="auto_approve"):
        await _collect(hass, ws, approval_handler=nobody)
    assert ws.sent[-1]["decision"] == "deny"


@pytest.mark.parametrize(("answer", "decision"), [(True, "approve"), (False, "deny")])
async def test_a_persons_answer_is_sent_and_the_turn_continues(
    hass: HomeAssistant, answer, decision
):
    asked = []

    async def person(tool, summary, timeout_secs):
        asked.append((tool, summary, timeout_secs))
        return answer

    ws = _FakeWs([APPROVAL, {"type": "chunk", "content": "Ok."}, {"type": "done"}])
    assert await _collect(hass, ws, approval_handler=person) == ["Ok."]
    assert asked == [("home_assistant__cover__HassOpenCover", "name: Cancello", 120)]
    assert ws.sent[-1] == {
        "type": "approval_response",
        "request_id": "req-1",
        "decision": decision,
    }
