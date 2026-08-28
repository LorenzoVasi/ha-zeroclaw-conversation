# ZeroClaw Conversation for Home Assistant

<p align="center">
  <img src="assets/ha-zeroclaw-conversation.png" alt="Home Assistant <-> ZeroClaw" width="340" />
</p>

A custom Home Assistant integration that registers
[ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) as:

- An **Assist conversation agent** — so the Assist mic/text box talks
  directly to your ZeroClaw agent, which can act on Home Assistant (turn
  things on/off, answer questions about state, etc.) via ZeroClaw's own MCP
  connection back into Home Assistant's
  [`mcp_server`](https://www.home-assistant.io/integrations/mcp_server/)
  integration.
- An [**AI Task**](https://www.home-assistant.io/integrations/ai_task/)
  provider — so automations and scripts can call `ai_task.generate_data`
  against ZeroClaw directly, independent of Assist.

This integration is the Assist-side half of a two-repo setup; the other half
is the [`zeroclaw` Home Assistant add-on](https://github.com/LorenzoVasi/addon-zeroclaw)
that actually runs ZeroClaw. Install that first.

## How it works

```
Assist mic/text ─▶ this integration ─▶ GET /ws/chat on ZeroClaw's gateway
                                        (?session_id=<conversation_id>&agent=<alias>)
                  ◀─ speech reply ──── {"type": "done", "full_response": "..."}
```

One short-lived WebSocket connection per Assist turn, not a held-open
socket — ZeroClaw resumes the same conversation history server-side as long
as `session_id` (Home Assistant's own `conversation_id`) stays the same,
which it does for as long as the Assist chat window stays open. Multi-turn
context lives on ZeroClaw's side this way, not duplicated into Home
Assistant's own chat log. (The `ai_task` platform uses the older, plain
`POST /webhook` instead — a one-shot data-generation call has no need for
conversation continuity.)

## Install

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories → add this repo's URL,
   category "Integration".
2. Install **ZeroClaw Conversation**, restart Home Assistant.

### Manual

Copy `custom_components/zeroclaw_conversation/` into your Home Assistant
`config/custom_components/`, then restart.

## Set up

1. Make sure the [`zeroclaw` add-on](https://github.com/LorenzoVasi/addon-zeroclaw)
   (or any reachable ZeroClaw daemon) is running, and note the `api_token`
   you configured for it.
2. **Settings → Devices & Services → Add Integration → ZeroClaw Conversation**.
3. Enter the gateway URL — pre-filled with `http://local-zeroclaw:42617`,
   the confirmed internal hostname for a *locally installed* `zeroclaw`
   add-on (slug `zeroclaw`, installed from "Local add-ons" rather than a
   published repository; confirmed by running `hostname` inside the actual
   container). If yours came from a published repository, or you have more
   than one ZeroClaw instance reachable on your network, double check this
   — run `hostname` inside the container (Portainer's console, or the SSH
   add-on) rather than trusting Portainer's container-list name, which
   shows the *Docker container name* (`app_local_zeroclaw`, underscored) —
   a different, easily-confused string from the actual resolvable hostname
   (hyphenated).
4. When picking an agent, you can also **create a new one** right from this
   flow instead of choosing an existing one: name it and pick which
   **already-configured model provider** it should use. Provider
   credentials themselves aren't entered here — configure at least one
   provider first in the `zeroclaw` add-on's own **Configuration** tab
   (its `providers` option), or directly in ZeroClaw's own dashboard; this
   flow just picks from whatever's already set up (more reliable — see
   `docs/DECISIONS.md` for why credential entry moved out of this
   integration). The new agent is created with a personality suited to
   being a respectful, household-aware home-automation helper (layered on
   top of ZeroClaw's own default personality files, not a replacement),
   and — if the `zeroclaw` add-on has Home Assistant integration configured
   — is immediately granted its `home_assistant` MCP bundle, so it can act
   on Home Assistant from the moment it's created rather than waiting for
   the add-on's next restart. Add real household member names afterward,
   either in ZeroClaw's own dashboard (`USER.md`) or by hand.
5. **Settings → Voice Assistants**, edit a pipeline, set **ZeroClaw** as its
   conversation agent.

## Scheduling, notifications, and event-driven triggers

Step 2 of setup also asks for one optional field: **Home Assistant URL**
(default `http://homeassistant:8123`) — enables the whole feature below
when set, skips it entirely when left blank.

Notifications always appear in Home Assistant's own notification center,
and additionally push to whoever's **phone** most recently talked to the
agent — resolved automatically from Home Assistant's own person/device
linkage (whichever mobile-app device is registered to that user account),
not something you pick once at setup. Nothing to configure for this part;
just make sure whoever should get notified has the Home Assistant
Companion App set up and has actually talked to the agent at least once
since HA last restarted (there's no "last known user" before that).

With Home Assistant URL set, a newly-created agent (not one picked from the existing-
agent list — see below) gets taught, via its own `TOOLS.md`, how to:

- **Notify you** — proactively, after finishing something you didn't watch
  it do directly (a scheduled task, something triggered by an automation),
  not for ordinary replies in a live conversation.
- **Watch for an event and act on it** — e.g. "tell me when the washing
  machine finishes, then start the dryer": the agent arms a watch on the
  relevant entity's state instead of polling. **Fires once by default** —
  say "every time" or give an actual recurring schedule if you want it to
  keep firing.
- **Manage its own schedule** — ZeroClaw's own `cron_*` tools, unblocked by
  the add-on (see its own README/DOCS.md) so the agent can add/list/remove
  scheduled jobs without an approval prompt on every call.

This also registers a **`zeroclaw_conversation.notify_agent`** Home
Assistant service, targeting a ZeroClaw conversation entity — the other
direction: an automation whose trigger is a state change can call this as
its action, to tell the agent about an event directly instead of the agent
finding out by polling. Example: an automation triggered by the washing
machine turning off, action `notify_agent` with message "The washing
machine just finished."

An agent picked from the *existing*-agent list, or one created directly
through ZeroClaw's own dashboard, doesn't get the `TOOLS.md` addition
automatically — same retrofit story as the personality files in general
(see `docs/DECISIONS.md`): add it by hand in ZeroClaw's dashboard if you
want an existing agent to have this too.

## Status / known gaps

- Setup only checks that the gateway is reachable (`GET /health`); it does
  not validate the API token (that would mean triggering a real LLM call
  during config flow). A wrong token surfaces on first real Assist turn
  instead, with a clear error message.
- `_async_handle_message` (the `ConversationEntity` method this integration
  implements) was confirmed against Home Assistant's developer docs, not
  against a live `homeassistant-core` checkout — verify against your actual
  HA version. See `docs/DECISIONS.md`.
- The notify/watch webhook, the `notify_agent` service, and watches
  surviving an HA restart are not yet verified against a real running Home
  Assistant instance — every Home Assistant API involved was checked
  against current `home-assistant/core` source, but that's not the same as
  having actually been loaded and exercised live. See `docs/DECISIONS.md`.
- No automated tests yet.

## License

MIT — see [LICENSE](LICENSE).
