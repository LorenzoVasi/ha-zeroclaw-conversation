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

## Pushed to GitHub; CI's first real run caught a real issue, and a real gap

2026-08-27: repo pushed public to
`github.com/LorenzoVasi/ha-zeroclaw-conversation` (branch renamed
`master` → `main` first, to match what `validate.yml` already triggers
on). `REPLACE_WITH_GH_OWNER` filled in across `manifest.json`/`README.md`.
First CI run on `main`: `hassfest` passed clean; `ruff` and `hacs` both
failed, for two different reasons worth recording separately.

**`ruff` — real, fixed.** Four files (`api.py`, `config_flow.py`,
`conversation.py`, `webhook.py`) had unsorted/unformatted import blocks
(`I001`) — genuine issues introduced across the session's many edits to
each file's import list, never run through a formatter locally along the
way. `pip install ruff && python -m ruff check custom_components --fix`
fixed all four automatically (mostly blank-line placement between import
groups; one long `from .personality import a, b, c` line reformatted to
one-per-line). Re-ran `ruff check` clean and `py_compile` clean before
committing — an auto-fix changing import structure is exactly the kind of
change worth actually re-verifying, not trusting blindly.

**`hacs` — real, not fixed, documented instead.** `hacs/action`'s brands
check failed: "The repository does not provide brand assets and is not
listed in the Home Assistant brands repository." This is a genuine,
correctly-flagged gap, not a false positive — this repo has no
`custom_components/zeroclaw_conversation/brand/icon.png` (or an entry in
the separate `home-assistant/brands` repo), and no image-generation tool
was available in any session that's worked on this project so far (the
add-on repo has the identical gap for its own `icon.png`/`logo.png`, see
its own `docs/DECISIONS.md`). Left failing rather than faked with a
placeholder — branding needs to be real, and a hastily-generated
placeholder would just need replacing later anyway. This specifically
blocks HACS **default-list** submission only (see the next entry); the
integration installs and works fine as a HACS **custom repository** today,
brand icon or not.

## Filled the brand-icon gap: a from-scratch icon, drawn with Pillow

User request (2026-08-28): generate the missing brand icon (the gap the
entry above left open) and add hero images to both repos' READMEs — HA
icon + ZeroClaw icon side by side here in the add-on's README, the same
pair connected by a bidirectional arrow in this repo's README (to
communicate "this integration is the two-way bridge between them").

No image-generation tool was available (same limitation noted for
`icon.png`/`logo.png` in earlier sessions), so this was drawn
programmatically with Pillow (`pip install pillow`, no native
dependencies needed, unlike `cairosvg` which failed on this Windows
machine for lack of `libcairo-2.dll`) — plain shape primitives
(`rounded_rectangle`, `polygon`, `ellipse`), not traced from or copied off
any existing logo file. House glyph for Home Assistant in their documented
brand blue (`#18BCF2`); for ZeroClaw, a small crab (not an abstract claw
fragment — see below) in a warm orange, echoing their own public
branding: their README's own H1 is literally "🦀 ZeroClaw", a crab/claw
motif they picked themselves, not one invented here.

**Getting the ZeroClaw side to actually read as "ZeroClaw" took three
failed attempts before the fourth worked** — worth recording exactly
which shapes didn't work and why, since "draw a claw" turned out to be a
much harder small-icon problem than it sounds:

1. A circle with a pie-slice wedge cut out (a "Pac-Man" silhouette) — read
   as a pie chart, not a claw.
2. Two straight tapered "finger" polygons fanning out from a hinge point —
   read as a boomerang or a check-mark, not a claw, regardless of the
   angle between them.
3. A ring (annulus) with a wedge cut from one side, forming a "C"/bracket
   shape — read as the letter C.
4. **A small, whole, front-facing crab** (a rounded-capsule body, two
   round claw nubs on the sides, three small leg dots per side, two eyes
   on stalks with pupils) — reads unambiguously as "crab" at a glance,
   including scaled down to 64×64. The lesson generalizes: an abstract
   *fragment* of a recognizable thing (just a claw, detached) needs a lot
   of context to read correctly at icon scale; the *whole, simplified*
   thing reads instantly. Also matches the user's "carina" (cute) brief
   far better than an abstract shape would have.

Saved to `custom_components/zeroclaw_conversation/brand/icon.png`
(256×256, the exact path HACS's brands check names in its own error
message) — a diagonal-split badge, HA-blue/house upper-left,
ZeroClaw-orange/crab lower-right. The two README hero images
(`assets/*.png` in each repo) reuse the identical shape geometry as two
separate solid-color badges instead of one split one, generated from the
same coordinate math so there's no risk of a hand-transcribed copy
drifting from the version actually looked at and approved.

Verified by actually looking at the rendered output before shipping, not
just trusting the drawing code — rendered a PNG preview after each of the
four claw attempts and viewed it (catching failures 1–3 above), then
checked the final crab design specifically at 64×64 to confirm it still
reads correctly at realistic UI-icon scale, not just at the 256×256
working resolution.

## Replaced the hand-drawn icons with the real logos

