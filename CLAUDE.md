# ha-zeroclaw-conversation

A custom Home Assistant integration (`custom_components/zeroclaw_conversation/`)
that bridges Home Assistant to a [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)
gateway: as an **Assist conversation agent** and as an **`ai_task`**
provider. Distributed manually or via HACS, not through the HA add-on
store — this is Python loaded into HA core, a different extension
mechanism from the companion add-on.

**Companion repo**: `../addon-zeroclaw` — the actual Home Assistant add-on
that runs ZeroClaw itself (Docker, Ingress web UI). This repo just talks to
it over HTTP (`api.py`). The two are separate deliberately (see
`addon-zeroclaw/docs/DECISIONS.md`) but form one project.

**Read `docs/DECISIONS.md` before assuming anything about how this works.**
It's the detailed, continuously-updated record of every bug found, most of
them only surfacing against a *real* Home Assistant instance — e.g.
`ConversationEntity.supported_languages` being an abstract property despite
the developer docs not mentioning it, or the config flow's default host
guess being wrong twice in a row until confirmed by running `hostname`
inside the actual container. Static analysis / `py_compile` was not enough
to catch these; check DECISIONS.md first, and add to it (don't just fix
silently) when something similar happens again.

## Architecture at a glance

- `api.py`: the one place that calls ZeroClaw's gateway (`GET /ws/chat` for
  `conversation.py`'s multi-turn Assist calls, `POST /webhook` for
  `ai_task.py`'s stateless one-shot calls — `/webhook` has no session
  concept at all, don't use it where turn continuity matters, see
  DECISIONS.md's "Correction" entry — plus `GET /api/quickstart/state`,
  `GET/PUT /api/config/prop`). Auth is `Authorization: Bearer <token>`
  (ZeroClaw's pairing mechanism, not a webhook-secret header — that field
  only exists on ZeroClaw's unreleased `master` branch, see DECISIONS.md).
- `conversation.py`: the Assist agent (`ConversationEntity`). Declares
  `ConversationEntityFeature.CONTROL` (ZeroClaw handles device control
  itself, via its own MCP connection back into HA — not through HA's local
  exposed-entity intent matching).
- `ai_task.py`: the `ai_task.generate_data` provider (`AITaskEntity`).
  `GENERATE_DATA` only — no attachments, no image generation, structured
  (`task.structure`) output is prompt-engineered best-effort, not native.
- `config_flow.py`: two steps — host+token+optional HA-URL (validated by
  reachability), then an agent picked from a live-fetched dropdown
  (`async_fetch_agents`). Uniqueness is `(host, agent)`, not host alone, so
  multiple entries can target the same gateway with different agents —
  each gets a disambiguated display name (`"ZeroClaw (<agent>)"`). Also
  generates this entry's webhook ID here (blank HA-URL = feature off).
- `webhook.py` / `watch.py` / `person_notify.py`: the scheduling/
  event-driven-trigger feature (see DECISIONS.md, "Scheduling and
  event-driven triggers" and its immediate follow-up entry — read both
  before touching any of these three files). One inbound HA webhook per
  config entry (agent → HA: notify + create/list/cancel watch, dispatched
  on a `"type"` field) and a `WatchManager` singleton (`hass.data[DOMAIN][
  DATA_WATCH_MANAGER]`, one for all entries) that arms `async_track_state_
  change_event` listeners and persists them via `Store`. Notify targets are
  resolved dynamically (`person_notify.py`), not a fixed config field —
  whoever's `Context.user_id` was on the most recent Assist turn
  (`conversation.py` records it into `hass.data[DOMAIN][DATA_LAST_USER_ID]`
  on every turn), mapped to their `mobile_app` devices' `notify.*`
  entities. The other direction (HA → agent) is `conversation.py`'s
  `notify_agent` entity service, not a webhook — an automation's action,
  not something the agent calls. A watch firing notifies the household
  directly (`webhook.async_notify_household`, called from `watch.py`'s
  `_async_fire`) — not left up to the agent choosing to relay it; the
  agent is separately, best-effort told too, for any follow-up action.
- `sensor.py`: one entity per armed watch, grouped under its agent's
  device (visibility into what's armed — see DECISIONS.md's watch-entity
  entry). Entities are added/removed/updated via
  `homeassistant.helpers.dispatcher` signals `WatchManager` sends
  (`SIGNAL_WATCH_ADDED`/`_REMOVED`/`_UPDATED`, `const.py`), since watches
  are created from an HTTP webhook request, not from this platform's own
  setup — initial entities at startup come from `manager.list_for_entry`
  directly instead (load order: `WatchManager.async_load()` runs before
  platform setup, see `__init__.py`).
- `session_cleanup.py`: deletes ZeroClaw's own `/ws/chat` sessions
  (`api.py`'s `async_list_sessions`/`async_delete_session`, `GET`/`DELETE
  /api/sessions`) that this entry's agent hasn't touched in 24h — every
  closed-and-reopened Assist window otherwise leaves a permanent unused
  entry in ZeroClaw's own session backend, see DECISIONS.md. Runs on a
  timer set up from `conversation.py`'s `async_setup_entry`; skipped
  entirely for an entry with no specific agent selected (`CONF_AGENT`
  blank) since sessions can't be safely attributed in that case.

## Deployment

The user has a real Home Assistant OS instance reachable via a Samba share
mapped as `Z:` (the HA `config` share). After changing anything here, sync
to the live instance with:

```
robocopy "c:\Users\loren\Desktop\ha-zeroclaw-conversation\custom_components\zeroclaw_conversation" "Z:\custom_components\zeroclaw_conversation" /E /MIR
```

Then restart Home Assistant Core (`custom_components/` only loads at HA
startup) before testing.

## Verifying changes before shipping

`py_compile` and JSON validation catch syntax errors, nothing more — this
project has repeatedly shipped code that looked correct against the
developer docs and only failed once actually loaded by real HA core (see
DECISIONS.md). Before telling the user something works, either check the
relevant behavior against real home-assistant/core source
(`gh search code` / `gh api repos/home-assistant/core/...`) the way past
entries in DECISIONS.md did, or have the user test against their real
instance and check Settings → System → Logs for a traceback.
