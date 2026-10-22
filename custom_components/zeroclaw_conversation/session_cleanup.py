"""Periodic cleanup for ZeroClaw's own "zombie" `/ws/chat` sessions.

User report (2026-08-28): "ho notato su zeroclaw che le sessioni
precedenti rimangono attive... una sessione morta" — every time an Assist
chat window is closed and reopened, Home Assistant mints a brand-new
`conversation_id` (see `conversation.py`'s own docstring: the frontend
never reuses an old one once the dialog is closed). Since that value is
passed straight through as `/ws/chat`'s `session_id`, the *previous*
session's history stays sitting in ZeroClaw's own session backend
indefinitely — nothing in ZeroClaw itself expires it, and nothing on this
side ever explicitly deleted it before this module existed. Over enough
Assist conversations, this accumulates a growing pile of sessions nobody
will ever reconnect to.

Deliberately time-based (idle-age threshold), not tied to any "the window
was just closed" event — Home Assistant doesn't tell integrations when the
Assist dialog closes, only that a turn happened with whatever
`conversation_id` the frontend currently holds. A session idle for longer
than `ZOMBIE_MAX_AGE` is about as close to "provably abandoned" as this
integration can determine without that signal.

Only ever deletes sessions this integration *itself* opened, tracked in a
per-entry `SessionRegistry` (a `Store`): every `session_id` this entry
passes to `/ws/chat` is recorded there, and the cleanup pass refuses to
touch anything that isn't. An earlier version filtered on "this entry's
agent, no `channel_id`, idle >24h" alone — which a chat opened from
ZeroClaw's own dashboard, or by any other `/ws/chat` client on the same
agent, matches just as well, so those were permanently deleted too (PR
review, 2026-10-22; see docs/DECISIONS.md).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import ZeroClawError, async_delete_session, async_list_sessions
from .const import CONF_AGENT, CONF_API_TOKEN, CONF_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)

ZOMBIE_MAX_AGE = timedelta(hours=24)
"""How long a session can sit idle before this integration considers it
abandoned and deletes it. Chosen as "clearly longer than any real gap
between messages in an actual conversation, clearly shorter than
'accumulating forever'" — not user-configurable today; a fixed default
was judged good enough for the actual problem reported, not a knob
anyone's asked for yet."""

CLEANUP_INTERVAL = timedelta(hours=6)
STARTUP_DELAY = timedelta(minutes=2)
"""First cleanup pass waits this long after Home Assistant starts, so it
doesn't compete with everything else initializing during boot."""


STORAGE_VERSION = 1
SAVE_DELAY = 10
"""Seconds to batch registry writes over — a new id arrives at most once
per Assist conversation, and `Store` flushes pending delayed saves on
Home Assistant shutdown anyway, so nothing is lost by not writing
synchronously on every turn."""


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}_sessions_{entry_id}"


class SessionRegistry:
    """The `session_id`s this config entry has passed to `/ws/chat` — the
    only sessions `_async_cleanup` is allowed to delete.

    One `Store` per entry (not one shared file, like `WatchManager`'s):
    two entries can target the same gateway and agent, and each must only
    ever clean up what *it* opened. Removed with the entry
    (`async_remove_registry`, called from `__init__.async_remove_entry`).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, _storage_key(entry_id))
        self._session_ids: set[str] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._session_ids = set(data.get("session_ids") or [])

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._session_ids

    def __iter__(self):
        return iter(set(self._session_ids))

    def async_add(self, session_id: str) -> None:
        """Record `session_id` as opened by this entry. Called on every
        Assist turn, not just the first one of a conversation: cheap when
        already known (no write), and it re-records an id that a cleanup
        pass pruned because ZeroClaw hadn't persisted it yet."""
        if session_id in self._session_ids:
            return
        self._session_ids.add(session_id)
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def async_discard(self, session_id: str) -> None:
        if session_id not in self._session_ids:
            return
        self._session_ids.discard(session_id)
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    def _data_to_save(self) -> dict:
        return {"session_ids": sorted(self._session_ids)}


