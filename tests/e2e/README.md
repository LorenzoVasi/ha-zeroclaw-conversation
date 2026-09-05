# End-to-end tests

These are the only tests in this repository that span **both** repos.
Everything in the path is real except the model:

```
Home Assistant core (test harness)
  └─ this integration
       └─ a real ZeroClaw daemon, built from ../addon-zeroclaw/Dockerfile
            └─ fake_llm.py  ← the only fake part
```

`fake_llm.py` is a small OpenAI-compatible server that answers by
substring match from a lookup table. ZeroClaw's `custom` provider slot
accepts any OpenAI-compatible endpoint, so this swaps out the one
component that is non-deterministic, rate-limited and costs money, and
leaves every other link genuine.

## Why bother

Almost every bug recorded in `docs/DECISIONS.md` in both repos was an
assumption about ZeroClaw's contract that only a running instance
disproved: a session header that was never read, an endpoint with no
session concept at all, a config write that reported success and did
nothing. Unit tests cannot see any of that, and neither can reading the
documentation — which has been wrong more than once about this exact
version.

## Running them

```bash
# from the repository root
ADDON_REPO=../addon-zeroclaw ./tests/e2e/up.sh
ZEROCLAW_E2E_HOST=http://localhost:42617 python -m pytest -m e2e
./tests/e2e/down.sh
```

The ordinary `pytest` run excludes them (`addopts = -m "not e2e"` in
`pytest.ini`) and they skip themselves if `ZEROCLAW_E2E_HOST` isn't set,
so nothing here slows down or breaks the hermetic suite.

## Two things that will waste your afternoon otherwise

**Seeding a provider does not bind an agent to it.** The add-on writes
`[providers.models.custom.fake]`, but each agent carries its own
`model_provider`, and the baked-in default points at
`openrouter.default` — so every turn fails with `LLM request failed`
until the agent is repointed. In normal use the integration's config
flow does this when it creates an agent; `up.sh` does it explicitly.
It also has to go through the live `PUT /api/config/prop` API, because
`zeroclaw config set` rejects `agents.<alias>.model_provider` as an
unknown property (the same dynamic-map-path limitation the add-on's
`run.sh` already works around for `mcp.servers`).

**The gateway answers on 42617, not 8099.** ZeroClaw itself binds
loopback-only on 8099; nginx fronts it on 42617, which is the only port
reachable from outside the container. Pointing `ZEROCLAW_E2E_HOST` at
8099 gets a connection refused that looks like the daemon is down.

## Notes on the harness

Two things in `conftest.py` exist purely because Home Assistant's test
harness is built to prevent exactly what these tests do:

- Sockets are blocked, and pinned to localhost when enabled. The gateway
  host is added to pytest-socket's allowlist rather than switching the
  guard off, so a stray call anywhere else still fails.
- `async_get_clientsession` is replaced with one that cannot reach the
  network — it nulls the DNS resolver and binds to a different event
  loop. The `real_client_session` fixture hands `api.py` a genuine
  session created on Home Assistant's own loop. Without it the failure
  reads `'NoneType' object has no attribute 'getaddrinfo'`, which says
  nothing about mocking.
