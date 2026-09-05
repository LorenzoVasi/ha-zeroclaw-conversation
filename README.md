# ZeroClaw Conversation for Home Assistant

<p align="center">
  <img src="assets/ha-zeroclaw-conversation.png" alt="Home Assistant <-> ZeroClaw" width="340" />
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=LorenzoVasi&repository=ha-zeroclaw-conversation&category=integration">
    <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open this repository inside the Home Assistant Community Store." />
  </a>
</p>

This is the piece that connects [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)
to the parts of Home Assistant that actually talk to *you*. Install it and
your ZeroClaw agent can answer through Assist's mic and text box, and be
called on by your own automations and scripts too. It's the companion to
the [`zeroclaw` add-on](https://github.com/LorenzoVasi/addon-zeroclaw),
which runs ZeroClaw itself — install that one first.

## What it can do

- **Becomes your Assist agent** — talk to it by voice or text through
  Home Assistant's own Assist, and it answers back, acting on your home
  when asked (turning things on/off, checking on the state of things,
  whatever it's set up to handle).
- **Remembers the conversation** while you're talking, and starts fresh
  each time you open a new one — no mixing up unrelated chats.
- **Knows who it's talking to.** If your Home Assistant account is linked
  to one of your household's people, it recognizes you and greets you by
  name at the start of a conversation.
- **Notifies you proactively** — after finishing something you weren't
  watching it do, it'll actually tell you, both in Home Assistant's
  notification center and pushed to your phone.
- **Watches for things happening around the house and reacts** — ask it to
  keep an eye out for something ("tell me when the washing machine's
  done, then start the dryer") and it arms a watch instead of endlessly
  checking; every watch it's keeping shows up as its own entity, so you
  can always see what it's waiting for.
- **Can be triggered by your own automations**, not just by talking to it
  — a new Home Assistant service lets any automation hand it an event
  directly.
- **Tidies up after itself**, clearing out old, abandoned conversations on
  its own so nothing piles up unnoticed.
- **Works as an AI Task provider** too, so scripts and automations can ask
  it to generate data on demand, separately from Assist.

## Requirements

- The [`zeroclaw` add-on](https://github.com/LorenzoVasi/addon-zeroclaw)
  (or any reachable ZeroClaw instance) up and running.
- Home Assistant's own
  [MCP Server](https://www.home-assistant.io/integrations/mcp_server/)
  integration, installed and enabled (**Settings → Devices & Services →
  Add Integration → MCP Server**) on the ZeroClaw side — it's what lets
  the agent actually act on your home rather than just talk about it.

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
   (or any reachable ZeroClaw instance) is running, and note the
   `api_token` you set for it.
2. **Settings → Devices & Services → Add Integration → ZeroClaw Conversation**.
3. Enter the gateway URL — it's pre-filled with the address that works for
   a locally-installed `zeroclaw` add-on. If that doesn't match your setup
   (a different install method, or more than one ZeroClaw around), you'll
   need the right one — see `docs/DECISIONS.md` if you get stuck tracking
   it down. There's also an optional **webhook secret** field: fill it in
   only if you set one in the add-on, with exactly the same value. You can
   change it later without redoing any of this — the integration's
   **Configure** button lets you edit just that.
4. When picking an agent, you can also create a brand-new one right from
   this screen — just name it and choose which already-configured AI
   provider it should use. It comes pre-shaped into a respectful,
   household-aware home helper, and if Home Assistant access is set up on
   the add-on side, it can act on your home from the moment it's created.
   Add real household details afterward, either through ZeroClaw's own
   dashboard or by hand.
5. **Settings → Voice Assistants**, edit a pipeline, set **ZeroClaw** as its
   conversation agent.

Most of what's described above under "What it can do" — the notifications,
the watches, the proactive greeting — only comes to life on a
freshly-created agent, or after adding the matching snippet by hand to an
existing one's personality files. The full details of exactly what to add
and why live in `docs/DECISIONS.md`, kept up to date as this project
evolves.

## Known iOS Companion app quirk

Voice input from the iOS Companion app's Assist screen can silently
produce no reply if you let on-device silence detection end your turn —
tap the mic button to manually end it instead and it works every time.
This is a Companion app behavior, not something specific to this
integration — see `docs/DECISIONS.md` for how that was tracked down.

## Built with agentic AI development

This integration — and its companion add-on — were built through agentic
AI development: multiple coordinated Claude Code agents doing the actual
research, coding, and testing, with a human checking real behavior against
a running instance before trusting any of it. Every decision made along
the way, including the mistakes and the dead ends, is logged in
[`docs/DECISIONS.md`](docs/DECISIONS.md) for anyone curious how it
actually came together.

## Status / known gaps

Still young: some corners (a few of the Home Assistant APIs this leans on,
watches surviving a restart) have been checked carefully against real
source but not yet fully exercised on a live, long-running instance. No
automated test suite yet either. Nothing that should stop you from trying
it — just worth knowing before you lean on it for something critical.

## License

MIT — see [LICENSE](LICENSE).
