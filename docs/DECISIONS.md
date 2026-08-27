# Architecture decisions & open follow-ups

Companion repo to the `zeroclaw` Home Assistant add-on
(https://github.com/LorenzoVasi/addon-zeroclaw — see its own
`docs/DECISIONS.md` for the add-on side of this project, including the full
empirical testing notes referenced below).

## Auth: `Authorization: Bearer <token>`, not a webhook-secret header

The original design used an `X-Webhook-Secret` header against a
`gateway.webhook_secret` config field. Building and actually running the
`zeroclaw` add-on's image (`ghcr.io/zeroclaw-labs/zeroclaw:dist-v0.8.4`)
showed that field **does not exist** in the current stable release — it's
only on ZeroClaw's unreleased `master` branch. `zeroclaw config list` on a
running 0.8.4 container has no such field.

What's confirmed working, tested end-to-end against a running container:
`gateway.require_pairing = true` + a statically pre-provisioned
`gateway.paired_tokens` entry, checked as a normal
`Authorization: Bearer <token>` header on both `/webhook` and `/api/*`. This
integration sends that header (`conversation.py`); the token is whatever the
companion add-on's `api_token` option was set to (it provisions the
matching `paired_tokens` entry — see the add-on repo's `run.sh` /
`docs/DECISIONS.md`).

## The `/webhook` contract this integration relies on

Confirmed both by reading ZeroClaw's gateway source
(`crates/zeroclaw-gateway/src/lib.rs`: `handle_webhook`,
`authorize_webhook_request`, `WebhookBody`/`WebhookQuery` structs) and by
`curl`-ing a real running 0.8.4 container:

- `POST /webhook?agent=<alias>` (agent optional, omitted here — always uses
  whichever agent ZeroClaw resolves as default)
- Headers: `Authorization: Bearer <token>` (checked against
  `gateway.paired_tokens` when `gateway.require_pairing = true`),
  `X-Session-Id: <id>` for multi-turn continuity (alnum/`-`/`_`/`.`, ≤128
  chars — a `uuid4().hex` fits)
- Body: `{"message": "<text>"}`
- Success `200`: `{"response": "<reply text>", "model": "<model label>"}`
- Errors confirmed live: `401` (bad/missing bearer),
  `503` `{"error":"needs_quickstart","url":"/quickstart"}` (no model
  configured yet), `500` `{"error":"LLM request failed"}` (a real LLM call
  was never exercised in testing — no API key configured — so only the
  auth/routing layer is confirmed working end-to-end, not an actual chat
  completion).

## Correction: `X-Session-Id` never did anything — `/webhook` is fully stateless

The entry above is wrong about one specific claim and left as-is (not
edited) so the mistake stays visible. `X-Session-Id` was **never read by
ZeroClaw anywhere** — confirmed 2026-08-27 by grepping the actual current
`zeroclaw-labs/zeroclaw` source for that exact string (both cases) across
the whole repo: zero matches. The real `/webhook` handler is
`crates/zeroclaw-gateway/src/api_webhook.rs` (`crates/zeroclaw-gateway/src/
lib.rs: handle_webhook`/`authorize_webhook_request`/`WebhookBody`/
`WebhookQuery`, cited above, do not exist in this codebase — a source
citation that was apparently never actually checked). The authoritative
contract is documented plainly in ZeroClaw's own `.claude/skills/zeroclaw/
references/rest-api.md`: `/webhook` accepts exactly `Authorization`,
`Content-Type`, `X-Webhook-Secret` (optional), `X-Idempotency-Key`
(optional, for request dedup, not sessions) — no session/thread field
anywhere in the request or the `{"response", "model"}` reply.

Practical effect, reported by the user (2026-08-26): talking to the Assist
agent felt like every single message opened a brand-new conversation,
because it did — `/webhook` has no mechanism to do otherwise. This wasn't
caught earlier because the original verification only checked that sending
the header didn't error, never that a second call with the same header
actually continued context — the note above even says as much ("only the
auth/routing layer is confirmed working end-to-end, not an actual chat
completion"), which in hindsight was the tell.

**Fix**: switched `conversation.py` (not `ai_task.py` — see below) from
`/webhook` to `GET /ws/chat`, which genuinely does support a client-chosen
`session_id` for server-side history persistence — confirmed this time by
reading the actual handler, `crates/zeroclaw-gateway/src/ws.rs`
(`WsQuery.session_id`, doc comment "Client-chosen session ID for memory
persistence"; the `session_start` reply reports `resumed`/`message_count`
for it), **and** by round-tripping it against a real running container
(`docker build`/`run`, no real LLM key configured so the actual chat
completion fails, but that's irrelevant to what was being tested): calling
`/ws/chat?agent=default&session_id=test-session-A` twice in a row returned
`message_count: 0, resumed: false` then `message_count: 1, resumed: true`;
a third call with a different `session_id` came back `resumed: false`
again — session persistence and isolation both behave exactly as `ws.rs`
describes, not just as documented but as actually observed.

`api.py` now has two functions: `async_call_webhook` (kept, used only by
`ai_task.py`, genuinely fine to be stateless — a `generate_data` task isn't
a conversation) and the new `async_call_ws_chat` (used by `conversation.py`
only). `conversation.py` opens a **new short-lived WebSocket connection per
Assist turn**, passing Home Assistant's own `conversation_id` as
`session_id` each time, rather than holding one socket open for the whole
chat window — deliberately, since `session_id`-based resume means a fresh
per-turn connection gets identical continuity to a held-open one without
this integration having to manage a live connection's lifecycle (reconnect
on drop, detect a stale/dead socket, etc.) across separate, independent
`_async_handle_message` calls, which Home Assistant already invokes as one
call per turn regardless. Home Assistant's own `conversation_id` lifecycle
(confirmed by reading `homeassistant/components/conversation/entity.py`,
`helpers/chat_session.py`, and `home-assistant/frontend`'s
`ha-assist-chat.ts`) already matches what the user asked for on its own,
with no changes needed on that side: the Assist chat frontend keeps one
`conversation_id` client-side for as long as its chat window stays open,
sending it back on every turn, and only mints a new one when the window is
closed and reopened.

**Not yet tested**: a real end-to-end Assist conversation against a
container with an actual LLM provider configured (this verification used a
throwaway container with no provider key, confirming the session mechanism
itself but not a real chat reply) — have the user confirm on their real
instance and report back. Also unconfirmed: what happens if ZeroClaw's
`always_ask` risk-profile gate (see the add-on repo's `docs/DECISIONS.md`)
fires mid-turn over this connection — the code here ignores any frame that
isn't `"done"`/`"error"` while waiting, so an `approval_request` frame with
nothing to answer it would either hang until the 30s timeout or ZeroClaw's
own server-side approval timeout, whichever is shorter; not exercised by
this test since no cover/lock action was attempted.

## Config flow only checks reachability, not the token

`GET /health` is used to validate the host during setup (also confirmed
live: returns `{"paired": ..., "require_pairing": ..., ...}`). Actually
validating `api_token` would require a real `POST /webhook` call, which
either burns an LLM call (real message) or exercises undocumented behavior
(empty `message`). Traded off in favor of surfacing auth errors on the
first real Assist turn instead, with a clear message.

## `supported_languages` must be a real property override, not just `_attr_supported_languages`

Confirmed against a real Home Assistant instance (2026.8.3): setup failed
outright with `TypeError: Can't instantiate abstract class
ZeroClawConversationEntity without an implementation for abstract method
'supported_languages'`. On this HA version, `ConversationEntity.
supported_languages` is declared as an abstract `@property` — the usual HA
pattern of setting `_attr_supported_languages` as a class attribute (which
normally works for most `Entity` attributes, auto-exposed as a property by
the base `Entity` class) does **not** satisfy Python's ABC mechanism here,
since that requires a *concrete* `supported_languages` property defined on
the subclass itself. Fixed in `conversation.py` by adding an explicit:

```python
@property
def supported_languages(self) -> list[str] | str:
    return "*"
