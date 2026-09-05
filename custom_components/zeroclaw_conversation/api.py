"""Shared helper for calling a ZeroClaw gateway.

Two different endpoints, for two different needs — see `async_call_webhook`
and `async_call_ws_chat` docstrings for why they aren't interchangeable.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_WEBHOOK_SECRET, DEFAULT_TIMEOUT


class ZeroClawError(Exception):
    """Raised when a call to ZeroClaw's gateway fails or errors out."""


def webhook_secret_for(entry) -> str | None:
    """This entry's effective `X-Webhook-Secret` value, or `None` when it
    has none configured (the default, and the case for every entry created
    before the field existed — see `CONF_WEBHOOK_SECRET` in const.py).

    An options-flow edit wins over the value captured at setup time, so an
    existing install can start sending the header without being torn down
    and re-added. An empty string is treated as "not configured" rather
    than as a secret that happens to be empty, so clearing the field in
    the options flow genuinely turns the header back off.

    Presence in `options` — not truthiness — is what decides which source
    wins, and that distinction is load-bearing: a plain
    `options.get(...) or data.get(...)` silently falls back to the
    setup-time value when the operator *clears* the field, so removing a
    secret would leave this still sending the stale one and 401-ing on
    every `/webhook` call while the UI showed an empty field. Caught by
    the precedence tests, not by review.
    """
    if CONF_WEBHOOK_SECRET in entry.options:
        value = entry.options[CONF_WEBHOOK_SECRET]
    else:
        value = entry.data.get(CONF_WEBHOOK_SECRET)
    return value or None