Immediate user follow-up (2026-08-28): the from-scratch crab/house
drawing above wasn't wanted at all — "usa le icone originali di zeroclaw
e homeassistant, non generartele te, cercale su internet e usa quelle."
User supplied the exact two source URLs directly rather than leaving the
search to this session: `https://images.icon-icons.com/2407/PNG/512/
home_assistant_icon_146164.png` (Home Assistant's actual circuit-tree
house mark, 512×512) and `https://zeroclaw.dev/assets/zeroclaw_icon.png`
(ZeroClaw's own official claw icon, served from their own domain,
1024×1024 — confirms it's their real first-party asset, not a
fan-made rendering). Both fetched directly (the first needed a browser
`User-Agent` header — a bare `curl` got Cloudflare's bot-challenge page
instead of the image, silently returning HTML with a `.png` filename
until the response was actually inspected).

Recomposed all three assets from the real icons rather than the drawn
ones, same layouts as before: `assets/ha-zeroclaw-conversation.png`
(bidirectional-arrow README hero) and this repo's brand icon
(`custom_components/zeroclaw_conversation/brand/icon.png`) — the latter
redesigned as a layered composition (ZeroClaw's claw badge full-bleed,
Home Assistant's badge as a smaller white-ringed corner badge) rather than
the previous diagonal split, since the two real assets are already
complete self-contained badges (their own background colors/shapes) and
don't split cleanly into flat halves the way plain shape fills did. The
companion `addon-zeroclaw` repo's `assets/ha-zeroclaw.png` (plain side by
side, no arrow) was rebuilt the same way — see its own `docs/DECISIONS.md`.

Checked visually again before shipping, same discipline as the drawn
version: rendered and looked at all three compositions (side-by-side,
bidirectional, and the layered brand icon) before saving them over the
old files.

## Watches were notifying for changes the household already knew about

User bug report (2026-08-28): armed a watch on the lights in "camera di
Lorenzo," turned them off with a **physical Zigbee device** (not the HA
dashboard, not Assist) — no notification arrived. Diagnosis wasn't "the
watch is broken" (`async_track_state_change_event` fires on *any* state
change regardless of what caused it, dashboard or device — confirmed by
re-reading the design, nothing in `_arm`/`_on_change` filtered on cause at
all before this fix) but the opposite: **the watch had no way to
distinguish who/what caused a change**, so the fix isn't "make Zigbee
changes fire" (they always did) — it's "stop *also* firing for changes
the household already knows about," which the user then generalized
explicitly: notify for external devices, but never for a change made via
the HA dashboard or via talking to the agent itself.

The distinguishing signal is `Context.user_id` on the entity's `new_state`
— confirmed by reading `homeassistant/core.py` directly (`State.context:
Context`, `Context.user_id: str | None`): a browser dashboard action and a
REST API call authenticated with a bearer token (which is exactly how
ZeroClaw's own `home_assistant_token` authenticates every MCP tool call it
makes, whether from Assist or from a watch's own triggered follow-up
action) both get a `Context` with `user_id` set to the authenticated
user — genuinely indistinguishable from each other by `user_id` alone,
since in practice both are usually the *same* HA user (whoever generated
that long-lived token for the household). A device integration (ZHA,
Zigbee2MQTT, MQTT generally) calling `hass.states.async_set(...)` directly
in response to a physical device report is not an authenticated service
call at all, so its resulting `Context.user_id` stays `None`.

That ambiguity turns out to be exactly the right filter for what was
actually asked: "dashboard" and "the agent itself" don't need to be told
apart, because both cases reduce to the same thing — the household already
knows, because either they just clicked something or they just asked the
agent to do it. `_on_change` (`watch.py`) now returns early when
`new_state.context.user_id is not None`, firing only for changes with no
attached user — physical/Zigbee/MQTT devices, other automations, anything
not directly attributable to a person acting through Home Assistant or to
ZeroClaw itself. Considered a dedicated separate HA user account for
ZeroClaw's token as an alternative (would let dashboard vs. agent be told
apart individually) and rejected it for now — more setup burden on the
user for a distinction that isn't actually needed here, since the request
was to treat both the same way.

Also added a paragraph to `TOOLS.md`'s watch section telling the agent
this rule explicitly, so if asked "why didn't you notify me," it can
correctly diagnose "that change was made through Home Assistant itself"
rather than guessing or claiming the watch is broken.