async def async_remove_registry(hass: HomeAssistant, entry_id: str) -> None:
    """Delete an entry's registry file once the entry itself is removed."""
    await Store(hass, STORAGE_VERSION, _storage_key(entry_id)).async_remove()


def async_setup_cleanup(
    hass: HomeAssistant, entry: ConfigEntry, registry: SessionRegistry
) -> None:
    """Schedule periodic zombie-session cleanup for one config entry's
    agent. Registers its own unload via `entry.async_on_unload`, so
    nothing further is needed when the entry is removed/reloaded.
    """

    async def _run(_now) -> None:
        await _async_cleanup(hass, entry, registry)

    entry.async_on_unload(async_call_later(hass, STARTUP_DELAY, _run))
    entry.async_on_unload(
        async_track_time_interval(hass, _run, CLEANUP_INTERVAL, name="zeroclaw_session_cleanup")
    )


async def _async_cleanup(
    hass: HomeAssistant, entry: ConfigEntry, registry: SessionRegistry
) -> None:
    host = entry.data[CONF_HOST].rstrip("/")
    token = entry.data.get(CONF_API_TOKEN) or ""
    agent = entry.data.get(CONF_AGENT) or ""

    if not agent:
        # This entry lets ZeroClaw pick an agent on its own (CONF_AGENT
        # left blank at setup — see config_flow.py's AUTO_AGENT_VALUE).
        # `GET /api/sessions` reports which agent actually handled each
        # session, but this integration has no way to know which alias
        # ZeroClaw resolved "auto" to, so it can't safely attribute any
        # session to itself here — skip cleanup entirely rather than risk
        # deleting a session some *other* caller of the same agent created.
        return

    try:
        sessions = await async_list_sessions(hass, host, token)
    except ZeroClawError as err:
        _LOGGER.debug("Session cleanup: could not list sessions: %s", err)
        return

    # Registry ids ZeroClaw no longer has (deleted from its dashboard, or
    # never persisted) have nothing left to clean up — drop them so the
    # registry doesn't grow forever. `session_id` is the id exactly as
    # this integration passed it: ZeroClaw stores a `/ws/chat` session
    # under `gw_<session_id>` and lists it with that prefix stripped
    # (`handle_api_sessions_list` in crates/zeroclaw-gateway/src/api.rs).
    listed = {s.get("session_id") for s in sessions}
    for session_id in registry:
        if session_id not in listed:
            registry.async_discard(session_id)

    cutoff = dt_util.utcnow() - ZOMBIE_MAX_AGE
    deleted = 0
    for session_meta in sessions:
        # Only ever touch sessions this entry itself opened (the
        # registry). The agent/channel checks are a second guard, not the
        # ownership test: on their own they also match chats opened from
        # ZeroClaw's own dashboard or any other /ws/chat client.
        session_id = session_meta.get("session_id")
        if not session_id or session_id not in registry:
            continue
        if session_meta.get("agent_alias") != agent or session_meta.get("channel_id"):
            continue

        last_activity = dt_util.parse_datetime(session_meta.get("last_activity") or "")
        if last_activity is None or last_activity >= cutoff:
            continue

        session_key = session_meta.get("session_key")
        if not session_key:
            continue

        try:
            await async_delete_session(hass, host, token, session_key)
            registry.async_discard(session_id)
            deleted += 1
        except ZeroClawError as err:
            _LOGGER.debug(
                "Session cleanup: could not delete session %s: %s", session_key, err
            )

    if deleted:
        _LOGGER.debug(
            "Session cleanup: deleted %d zombie ZeroClaw session(s) for agent '%s' "
            "(idle longer than %s)",
            deleted,
            agent,
            ZOMBIE_MAX_AGE,
        )