async def async_call_webhook(
    hass: HomeAssistant,
    host: str,
    token: str,
    message: str,
    session_id: str,
    agent: str | None = None,
    webhook_secret: str | None = None,
) -> str:
    """POST one message to ZeroClaw's `/webhook` and return the reply text.

    `agent` names a specific ZeroClaw `[agents.<alias>]` entry via the
    `?agent=` query param `/webhook` supports. Left unset, ZeroClaw picks
    one on its own (the "default" agent, or else whichever is enabled
    first) — which may not be the agent a later run of ZeroClaw's own
    Quickstart wizard actually configured, see docs/DECISIONS.md.

    Raises ZeroClawError on any failure (network, non-200, or malformed
    response) — callers are responsible for turning that into whatever
    error type their own platform expects.

    `session_id` is accepted for API-shape symmetry with `async_call_ws_chat`
    but is **not sent anywhere** — `/webhook` is stateless (confirmed by
    reading ZeroClaw's own REST API reference and its Rust handler source:
    the only headers it recognizes are `Authorization`, `Content-Type`,
    `X-Webhook-Secret`, and `X-Idempotency-Key`; there is no session/thread
    field anywhere in its request or response contract). An earlier version
    of this function sent a `X-Session-Id` header believing it threaded
    conversation history — it did not; ZeroClaw silently ignored it. Use
    `async_call_ws_chat` for anything that needs multi-turn continuity; this
    function is for one-shot calls only (`ai_task`'s `generate_data`, which
    is stateless by design).

    `webhook_secret`, when given, is sent as `X-Webhook-Secret` *in
    addition to* the bearer token — ZeroClaw's `gateway.webhook_secret`
    (0.8.5+) is a second factor on this endpoint, not an alternative to
    pairing: with both configured the gateway requires both, and rejects
    the call with 401 if either is missing or wrong. Left unset, no header
    is sent, which is correct whenever the gateway has no secret
    configured. Use `webhook_secret_for(entry)` to resolve it.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if webhook_secret:
        headers["X-Webhook-Secret"] = webhook_secret
    params = {"agent": agent} if agent else None

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            f"{host}/webhook",
            json={"message": message},
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                error = (
                    payload.get("error", f"HTTP {resp.status}")
                    if isinstance(payload, dict)
                    else f"HTTP {resp.status}"
                )
                raise ZeroClawError(str(error))
            return payload.get("response", "") if isinstance(payload, dict) else ""
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(f"Could not reach ZeroClaw at {host}: {err}") from err


async def async_call_ws_chat(
    hass: HomeAssistant,
    host: str,
    token: str,
    message: str,
    session_id: str,
    agent: str | None = None,
) -> str:
    """Send one turn to ZeroClaw over `GET /ws/chat` and return its reply.

    Unlike `/webhook` (stateless, see `async_call_webhook`), `/ws/chat`
    accepts a client-chosen `session_id` query param that ZeroClaw uses to
    persist and resume conversation history server-side — confirmed by
    reading `crates/zeroclaw-gateway/src/ws.rs`: `session_id` is documented
    there as "Client-chosen session ID for memory persistence", and the
    server's own `session_start` reply reports `resumed`/`message_count`
    for it. Opening a fresh, short-lived connection per turn and reusing the
    same `session_id` (this integration passes Home Assistant's own
    `conversation_id`) gets the same multi-turn continuity a single
    held-open socket would, without this integration having to manage a
    live connection's lifecycle across separate, independent
    `_async_handle_message` calls.

    Protocol (confirmed against `ws.rs`, no public doc covers the exact
    frame sequence): the server sends `{"type": "session_start", ...}`
    immediately on connect, before it will read anything — this function
    doesn't wait for it (sending our `message` frame doesn't depend on it
    arriving first) and simply ignores any frame that isn't `"done"` or
    `"error"`. `{"type": "message", "content": "..."}` is the outbound
    frame; the reply is `{"type": "done", "full_response": "..."}`.

    Raises ZeroClawError on any failure — same contract as
    `async_call_webhook`.
    """
    ws_host = host.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"session_id": session_id}
    if agent:
        params["agent"] = agent

    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(DEFAULT_TIMEOUT):
            async with session.ws_connect(
                f"{ws_host}/ws/chat", headers=headers, params=params
            ) as ws:
                await ws.send_json({"type": "message", "content": message})
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    msg_type = data.get("type")
                    if msg_type == "done":
                        return data.get("full_response", "")
                    if msg_type == "error":
                        raise ZeroClawError(
                            str(data.get("message", "unknown error"))
                        )
                    # session_start, connected, approval_request, etc. —
                    # not the final reply, keep waiting.
                raise ZeroClawError(
                    "ZeroClaw closed the chat connection without a reply"
                )
    except TimeoutError as err:
        raise ZeroClawError(
            f"Timed out waiting for ZeroClaw's reply from {host}"
        ) from err
    except aiohttp.ClientError as err:
        raise ZeroClawError(f"Could not reach ZeroClaw at {host}: {err}") from err
    except json.JSONDecodeError as err:
        raise ZeroClawError(f"ZeroClaw sent an invalid chat frame: {err}") from err


async def async_fetch_quickstart_state(hass: HomeAssistant, host: str, token: str) -> dict:
    """Return the raw `GET /api/quickstart/state` payload.

    The same endpoint ZeroClaw's own dashboard calls to decide whether to
    show its Quickstart wizard. Carries, among other things, `"agents"`
    (configured agent aliases) and `"model_provider_types"` (every provider
    kind ZeroClaw's build supports, with a display name and whether it's a
    "local" — no API key — kind) — confirmed against a real running
    gateway. There's no more specific endpoint documented for either.
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/quickstart/state",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise ZeroClawError(f"HTTP {resp.status}")
            payload = await resp.json(content_type=None)
            return payload if isinstance(payload, dict) else {}
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(f"Could not fetch quickstart state from {host}: {err}") from err


async def async_fetch_agents(hass: HomeAssistant, host: str, token: str) -> list[str]:
    """Return the configured agent aliases ZeroClaw currently knows about."""
    state = await async_fetch_quickstart_state(hass, host, token)
    agents = state.get("agents")
    return [str(a) for a in agents] if isinstance(agents, list) else []


