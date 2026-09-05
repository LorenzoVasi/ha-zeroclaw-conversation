# Changelog

Versioning rules for this repository are in
[`docs/DECISIONS.md`](docs/DECISIONS.md) ("Versioning and releases").
Short version: `0.MINOR.PATCH`, MINOR when the intended behaviour
changes or grows, PATCH when something that was already meant to work
is made to work.

## 0.2.2

**Fixes the AI Task entity disappearing** — `AI Task entity
ai_task.<name> not found` — which 0.2.1 caused on newer Home Assistant
versions. Sorry: that was a regression I introduced with the previous
fix, and it made the feature vanish rather than just misbehave.

0.2.1 imported a Home Assistant dependency by a name that recent versions
no longer use (it was renamed upstream). The import failed, so the whole
AI Task platform failed to load and its entity stopped existing. It now
works with either name, and — the part that actually matters — can no
longer take the platform down: if that helper is missing entirely, the
prompt is simply a little less precise instead of the entity being gone.

If you were hit by this, updating and restarting is enough; the entity
comes back with the same name, so anything pointing at it keeps working.

## 0.2.1

**Fixes Home Assistant's built-in AI suggestions against ZeroClaw**,
which failed with `did not return valid JSON for this structured task:
unexpected character: line 1 column 1 (char 0)`.

Two causes, both fixed:

- The wanted shape was being described to the model as a raw Python
  object rather than as a JSON schema, so it often had no real idea what
  to produce.
- A reply that *was* correct JSON but came wrapped in a code fence or a
  polite sentence — which is exactly what a warm, chatty household agent
  tends to produce — was rejected outright. Those are now unwrapped.
  A reply with no JSON in it at all still fails, rather than guessing.

When it does fail now, the error names what the agent actually replied
instead of only where the parser gave up.

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
