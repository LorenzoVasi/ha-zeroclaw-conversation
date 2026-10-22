"""`/ws/chat` frames Assist can't answer must fail fast, and say why.

An `approval_request` (ZeroClaw's `supervised` level, for any tool not in
`auto_approve`) used to sit unanswered until the 30s timeout, surfacing as
"Timed out waiting for ZeroClaw's reply" — see docs/DECISIONS.md.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.zeroclaw_conversation.api import (
    ZeroClawError,
    async_call_ws_chat,
)

HOST = "http://local-zeroclaw:42617"


class _FakeWs:
    def __init__(self, frames: list[dict]) -> None:
        self._frames = frames
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for frame in self._frames:
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))


def _patched_session(ws: _FakeWs):
    @asynccontextmanager
    async def ws_connect(*_args, **_kwargs):
        yield ws

    return patch(
        "custom_components.zeroclaw_conversation.api.async_get_clientsession",
        return_value=SimpleNamespace(ws_connect=ws_connect),
    )


async def test_reply_is_returned(hass: HomeAssistant):
    ws = _FakeWs(
        [
            {"type": "session_start"},
            {"type": "tool_call", "id": "1", "name": "x", "args": {}},
            {"type": "done", "full_response": "Fatto."},
        ]
    )
    with _patched_session(ws):
        assert await async_call_ws_chat(hass, HOST, "tok", "ciao", "s1") == "Fatto."


async def test_approval_request_is_denied_and_names_the_tool(hass: HomeAssistant):
    ws = _FakeWs(
        [
            {"type": "session_start"},
            {
                "type": "approval_request",
                "request_id": "req-1",
                "tool": "home_assistant__HassTurnOff",
                "arguments_summary": "{}",
                "timeout_secs": 120,
            },
            # Never reached: the call must not keep waiting after denying.
            {"type": "done", "full_response": "should not be returned"},
        ]
    )
    with _patched_session(ws), pytest.raises(ZeroClawError) as err:
        await async_call_ws_chat(hass, HOST, "tok", "spegni le luci", "s1")

    assert "home_assistant__HassTurnOff" in str(err.value)
    assert "auto_approve" in str(err.value)
    assert ws.sent[-1] == {
        "type": "approval_response",
        "request_id": "req-1",
        "decision": "deny",
    }
