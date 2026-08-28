"""Constants for the ZeroClaw Conversation integration."""

DOMAIN = "zeroclaw_conversation"

CONF_HOST = "host"
# ZeroClaw 0.8.4 has no generic webhook-secret header (that only exists on
# ZeroClaw's unreleased `master` branch). Auth here is a bearer token
# checked against ZeroClaw's `gateway.paired_tokens` (pairing enforced via
# `gateway.require_pairing = true`) — the same token the companion
# `zeroclaw` add-on's `api_token` option provisions. See
# docs/DECISIONS.md for how this was confirmed against a running gateway.
CONF_API_TOKEN = "api_token"

# ZeroClaw's `/webhook` picks an agent on its own when no `?agent=` query
# param is given: the migration-synthesized "default" agent, or else
# whichever agent is enabled first. Running ZeroClaw's own Quickstart wizard
# can create a differently-named agent instead of reusing "default" — in
# that case every webhook call silently keeps hitting the old (possibly
# still-unconfigured) agent unless the caller names the right one
# explicitly. Optional so existing single-agent setups keep working
# unchanged.
CONF_AGENT = "agent"

# Home Assistant's own address, as reachable from *inside* the ZeroClaw
# container (not from this HA instance's own perspective) — used only to
# build the notify-webhook URL taught to a newly created agent's `TOOLS.md`
# (see personality.py). Same value, same default, and the same underlying
# reason as the companion `zeroclaw` add-on's own `home_assistant_url`
# option (Supervisor's internal DNS): the two are independently configured
# because this integration and that add-on are separate installs that don't
# share config, not because the value is expected to differ in practice.
CONF_HA_URL = "ha_url"
DEFAULT_HA_URL = "http://homeassistant:8123"

# The HA webhook ID this config entry registers for its agent to POST
# notifications to (see webhook.py) — generated once at entry creation
# (`homeassistant.components.webhook.async_generate_id()`) and stored so it
# survives restarts; regenerating it on every setup would silently break
# whatever URL was already taught to the agent's `TOOLS.md`.
CONF_WEBHOOK_ID = "webhook_id"

# `hass.data[DOMAIN][DATA_WATCH_MANAGER]` — the one `WatchManager` instance
# shared by every config entry (see `watch.py`, `__init__.py`). Watches
# aren't scoped to a single config entry's setup/unload lifecycle the way
# platforms are, so this lives one level up, keyed into `hass.data[DOMAIN]`
# rather than `entry.runtime_data`.
DATA_WATCH_MANAGER = "watch_manager"

# `hass.data[DOMAIN][DATA_LAST_USER_ID][entry.entry_id]` — the HA user_id
# behind the most recent Assist turn for that entry's agent (see
# `conversation.py`, updated from `ConversationInput.context.user_id` on
# every turn). Read by `webhook.py`'s notify handler to resolve *whose*
# mobile_app devices to push to (`person_notify.py`) — replaces an earlier
# design that asked the user to configure one fixed `notify.*` service by
# hand, per explicit user request (2026-08-27): resolve it from Home
# Assistant's own person/device linkage instead, dynamically, from whoever
# is actually talking to the agent. `None` (or missing) when nobody has
# talked to this agent yet, or the turn had no attached user (e.g. a
# non-authenticated context) — the notify handler falls back to
# persistent-notification-only in that case, same as before.
DATA_LAST_USER_ID = "last_user_id"

# Dispatcher signals `sensor.py` listens on to add/remove/update a watch's
# entity as `WatchManager` (watch.py) arms, cancels, or fires one — watches
# are created from an HTTP webhook call, not from anything the entity
# platform itself does, so there's no other path for the platform to learn
# about them as they come and go. One signal name per entry (formatted with
# `entry_id`), not global, so entities from a different agent's watches
# never cross-notify.
SIGNAL_WATCH_ADDED = f"{DOMAIN}_watch_added_{{entry_id}}"
SIGNAL_WATCH_REMOVED = f"{DOMAIN}_watch_removed_{{entry_id}}"
SIGNAL_WATCH_UPDATED = f"{DOMAIN}_watch_updated_{{entry_id}}"

DEFAULT_TIMEOUT = 30