Not verified end-to-end against a real instance (no network path to the
user's own Zigbee/HA setup from this session) — the fix is verified
against `homeassistant/core.py`'s actual `Context`/`State` field
definitions, and follows directly from re-reading the existing, unchanged
`async_track_state_change_event` design, but ask the user to re-test the
exact scenario from their report (Zigbee off → notified; dashboard/Assist
off → not notified) once deployed.

## The real bug: watches store `to_state` as literal text, and the agent wrote Italian

Immediate follow-up (2026-08-28): the `context.user_id` fix above wasn't
actually the bug — the user confirmed a watch was genuinely armed
(`list_watches` showed it), they turned the lights off, no notification
arrived, **and the watch stayed armed** (didn't fire and disarm either).
That combination rules out the `context.user_id` filter as the cause (a
filtered-out change still leaves the watch armed, correctly — matches
what was observed, but so does a watch that never matches at all) and
points at something more basic: the watch's `to_state` was `"spento"`,
Home Assistant's own Italian word for "off". `_on_change` (`watch.py`)
compares `new_state.state != watch.to_state` as an exact string — and
`new_state.state` for a light is always the literal English value `"off"`,
never a translated one, regardless of what language Home Assistant's UI
or this agent happens to be speaking. `"spento" != "off"` forever, so the
watch could never fire, for any cause — Zigbee included, contrary to the
previous entry's framing. No error surfaced at creation time either:
`to_state` is just a free-text string as far as the webhook validation
was concerned, any non-empty value passes.

This is exactly the kind of mistake this project's own agent-facing text
is prone to: the agent is instructed (repeatedly, throughout `SOUL.md`) to
think and respond in Italian, and `to_state`'s own description in
`TOOLS.md` — "the state that means it happened" — gave no hint that this
one specific field needs to break that pattern and be Home Assistant's
raw internal English value instead. A soft, personality-file-only fix
would have the exact same reliability problem already documented for the
cover/lock and Area/domain findings — worth fixing at both layers again:

- **`TOOLS.md`** (`personality.py`, both `en`/`it`) now says explicitly
  that `to_state` is Home Assistant's actual internal state string, always
  English, gives the common values by domain (`on`/`off`, `open`/`closed`,
  `locked`/`unlocked`, `home`/`not_home`), and tells the agent to check
  `GetLiveContext` for the exact value rather than guessing when unsure.
- **`webhook.py`**: a new `_STATE_ALIASES` table and `_normalize_state()`
  applied to every `create_watch` call, mapping common Italian state words
  (`spento`→`off`, `acceso`→`on`, `aperto`→`open`, `chiuso`→`closed`,
  `bloccato`→`locked`, `sbloccato`→`unlocked`, `casa`→`home`,
  `fuori`→`not_home`, and gendered/plural variants of each) to Home
  Assistant's real values — independent of whether the agent gets the
  `TOOLS.md` instruction right, the same belt-and-suspenders shape as
  every other soft-plus-hard mitigation in this project. Anything not in
  the table falls back to a lowercased, underscore-joined form of whatever
  was sent (`"ON"` → `"on"`) rather than passing it through as-is, since
  Home Assistant's real state values are conventionally lowercase
  snake_case — a caller sending mixed case is far more likely to have made
  a casing mistake than to be targeting a genuinely case-sensitive custom
  state. `create_watch`'s response now echoes back the normalized
  `to_state` actually stored, so the agent's own confirmation to the
  household reflects what's really being compared, not what was typed.

Verified the normalization function in isolation (pure string/dict logic,
no Home Assistant imports, so directly runnable without a live instance):
`"spento"`/`"Spento"`/`" spento "` all → `"off"`, `"off"` → `"off"`
(idempotent), `"ON"` → `"on"`, `"not a real word"` →
`"not_a_real_word"` (harmless fallback, not a crash), `"42.5"` → `"42.5"`
(numeric sensor values pass through untouched). Not verified against a
real watch actually firing end-to-end — same acknowledged gap as the
entry above; ask the user to cancel the existing broken watch (created
before this fix, so its stored `to_state` is still the literal `"spento"`
on disk — this fix does not retroactively repair already-armed watches)
and recreate it once deployed.

## Watch visibility as HA entities, and the notification stopped being optional

Immediate follow-up (2026-08-28): with the `to_state` bug fixed, the user
recreated the watch, turned the lights off — the watch fired (disarmed
itself, matching one-shot behavior) — and still got no notification. That
ruled out `to_state` too, and pointed at the architecture of firing
itself: `_async_fire` (`watch.py`) only ever sent `watch.message` to the
**agent**, via a stateless `/webhook` call, on the assumption the agent
would then itself decide to call the notify webhook to actually tell the
household. That assumption was never actually enforced anywhere — a
`SOUL.md` bullet says to notify "after finishing something," which reads
naturally for an *action* (start the dryer, then tell them) but not
obviously for a *pure* watch message that's already just informational
text ("le luci sono state spente") with nothing to finish. The model
receiving that message plausibly just... didn't act on it. Soft,
personality-file-only enforcement, same class of problem as the cover/lock
and `to_state` findings above, and same fix shape: pair it with something
that doesn't depend on the model's judgment.

**Fix**: `webhook.py`'s notify logic was pulled out of `_handle_notify`
into a standalone `async_notify_household(hass, entry, message)`.
`_async_fire` now calls it **directly** — the household is notified the
instant a watch fires, unconditionally, by this integration's own code,
not by hoping the agent relays it. The agent is *still* separately told
too (same `/webhook` call as before, now clearly commented as best-effort
"for any follow-up action"), so "...then start the dryer" keeps working —
firing is now two independent things happening (guaranteed notify, plus a
best-effort instruction to the agent) instead of one thing depending on
the other. Traded off explicitly, not silently: a watch whose message
implies an action can now produce two notifications in practice — the raw
watch message itself, and whatever the agent's own follow-up naturally
produces (e.g. its own "Ho avviato l'asciugatrice!" if `TOOLS.md`'s
"notify after finishing something" instruction *does* fire this time).
Preferred over silence: the user's stated core problem, twice now, was
"I'm not being told," not "I was told slightly redundantly."

**Watch visibility, the actual feature requested this turn**: "voglio
che... generi un'entità di tipo watch legato a quell'agente... quali sono
i watch attivi, quando vengono triggerati, se avvisato una sola volta o
sempre." New `sensor.py`: one entity per armed watch, grouped under its
agent's existing device (same `identifiers` as `conversation.py`'s
entity), state constant `"armed"` for as long as it's armed,
`extra_state_attributes` carrying `entity_id`/`to_state`/`message`/
`recurring`/`created_at`/`last_triggered`. Deliberately not a
"disarmed"/"triggered" state that lingers — the entity is **removed
outright** the moment the underlying watch stops being armed (fired and
one-shot, or explicitly cancelled), matching the watch's real lifecycle;
a recurring watch's entity instead stays and just updates
`last_triggered` in place after each fire. This also directly answers
"quando vengono triggerati" for free: Home Assistant's own recorder keeps
an entity's state/attribute history queryable even after the entity is
later removed, so a fired one-shot watch's trigger moment (and its
`last_triggered` attribute update, for a recurring one) is still visible
in Logbook/History afterward, without this integration building any
custom event logging of its own.

