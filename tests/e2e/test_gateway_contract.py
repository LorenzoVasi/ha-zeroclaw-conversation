"""Every ZeroClaw gateway surface this integration depends on, checked
against a real daemon.

These are deliberately raw HTTP rather than going through Home Assistant:
they pin the *contract* — the endpoints, the auth rules, the response
shapes — separately from the code that consumes it. When one of these
fails after a ZeroClaw upgrade, the cause is upstream, and knowing that
before reading any Python is the point.

Every assumption recorded in docs/DECISIONS.md as "confirmed against a
running gateway" should have a test here, because that file is otherwise
the only thing keeping those findings alive.
"""

from __future__ import annotations

import json
import os
import uuid

import aiohttp
import pytest

HOST = os.environ.get("ZEROCLAW_E2E_HOST", "")
SECURED_HOST = os.environ.get("ZEROCLAW_E2E_SECURED_HOST", "")
TOKEN = os.environ.get("ZEROCLAW_E2E_TOKEN", "e2e-token")
SECRET = os.environ.get("ZEROCLAW_E2E_WEBHOOK_SECRET", "e2e-webhook-secret")
AGENT = os.environ.get("ZEROCLAW_E2E_AGENT", "default")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not HOST, reason="ZEROCLAW_E2E_HOST not set; see tests/e2e/README.md"
    ),
]

needs_secured = pytest.mark.skipif(
    not SECURED_HOST, reason="ZEROCLAW_E2E_SECURED_HOST not set"
)


async def _post_webhook(host: str, headers: dict[str, str], message: str = "ping"):
    async with aiohttp.ClientSession() as session, session.post(
        f"{host}/webhook",
        json={"message": message},
        headers={"Content-Type": "application/json", **headers},
        timeout=aiohttp.ClientTimeout(total=60),
    ) as resp:
        body = await resp.text()
        return resp.status, body


