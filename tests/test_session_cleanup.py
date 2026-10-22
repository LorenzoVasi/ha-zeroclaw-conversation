"""Zombie-session cleanup must only ever delete what this entry opened.

A session from ZeroClaw's own dashboard looks identical to one this
integration opened — same agent, no `channel_id`, just as idle — so the
registry is the only thing standing between the cleanup pass and someone
else's chat. See docs/DECISIONS.md, "Correction: the filter proved
nothing about ownership".
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.zeroclaw_conversation.const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HOST,
    DOMAIN,
)
from custom_components.zeroclaw_conversation.session_cleanup import (
    SAVE_DELAY,
    SessionRegistry,
    _async_cleanup,
)

MODULE = "custom_components.zeroclaw_conversation.session_cleanup"
AGENT = "mario"


def _session(session_id: str, *, idle: timedelta, agent: str = AGENT, channel=None):
    return {
        "session_id": session_id,
        "session_key": f"gw_{session_id}",
        "last_activity": (dt_util.utcnow() - idle).isoformat(),
        "agent_alias": agent,
        "channel_id": channel,
    }


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "http://local-zeroclaw:42617", CONF_API_TOKEN: "tok", CONF_AGENT: AGENT},
    )
    entry.add_to_hass(hass)
    return entry


async def _cleanup(hass, entry, registry, sessions) -> list[str]:
    """Run one pass against `sessions`, return the keys it deleted."""
    delete = AsyncMock()
    with (
        patch(f"{MODULE}.async_list_sessions", AsyncMock(return_value=sessions)),
        patch(f"{MODULE}.async_delete_session", delete),
    ):
        await _async_cleanup(hass, entry, registry)
    return [call.args[3] for call in delete.call_args_list]


async def test_only_sessions_this_entry_opened_are_deleted(hass: HomeAssistant, entry):
    registry = SessionRegistry(hass, entry.entry_id)
    registry.async_add("assist-old")
    registry.async_add("assist-recent")

    deleted = await _cleanup(
        hass,
        entry,
        registry,
        [
            _session("assist-old", idle=timedelta(days=2)),
            _session("assist-recent", idle=timedelta(hours=1)),
            # Opened from ZeroClaw's dashboard: same agent, no channel, idle.
            _session("dashboard-chat", idle=timedelta(days=2)),
        ],
    )

    assert deleted == ["gw_assist-old"]
    assert "assist-old" not in registry
    assert "assist-recent" in registry


async def test_registered_id_on_another_agent_or_channel_is_kept(
    hass: HomeAssistant, entry
):
    """The agent/channel checks still apply as a second guard."""
    registry = SessionRegistry(hass, entry.entry_id)
    registry.async_add("other-agent")
    registry.async_add("telegram")

    deleted = await _cleanup(
        hass,
        entry,
        registry,
        [
            _session("other-agent", idle=timedelta(days=2), agent="lili"),
            _session("telegram", idle=timedelta(days=2), channel="telegram.main"),
        ],
    )

    assert deleted == []


async def test_ids_zeroclaw_no_longer_has_are_pruned(hass: HomeAssistant, entry):
    registry = SessionRegistry(hass, entry.entry_id)
    registry.async_add("gone")
    registry.async_add("still-there")

    await _cleanup(
        hass, entry, registry, [_session("still-there", idle=timedelta(hours=1))]
    )

    assert "gone" not in registry
    assert "still-there" in registry


async def test_registry_survives_a_restart(hass: HomeAssistant, entry, hass_storage):
    registry = SessionRegistry(hass, entry.entry_id)
    registry.async_add("assist-1")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    await hass.async_block_till_done()
    assert hass_storage[f"{DOMAIN}_sessions_{entry.entry_id}"]["data"] == {
        "session_ids": ["assist-1"]
    }

    reloaded = SessionRegistry(hass, entry.entry_id)
    await reloaded.async_load()
    assert "assist-1" in reloaded
