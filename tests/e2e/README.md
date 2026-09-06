# End-to-end tests

The only tests here that span **both** repos. Everything in the path is
real except the model:

```
Home Assistant core (test harness)
  └─ this integration
       └─ two real ZeroClaw daemons, built from ../addon-zeroclaw/Dockerfile
            └─ fake_llm.py  ← the only fake part
```

`fake_llm.py` is a small OpenAI-compatible server that answers from a
lookup table. ZeroClaw's `custom` provider slot accepts any
OpenAI-compatible endpoint, so this swaps out the one component that is
non-deterministic, rate-limited and costs money, and leaves every other
link genuine.

**Two gateways, not one**: a plain one and one with
`gateway.webhook_secret` configured, so the webhook auth matrix is tested
rather than reasoned about — including the asymmetry that a secret set on
the integration but not the gateway is harmless, while the reverse breaks
AI Tasks and watch follow-ups but *not* Assist.

## What is covered

`test_gateway_contract.py` — raw HTTP against the daemon, pinning the
contract separately from the code that consumes it. When one of these
fails after a ZeroClaw upgrade, the cause is upstream, and knowing that
before reading any Python is the point.

- `/webhook`: replies, rejects a missing/wrong token, and is stateless.
- `/ws/chat`: replies, resumes a session, treats a new id as fresh.
- `/api/quickstart/state`, `/api/personality/templates`: what the config
  flow reads.
- `/api/sessions`: list, delete, and delete-again-is-404 — the whole
  basis of `session_cleanup.py`.
- The webhook-secret matrix on both gateways.

`test_end_to_end.py` — driven through Home Assistant.

- Assist: a turn, a second phrase, conversation continuity.
- Speaker recognition, asserted **in the prompt** rather than the reply
  (see the recorder below).
- AI Task: unstructured, structured-with-prose, structured-clean,
  structured-failure, and that a real JSON schema is sent.
- `notify_agent` service; a watch firing on an external change and
  notifying; a watch correctly *not* firing on a change Home Assistant
  itself made.
- `api.py`'s own client functions against the live gateway.
- The webhook secret matching, missing, and not affecting Assist.

## The recorder

The fake model exposes `GET /_requests` and `POST /_reset`, so tests can
assert on **what was sent to the model**, not only on what came back.
That direction matters: the 0.2.1 bug shipped a prompt containing a
`vol.Schema` Python repr instead of a JSON schema, and no assertion on
the reply could have caught it — the reply was a perfectly good answer to
a badly-phrased question.

## Running them

```bash
# from the repository root
ADDON_REPO=../addon-zeroclaw ./tests/e2e/up.sh
ZEROCLAW_E2E_HOST=http://localhost:42617 \
ZEROCLAW_E2E_SECURED_HOST=http://localhost:42618 \
ZEROCLAW_E2E_FAKE_LLM=http://localhost:8081 \
  python -m pytest -m e2e
./tests/e2e/down.sh
```

Takes about three minutes: every turn is a real agent loop. The ordinary
`pytest` run excludes them (`addopts = -m "not e2e"`) and each test skips
itself when its host isn't set, so nothing here slows the hermetic suite.

**In CI that skipping would be a trap** — a workflow missing a variable
would go green while testing nothing — so both workflows check the
variables in a shell step *before* pytest runs.

## Things that will waste your afternoon otherwise

**Seeding a provider does not bind an agent to it.** The add-on writes
`[providers.models.custom.fake]`, but each agent carries its own
`model_provider`, and the baked-in default points at
`openrouter.default` — so every turn fails with `LLM request failed`
until the agent is repointed. `up.sh` does it explicitly, through the
live `PUT /api/config/prop` API, because `zeroclaw config set` rejects
`agents.<alias>.model_provider` as an unknown property.

**The gateway answers on 42617, not 8099.** ZeroClaw binds loopback-only
on 8099; nginx fronts it on 42617, the only port reachable from outside.

**Order matters in the fake model's lookup table.** Every structured
`ai_task` prompt carries the boilerplate `ai_task.py` appends, so a
generic entry matching that phrase will swallow more specific ones. Put
specific phrases first.

**Leftover pytest containers starve Docker.** A run killed by a timeout
leaves its container holding the mount, and enough of them will push the
gateways into `unhealthy` — at which point tests hang rather than fail.
`docker ps -q --filter ancestor=ha-pytest | xargs -r docker rm -f`.

## Notes on the harness

Two things in `conftest.py` exist purely because Home Assistant's test
harness is built to prevent exactly what these tests do:

- Sockets are blocked and pinned to localhost. Every configured host is
  added to pytest-socket's allowlist rather than switching the guard off;
  a missed one shows up as `SocketConnectBlockedError` against a bare IP,
  which looks like a network fault rather than a fixture to update.
- `async_get_clientsession` is replaced with one that cannot reach the
  network — nulled DNS resolver, different event loop. The
  `real_client_session` fixture hands `api.py` a genuine session on Home
  Assistant's own loop. Without it the failure reads `'NoneType' object
  has no attribute 'getaddrinfo'`, which says nothing about mocking.