async def _get(host: str, path: str, headers: dict[str, str] | None = None):
    async with aiohttp.ClientSession() as session, session.get(
        f"{host}{path}",
        headers=headers or {},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        return resp.status, await resp.text()


# --- POST /webhook: the stateless one-shot surface --------------------
# Used by ai_task, the notify_agent service, and a fired watch's
# follow-up message.


async def test_webhook_answers_with_the_model_reply():
    status, body = await _post_webhook(HOST, {"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    assert json.loads(body)["response"] == "pong"


async def test_webhook_rejects_a_missing_token():
    status, _ = await _post_webhook(HOST, {})
    assert status == 401


async def test_webhook_rejects_a_wrong_token():
    status, _ = await _post_webhook(HOST, {"Authorization": "Bearer nope"})
    assert status == 401


async def test_webhook_is_stateless():
    """`/webhook` has no session concept — the contract `api.py`'s
    docstring records after an earlier version wrongly believed an
    `X-Session-Id` header threaded history through it. Two calls that
    would obviously differ if history were kept must not differ."""
    headers = {"Authorization": f"Bearer {TOKEN}"}
    _, first = await _post_webhook(HOST, headers, "ping")
    _, second = await _post_webhook(HOST, headers, "ping")
    assert json.loads(first)["response"] == json.loads(second)["response"] == "pong"


# --- GET /ws/chat: the surface every Assist turn uses ------------------


async def _ws_turn(session_id: str, text: str) -> tuple[str, dict]:
    ws_host = HOST.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
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


async def test_ws_chat_answers():
    reply, _ = await _ws_turn(f"e2e-{uuid.uuid4().hex}", "ping")
    assert reply == "pong"


async def test_ws_chat_resumes_the_same_session():
    """The reason Assist was moved off `/webhook`: only this endpoint
    keeps history, and `resumed` on the second connect is the proof."""
    session_id = f"e2e-{uuid.uuid4().hex}"

    _, first = await _ws_turn(session_id, "ping")
    assert first.get("resumed") is False

    _, second = await _ws_turn(session_id, "ping")
    assert second.get("resumed") is True
    assert second.get("message_count", 0) > first.get("message_count", 0)


async def test_ws_chat_treats_a_new_session_id_as_a_fresh_conversation():
    """Closing and reopening the Assist window mints a new
    `conversation_id`; that must start clean rather than resume."""
    _, first = await _ws_turn(f"e2e-{uuid.uuid4().hex}", "ping")
    _, second = await _ws_turn(f"e2e-{uuid.uuid4().hex}", "ping")
    assert first.get("resumed") is False
    assert second.get("resumed") is False


# --- /api/*: what the config flow and session cleanup rely on ---------


async def test_quickstart_state_exposes_agents_and_providers():
    """`async_fetch_agents` / `async_fetch_configured_providers` read
    this; the config flow's agent dropdown is built from it."""
    status, body = await _get(
        HOST, "/api/quickstart/state", {"Authorization": f"Bearer {TOKEN}"}
    )
    assert status == 200
    payload = json.loads(body)
    assert AGENT in payload["agents"]
    # The add-on seeded exactly this provider for the stack.
    assert "custom.fake" in payload["model_providers"]


async def test_personality_templates_are_fetchable():
    """`async_fetch_personality_templates` — the base files a
    newly-created agent is built from."""
    status, body = await _get(
        HOST, "/api/personality/templates", {"Authorization": f"Bearer {TOKEN}"}
    )
    assert status == 200
    filenames = {f["filename"] for f in json.loads(body)["files"]}
    # The three this integration customizes must exist upstream, or
    # build_personality_files silently has nothing to layer onto.
    assert {"SOUL.md", "IDENTITY.md", "USER.md"} <= filenames


async def test_rest_api_requires_the_token():
    status, _ = await _get(HOST, "/api/sessions")
    assert status == 401


async def test_sessions_can_be_listed_and_deleted():
    """The whole basis of `session_cleanup.py`: sessions accumulate and
    nothing in ZeroClaw expires them, so this integration deletes its
    own."""
    session_id = f"e2e-cleanup-{uuid.uuid4().hex}"
    await _ws_turn(session_id, "ping")

    auth = {"Authorization": f"Bearer {TOKEN}"}
    status, body = await _get(HOST, "/api/sessions", auth)
    assert status == 200
    sessions = json.loads(body)["sessions"]

    mine = [s for s in sessions if session_id in s.get("session_id", "")]
    assert mine, f"the session just created is not listed: {session_id}"
    entry = mine[0]
    # The fields session_cleanup.py filters on must actually be present.
    assert entry.get("agent_alias") == AGENT
    assert "last_activity" in entry
    key = entry["session_key"]

    async with aiohttp.ClientSession() as session:
        async with session.delete(
            f"{HOST}/api/sessions/{key}", headers=auth,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            assert resp.status == 200
        # Deleting again is a 404, which async_delete_session treats as
        # success — an idempotency this integration relies on.
        async with session.delete(
            f"{HOST}/api/sessions/{key}", headers=auth,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            assert resp.status == 404


# --- the webhook-secret matrix ----------------------------------------
# `gateway.webhook_secret` is additive to pairing and applies to
# /webhook only. Configured on one side alone it silently 401s, which is
# exactly the kind of thing worth testing rather than documenting and
# hoping.


@needs_secured
async def test_secured_gateway_accepts_bearer_plus_secret():
    status, body = await _post_webhook(
        SECURED_HOST,
        {"Authorization": f"Bearer {TOKEN}", "X-Webhook-Secret": SECRET},
    )
    assert status == 200
    assert json.loads(body)["response"] == "pong"


@needs_secured
async def test_secured_gateway_rejects_bearer_alone():
    """The failure an operator hits when the add-on has a secret and the
    integration doesn't."""
    status, _ = await _post_webhook(SECURED_HOST, {"Authorization": f"Bearer {TOKEN}"})
    assert status == 401


@needs_secured
async def test_secured_gateway_rejects_a_wrong_secret():
    status, _ = await _post_webhook(
        SECURED_HOST,
        {"Authorization": f"Bearer {TOKEN}", "X-Webhook-Secret": "wrong"},
    )
    assert status == 401


@needs_secured
async def test_secured_gateway_rejects_the_secret_alone():
    """Additive, not an alternative: the secret does not replace pairing."""
    status, _ = await _post_webhook(SECURED_HOST, {"X-Webhook-Secret": SECRET})
    assert status == 401


@needs_secured
async def test_secured_gateway_still_serves_the_rest_api_without_the_secret():
    """The secret covers `/webhook` and `/sop/*` only — Assist and the
    config flow must keep working without it."""
    status, _ = await _get(
        SECURED_HOST, "/api/sessions", {"Authorization": f"Bearer {TOKEN}"}
    )
    assert status == 200


async def test_plain_gateway_ignores_an_unnecessary_secret_header():
    """The harmless half of a mismatch: a secret configured on the
    integration but not on the gateway must not break anything. Only the
    reverse is a problem — worth knowing which way round it is."""
    status, body = await _post_webhook(
        HOST,
        {"Authorization": f"Bearer {TOKEN}", "X-Webhook-Secret": "not-configured-here"},
    )
    assert status == 200
    assert json.loads(body)["response"] == "pong"