async def async_fetch_configured_providers(
    hass: HomeAssistant, host: str, token: str
) -> list[str]:
    """Return `model_providers`: already-configured `<type>.<alias>` strings
    (e.g. `"anthropic.household"`) — confirmed against a real running
    gateway to reflect exactly the literal `[providers.models.<type>.
    <alias>]` sections in `config.toml`, nothing implicit/synthesized.

    These are meant to be pre-configured by the companion `zeroclaw`
    add-on's own `providers` option (seeded before the daemon starts, on
    every boot — see that repo's docs/DECISIONS.md for why credentials
    written through this integration's own config flow turned out *not* to
    reliably take effect for actual LLM calls, even though the write itself
    persisted). This integration no longer collects fresh provider
    credentials itself for that reason — only picks from this list.
    """
    state = await async_fetch_quickstart_state(hass, host, token)
    providers = state.get("model_providers")
    return [str(p) for p in providers] if isinstance(providers, list) else []


async def async_apply_quickstart(
    hass: HomeAssistant,
    host: str,
    token: str,
    *,
    agent_name: str,
    system_prompt: str,
    model_provider_alias: str,
) -> None:
    """Create a brand-new agent (`POST /api/quickstart/apply`) referencing
    an *already-configured* `model_provider_alias` (a `<type>.<alias>`
    string from `async_fetch_configured_providers`, `"mode": "existing"`) —
    plus a fresh "balanced" risk/runtime profile pair, a fresh sqlite
    memory backend, no channels. Confirmed against a real running gateway
    that "existing" mode needs no separate model field (it's read from the
    provider's own config). This is the exact payload shape ZeroClaw's own
    Quickstart wizard sends — reverse engineered field-by-field against a
    real running gateway (undocumented anywhere) by reading each `missing
    field '...'` deserialization error in turn; see docs/DECISIONS.md for
    the full derivation.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "model_provider": {"mode": "existing", "value": model_provider_alias},
        "risk_profile": {"mode": "fresh", "value": "balanced"},
        "runtime_profile": {"mode": "fresh", "value": "balanced"},
        "memory": {"mode": "fresh", "value": "sqlite"},
        "channels": [],
        "agent": {"name": agent_name, "system_prompt": system_prompt},
    }

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            f"{host}/api/quickstart/apply",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or (isinstance(body, dict) and body.get("kind") == "errors"):
                errors = body.get("errors") if isinstance(body, dict) else None
                detail = "; ".join(
                    str(e.get("message", e)) for e in errors
                ) if errors else f"HTTP {resp.status}"
                raise ZeroClawError(f"ZeroClaw rejected the new agent: {detail}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(f"Could not create the agent on {host}: {err}") from err


async def async_fetch_personality_templates(
    hass: HomeAssistant, host: str, token: str
) -> list[dict]:
    """Return ZeroClaw's own default personality file templates —
    `GET /api/personality/templates` → `{"preset", "files": [{"filename",
    "content"}, ...]}`. These are the same files a fresh Quickstart-created
    agent starts with if you don't write your own (SOUL.md, IDENTITY.md,
    USER.md, AGENTS.md, TOOLS.md, HEARTBEAT.md, MEMORY.md).
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/personality/templates",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise ZeroClawError(f"HTTP {resp.status}")
            payload = await resp.json(content_type=None)
            files = payload.get("files") if isinstance(payload, dict) else None
            return list(files) if isinstance(files, list) else []
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(f"Could not fetch personality templates from {host}: {err}") from err


