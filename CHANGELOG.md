# Changelog

Versioning rules for this repository are in
[`docs/DECISIONS.md`](docs/DECISIONS.md) ("Versioning and releases").
Short version: `0.MINOR.PATCH`, MINOR for anything you'd want to know
about before updating, PATCH for fixes that change nothing you can see.

## 0.2.0

**You can now give the gateway a webhook secret.** If you set a
`webhook_secret` in the `zeroclaw` add-on (0.2.0 or later), put the same
value here — Settings → Devices & Services → ZeroClaw Conversation →
**Configure**. It's an optional second lock on the agent-facing endpoint,
on top of the API token. Leave both blank and nothing changes.

⚠️ The two sides have to match. If the add-on has a secret and this
doesn't (or they differ), AI Tasks, the `notify_agent` service and watch
follow-ups start being rejected. Talking to Assist is unaffected either
way.

- Added a **Configure** screen, so the webhook secret can be changed
  later without removing and re-adding the integration — which would
  otherwise throw away the agent selection and the notify-webhook the
  agent was already taught.
- The agent's instructions are shorter and sharper: `SOUL.md`'s
  home-helper section is about half its previous length with every rule
  intact, and the memory instruction now says plainly what's worth
  remembering (where things are, household preferences, anything you ask
  it to remember) and what isn't (that it turned a light off).
- Added a test suite: 63 tests against real Home Assistant core, plus an
  opt-in end-to-end suite that drives a real Assist conversation through
  a real ZeroClaw daemon with a fake model. Nothing user-facing, but it's
  why the above could be changed with any confidence.

## 0.1.0

Backfilled entry — this release predates the changelog.

First release: registers ZeroClaw as an Assist conversation agent and an
AI Task provider, with proactive notifications, event-driven watches
(each visible as its own entity), recognition of who's speaking, and
automatic cleanup of abandoned ZeroClaw sessions.