```

This resolved the exact known gap flagged in the original code comment
("only tested by reading Home Assistant's developer docs... not against a
live homeassistant-core checkout") — it really did need live verification,
and this is exactly the kind of thing that wouldn't have been caught by
`py_compile` or any static check, only by an actual `async_setup_entry`
call inside real HA core.

## Added the `ai_task` platform

Extended the integration to also implement Home Assistant's
[`ai_task`](https://www.home-assistant.io/integrations/ai_task/) building
block (`ai_task.generate_data`), so automations/scripts can call ZeroClaw
directly, not just Assist. New files: `api.py` (factored the `/webhook`
call out of `conversation.py` so both platforms share it — no behavior
change to `conversation.py`, pure refactor), `ai_task.py`. `__init__.py`
now forwards the same config entry to both `["conversation", "ai_task"]`
platforms; `manifest.json` gained `"ai_task"` in `dependencies`.

This time, checked against real home-assistant/core source **before**
writing anything (`homeassistant/components/ai_task/entity.py` for the
base class, `homeassistant/components/anthropic/ai_task.py` for a real
provider's implementation) rather than only the developer docs — precisely
because the `conversation` platform above was bitten once by an
undocumented abstract-method requirement. Result: `AITaskEntity.state` and
`.supported_features` have concrete default implementations in the base
class (unlike `ConversationEntity.supported_languages`), so there's no
equivalent instantiation trap here — confirmed by reading the class, not
just by hoping.

Design choices, and why:
- **Declares `GENERATE_DATA` only** — no `SUPPORT_ATTACHMENTS` (`/webhook`
  has no file-upload mechanism, just `{"message": "<text>"}`), no
  `GENERATE_IMAGE` (ZeroClaw exposes no dedicated image-generation
  endpoint through the gateway).
- **`task.structure` (structured/JSON output) is best-effort**: the schema
  is described in the prompt text, the reply is parsed as JSON. `/webhook`
  has no native JSON-schema-constrained decoding, unlike Anthropic/OpenAI's
  own native structured-output APIs that their `ai_task.py` implementations
  use — this depends entirely on whether the model ZeroClaw is configured
  with reliably follows a "reply with only JSON" instruction. Raises
  `HomeAssistantError` with the parse error and logs the raw reply on
  failure, rather than silently returning garbage.
- Anthropic's own real implementation uses HA's newer **config subentries**
  pattern (one config entry containing multiple typed subentries: a
  conversation one, an ai_task one, etc.). This integration doesn't use
  subentries at all — one config entry maps to exactly one host+token pair,
  and both platforms are set up directly from that same entry (the
  simpler, older, still-fully-supported pattern already used by
  `conversation.py`). Kept consistent rather than introducing subentries
  for just this one platform.

**Not yet tested against a real Home Assistant instance** (unlike
`conversation.py`, which was fixed after a real setup failure) — only
`py_compile`-checked. Verify `ai_task.generate_data` actually works
end-to-end (plain text case first, then a `structure`-constrained case)
before relying on it, the same way the `supported_languages` gap only
surfaced under a real HA core.

## Wrong default host: local add-ons are `app_local_<slug>`, not `<slug>`

The config flow's default host (`http://localhost:42617`, later assumed to
be reachable as `http://zeroclaw:42617`) was never actually confirmed
against a real Supervisor network — flagged as a guess when the add-on
repo's README/DOCS were written. It caused a real, confusing failure: the
integration silently connected to the user's **other**, pre-existing,
already-paired ZeroClaw instance (a separate manual install, unrelated to
this add-on, reachable on the same network) instead of the add-on, so every
call failed with a genuine `401 Unauthorized` from *that* instance's
pairing requirement — the add-on's own `require_pairing: false` was correct
and irrelevant, since traffic never reached it.