async def async_write_personality_file(
    hass: HomeAssistant, host: str, token: str, agent: str, filename: str, content: str
) -> None:
    """Write one personality file for `agent` —
    `PUT /api/personality/<filename>?agent=<agent>`, body
    `{"content", "expected_mtime_ms"}`. `expected_mtime_ms: null` means
    "create or overwrite unconditionally" (ZeroClaw's own dashboard sends
    the file's last-known mtime here to detect concurrent edits — not a
    concern for a file this integration just created).
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session = async_get_clientsession(hass)
    try:
        async with session.put(
            f"{host}/api/personality/{filename}",
            params={"agent": agent},
            json={"content": content, "expected_mtime_ms": None},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise ZeroClawError(f"HTTP {resp.status} writing {filename}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(f"Could not write {filename} on {host}: {err}") from err


async def async_grant_mcp_bundle(
    hass: HomeAssistant, host: str, token: str, agent: str, bundle: str
) -> None:
    """Add `bundle` to `agent`'s `mcp_bundles` list, if not already present.

    Same read-modify-write pattern confirmed working for the companion
    `zeroclaw` add-on's own post-boot MCP-bundle reconciliation (its
    `run.sh` does the equivalent over loopback with `curl`+`jq` — see that
    repo's docs/DECISIONS.md, including why this has to go through the
    live `/api/config/prop` endpoint rather than the offline CLI, which
    can neither read nor reliably write this dynamic map path).

    `GET /api/config/prop` returns the array as a JSON-encoded *string*
    (e.g. `"[\\"wallet\\"]"`), not a native JSON array — parsed here, not
    assumed. Fetching the current value first (rather than blindly
    appending) is what keeps this idempotent: calling it again for an
    agent that already has the bundle is a no-op, not a growing duplicate
    list.
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    path = f"agents.{agent}.mcp_bundles"

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/config/prop",
            params={"path": path},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise ZeroClawError(f"HTTP {resp.status} reading {path}")
            payload = await resp.json(content_type=None)

        raw_value = payload.get("value") if isinstance(payload, dict) else None
        try:
            current = json.loads(raw_value) if raw_value else []
        except (TypeError, ValueError):
            current = []
        if not isinstance(current, list):
            current = []

        if bundle in current:
            return  # already granted, nothing to do

        put_headers = {**headers, "Content-Type": "application/json"}
        async with session.put(
            f"{host}/api/config/prop",
            json={"path": path, "value": [*current, bundle]},
            headers=put_headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise ZeroClawError(f"HTTP {resp.status} writing {path}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(
            f"Could not grant MCP bundle '{bundle}' to agent '{agent}' on {host}: {err}"
        ) from err


async def async_list_sessions(hass: HomeAssistant, host: str, token: str) -> list[dict]:
    """Return `GET /api/sessions`'s `"sessions"` list — every `/ws/chat`
    session ZeroClaw's own session backend has persisted, each entry
    carrying `session_id` (display form), `session_key` (the full DB key
    `async_delete_session` needs), `created_at`, `last_activity`,
    `message_count`, `agent_alias`, `channel_id`, and an optional `name` —
    confirmed against `crates/zeroclaw-gateway/src/api.rs`'s
    `handle_api_sessions_list`. Used for zombie-session cleanup
    (`session_cleanup.py`): every Assist chat window this integration ever
    talks through mints a fresh `conversation_id`/`session_id` when
    reopened (see `conversation.py`'s docstring), so the *previous*
    session's history sits in ZeroClaw's session backend forever unless
    something explicitly deletes it — nothing in ZeroClaw itself expires
    these on its own.
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{host}/api/sessions",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise ZeroClawError(f"HTTP {resp.status}")
            payload = await resp.json(content_type=None)
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
            return list(sessions) if isinstance(sessions, list) else []
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(f"Could not list sessions from {host}: {err}") from err


async def async_delete_session(
    hass: HomeAssistant, host: str, token: str, session_key: str
) -> None:
    """`DELETE /api/sessions/{session_key}` — permanently remove one
    session's persisted history. `session_key` must be the full DB key
    `async_list_sessions` returns (not the shorter display `session_id` —
    ZeroClaw accepts either form and resolves them the same way
    internally, but the full key avoids relying on that fallback).
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    session = async_get_clientsession(hass)
    try:
        async with session.delete(
            f"{host}/api/sessions/{session_key}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            if resp.status not in (200, 404):
                # 404 = already gone, treated as success (idempotent delete).
                raise ZeroClawError(f"HTTP {resp.status} deleting session {session_key}")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ZeroClawError(
            f"Could not delete session {session_key} on {host}: {err}"
        ) from err
