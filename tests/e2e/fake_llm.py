"""A fake, hardcoded, OpenAI-compatible model provider.

ZeroClaw is provider-agnostic and its `custom` provider slot accepts any
OpenAI-compatible endpoint, so pointing it at this makes a real ZeroClaw
daemon run real agent turns with completely deterministic replies, no API
key, and no network egress. That is what makes an end-to-end test of
"Home Assistant → integration → ZeroClaw → model" possible at all: the
one genuinely non-deterministic, costly, rate-limited component gets
swapped for a lookup table, and everything else in the path stays real.

Replies are chosen by substring match on the last user message
(`RESPONSES`, first match wins), falling back to `DEFAULT_RESPONSE`. Both
the plain and the SSE-streaming shapes of the chat-completions API are
served, because which one ZeroClaw uses is its choice, not ours.

Run standalone:

    python fake_llm.py --port 8081
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from aiohttp import web

MODEL_ID = "fake-model"

# Substring (lowercased) -> canned reply. First match wins, so put the
# more specific phrases first.
RECORDED: list[dict[str, Any]] = []
"""Every chat request this server has served, newest last.

Exposed on `GET /_requests` so tests can assert on what was *sent to the
model*, not only on what came back. That direction matters: the 0.2.1
bug shipped a prompt containing a `vol.Schema` Python repr instead of a
JSON schema, which no assertion on the reply could ever have caught,
because the reply was fine — it was simply an answer to a badly-phrased
question.
"""

MAX_RECORDED = 50


RESPONSES: list[tuple[str, str]] = [
    # Structured `ai_task` requests, answered the way a chatty household
    # agent really answers them: correct JSON, wrapped in prose and a
    # markdown code fence. This is the shape that broke Home Assistant's
    # built-in AI suggestions against a live instance, so the e2e suite
    # reproduces it rather than assuming the model behaves.
    # NOTE ON ORDER: every structured `ai_task` prompt also carries the
    # boilerplate "This is an automated data request from Home Assistant"
    # that `ai_task.py` appends, so the generic entry below matches all of
    # them. Anything that needs a *different* structured reply has to come
    # first — this bit me once already.
    #
    # Structured reply that is already clean JSON — the easy path, kept
    # distinct so a regression in fence-stripping can't be masked by it.
    ("richiesta pulita", '{"suggestions": ["Chiudi il garage"]}'),
    # Structured request the model answers in prose: the integration must
    # surface a clear error rather than invent data.
    ("richiesta impossibile", "Mi dispiace, non ho capito cosa vuoi."),
    (
        "automated data request",
        (
            'Certo! Ecco i suggerimenti:\n\n```json\n'
            '{"suggestions": ["Spegni le luci del corridoio"]}\n```\n\n'
            "Fammi sapere."
        ),
    ),
    # Greeting-by-name: the reply echoes the name so a test can prove the
    # speaker context actually reached the model, rather than assuming.
    ("lorenzo", "Ciao Lorenzo! Come posso aiutarti in casa?"),
    ("ping", "pong"),
    ("lavatrice", "La lavatrice ha finito, avvio l'asciugatrice."),
    ("chi sei", "Sono l'assistente di casa."),
    ("hello", "Hello! How can I help around the house?"),
    # Fired-watch follow-up and the notify_agent service both arrive as
    # ordinary one-shot turns; distinct replies make them identifiable in
    # the recorded requests.
    ("accendi light.luci_scale", "Fatto, ho acceso le luci delle scale."),
    ("automazione", "Ricevuto, me ne occupo io."),
]

DEFAULT_RESPONSE = "ok"


def _reply_for(messages: list[dict[str, Any]]) -> str:
    """The canned reply for the last user turn."""
    last_user = ""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            content = message.get("content")
            # `content` is a plain string on the chat-completions wire,
            # but multimodal callers send a list of parts; accept both so
            # a future image test doesn't silently match nothing.
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            last_user = str(content or "")
            break

    lowered = last_user.lower()
    for needle, reply in RESPONSES:
        if needle in lowered:
            return reply
    return DEFAULT_RESPONSE


def _completion_body(reply: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        # Non-zero so anything downstream that records token usage (the
        # add-on turns on cost tracking) has something real to record.
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    }


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def handle_models(_request: web.Request) -> web.Response:
    """`GET /v1/models` — used for onboarding and model listing.

    Deliberately carries no `pricing` block: this stands in for a gateway
    that doesn't publish prices, which is the case the add-on's
    `live_pricing` seeding has to degrade gracefully for.
    """
    return web.json_response(
        {
            "object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "owned_by": "fake"}],
        }
    )


async def handle_requests(_request: web.Request) -> web.Response:
    """`GET /_requests` — what this server has been asked, for assertions
    about the prompt. Not part of any real API; purely a test affordance."""
    return web.json_response({"requests": RECORDED})


async def handle_reset(_request: web.Request) -> web.Response:
    """`POST /_reset` — clear the recording between tests."""
    RECORDED.clear()
    return web.json_response({"status": "ok"})


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """`POST /v1/chat/completions`, streaming or not."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is the caller's bug
        return web.json_response({"error": "invalid JSON"}, status=400)

    messages = body.get("messages", [])
    RECORDED.append(
        {
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "messages": messages,
            # Everything the caller sent beyond the standard fields —
            # `chat_template_kwargs` and friends ride in here, so a test
            # can assert the add-on's provider seeding actually reaches
            # the wire instead of only reaching config.toml.
            "extra": {
                k: v
                for k, v in body.items()
                if k not in {"model", "messages", "stream"}
            },
        }
    )
    del RECORDED[:-MAX_RECORDED]

    reply = _reply_for(messages)

    if not body.get("stream"):
        return web.json_response(_completion_body(reply))

    response = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
    )
    await response.prepare(request)
    await response.write(_chunk({"role": "assistant"}))
    await response.write(_chunk({"content": reply}))
    await response.write(_chunk({}, finish="stop"))
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    return response


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/_requests", handle_requests)
    app.router.add_post("/_reset", handle_reset)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    # Some callers are configured with a base URL that already ends in
    # /v1 and some aren't; serve both so a trailing-slash mistake in a
    # fixture fails the assertion rather than the connection.
    app.router.add_get("/models", handle_models)
    app.router.add_post("/chat/completions", handle_chat_completions)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    web.run_app(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