First guess from the user, based on Portainer's container list: the
container name shown there is `app_local_zeroclaw` (underscored) — status
"healthy", confirming it really is this add-on's own container. Using that
as the host **still failed** ("Could not reach ZeroClaw at that address").
Root cause: Portainer's container-list name is the *Docker container
name*, not necessarily a resolvable DNS hostname. Confirmed by running
`hostname` **inside** the actual running container (via the SSH add-on):
`local-zeroclaw` — hyphenated, not underscored. Supervisor sanitizes
container names into RFC-1123-valid hostnames (`_` → `-`) for DNS
purposes, so the two strings genuinely differ; only the hyphenated one
resolves. Updated `config_flow.py`'s default to `http://local-zeroclaw:
42617` and fixed this file's own earlier (wrong) entry above it. This is a
`local`-install-specific value (config.yaml slug `zeroclaw`, installed via
"Local add-ons"); an add-on installed from a published repository would
use a different hostname (untested) — same lesson applies: confirm with
`hostname` inside the container, don't infer from a container-manager UI's
display name.

**Lesson for next time**: when multiple instances of an integration's
backend can plausibly coexist on the same network (as here — nothing stops
someone running ZeroClaw standalone *and* via this add-on), a wrong-but-
reachable default host fails with a *plausible-looking* error from a real
server, not an obvious connection failure — much harder to diagnose than a
timeout. Worth calling out prominently in setup docs, not just getting the
default right.

## Declared `ConversationEntityFeature.CONTROL`

Assist showed "This assistant can't control your home" — misleading, since
ZeroClaw *can* act on Home Assistant, just via its own MCP connection
rather than HA's local exposed-entity intent matching. Checked
`assist_pipeline/pipeline.py` in home-assistant/core directly rather than
guessing: `CONTROL` is purely declarative from this entity's side — it only
changes how HA's *own* pipeline pre-filters sentences before they reach the
configured agent (skips some local intent matching when
`prefer_local_intents` is on), nothing the entity itself needs to
implement. Every LLM-backed conversation agent in home-assistant/core
declares it (confirmed via `gh search code` across
anthropic/openai_conversation/ollama/google_generative_ai_conversation/
litellm/open_router/wyoming/cloud) with no extra code beyond the class
attribute — added the same way.

## Added an optional `agent` field — Quickstart can create a differently-named agent

Reported by the user: after connecting successfully and running ZeroClaw's
own Quickstart wizard to configure a real provider, `/webhook` calls kept
failing (`LLM request failed`) even though Quickstart had completed. Root
cause: `/webhook` with no `?agent=` query param picks an agent on its own
(ZeroClaw's documented "legacy pick": the migration-synthesized "default"
agent, or else whichever is enabled first) — and Quickstart can create a
new agent under its own name rather than reusing/reconfiguring "default",
so the webhook kept hitting the old, still-unconfigured seed agent while a
perfectly good newly-configured one sat unused.

Added `CONF_AGENT` (optional, blank by default so existing single-agent
setups are unaffected) to the config flow, threaded through
`async_call_webhook` in `api.py` as the `?agent=` query param, used by both
`conversation.py` and `ai_task.py`. Finding the right value requires
checking ZeroClaw's own dashboard (Config → Agents) or `config.toml` for
the actual alias Quickstart assigned — this integration has no way to
discover that on its own.

No `OptionsFlow` yet (see below) — changing this on an existing entry still
means delete-and-recreate, same as host/token.

## Two-step config flow: pick the agent from a live-fetched list

User request: rather than typing an agent alias by hand (error-prone —
needs finding the exact string in ZeroClaw's own dashboard/config.toml
first), let the config flow fetch and offer a dropdown. Implemented as a
second step (`async_step_agent`) after host/token validation, using
`async_fetch_agents` (`api.py`) against `/api/quickstart/state`.

Checked the exact `SelectSelector`/`SelectSelectorConfig`/`SelectOptionDict`
usage against real home-assistant/core config flows (`gios`, `mta`, others)
before writing this — same caution as the `ai_task` platform, for the same
reason: this integration has now been bitten once by code that looked
right against docs alone.

Design choices:
- If the fetch fails (wrong/missing token against a paired gateway, an
  older ZeroClaw without this endpoint, network hiccup) setup is **not**
  blocked — falls back to a plain free-text field with an inline note,
  same end state as before this change.
- An explicit "Auto (let ZeroClaw pick)" option maps to the empty string
  (`AUTO_AGENT_VALUE`), so it round-trips cleanly through `CONF_AGENT` and
  `api.py`'s existing `if agent:` handling with no extra translation layer.
- Intermediate host/token are held as plain instance attributes
  (`self._host`, `self._token`) between the two steps — the standard HA
  pattern for multi-step config flows (the flow handler instance persists
  for the flow's duration).

**Not yet tested against a real Home Assistant instance** — only
`py_compile` and JSON-validated. The two-step flow, the live agent fetch,
and the fallback-to-free-text path should all be exercised for real before
trusting this, per the project's now-established pattern of verifying
config-flow/entity code against actual HA behavior rather than assuming
docs-level correctness is enough.

## Uniqueness is (host, agent), not host alone

User request: a single ZeroClaw gateway can host multiple agents, each
worth its own config entry (different name/area/pipeline in HA) — but
`_async_abort_entries_match({CONF_HOST: host})` in step 1 blocked a second
entry against the same host outright, before the agent was even chosen.
Moved the duplicate check to the end of step 2 (`async_step_agent`), once
`agent` is known, matching on `{CONF_HOST, CONF_AGENT}` together — two
entries with the same host now coexist fine as long as their agent differs
(including one of them being the empty "auto" value, as long as the other
one names something specific).

Follow-on fix once two same-host entries were possible: both would've
displayed identically as "ZeroClaw" everywhere (entry title, device name,
entity name) — no way to tell them apart in the UI, the Assist agent
picker included. Both the entry title and the entity's `_attr_name` /
`device_info["name"]` now become `"ZeroClaw ({agent})"` when an agent is
set, plain `"ZeroClaw"` for the auto case (kept unchanged so an existing
single-agent setup's display name doesn't shift).

## No broadcast/global MCP grant — confirmed against ZeroClaw's own docs, not assumed

User asked whether the `home_assistant` MCP server/bundle could be granted
to *all* agents automatically, present and future, instead of per-agent by
hand. Checked `tools/mcp.md` directly rather than assuming: ZeroClaw states
plainly that omission is not a grant, deliberately, as a security default —
there's no wildcard, no "all agents" bundle target, no global-scope
setting. This is a ZeroClaw design decision, not a gap in this add-on's
automation. Told the user this plainly, and offered (but did not build,
absent an explicit yes) a periodic reconciler as an opt-in alternative that
would deliberately work against that default — flagged as a real tradeoff,
not a free enhancement.

## Create-a-new-agent flow, with an automatic home-helper personality

User request: instead of only picking from ZeroClaw's *existing* agents,
let this integration create one — provider, model, name — and
automatically set it up with a personality suited to being a respectful,
household-aware home-automation helper (not ZeroClaw's bare generic
default).

None of the endpoints this needed are documented anywhere (ZeroClaw's own
docs cover the *concept* of Quickstart, not its API). All four were reverse
engineered against a real running gateway, mostly by reading the
`Failed to deserialize the JSON body...: missing field '...'` error each
malformed request returns and adding one field at a time — a genuinely
usable discovery loop, not guesswork:

- `POST /api/quickstart/fields {"section":"model_provider","type_key":"<kind>"}`
  → the dynamic field list for that provider kind (e.g. anthropic:
  `model`, `auth_mode` [enum: api_key/setup_token], `api_key` [secret],
  `uri` [optional]). Each entry carries `key`/`label`/`help`/`kind`/
  `is_secret`/`enum_variants`/`required`/`default` — enough to build a form
  from directly, no per-provider hardcoding needed on this side.
- `POST /api/quickstart/apply` → creates everything at once: a model
  provider entry, risk/runtime profiles, a memory backend, and the agent
  itself. Every one of `model_provider`/`risk_profile`/`runtime_profile`/
  `memory` is an "adjacently tagged enum" — `{"mode": "existing"|"fresh",
  "value": ...}` — `"existing"` references something already configured
  (`value` is a bare alias string, or for model_provider a `<type>.<alias>`
  string), `"fresh"` creates something new from a bare preset/type name.
  This integration always uses `"fresh"` for all four (a brand-new
  provider entry, keyed by **the agent's own name** as its alias rather
  than a fixed `"default"` — confirmed this avoids collisions when
  creating multiple agents on the same provider kind, e.g.
  `providers.models.anthropic.casa` vs `...anthropic.pongo`).
- `GET /api/personality/templates` → ZeroClaw's own default personality
  files (`SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`,
  `HEARTBEAT.md`, `MEMORY.md`) with their stock content.
- `PUT /api/personality/<filename>?agent=<alias>` → write one, body
  `{"content", "expected_mtime_ms"}` (`null` = create/overwrite
  unconditionally; ZeroClaw's own dashboard sends the file's last-known
  mtime here for conflict detection on a real edit, not relevant when this
  integration is writing a file that didn't exist a second earlier).

`personality.py` layers a home-helper role onto the *fetched* templates
rather than hardcoding replacement files — stays in sync with whatever
ZeroClaw's own defaults are on a given install. Customizes only `SOUL.md`
(appends a "Role: Home Assistant Helper" section — warm/respectful tone,
prefer acting over asking for reversible home actions, know the household),
`IDENTITY.md` (Vibe/Emoji lines only), and `USER.md` (appends a "Who Lives
Here" placeholder section). Deliberately does **not** invent real household
member names or other personal details this integration has no way of
knowing — it just adds the right place for the user to fill that in
themselves. `AGENTS.md`/`TOOLS.md`/`HEARTBEAT.md`/`MEMORY.md` are
operational, not identity, and are written back byte-identical to the
fetched template.

New config flow steps: `agent` (step 2) gained a "+ Create a new agent"
option (only offered when the live agent-list fetch succeeded, since
creation needs the same live API); `new_agent` (name + provider, provider
list live-fetched same as the agent list) → `new_agent_config` (fields
built dynamically from `POST /api/quickstart/fields`'s response — `enum`
kind → `SelectSelector`, `is_secret` → `TextSelector(...PASSWORD)`, `str`
otherwise; `required` fields *with* a default become `vol.Optional` with
that default pre-filled, `required` with no default become `vol.Required`)
→ on submit, `async_apply_quickstart` then a best-effort personality-file
write (failure here is logged and swallowed, not fatal — the agent already
exists and works, just with ZeroClaw's bare default personality) → same
`_finish()` tail as the existing-agent path (factored out of the old
`async_step_agent` body once there were two callers).

**Confirmed working end-to-end** against a real running container: (1) the
full `/api/quickstart/fields` → `/api/quickstart/apply` round trip actually
creates a working agent (`{"kind":"applied",...}`, verified in
`config.toml` and via `/api/quickstart/state` afterward); (2) using the
agent name as the provider alias avoids the collision it was meant to
avoid; (3) `personality.py`'s `build_personality_files` against real
fetched templates — correct SOUL.md/IDENTITY.md/USER.md customization,
byte-identical passthrough for the other four files, checked with
assertions, not just eyeballing; (4) all 7 personality files written via
`PUT` for a real created agent and confirmed via `GET /api/personality`
afterward (`exists: true` for all seven). **Not yet exercised through an
actual Home Assistant config flow UI** — the HTTP contract and the pure
Python logic are both verified directly, but the multi-step
`ConfigFlow`/`voluptuous`/`selector` orchestration itself has not been
run inside real HA core, the way `conversation.py`'s abstract-method gap
was only ever going to be caught by that. Test this flow for real before
trusting it blindly.

## Agent alias sanitization, and a testing false alarm about Unicode

User report: the new-agent name field rejected input with "special
characters" for an identifier. Confirmed against a real running gateway,
iterating on `POST /api/quickstart/apply`'s own rejection messages one
field-value at a time: the agent alias must be lowercase ASCII
letters/digits with single underscores as separators, must start *and*
end with a letter or digit, and must never contain `__` (reserved as the
env-var grammar's path separator — same grammar as `ZEROCLAW_<section>__
<field>` env var overrides elsewhere in this project). Exact rejection
messages seen: `"must start with a lowercase letter or digit"`,
`` `contains invalid character '…'; only lowercase letters, digits, and
single underscores are allowed (no hyphen, no uppercase)` ``, `"must end
with a lowercase letter or digit"`, `` `must not contain `__`` ``.

