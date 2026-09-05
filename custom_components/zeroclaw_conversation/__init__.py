"""The ZeroClaw Conversation integration.

Bridges Home Assistant to a ZeroClaw gateway
(https://github.com/zeroclaw-labs/zeroclaw), typically the companion
`zeroclaw` Home Assistant add-on: as an Assist conversation agent
(conversation.py), an `ai_task` provider for automations/scripts
(ai_task.py), and — one `sensor` entity per armed watch (sensor.py) — a
way to actually see what an agent is watching for from within Home
Assistant itself. All three share one HTTP call helper (api.py). This
module forwards the config entry to all three platforms, and additionally
owns the two pieces that aren't scoped to any one platform specifically:
the inbound notify/watch webhook (webhook.py) and the shared
`WatchManager` singleton (watch.py) — see docs/DECISIONS.md, "Scheduling
and event-driven triggers."
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DATA_WATCH_MANAGER, DOMAIN
from .watch import WatchManager
from .webhook import async_register_webhook, async_unregister_webhook

PLATFORMS = ["conversation", "ai_task", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ZeroClaw Conversation from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if DATA_WATCH_MANAGER not in domain_data:
        # One WatchManager total, not one per config entry — armed watches
        # aren't scoped to a single entry's setup/unload lifecycle any more
        # than a Home Assistant state change is. `async_load` restores
        # anything persisted from a previous run and re-arms it.
        manager = WatchManager(hass)
        await manager.async_load()
        domain_data[DATA_WATCH_MANAGER] = manager

    await async_register_webhook(hass, entry)
    # The webhook secret is read at call time (`api.webhook_secret_for`),
    # but the entity objects doing the calling are built once here at
    # setup — so an options-flow edit only takes effect after a reload.
    # Doing that automatically rather than making the user restart Home
    # Assistant after rotating the secret.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload this entry when its options change (see `async_setup_entry`)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await async_unregister_webhook(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
