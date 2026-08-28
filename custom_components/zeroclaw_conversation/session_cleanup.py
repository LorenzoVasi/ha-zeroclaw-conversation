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
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import ZeroClawError, async_delete_session, async_list_sessions
from .const import CONF_AGENT, CONF_API_TOKEN, CONF_HOST

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


def async_setup_cleanup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedule periodic zombie-session cleanup for one config entry's
    agent. Registers its own unload via `entry.async_on_unload`, so
    nothing further is needed when the entry is removed/reloaded.
    """

    async def _run(_now) -> None:
        await _async_cleanup(hass, entry)

    entry.async_on_unload(async_call_later(hass, STARTUP_DELAY, _run))
    entry.async_on_unload(
        async_track_time_interval(hass, _run, CLEANUP_INTERVAL, name="zeroclaw_session_cleanup")
    )


async def _async_cleanup(hass: HomeAssistant, entry: ConfigEntry) -> None:
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

    cutoff = dt_util.utcnow() - ZOMBIE_MAX_AGE
    deleted = 0
    for session_meta in sessions:
        # Only ever touch sessions this integration itself could have
        # created: this entry's own agent, over a plain /ws/chat
        # connection (no channel_id — a channel-driven session, e.g. a
        # Telegram chat, is owned by that channel, not by us, even if it
        # happens to share this agent).
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