Added `personality.sanitize_agent_alias()`: NFKD-folds accented Latin
letters to their ASCII base (`"café"` → `"cafe"`, friendlier than just
dropping them) before collapsing every run of disallowed characters to a
single underscore and trimming leading/trailing underscores. Verified with
13 cases including the exact strings ZeroClaw's own error messages were
triggered by, plus a realistic Italian example (`"Città Bella!"` →
`"citta_bella"`), and confirmed the sanitized output is actually accepted
by a real running gateway. The **display name** the user actually typed is
preserved unsanitized for the system prompt and `IDENTITY.md`'s `Name:`
line — those are prose a human/model reads, not an identifier, and
`sanitize_agent_alias` is intentionally *not* applied there (see next
entry for why that's safe).

**False alarm caught before shipping**: while testing this, sending an
accented `system_prompt` (`"Città Bella"`) and later an accented
personality-file `content` both hit `Failed to parse the request body as
JSON: ...: invalid unicode code point` — looked at first like ZeroClaw's
JSON layer rejects *all* non-ASCII content, not just the alias. Retested
both with a payload built from a Python-written, verified-UTF-8-encoded
file via `curl --data-binary`, instead of a shell `-d '...'` argument, and
both succeeded cleanly. Root cause: this session's own `curl -d` shell
invocations were mangling the UTF-8 bytes (a git-bash/Windows console
codepage issue), not a real ZeroClaw restriction — confirmed by getting a
*different*, unrelated error (`"unknown agent alias"`) once the encoding
was fixed, meaning the JSON parsed fine and the request reached real
handler logic. Home Assistant's own HTTP client (`aiohttp`, via
`json=...`) doesn't have this problem — it encodes UTF-8 correctly on its
own — so no ASCII-folding of `system_prompt` or personality-file content
was needed in the actual integration code. Worth recording precisely
*because* it looked like a real finding for a while and very nearly became
an unnecessary "fold everything to ASCII" change.

## API key silently accepted blank, then failed at actual use

User report: created an agent through the new flow without an API key
(the form let them skip it), and got a confusing runtime failure later
(`Anthropic credentials not set...`) instead of a clear error at setup
time — visible in ZeroClaw's own dashboard as the `api_key` field showing
"REQUIRED FOR API-KEY AUTH" / "unset" after the fact.

Root cause: ZeroClaw's own `POST /api/quickstart/fields` reports `api_key`
as `"required": false` (confirmed against a real gateway) — almost
certainly because it's *conditionally* required on `auth_mode` (Anthropic's
`setup_token` mode doesn't need one), a dependency the static per-field
metadata this integration reads doesn't express. The original code trusted
that flag literally, so a blank `api_key` sailed through as an
`vol.Optional(..., default="")` field, and `/api/quickstart/apply` itself
also accepts an empty string without complaint — the agent gets created,
just non-functional.