Watches are created from an HTTP webhook request (an agent's own tool
call), not from anything `sensor.py`'s platform setup does — so
`WatchManager` can't just hand new entities to `async_add_entities` the
normal way when one appears mid-session. Wired through
`homeassistant.helpers.dispatcher` instead (confirmed exact
`async_dispatcher_connect`/`async_dispatcher_send` signatures against
`homeassistant/helpers/dispatcher.py`): three per-entry signal names
(`SIGNAL_WATCH_ADDED`/`_REMOVED`/`_UPDATED`, `const.py`, each formatted
with `entry_id` so a different agent's watches never cross-notify),
`WatchManager` sends on create/cancel/fire, `sensor.py` listens and
adds/removes/updates entities accordingly. Startup is a separate path,
not a signal: `WatchManager.async_load()` (restoring watches persisted
from a previous run) runs in `__init__.py` *before*
`async_forward_entry_setups` — confirmed this ordering is preserved, not
assumed — so by the time `sensor.py`'s own `async_setup_entry` runs,
`manager.list_for_entry(entry.entry_id)` already reflects anything
restored from storage, and initial entities are built directly from that
rather than needing a signal sent before anything was listening for it.

Every new Home Assistant API used here was checked against current core
source before use, same discipline as every entry in this file:
`SensorEntity`, `Entity.async_remove(*, force_remove: bool = False)`
(confirmed exact signature — `force_remove=True` actually deletes the
state rather than marking it unavailable), and the dispatcher signatures
above. Not verified end-to-end against a real Home Assistant instance —
same acknowledged gap as every entry above; ask the user to confirm both
fixes together: recreate the watch, trigger it via an external device, and
check that (a) a notification actually lands this time and (b) a
`sensor.<agent>_watch_...` entity appears while armed and disappears (with
a Logbook entry) the moment it fires.

## Recognizing "when X, do Y" as a watch, and separating message from notification

User follow-up (2026-08-28), two related refinements once the watch
mechanism itself was actually working:

**Trigger recognition was too narrow.** "Scusami quando spengo le luci in
camera di Lorenzo potresti accendermi le luci scale?" got a description of
what an automation *would* do, not an actual `create_watch` call — only
rephrasing it explicitly as "creami un watch che..." worked. The existing
`SOUL.md` bullet (see the earlier "conversational triggers" entry) was
anchored on "tell me when X happens" specifically, which the user's actual
phrasing never said — no "dimmi"/"tell me"/"avvisami" at all, just a bare
"quando X, potresti Y?" conditional. Broadened the bullet (`en`/`it`) to
key off the conditional itself — "quando"/"appena"/"when"/"as soon as"
introducing a future condition — rather than any particular wording about
being told, using the user's own failing example as the concrete
illustration in the text.

**`message` and `notification` were the same field, and shouldn't have
been.** Once watches started actually notifying (previous entry),
`_async_fire` used `watch.message` — the instruction meant for the
agent's own follow-up action — as the literal notification text too. A
watch created to "accendi luci scale" therefore sent the household a
notification reading "accendi luci scale" verbatim: a bare command, not
something a person would say to another person. Split it: `Watch` gained
a `notification: str | None = None` field, `create_watch`'s wire format
gained an optional `"notification"` alongside `"message"`, and
`_async_fire` now notifies with `watch.notification or watch.message` —
falls back to the old behavior if an agent omits the new field, rather
than failing or notifying nothing. `TOOLS.md` (`en`/`it`) spells out the
distinction with the user's own example pair: `message` can be the bare
instruction ("accendi le luci delle scale"), `notification` should be a
full sentence ("Si sono spente le luci in camera di Lorenzo, come
richiesto accendo le luci delle scale"). `sensor.py`'s watch entities show
both attributes (`message` and `notification`, the latter already
resolved through the same fallback) for the same reason every other watch
detail is visible there — so a mismatch between the two is checkable
without needing to ask the agent.

Verified: `py_compile` and `ruff check` clean across every changed file;
the full 64-language sweep (same standalone script used for every
`personality.py` change in this file) confirms every language still
produces valid, correctly-substituted `TOOLS.md`/`SOUL.md` content with
the new text and the added `{{"notification": ...}}` JSON-brace escaping
included. Not verified end-to-end against a real conversation — whether
the broadened trigger-recognition bullet actually gets the "quando X,
potresti Y?" phrasing recognized this time is exactly the kind of thing
only a live retest can confirm, same LLM-judgment caveat as every
personality-file change in this project.

## Zombie ZeroClaw sessions: periodic cleanup, not left to accumulate

User report (2026-08-28): "ho notato su zeroclaw che le sessioni
precedenti rimangono attive... una sessione morta" — every Assist chat
window this integration talks through mints a fresh `conversation_id` when
reopened (this repo's own confirmed finding, `ha-assist-chat.ts`), passed
straight through as `/ws/chat`'s `session_id`. ZeroClaw resumes/persists
history per `session_id` server-side (the whole point of the earlier
"Correction" entry fixing multi-turn continuity) — but nothing ever told
it the *previous* session_id was done. Every closed-and-reopened Assist
window left a permanent, never-revisited entry in ZeroClaw's own session
backend.

Found the actual lever by reading `crates/zeroclaw-gateway/src/api.rs`
directly rather than guessing: `GET /api/sessions` (list, with
`agent_alias`/`channel_id`/`last_activity`/`session_key` per entry) and
`DELETE /api/sessions/{id}` both already exist — this needed zero new
ZeroClaw-side capability, only for this integration to actually call them.
New `api.py` functions `async_list_sessions`/`async_delete_session`; new
`session_cleanup.py` runs a cleanup pass 2 minutes after startup and every
6 hours after that (`homeassistant.helpers.event.async_call_later`/
`async_track_time_interval`, both confirmed exact signatures against
`homeassistant/helpers/event.py`), deleting any session that's (a) this
entry's own `agent_alias`, (b) has no `channel_id` (a channel-driven
session — Telegram, etc. — is owned by that channel, not by this
integration, even sharing the same agent), and (c) has been idle longer
than a fixed 24-hour `ZOMBIE_MAX_AGE`. Skips cleanup entirely for an entry
configured with "auto" agent selection (`CONF_AGENT` blank) — this
integration has no way to know which alias ZeroClaw actually resolved
"auto" to for a given session, so there's no way to safely attribute (and
therefore no way to safely delete) anything in that case; silently doing
nothing was judged safer than guessing.

**Verified against a real running container**, not just read from source:
opened two `/ws/chat` sessions with distinct `session_id`s (same technique
as the earlier session-continuity verification), confirmed `GET /api/
sessions` returned exactly the documented shape for both, `DELETE /api/
sessions/gw_test-session-B` returned `{"deleted": true, ...}` and the
session was actually gone from a follow-up `GET /api/sessions`, and a
second `DELETE` of the same now-gone session returned `404 {"error":
"Session not found"}` — confirming `async_delete_session`'s "404 = already
gone, treat as success" handling matches real behavior, not an assumption
about it. Not verified: the full periodic-cleanup path end-to-end against
a real Home Assistant instance over real elapsed time (waiting 24 hours in
a session isn't practical to simulate) — the underlying API calls are
confirmed working; the scheduling and age-filtering logic around them
follows directly from that but hasn't been watched actually fire on a
live schedule.

## "Voice doesn't work from the iOS Companion app" — not a bug in this repo

2026-08-28: user report — the agent worked from the web frontend and from
typed text in the iOS Companion app's Assist screen, but not from voice on
the same app: speech was transcribed correctly (visible in the UI), the
"waiting for response" indicator appeared, and then nothing ever arrived —
indefinitely. Manually pressing the mic button to end the turn (instead of
letting on-device silence detection end it) made voice work too.

Ruled out this integration as the cause without needing to read any of its
code: typed text on the same device, through the same pipeline, reaches
this integration's `conversation.py` and gets a normal reply — so
`_async_handle_message` is being called and ZeroClaw is responding
correctly. If a voice turn's transcribed text never produces a reply
*either*, and there's no distinction in this integration's code between a
turn that arrived via STT vs. one that arrived typed (both are just
`user_input.text` by the time `_async_handle_message` sees it), the turn
in question is not reaching this integration at all. The only place left
for it to be lost is client-side, in the iOS Companion app's own handling
of on-device dictation + automatic end-of-speech detection — before a
pipeline run is ever dispatched to Home Assistant. Forcing the end of the
turn manually (tapping the mic button rather than waiting for auto-detected
silence) sidesteps whatever that client-side path is doing and works
every time, which is consistent with this diagnosis.

Not something this repo can fix: it's an Home Assistant iOS Companion App
behavior, independent of which conversation agent is configured — would
happen the same way with any agent, not specific to ZeroClaw. Documented
here only so a future report of "voice doesn't work" isn't re-diagnosed as
an integration bug from scratch; the practical workaround (manually ending
the mic turn instead of relying on auto-silence-detection) is enough to
unblock actual use.

## `TOOLS.md`: require resolved `entity_id`s in watches, not friendly names

User request: a watch's follow-up action should reference the exact
entity (e.g. `switch.X`/`light.Y`), not a guessed-at friendly name, "in
modo tale da essere sicuri del suo funzionamento."

The watched entity's `entity_id` was already hard-validated —
`_handle_create_watch` (`webhook.py`) rejects any `entity_id` that
`hass.states.get()` doesn't resolve, before a watch is ever armed — so
that half of the request was already covered structurally, not just by
instruction. The gap is the *other* entity: whatever `message` describes
acting on when the watch fires (e.g. "accendi le luci delle scale") is
opaque free text as far as this integration is concerned — it's the
agent's own future instruction to itself, delivered back over the
stateless `/webhook` call with none of the current conversation's
context, and this integration has no way to validate an entity reference
buried inside prose. Nothing server-side to add here; the fix is
`TOOLS.md` telling the agent to resolve exact `entity_id`s (via
`GetLiveContext` or its entity-listing tool) for *both* the watched
entity and any entity named in `message`, and to write the resolved
`entity_id` directly into `message` (e.g. "accendi light.luci_scale") —
so that when `message` comes back with none of this conversation's
context, it still carries an unambiguous target instead of a friendly
name the agent would otherwise have to re-guess from scratch at fire
time. Same belt-and-suspenders shape as the `to_state` guidance right
next to it (also instruction-only, also paired with what structural
validation is possible — see the state-alias entry above): the difference
here is that no structural validation is possible for `message`'s
contents at all, so the instruction is the entire mitigation for that
half.

Verified: `personality.py` compiles and `build_personality_files` renders
`TOOLS.md` correctly with the new paragraphs in both `en` and `it` (no
`.format()` brace conflicts) — not verified against a real agent actually
following the new instruction correctly, same acknowledged
instruction-following gap as the rest of `TOOLS.md`.

## Greeting the speaker by name

User request (2026-08-28): "voglio che ad ogni sessione lui vede quali
sono gli utenti (entità persone) presenti in casa e voglio che quando io
accedo all'assistente lui riconosca chi sono tra quelli... e mi saluti
tipo Ciao Lorenzo."

Two distinct problems bundled in one request, solved differently:

**"Knows the household roster"** doesn't need new code at all. The agent
already has live Home Assistant access via its `home_assistant` MCP
bundle, so `person.*` entities (name, home/away) are already one
`GetLiveContext` call away — no reason to duplicate that into a
personality file that would only go stale. `USER.md`'s "Who Lives
Here"/"Chi Vive Qui" section (previously just a static "fill this in
yourself" placeholder) now says exactly that: check `person.*` via
`GetLiveContext` for the live, authoritative list; the free-text section
stays for things Home Assistant can't express (preferences, nicknames).

**"Recognizes who's currently talking and greets them"** is the real new
capability, and *does* need code — an LLM reading `USER.md` has no way to
connect "the text I'm receiving right now" to a specific household member
on its own; Home Assistant's Assist protocol only ever hands a
conversation agent plain text, nothing about the speaker's identity.
`ConversationInput.context.user_id` (already read every turn since the
notify-target feature, see the person_notify.py entry above) is the only
thread back to a real identity — and the `person` integration is the only
place that value maps to a human display name: reading
`homeassistant/components/person/__init__.py`/`const.py` confirmed a
person entity linked to a user account (`CONF_USER_ID`, set via
**Settings → People** → "Linked user account") carries that same user_id
right back out as a plain state attribute
(`PersonEntityStateAttribute.USER_ID`, i.e. literally `"user_id"`) — the
reverse direction of the exact join `person_notify.py` already used for
notify targets. New `person_notify.py` function:
`async_resolve_person_name(hass, user_id)`, a plain (not `async def`)
function in the same style as `async_notify_targets_for_user` right next
to it, since `hass.states.async_all()` is synchronous despite the name
(HA's usual "call only from the event loop" convention, not a coroutine).

Getting the greeting delivered required deciding when a turn is "the
start of a new conversation" — the answer was already sitting in
`conversation.py`, unused for this purpose: `user_input.conversation_id`
is falsy exactly once per conversation, the very first turn, before this
integration mints its own id (`conversation_id = user_input.conversation_id
or uuid.uuid4().hex`) — captured as `is_new_conversation` *before* that
line overwrites it. Deliberately did not use `/ws/chat`'s own
`session_start.resumed` flag for this (briefly implemented, then reverted
— see the `async_call_ws_chat` docstring's own note on `resumed` for what
that flag is for) even though it answers a similar-sounding question:
`resumed` only becomes known *after* the message frame carrying this
turn's text has already been sent, so it can never gate what that same
message contains — a chicken-and-egg problem `user_input.conversation_id`
doesn't have, since it's known before anything is sent. `resumed` stays
unused/unexposed; no reason to carry dead complexity for a question
`conversation_id` already answers earlier and more directly.

When both are true — new conversation, speaker resolved —
`_async_handle_message` prepends a fixed, English, bracketed system note
(`_SPEAKER_CONTEXT_TEMPLATE`) to `user_input.text` before sending it to
ZeroClaw, naming the speaker and instructing the agent to greet them
without quoting the note itself; `SOUL.md` (both `en`/`it`, same
instruction) teaches the agent what that note means and how to react.
English regardless of the household's own language — deliberately, not an
oversight: the per-language `SOUL.md` "Always respond in `<language>`"
directive (see `_localize`/`_resolve_ha_language`) already anchors reply
language for all 64 supported languages, so the note doesn't need
translating to be understood the same way English `SOUL.md` content
already is for every non-en/it language today. No linked person entity for
that HA user (guest login, or a household that's never linked `person.*`
to user accounts) means `speaker_name` is `None` and nothing is prepended
— silent fallback, not an error, matching every other "unresolvable, so
skip gracefully" case in this integration.

Verified: `person_notify.py`, `conversation.py`, `api.py` (reverted to its
pre-`resumed` form), and `personality.py` all compile;
`build_personality_files` renders the updated `USER.md`/`SOUL.md` for
`en`/`it`/a fallback language (`de`) with the new content present and no
`.format()` conflicts. Not verified against a real Home Assistant
instance — same acknowledged gap as every other feature in this file that
needs a live `person.*` entity linked to a real user account to exercise
end-to-end; the existing agent (this household's "Mario") also needs the
`USER.md`/`SOUL.md` additions applied by hand to get this at all, same
retrofit story as every personality-file change here.

## Compressed `SOUL.md`'s home-helper addition

User request (2026-08-28): "fai prompt compression, i vari MD di un
certo agente voglio che siano ridotti all'osso ma con tutti gli elementi
importanti." Clarified on request: not a live agent's actual on-disk
files (those aren't reachable from this dev machine — a direct attempt
to read the connection token out of Home Assistant's own
`.storage/core.config_entries` to fetch them over the API was correctly
blocked by the auto-mode permission classifier, and wasn't pursued
further) — the target is `personality.py`'s own `_SOUL_ADDITIONS`
template, the one every newly-created agent gets, which had grown
wordy across many incremental edits this session (each one individually
reasonable, the sum less so).

Rewrote both `en`/`it` bodies tighter: cut repeated/redundant phrasing
(the watch-recognition bullet dropped its full worked "Quando spengo le
luci..." vs "tell me when the washing machine finishes" side-by-side
comparison down to just the trigger words and the core rule), collapsed
multi-clause sentences, dropped throat-clearing ("If you don't have
control over something, say so honestly" merged onto the confirm-plainly
bullet instead of its own bullet). Every distinct rule survived —
nothing safety-relevant (cover/lock tool routing, the watch's
once-not-forever default) was thinned for length; only prose was cut,
never substance. `en`: 303 words / 2212 chars. `it`: 330 words / 2463
chars — both roughly half their pre-compression size.

Verified: `personality.py` compiles, ruff clean, `build_personality_files`
still renders `SOUL.md` correctly with the compressed content. Same
retrofit story as every other personality-file change: only newly-created
agents get this version — an existing agent (e.g. this household's
"Mario") keeps whatever SOUL.md it already has unless the household
applies the same edit there by hand.

## Sharpened the memory bullet: durable facts, not a command log

Companion to a change in the `addon-zeroclaw` repo (see its own
`docs/DECISIONS.md`, "`memory.auto_save = false`"): the add-on now turns
off ZeroClaw's blanket auto-save of every message as conversation
history, on user request ("non mi interessa che salvi in memoria che hai
spento la luce, mi interessa che salvi... questa entità si trova in
camera di X"). With that structural logging off, the *only* remaining
path for anything durable to end up in memory is the agent's own
`memory_store` tool calls — so the instruction guiding those calls needed
to be explicit about what's actually worth storing, not left implicit.

`SOUL.md`'s memory bullet (`_SOUL_ADDITIONS`, both `en`/`it`) went from
"`memory_store` area/domain/entity names as learned" to naming the
category directly — entity locations, aliases, household preferences,
anything explicitly asked to be remembered — **and** naming the negative
case explicitly ("turned off the kitchen light" isn't memory-worthy),
since without `auto_save` catching routine actions as a side effect
anymore, the agent has no safety net if it under-stores; better to be
explicit about the boundary than rely on it inferring "don't log
commands" on its own.

Verified: `personality.py` compiles and lints clean.

## Optional `X-Webhook-Secret` on `/webhook`, and an options flow to rotate it

Companion to the `addon-zeroclaw` change of the same session (see that
repo's `docs/DECISIONS.md`, "`gateway.webhook_secret`: what it actually
is, and the `config set` trap" — including why it must be set through an
env var rather than `zeroclaw config set`, which silently no-ops).

What this side needed: send `X-Webhook-Secret` on `POST /webhook` calls
when the gateway has a secret configured. Deliberately scoped to that one
endpoint, because that is all ZeroClaw applies it to — `/ws/chat` (every
Assist turn) and `/api/*` (this integration's whole config flow) are
untouched by it, so a mismatch degrades `ai_task`, the `notify_agent`
service and a fired watch's follow-up, but never Assist itself.

New `CONF_WEBHOOK_SECRET`, an optional field in config-flow step 1, and
`api.webhook_secret_for(entry)` resolving the effective value for the
three `/webhook` call sites (`ai_task.py`, `conversation.py`'s
`async_notify_agent`, `watch.py`'s `_async_fire`). Absent/blank — which
is what *every* entry created before this looks like — means no header,
matching a gateway with no secret set, so nothing breaks on upgrade.

**An options flow was necessary, not a nicety.** There was no options or
reconfigure flow at all, so the only way to give an existing entry a
secret would have been removing and re-adding the integration — which
throws away its agent selection and, worse, its registered notify-webhook
ID, already baked into whatever `TOOLS.md` its agent was taught. The
handler exposes only the webhook secret: host and agent are this entry's
identity (`_async_abort_entries_match` keys on `(host, agent)`) and the
webhook ID must stay stable, so neither belongs in an editable options
form. Shape confirmed against real `home-assistant/core` source
(`homeassistant/components/iss/config_flow.py`) rather than written from
memory: `@staticmethod @callback async_get_options_flow(config_entry)`
returning an `OptionsFlow` whose `async_step_init` calls
`async_create_entry(data=...)` — options land in `entry.options`, not
`entry.data`. `__init__.py` registers an update listener that reloads the
entry, since the entities doing the calling are constructed once at setup
and would otherwise keep the old value until a Home Assistant restart.

**A real bug was caught by testing the precedence logic, not by review.**
The obvious `entry.options.get(X) or entry.data.get(X)` is wrong: when the
operator *clears* the field, `"" or "old"` resolves back to the
setup-time value, so removing a secret would leave this still sending the
stale one — 401 on every `/webhook` call, while the UI showed an empty
field. Fixed to branch on key *presence* in `options` rather than
truthiness. Verified with a table of seven cases (legacy entry, set at
setup, empty at setup, options override, cleared via options, options
only, unrelated options present) run against the real `api.py` with the
Home Assistant modules stubbed out — the same importlib technique used
for `personality.py` elsewhere in this file.

Not verified: the options flow actually rendering and round-tripping in a
live Home Assistant UI — the usual gap for this repo, which has no
`pytest-homeassistant-custom-component` harness. The API shape is
source-confirmed and everything compiles and lints clean; the header
contract itself (name, additive-to-bearer semantics, 401 on mismatch) was
verified end-to-end against a real ZeroClaw container on the add-on side.

## Automated tests: a hermetic suite, and a cross-repo one with a fake model

User request (2026-09-05): "dei test automatici come potrei attuarli per
entrambe le repo […] tipo un test automatico sovrapposto tra i 2 repo […]
anche il deploy di homeassistant e un AI fake (che risponde in modo
hardcodato)". The instinct was right, and it split cleanly into two
layers that are worth keeping separate.

**Layer 1 — hermetic (`pytest`, ~3s, no network, no containers).** Real
Home Assistant core through `pytest-homeassistant-custom-component`.
Covers what unit tests can actually catch, chosen by looking at what has
genuinely broken here rather than by chasing coverage: `_normalize_state`
(the `"spento"` watch that could never fire), the notify/watch webhook's
validation and `to_state` normalization, `webhook_secret_for`'s
precedence, personality rendering in `en`/`it`/a fallback language (a
stray brace in `TOOLS.md` raises mid-config-flow), and the config flow
including its `(host, agent)` uniqueness rule. 63 tests.

Getting the harness running took three rounds of a dependency chase that
is worth recording, because none of it is guessable: `conversation`
imports `hassil`, whose version must match HA's own pin; `ai_task` pulls
`camera`, which needs `PyTurboJPEG`; and the `homeassistant` base
component has to be set up explicitly or `conversation` dies on
`KeyError: 'homeassistant.exposed_entities'`. `requirements_test.txt`
carries the pins and the one-liner that reads them back out of HA's own
manifests when the harness is bumped. On Windows the harness cannot be
installed at all without a C toolchain (`lru-dict`), so local runs go
through a Linux container — which is also what CI does, so the two match.

**Layer 2 — end-to-end across both repositories.** Real HA core → this
integration → a real ZeroClaw daemon built from `addon-zeroclaw`'s own
Dockerfile → `tests/e2e/fake_llm.py`. The fake is the whole trick: ZeroClaw's
`custom` provider slot takes any OpenAI-compatible endpoint, so the one
component that is non-deterministic, rate-limited and costs money gets
swapped for a substring lookup table, and every other link stays
genuine. Four tests: `/webhook` returns the canned reply (the contract
`ai_task`/`notify_agent`/watch follow-ups depend on), `/ws/chat` resumes
the same session across two connects (the thing the fictional
`X-Session-Id` header silently broke), and two real Assist turns driven
through `conversation.async_converse`.

Both repositories run this same stack, each checking the other out —
deliberately, rather than one triggering the other through
`repository_dispatch`, which would need a PAT the household would have
to create and rotate. Both repos are public, so `actions/checkout`
suffices and there is no secret to manage.

Three things fought back, all now encoded in `tests/e2e/conftest.py` and
that directory's README:

- The harness blocks sockets and pins them to localhost. The gateway's
  host is added to pytest-socket's allowlist rather than switching the
  guard off, so a stray call elsewhere still fails loudly.
- The harness replaces `async_get_clientsession` with one that cannot
  reach the network — nulled DNS resolver, different event loop. The
  symptom is `'NoneType' object has no attribute 'getaddrinfo'`, which
  points nowhere near mocking. A `real_client_session` fixture hands
  `api.py` a genuine session on HA's own loop.
- **Seeding a provider does not bind an agent to it.** The add-on writes
  `[providers.models.custom.fake]`, but each agent carries its own
  `model_provider` and the baked-in default points at
  `openrouter.default`, so every turn failed with `LLM request failed`
  until the agent was repointed. Not a bug — the config flow does this
  binding in normal use — but it has to be done explicitly in `up.sh`,
  and it only works through the live `PUT /api/config/prop` API, since
  `zeroclaw config set` rejects `agents.<alias>.model_provider` as an
  unknown property (the same dynamic-map-path limitation the add-on's
  `run.sh` already works around for `mcp.servers`).

Everything above was run locally before being pushed: 63 hermetic tests
green, 4 e2e green against a stack built from scratch by `up.sh`, and the
add-on's new boot assertions dry-run against a real container. Not
verified: the workflows themselves on GitHub's runners — that only the
first real CI run can show.
