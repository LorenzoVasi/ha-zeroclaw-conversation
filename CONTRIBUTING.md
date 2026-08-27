# Contributing to ha-zeroclaw-conversation

Read [`CLAUDE.md`](CLAUDE.md) and [`docs/DECISIONS.md`](docs/DECISIONS.md)
before changing anything here. `DECISIONS.md` in particular is not
optional background reading — this integration has repeatedly shipped
code that looked correct against the Home Assistant developer docs and
only failed once actually loaded by real HA core (an abstract-method gap
the docs never mentioned; a stateless endpoint an earlier version
mistakenly treated as session-aware). Several files here only make sense
once you know the specific behavior they're working around.

## Verify against `home-assistant/core` source, not memory or docs

`py_compile` catches syntax errors and nothing else. The developer docs at
developers.home-assistant.io are a good starting point but have been wrong
or incomplete often enough in this project's history that they can't be
trusted alone for anything load-bearing — an abstract property the docs
never mention, a helper's exact signature, whether a field exists on a
dataclass. Before relying on a Home Assistant core API you haven't already
seen used correctly elsewhere in this codebase:

```sh
gh api repos/home-assistant/core/contents/homeassistant/<path>/<file>.py -q .content | base64 -d
# or, to find the right file first:
gh api -X GET search/code -f q='<symbol> repo:home-assistant/core' -q '.items[].path'
```

Read the actual current source, confirm the exact signature/behavior, and
cite what you found (file + what it showed) in the PR description or a
code comment if it's non-obvious. Every Home Assistant API this
integration calls that matters (`ConversationEntity`, `webhook`,
`persistent_notification`, `entity_registry`, `helpers.storage.Store`,
`helpers.event.async_track_state_change_event`, `entity_platform`) was
verified this way — see `docs/DECISIONS.md` for the specific findings.

There is currently no `pytest-homeassistant-custom-component` test harness
set up for this repo, and no way to load this integration into a real
running Home Assistant instance from CI. That's an accepted, documented
limitation, not something to quietly work around by skipping verification
— it means source-reading is the primary verification method here, and
"ask the user to test against their real instance and report back" is a
legitimate, expected step for anything CI can't cover (see the CI section
below for what it *can* catch).

## The retrofit caveat — say it every time

Personality-file content (`personality.py`'s `SOUL.md`/`USER.md`/
`TOOLS.md` additions) is only written when an agent is **created** through
this integration's config flow. Editing `personality.py` changes what a
*future* newly-created agent gets — it does nothing for an agent that
already exists, including one created five minutes before your change
shipped. There is no live-push mechanism from this integration to an
already-running agent's personality files today. If your change adds or
modifies personality content, say so explicitly (in the PR description,
and as a note in `docs/DECISIONS.md`) rather than letting it read as if
existing agents are automatically upgraded — they aren't, and a user who
assumes they are will file a confusing bug report.

## Prefer resolving from Home Assistant's own state over a new config field

When a fixed field in the config flow and a dynamic lookup from something
Home Assistant already tracks would both work, prefer the dynamic lookup.
The notify-target mechanism (`person_notify.py`) went through exactly this
tradeoff: an earlier version asked the user to pick one `notify.*` service
at setup; the current version resolves it live from whoever's Home
Assistant user account most recently talked to the agent, via the same
user_id join `mobile_app` itself uses internally. Fewer config fields to
keep in sync by hand, and it degrades gracefully (persistent-notification-
only) rather than needing reconfiguration when circumstances change. Not
a universal rule — `CONF_HOST`/`CONF_AGENT` are correctly static fields,
there's no live signal to resolve them from — but when Home Assistant
already has the answer, ask it instead of asking the user.

## Localizing new personality-file content

If you add new agent-facing text in `personality.py` (a `SOUL.md` bullet,
a `TOOLS.md` section), it needs a translation entry for every language key
that module tracks, following the existing pattern: a full hand-written
translation for `en` and `it` (this integration's two primary languages —
`it`, run through a real bilingual check, not machine-translated blind),
and the automatic directive-swap fallback (`_localize()`) covers the
other 62 Home Assistant-supported languages without you writing 62
translations by hand. Don't add a third full translation without a way to
actually verify its quality — the fallback mechanism exists specifically
so that isn't a blocker for adding new content.

## Before opening a PR

- [ ] Every new/changed Home Assistant core API call verified against
      current `home-assistant/core` source (see above), not assumed.
- [ ] `python -m py_compile custom_components/zeroclaw_conversation/*.py`
      passes (CI's `ruff` job also catches this, but faster locally).
- [ ] Any new/changed `strings.json` also mirrored into
      `translations/en.json` — they're kept in sync by hand in this repo,
      there's no build step that copies one to the other.
- [ ] New personality-file content localized per the section above, and
      the retrofit caveat noted if applicable.
- [ ] `docs/DECISIONS.md` updated for anything non-obvious you found or
      changed — see the companion `addon-zeroclaw` repo's own
      `CONTRIBUTING.md` for the exact entry shape both repos follow.

## CI: what it catches, and what it can't

`.github/workflows/validate.yml` runs `hassfest` (Home Assistant's own
integration-structure/manifest validator), `hacs/action` (HACS's own
submission-readiness checks — see the "Publishing" section of
`docs/DECISIONS.md` for what these gate), and `ruff` (lint + syntax). None
of these load the integration into a running Home Assistant instance or
exercise any code path at runtime — they catch structural and static
issues, not behavioral ones. A change that passes CI cleanly can still be
behaviorally wrong in ways only a real HA instance would surface; see
"Verify against `home-assistant/core` source" above for how this project
compensates for that gap without a live test harness.

## Why this repo is separate from `addon-zeroclaw`

Covered in the companion repo's `docs/DECISIONS.md` first entry — short
version: two different Home Assistant extension mechanisms (Python loaded
into HA core vs. a Docker add-on) with two different distribution channels
(HACS vs. a repository URL), kept as two repos on purpose. If a change
here needs a matching change there (or vice versa), say so explicitly in
the PR description; the two repos don't share CI or versioning.