Fixed with a blanket, deliberately simpler rule on this integration's own
side: **any field ZeroClaw marks `is_secret` is always required**,
regardless of its own `required` flag. Two layers: the schema always
builds `is_secret` fields as `vol.Required` with no blank default (was
`vol.Optional(default="")` whenever ZeroClaw's `required` said `false`),
*and* the submit handler independently checks every `is_secret` field is
non-empty (after `.strip()`) before calling `async_apply_quickstart`,
showing a `secret_required` error and re-rendering the form instead —
defense in depth, since a `vol.Required` marker alone only guarantees the
key is *present*, not that a user (or browser autofill) didn't submit
whitespace. Provider field metadata is now cached on `self._new_agent_fields`
once fetched (was re-fetched — and silently re-validated against
potentially-changed data — on every render of this step) so the retry
loop re-uses the same fields instead of hitting the API again.

## Create-agent flow simplified: pick a pre-configured provider, don't collect fresh credentials

User report: an agent created via this integration's original create-agent
flow (which entered fresh provider credentials and wrote them through
ZeroClaw's *live* API) kept failing at actual use with `Anthropic
credentials not set` — even after re-setting the key directly in
ZeroClaw's own dashboard, still against the running daemon. Root cause
traced to the same class of issue as the `zeroclaw` add-on repo's
"`config set` against an already-running daemon doesn't reliably persist"
finding — writing credentials live, however it's done, isn't dependable
for something that then needs to actually work for real LLM calls.

**Redesigned rather than patched**: provider credentials are no longer
collected by this integration at all. They now live in the companion
`zeroclaw` add-on's own `providers` option (see that repo's
docs/DECISIONS.md), seeded into `config.toml` *before* the daemon starts —
the one place in this whole project writes have proven reliable. This
integration's create-agent flow (`async_step_new_agent`) now only asks for
a **name** and a **pick from `GET /api/quickstart/state`'s
`model_providers`** (already-configured `<type>.<alias>` strings),
creating the agent with `model_provider: {"mode": "existing", "value":
"<type>.<alias>"}` — confirmed against a real running gateway that
`"existing"` mode needs no separate `model` field at all (it's read from
the provider's own config), which is also why this flow no longer needs
the dynamic per-provider field-fetching machinery the old design had.

Deleted entirely as a result: `async_fetch_provider_types`,
`async_fetch_provider_fields`, the `async_step_new_agent_config` step, and
all the `is_secret`/`blank_secrets`/dynamic-schema-building logic that
used to live there (that whole class of bug — ZeroClaw's own field
metadata under-reporting what's actually required — can't happen anymore,
since this integration doesn't build credential forms at all now). If no
provider is configured yet, the flow aborts with a clear
`no_providers_configured` reason pointing at the add-on's Configuration
tab, rather than showing a form with an empty, unusable dropdown.

**Confirmed working end-to-end** against a real running gateway (from the
add-on side, since that's where the reliability question actually lived):
a provider seeded pre-boot, `model_providers` correctly lists it,
`POST /api/quickstart/apply` in `"existing"` mode against it succeeds with
no `model` field in the request, and — the actual point of this whole
redesign — `GET /api/config/prop?path=providers.models.<type>.<alias>.
api_key` reports `"populated": true` afterward: a real, working credential,
the exact thing that was silently failing before.

## Grant the `home_assistant` MCP bundle at agent-creation time, not just at the add-on's next boot

User request: when creating a new agent through this integration, wire it
up to the `home_assistant` MCP server immediately (matching the add-on's
own naming — see that repo's `run.sh`), instead of only via that add-on's
"reconcile every existing agent" pass, which only runs on the add-on's own
boot and so wouldn't touch an agent created *between* boots.

Added `api.async_grant_mcp_bundle(hass, host, token, agent, bundle)` —
the exact same read-modify-write pattern already proven for the add-on's
own reconciliation (`GET /api/config/prop?path=agents.<alias>.
mcp_bundles`, parse the JSON-encoded-string value, append if not already
present, `PUT` the array back), just expressed in Python instead of
`curl`+`jq`. `async_step_new_agent` calls it with the hardcoded bundle
name `"home_assistant"` right after a successful `async_apply_quickstart`
and personality-file write — best-effort, like the personality write: a
failure (e.g. the add-on never configured Home Assistant integration, so
no such bundle exists) is logged and doesn't block agent creation.

The bundle name is hardcoded rather than discovered live because there is
currently no ZeroClaw endpoint that lists configured MCP *bundles* the way
`/api/quickstart/state` lists agents and providers — only individual
`mcp.servers`/`mcp_bundles` entries are readable one path at a time via
`/api/config/prop`.

**Confirmed working** against a real running container: replicated the
exact GET/PUT sequence in a standalone script (not just read the code and
assumed it matched the proven bash version) against a freshly created
agent with an empty `mcp_bundles` — GET correctly returned `"[]"`, the
append+PUT correctly persisted `["home_assistant"]"` to the actual
`config.toml` on disk, confirmed by `grep`, not just by trusting the API's
own response echo.

## Open follow-ups / not yet verified

- **`_async_handle_message` vs `async_process`**: implemented
  `_async_handle_message` per Home Assistant's developer docs
  (`developers.home-assistant.io/docs/core/entity/conversation/`). This was
  **not** cross-checked against an actual `homeassistant-core` checkout (no
  local clone available this session) or against a real Home Assistant
  instance — do that before shipping.
- **No end-to-end test against a real Home Assistant + the zeroclaw
  add-on together**: everything confirmed so far was the add-on's gateway
  tested in isolation via `curl`/`docker`. This integration's Python code
  compiles cleanly (`py_compile`) but has not actually been loaded by Home
  Assistant or driven a real Assist conversation.
- **No `OptionsFlow`**: to change the host/token today, remove and re-add
  the integration.
- **No automated tests**.
- **`manifest.json` / `README.md`**: filled in with the real GitHub home
  (`LorenzoVasi/ha-zeroclaw-conversation`) once this repo was actually
  pushed (2026-08-27).

## Personality-file reinforcement for the cover/lock permission gap

Companion to a change in the `addon-zeroclaw` repo (see its
`docs/DECISIONS.md`, "Default `home_assistant__*` risk-profile
permissions"): the add-on now hard-gates the *dedicated* cover-movement MCP
tools (`HassOpenCover`/`HassCloseCover`/`HassSetPosition`/`HassStopMoving`)
behind `always_ask`, per the user's explicit request that opening
doors/windows/gates always require confirmation while everything else stays
free. That hard gate has a real, unavoidable gap: Home Assistant's generic
`HassTurnOn`/`HassTurnOff`/`HassToggle` tools *also* operate on `cover.*` and
`lock.*` entities (confirmed by reading HA core's `OnOffIntentHandler`), and
those three tools must stay in `auto_approve` for ordinary "turn on the
light" requests to stay confirmation-free — ZeroClaw's risk profile has no
per-entity granularity to split "free for lights, gated for gates" within
one tool name. Locks are worse still: HA ships no dedicated lock intent at
all, so lock/unlock has *no* tool-name-level lever to gate, full stop.

User was presented this tradeoff directly and chose: keep the hard gate
scoped to the dedicated tools only (so lights/switches stay truly
zero-friction), and mitigate the gap with an explicit instruction in the
agent's own personality file — soft enforcement (a cooperative agent follows
it; doesn't defend against a genuinely adversarial prompt), but the only
lever available given ZeroClaw's tool-name-only gating model.

Added two new bullets to `HOME_ROLE_SOUL_ADDITION` in `personality.py`
(layered onto every new agent's `SOUL.md`, same mechanism as the existing
home-helper role text):

- Tells the agent to always use the dedicated cover tools
  (`HassOpenCover`/`HassCloseCover`/`HassSetPosition`/`HassStopMoving`) for
  any cover entity, and explicitly never the generic
  `HassTurnOn`/`HassTurnOff`/`HassToggle` for that purpose, spelling out why
  (the generic ones skip the confirmation gate the dedicated ones are set up
  to trigger).
- Tells the agent to always ask the household explicitly before any
  lock/unlock action, unconditionally — since there's no permission-level
  gate for locks to fall back on at all.

This only applies to **agents created through this integration's
"create a new agent" flow** (`_async_write_home_helper_personality` in
`config_flow.py`) — an agent picked from ZeroClaw's *existing* agents, or
created directly through ZeroClaw's own dashboard/quickstart, does not get
this SOUL.md addition and relies solely on the add-on's `always_ask` gate
for the dedicated cover tools (still enforced either way — this
personality-file text is a mitigation for the specific generic-tool gap,
not the primary control).

## Agent didn't know a named room is a Home Assistant Area

User report (2026-08-27): asked their agent to "spegnimi le luci in camera
di Lorenzo" — the agent replied it couldn't find an area/room called
"Lorenzo" and asked for the exact name, instead of recognizing "camera di
Lorenzo" as almost certainly referring to a configured Home Assistant
*Area* and either matching it directly or checking what areas actually
exist before giving up.

This is a model-knowledge gap, not a missing capability: Home Assistant's
intent-matching engine already supports targeting by area name (confirmed
by reading `homeassistant/helpers/intent.py` — `find_areas()` matches an
area's name *or any of its configured aliases*, and `MatchTargetsConstraints`
takes an `area_name`/`area_id` alongside the entity `name`), so the
`home_assistant__Hass*` tools this agent already has access to can resolve
"the lights in Lorenzo's room" correctly on their own, area alias and all
— the agent just didn't know to reach for that, or to check
`GetLiveContext` for the real area list before assuming a literal-name
match failed.

Added a new bullet to `HOME_ROLE_SOUL_ADDITION` in `personality.py`: tells
the agent that a named room/place is a Home Assistant Area, to pass it via
the tools' `area` argument instead of hunting for an entity with that exact
name, and to check `GetLiveContext` for the actual configured areas/aliases
and try the obvious match itself before asking the household to repeat
themselves more precisely.

Same caveat as the cover/lock bullets above: this only reaches agents
**created through this integration's "create a new agent" flow** — an
already-existing agent (like the one in this report) has a `SOUL.md`
snapshot written once at creation time that this change does not retroactively
touch. For an existing agent, the addition has to be pasted into that
agent's `SOUL.md` by hand, in ZeroClaw's own dashboard's Personality editor
— there is no live-push mechanism from this integration to an
already-running agent's personality files, and this session had no network
path to the user's real instance to do it via the API directly either.

## Plural entity types ("le luci") need a `domain` filter, not just an area

Same session, immediate follow-up from the Area finding above: once the
agent can resolve "camera di Lorenzo" to an area, "spegni **le luci** in
camera di Lorenzo" still isn't fully specified — plural nouns like "le
luci" name an entity *type*, not one device, and without telling the tool
that, area-only targeting risks acting on every onoff-capable entity in the
room (switches, covers, whatever else lives there) instead of just lights.

Confirmed this is a real, already-supported tool parameter, not something
to build: reading `homeassistant/helpers/intent.py`'s
`DynamicServiceIntentHandler.slot_schema` shows `domain` as a first-class
optional slot (`vol.Optional("domain"): vol.All(cv.ensure_list,
[domain_validator])`), and `async_handle` folds it straight into
`MatchTargetsConstraints(domains=domains, ...)` alongside `area_name` — the
two combine in one call, matching every entity of that domain within that
area. Also confirmed `domain` alone (no area) is a valid constraint on its
own (`match_constraints.has_constraints` only requires *some* constraint,
not specifically an area or name) — so "spegni tutte le luci" without a
room named is meant to work the same way, whole-home.

Added a bullet to the same `HOME_ROLE_SOUL_ADDITION` in `personality.py`
(right after the Area one, since they're meant to be used together) telling
the agent to treat a plural entity-type word as a `domain` filter and
combine it with `area` in one call rather than searching for a single
named entity or issuing one call per device, plus a compact Italian→domain
glossary (luci→light, prese/interruttori→switch, tapparelle/tende→cover,
termosifoni/clima→climate, ventilatori→fan, diffusori/altoparlanti→
media_player, serrature→lock, aspirapolvere→vacuum, umidificatori→
humidifier) since the domain keys themselves are English technical strings
the model has no reason to already associate with Italian household
vocabulary.

Same retrofit caveat as the Area entry: only reaches agents created through
this integration going forward; the user's existing agent needs this pasted
into its `SOUL.md` by hand too.

## Instruction-following regressed mid-conversation; asked for the missing-persistence and single-entity cases explicitly

User report (2026-08-27), a real conversation transcript: turn 1 ("spegni le
luci in camera di Lorenzo") correctly resolved to the one light entity in
that area and reported its state. Turn 2, same conversation, immediately
after ("accendimele per favore") — the agent reverted to asking for "il
nome preciso della luce", as if the Area/domain bullets from the previous
two entries didn't exist and it had never resolved this exact target one
message earlier.

Important to be precise about what this is and isn't: this is **not**
evidence the session-continuity fix (`GET /ws/chat` + `session_id`, see the
earlier "Correction" entry) is broken — nothing here suggests the model
lost access to turn 1's history, only that it didn't consistently *apply*
a `SOUL.md` rule it had access to. That's an inherent LLM instruction-
adherence limitation, not a bug with a deterministic fix; personality-file
text raises the odds of the desired behavior, it does not guarantee it the
way a risk-profile `always_ask` gate does. Said so plainly rather than
overselling what the change below can promise.

Two more bullets added to `HOME_ROLE_SOUL_ADDITION`, addressing what the
user asked for directly:

- **Single-entity and same-turn reuse.** When an area/domain search
  resolves to exactly one entity, or the agent already found the target
  earlier in the same conversation, act directly instead of asking for an
  exact name — explicitly called out as "the single most important rule in
  this section" precisely because failing it is what happened here.
  Also covers pronoun/implicit reference ("accendile", "quella") resolving
  to the same area/domain/entity as the immediately preceding turn.
- **Cross-session memory of house structure** — the user's broader ask:
  "mano a mano che ha sessioni lui va ad imparare come è strutturata la
  casa ... si ricordi il tutto." ZeroClaw already ships exactly the tools
  for this — `memory_store`/`memory_recall`, both already in this add-on's
  seeded `auto_approve` list (confirmed against the literal tool names in
  the drift-fix list in the add-on repo's `docs/DECISIONS.md`) — so this
  isn't new plumbing, only a missing instruction to actually use them for
  this purpose. Tells the agent to `memory_store` area names/aliases,
  which domains live where, and entity friendly names as it learns them
  from `GetLiveContext`, and to check `memory_recall` before re-discovering
  structural facts from scratch — while still treating live *state*
  (on/off, temperature) as something to always re-check, not something to
  trust from memory.

Not independently verified end-to-end this session (no network path to the
user's real instance) — ask the user to retest the exact turn-1/turn-2
sequence from their report after pasting the new bullets into their
existing agent's `SOUL.md`, and separately to try a fresh conversation
after a few real sessions to see whether `memory_recall` actually surfaces
previously-learned structure (it's a real tool call the model has to choose
to make; nothing forces it either).

## User-declared "don't touch this" restrictions, conversational and persistent

User request (2026-08-27): wants to be able to tell the agent, in plain
conversation, that a specific entity/area/domain is off-limits (their
example: "l'elemento aria condizionata voglio che tu non possa modificare
nulla") and have that stick — every future request touching it gets
refused with an explicit "questo ti è stato vietato", explicitly framed as
a safety net for the agent's *own* misunderstandings ("se per errore lui
capisce qualcosa di errato, non esegua quella operazione").

Checked first whether ZeroClaw's risk-profile system (the mechanism behind
the cover/lock `always_ask` gate, see the add-on repo's `docs/DECISIONS.md`)
could do this as a hard, config-level block instead of a soft,
personality-level one — it can't, for the same structural reason as
before: `auto_approve`/`always_ask`/`excluded_tools` gate by **tool name**
only (`home_assistant__HassClimateSetTemperature`, etc.), with no way to
scope a rule to one specific entity's `entity_id` within a call. Blocking
"the AC" specifically while leaving other climate entities free is not
expressible at that layer at all — same limitation already documented
twice in this file for covers/locks. A true hard, entity-specific block
does exist, but at the Home Assistant layer, not ZeroClaw's: un-exposing an
entity from Assist (Settings → Voice Assistants → Expose) removes it from
`GetLiveContext`/every `home_assistant__*` tool entirely, structurally, no
LLM cooperation required — but that's an HA admin-UI action, not something
sayable in a chat message, and it blocks *reads* too, not just control,
which isn't what was asked (the user's example only wants "can't modify",
reading state should still work). Given the request is explicitly framed
as something declared *conversationally*, mid-chat, a personality/memory
mechanism is the only fit — told the user plainly (in the reply, not in
this file) that this is a best-effort safety net for their own
misunderstandings, not an adversarial-proof hard gate, and that the HA
expose toggle is the airtight alternative if that's ever what's actually
needed.

Added a bullet to `HOME_ROLE_SOUL_ADDITION`: recognize a restriction
declared in conversation, `memory_store` it immediately (not just for the
rest of that conversation — durable, same mechanism as the house-structure
memory entry above), confirm back what was understood, and — the actual
enforcement point — check remembered restrictions **before calling any
control tool**, refusing and saying so explicitly if the target matches,
even when the rest of the request seemed reasonable. Explicitly scoped to
control actions only (`memory_store`'s marked as a "control restriction");
reading state is unaffected, matching the user's own "non possa modificare
nulla" wording. Restrictions persist until lifted the same conversational
way.

Not verified end-to-end this session, same reason as the entries above —
ask the user to test both directions: declaring a restriction and then
immediately trying to trigger it in the same conversation, and separately,
whether it's still honored in a *later* session (the real test of whether
`memory_store` actually persisted it rather than the model just holding it
in this conversation's own context).

**Reverted the same session, before ever being deployed to the existing
agent's `SOUL.md`.** Once the soft-vs-hard tradeoff above was spelled out
plainly, the user decided the HA-side "Expose" toggle (the hard, airtight
option already described above as the alternative) was what they actually
wanted instead — pulled the bullet back out of `HOME_ROLE_SOUL_ADDITION` in
`personality.py`. Left this whole entry in place rather than deleting it:
the reasoning for why ZeroClaw's risk profile can't do entity-level
blocking, and why a soft memory-based mechanism was the only *chat-driven*
option, are both still true and worth keeping on record in case a future
request revives the "declare a restriction conversationally" idea — the
mistake here wasn't the analysis, it's that presenting the tradeoff
up front (rather than after building it) would have surfaced the user's
actual preference sooner.

## Condensed the personality additions, and made them language-aware

User request (2026-08-27): two asks together — trim down all the
personality-file text (it had grown to nine increasingly-verbose bullets
across several sessions, each one explaining its own rationale inline), and
generate a new agent's personality/system prompt in whatever language Home
Assistant itself is configured with, not hardcoded English.

**Condensing.** Rewrote `HOME_ROLE_SOUL_ADDITION` bullet-by-bullet, cutting
the "why" prose (that reasoning already lives in this file's own history,
one entry per bullet) and keeping only the operational instruction, tool
names, and the Italian→domain glossary — none of that is safe to cut, it's
the actual content the model needs. Same treatment for
`USER_MD_HOUSEHOLD_ADDITION` and `default_system_prompt`. No behavioral
rule from prior entries was dropped, only the explanation of why each one
exists.

**Language.** `hass.config.language` (a standard HA Core attribute, e.g.
`it`, `en-GB`) now picks which translation gets written. Considered and
rejected translating ZeroClaw's own base template content (the
`SOUL.md`/`IDENTITY.md`/`USER.md` text `GET /api/personality/templates`
returns) — that's fetched fresh every time specifically so this module
stays in sync with whatever ZeroClaw's own defaults are on a given install
(see the very first entry in this file), and translating it would mean
maintaining a translation of content this module doesn't own and can't
predict. Instead, each translated addition opens with an explicit "always
respond in <language>" instruction, so the agent's actual behavior doesn't
depend on the surrounding (possibly still-English) base template text at
all. Two full translations shipped — `en` (also the fallback for anything
unrecognized, via `_resolve_language`) and `it` — not an exhaustive
locale list; a household running Home Assistant in, say, French gets the
English text today, same as before this change, with no error or partial
translation.

`default_system_prompt` and `build_personality_files` both gained an
optional `language` parameter (default `"en"`, keeping their old call
signature working for anything not yet updated); `config_flow.py` now
passes `self.hass.config.language` at both call sites (agent creation's
`system_prompt`, and the personality-file write step).

Verified with a standalone script (no HA instance needed — pure string
logic, `importlib` loading the module directly): `_resolve_language`
correctly maps `it`/`it-IT`→`it`, `en`/`en-GB`/`fr`/`None`/`""`→`en`;
`build_personality_files` with `language="it"` produces the Italian SOUL.md
addition, correctly substitutes `IDENTITY.md`'s Name/Vibe/Emoji lines, adds
the Italian USER.md section, and leaves an unrelated file (`AGENTS.md`)
byte-for-byte untouched, confirming the per-filename dispatch still only
touches the three files it's meant to.

Same retrofit caveat as every entry above: only reaches agents created
through this integration going forward. The user's existing agent's
`SOUL.md` still has the old, longer English bullets from prior sessions —
not touched by this change, no live-push mechanism exists to update it
automatically (see the Area entry, first one to note this limitation).

## Extended language support from 2 languages to all 64 Home Assistant supports

Immediate follow-up user request (2026-08-27): handle every language Home
Assistant itself supports, not just the `en`/`it` pair from the entry
above.

Got the authoritative list first rather than guessing one: `homeassistant/
generated/languages.py`'s `LANGUAGES` set — 64 codes, auto-generated from
the frontend's own translations directory, the same set `hass.config.
language` is validated against. Includes region/script variants that are
genuinely distinct codes, not just a base language with a country tag
(`en-GB`, `es-419`, `pt-BR`, `zh-Hans`/`zh-Hant`, `sr-Latn`).

Explicitly decided against hand-translating the full multi-paragraph
content into all 64. Two reasons: (1) it's genuinely a lot of content
(SOUL.md addition, USER.md addition, IDENTITY.md vibe line, system prompt)
× 64, and (2) more importantly, honesty about quality — this integration
has real confidence in `en` and `it`, but hand-producing fluent, natural
multi-paragraph Thai, Telugu, Welsh, or Icelandic isn't something to ship
without a way to verify it's actually correct and doesn't read like
translated-then-forgotten boilerplate.

What actually matters for the user's request — the agent replying in the
household's own language — doesn't require translating the *instructions*
into that language at all, only naming the target language reliably.
Modern LLMs follow "always respond only in X" as a language directive
regardless of what language the rest of the surrounding document is
written in; this is well-established, not a novel bet. So: kept the two
full translations exactly as they were, and for every other real HA
language code, `_localize()` takes the English content and does one exact
string swap — `"Always respond in English."` → `"Always respond only in
<native name>."` — on the response-language directive that already opened
`_SOUL_ADDITIONS["en"]` and closed `_SYSTEM_PROMPT_TEMPLATES["en"]`. Native
names for the swap come from a second dict built off the same
`languages.py` list, `_HA_LANGUAGE_NAMES` (e.g. `fr`→`Français`,
`ja`→`日本語`, `zh-Hans`→`简体中文`) — this required actually knowing each
language's own autonym, a much smaller and lower-risk claim than
hand-writing fluent paragraphs in each one.

`IDENTITY.md`'s vibe line has no directive sentence to swap (it's not
about response language), so `_localize()` on it for a non-`en`/`it`
language is effectively a no-op — the English vibe text is used as-is. Not
a bug: the vibe line is a minor personality descriptor, not something the
"respond in the household's language" request was actually about.

`_resolve_ha_language()` replaced the old `_resolve_language()`: matches
the exact HA code first (so genuinely distinct codes like `zh-Hans` don't
collapse into a `zh` that isn't even in the real list), falls back to the
lowercased base subtag for something HA itself wouldn't produce but a
caller theoretically could (`de-AT`→`de`), and only falls back to `en` for
input that doesn't resolve to a real HA language at all.

Verified with a standalone script (no HA instance needed, same method as
the entry above): confirmed the fetched dict has exactly 64 entries;
resolution tested against exact codes, a script/region variant (`zh-Hans`,
`zh-Hant` — must NOT collapse to a bogus `zh`), a base-tag fallback
(`de-AT`→`de`), and genuinely unknown input (`xx-YY`, `None`, `""`→`en`);
confirmed the directive swap produces the expected text for French,
Japanese, and German specifically (inspected output by hand for all three,
not just checked it didn't crash); confirmed `it`'s full translation is
completely unaffected by any of this. Then swept **all 64** language codes
through both `default_system_prompt` and `build_personality_files` in a
loop and confirmed zero exceptions — every single HA-supported language
code produces valid output, not just the ones spot-checked individually.

Same retrofit caveat as every entry above applies here too.

## Scheduling and event-driven triggers: notify webhook + integration-owned "watches"

User request (2026-08-27), the biggest single feature this session: (1)
manage ZeroClaw's own scheduled (`cron`) jobs, with the household notified
via an AI-generated message about what a job did; (2) let Home Assistant
trigger the agent directly on a real event — their example: "tell me when
the washing machine finishes, then start the dryer" — explicitly *not* via
a token-costing `HeartBeat` poll. A same-session follow-up sharpened (2):
if no recurrence is stated, the trigger should fire **once** and then turn
itself off, not keep re-triggering forever.

### Why not a real Home Assistant automation

The natural-sounding design — have the agent author a real Home Assistant
automation (trigger: state change; action: notify it) via the config
API — was considered and explicitly **rejected**, presented to the user as
a real tradeoff rather than assumed: an LLM generating raw automation
YAML/JSON has room to get trigger/condition/action syntax or logic subtly
wrong, the automation config REST API needs the long-lived Home Assistant
token exposed to the agent somehow (a real credential-handling question,
not solved by anything already built), and a wrong automation is visible,
persistent, and edits real Home Assistant state outside anything this
integration controls or can easily undo. User chose the alternative:
a lightweight primitive — a **"watch"** — owned entirely by this
integration's own Python (`watch.py`), armed with `homeassistant.helpers.
event.async_track_state_change_event`, persisted via `homeassistant.
helpers.storage.Store` so it survives an HA restart. Nothing shows up in
Settings → Automations; the agent manages its own watches conversationally
(`list_watches`/`cancel_watch`, see below) rather than a human editing them
in the UI. Simpler, fully within code this project owns and can test, no
new credential ever reaches the agent.

### Why ZeroClaw's own cron needed no new code here

`cron_add`/`cron_list`/`cron_remove`/`cron_update`/`cron_run`/`cron_runs`
already exist as ZeroClaw built-in tools (confirmed in `docs/book/src/
tools/overview.md`) — an agent can already manage its own schedule
conversationally ("ricordami ogni mattina alle 8 di..."). The add-on repo's
own change (see its `docs/DECISIONS.md`, same entry title) just unblocks
these tools from needing an approval prompt on every call and gives
`http_request` a narrow, Home-Assistant-only allowlist. Nothing to build
in this repo for the scheduling half specifically — only the *reporting*
half (notify) needed a new mechanism, which doubles as the event-driven
trigger's own notify path.

### One webhook, two directions

Two capabilities, cleanly separable by direction, share one design:

- **Inbound — Home Assistant → agent** (`notify_agent`, `conversation.py`):
  a **Home Assistant entity service**, registered via `entity_platform.
  async_register_entity_service` (confirmed exact signature against
  `homeassistant/helpers/entity_platform.py`) with a `message: str` field
  and standard entity targeting. An automation whose trigger is a state
  change calls this as its action — the actual, concrete answer to "Home
  Assistant triggers the agent instead of the agent polling." Delivered via
  the existing stateless `async_call_webhook` (same as `ai_task.py`) —
  correct here, not a regression of the earlier `/webhook`-is-stateless
  finding: an automation-triggered event isn't part of an ongoing Assist
  conversation, there's no `conversation_id` to thread continuity through,
  and the reply is intentionally discarded (this is a "tell the agent
  something happened" fire, not a question expecting an answer back to the
  automation — the *agent's own* reply, if it matters, goes out through the
  outbound direction below).
- **Outbound — agent → Home Assistant** (`webhook.py`, one `homeassistant.
  components.webhook` registration per config entry, confirmed exact
  `async_register`/`async_generate_id`/`async_generate_path` signatures
  against `homeassistant/components/webhook/__init__.py`): the one URL an
  agent's own `http_request` tool (see the add-on's allowlist) calls for
  everything it initiates — notifying the household, and creating/
  listing/cancelling watches. One webhook, not one per capability,
  dispatched on a `"type"` field in the JSON body, so there's exactly one
  URL for `TOOLS.md` to teach an agent rather than a growing list. Security
  model matches every other Home Assistant webhook: the 64-hex-char
  `webhook_id` itself is the credential (`local_only=True` on top, matching
  every other local-network-only assumption both repos already make — see
  the add-on's own docs/DECISIONS.md). Generated once at config-entry setup
  (`config_flow.py`), stored in entry data, never regenerated afterward —
  regenerating would silently invalidate whatever URL an agent's `TOOLS.md`
  already has.

`{"type": "notify", "message": "..."}` creates a fresh `persistent_
notification` (`notification_id` left unset — Home Assistant generates a
new one per call rather than overwriting the agent's last notification,
confirmed via `persistent_notification/__init__.py`'s `async_create`
signature) and, if the config entry set `CONF_NOTIFY_SERVICE`, additionally
pushes through `notify.<that>` — the user's explicit choice ("Entrambe")
when asked whether they wanted persistent-only, mobile-push-only, or both.
`CONF_HA_URL` (Home Assistant's own address as reachable *from inside the
ZeroClaw container* — same value, same default `http://homeassistant:8123`,
same underlying reason as the add-on's own `home_assistant_url`, kept as an
independent field because the two installs don't share config) and
`CONF_NOTIFY_SERVICE` are both optional in `config_flow.py`'s first step;
leaving `CONF_HA_URL` blank skips the whole feature for that entry (no
webhook_id generated, `TOOLS.md` doesn't get the notify section — see
`personality.py`'s `build_personality_files`, now taking an optional
`notify_webhook_url` parameter).

### Watches, and the one-shot-by-default requirement

`{"type": "create_watch", "entity_id", "to_state", "message", "recurring"?}`
arms a `WatchManager` entry (`watch.py`) — `async_track_state_change_event`
on `entity_id`; when `new_state.state == to_state`, `message` is delivered
back to the *same* agent via the inbound path's own `async_call_webhook`
call (not the entity service — the watch fires from `WatchManager`, which
isn't an entity, so it calls the shared webhook function directly with the
config entry's own host/token/agent, resolved by `entry_id` stored on the
watch). **`recurring` defaults to `false`** — this is the exact requirement
from the session's follow-up message: "se non gli specifico una ricorrenza
questa deve essere eseguita una volta ... deve essere spenta se non
specificato appunto sempre". A watch that fires disarms and removes itself
immediately afterward unless `recurring=true` was explicitly given.
Reinforced in both `TOOLS.md` (the technical contract) and `SOUL.md` (a
bullet stating plainly that "tell me when X happens" means once, not a
standing rule, unless the household says "every time" or gives a real
recurring schedule) — the same belt-and-suspenders pattern already used
for the cover/lock and Area/domain findings earlier in this file: the
*default value* in the wire contract is the enforced part, the personality
text is there so the agent doesn't accidentally ask for `recurring: true`
in the first place when it shouldn't.

`{"type": "list_watches"}` and `{"type": "cancel_watch", "watch_id"}` round
out the lifecycle — an agent can check what it already has armed before
creating a duplicate, or disarm one early if asked to stop. `WatchManager`
is a single instance shared by every config entry (`hass.data[DOMAIN][
DATA_WATCH_MANAGER]`, created and `async_load()`-ed once in `__init__.py`'s
`async_setup_entry`, guarded so a second config entry doesn't create a
second one) — watches aren't scoped to one entry's setup/unload lifecycle
any more than a Home Assistant state change is; `list_for_entry`/ownership
checks in `webhook.py` keep one agent from seeing or cancelling another
agent's watches even though they share the manager.

### Verification

Every Home Assistant core API used here was checked against actual
`home-assistant/core` source rather than assumed correct from memory or
docs (this project's established discipline after being burned by
`ConversationEntity.supported_languages` being an undocumented abstract
property, see an earlier entry) — `webhook.async_register`/
`async_unregister`/`async_generate_id`/`async_generate_path`,
`persistent_notification.async_create`, `helpers.event.
async_track_state_change_event`, `helpers.storage.Store.__init__`/
`async_load`/`async_save`, and `entity_platform.
async_register_entity_service`/`async_get_current_platform` — every
signature quoted in this entry's design was read directly from the current
source, not guessed. `personality.py`'s new content (the `TOOLS.md`
addition's URL substitution, and JSON-brace escaping through `.format()`)
was verified with a standalone script, both in isolation and swept across
all 64 supported languages with no exceptions, same as the entries above.

**Not verified end-to-end against a real Home Assistant instance** — this
repo's code compiles cleanly and every individual API call matches current
core source, but (as documented as an accepted limitation since this
repo's very first entries) that is not the same as having actually been
loaded and exercised by a running Home Assistant. In particular, untested
live: a config entry actually registering its webhook on setup and
unregistering it on removal; the `notify_agent` service actually appearing
in Developer Tools → Services and being callable from a real automation;
a watch actually surviving an HA restart via `Store` and re-arming
correctly; and the full round trip of an agent actually choosing to call
`http_request` against the notify webhook, end to end, from a real
conversation. Ask the user to test all of this against their real instance
and report back — this is exactly the kind of gap this project's own
CLAUDE.md says not to paper over.

## Notify targets resolved from Home Assistant's own person/device linkage, not a fixed field

Same-session follow-up (2026-08-27) to the feature above: replace the
config-flow's fixed `CONF_NOTIFY_SERVICE` field ("pick one `notify.*`
service up front") with dynamic resolution — "quando una persona scrive su
HA, zeroclaw-conversation vada a vedere quali sono i device legati a quella
persona ... così sa chi notificare nel caso." The fixed field is gone
entirely, not kept as a fallback option — the user's phrasing ("dobbiamo
lavorarla diversamente") was a replace, not an addition.

Went through the Home Assistant **user** (`Context.user_id`), not the
`person` integration the user mentioned by name — a deliberate
simplification, not a misread of the request: reading
`homeassistant/components/mobile_app/__init__.py` found its own
`_handle_user_removed` cleanup handler filtering mobile_app config entries
with `entry.data["user_id"] == user_id`, the exact same join the feature
needs — a mobile_app device is registered to a user account directly, so
routing through `person.*` (which itself just carries a `user_id`
attribute, see `homeassistant/components/person/__init__.py`) would be an
extra, unnecessary hop to the same key `mobile_app` already stores. New
module `person_notify.py`: `async_notify_targets_for_user(hass, user_id)`
walks every `mobile_app` config entry, keeps the ones matching `user_id`,
and returns the `notify.*` entities registered to each — the current,
entity-based mobile_app architecture (`NotifyEntity`, one per config entry
when the device supports push, confirmed in `mobile_app/notify.py`), called
via `notify.send_message` with standard entity targeting (`target:
{entity: {domain: notify}}` in the service's own `services.yaml` — this
supersedes the legacy `notify.mobile_app_<slug>` per-device service
pattern, which this integration never used).

**Wiring "whoever's talking" through to "whoever gets notified"**:
`conversation.py`'s `_async_handle_message` now records `user_input.
context.user_id` into `hass.data[DOMAIN][DATA_LAST_USER_ID][entry.
entry_id]` on every Assist turn (confirmed `ConversationInput.context` is
a `Context` with a `user_id` field by reading `homeassistant/components/
conversation/models.py` directly) — a value only set, never cleared, when
`context.user_id` is falsy for a given turn (a stale-but-real last-known
user beats notifying nobody). `webhook.py`'s notify handler reads that
value back and resolves targets fresh on every notify call, rather than at
watch-creation time or agent-creation time — "most recently talked to this
agent" as of right now, not a snapshot from whenever the watch was armed.

No known user yet (nobody's talked to the agent since Home Assistant last
restarted) or a known user with no mobile_app device at all: both fall back
to persistent-notification-only, silently — not an error state, the same
graceful-degradation shape the fixed-field design already had for "field
left blank."

Every Home Assistant API this relies on was checked against current
`home-assistant/core` source before use, same discipline as the rest of
this feature: `ConversationInput.context`/`Context.user_id`,
`ConfigEntries.async_entries`, `entity_registry.async_get`/
`async_entries_for_config_entry`, and `notify.send_message`'s entity-target
schema. Not verified end-to-end against a real instance — same acknowledged
gap as the rest of this feature, see the entry above.
