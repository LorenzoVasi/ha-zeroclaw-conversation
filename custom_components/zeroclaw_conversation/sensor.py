"""Home Assistant visibility for ZeroClaw watches (`watch.py`) — one
`sensor` entity per armed watch, grouped under its agent's device.

User request (2026-08-28): "voglio... un'entità di tipo watch legato a
quell'agente, in modo tale che tramite Home Assistant in qualche modo
mostro quali sono i watch attivi, quando vengono triggerati, se avvisato
una sola volta o sempre" — what's armed, and when it last fired, should be
visible in Home Assistant itself, not only answerable by asking the agent
(`{"type": "list_watches"}`, still there, but no longer the only way to
check).

Watches are created from an HTTP webhook request (an agent's own
`http_request` tool call), not from anything this platform does — so
`WatchManager` (watch.py) can't just hand entities to `async_add_entities`
the normal way when a watch appears after startup. It dispatches signals
instead (`SIGNAL_WATCH_ADDED`/`_REMOVED`/`_UPDATED`, one per config entry
so a different agent's watches never cross-notify), which this module
listens for.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_WATCH_MANAGER,
    DOMAIN,
    SIGNAL_WATCH_ADDED,
    SIGNAL_WATCH_REMOVED,
    SIGNAL_WATCH_UPDATED,
)
from .watch import Watch


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create entities for whatever watches this entry's agent already has
    armed (restored from storage before platforms are set up — see
    `__init__.py`), then keep them in sync as watches are created,
    triggered, or cancelled for the rest of this entry's lifetime.
    """
    manager = hass.data[DOMAIN][DATA_WATCH_MANAGER]
    entities: dict[str, ZeroClawWatchSensor] = {
        w.watch_id: ZeroClawWatchSensor(hass, entry, w)
        for w in manager.list_for_entry(entry.entry_id)
    }
    async_add_entities(list(entities.values()))

    @callback
    def _on_added(watch: Watch) -> None:
        if watch.watch_id in entities:
            return
        entity = ZeroClawWatchSensor(hass, entry, watch)
        entities[watch.watch_id] = entity
        async_add_entities([entity])

    @callback
    def _on_removed(watch_id: str) -> None:
        entity = entities.pop(watch_id, None)
        if entity is not None:
            hass.async_create_task(entity.async_remove(force_remove=True))

    @callback
    def _on_updated(watch: Watch) -> None:
        entity = entities.get(watch.watch_id)
        if entity is not None:
            entity.async_update_from_watch(watch)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_WATCH_ADDED.format(entry_id=entry.entry_id), _on_added
        )
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_WATCH_REMOVED.format(entry_id=entry.entry_id), _on_removed
        )
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_WATCH_UPDATED.format(entry_id=entry.entry_id), _on_updated
        )
    )


def _watched_entity_name(hass: HomeAssistant, entity_id: str) -> str:
    """The watched entity's own friendly name, if it currently has state
    (almost always true — `create_watch` already rejects an unknown
    `entity_id` at creation time); falls back to the raw entity_id itself
    rather than failing, since a friendly label is a nicety, not something
    this entity's own correctness depends on."""
    state = hass.states.get(entity_id)
    return state.name if state is not None else entity_id


class ZeroClawWatchSensor(SensorEntity):
    """One entity per armed watch. State is constant ("armed") for as long
    as the watch is armed — there's no "disarmed" state to show, because
    the entity is removed outright the moment the watch itself stops being
    armed (fired-and-one-shot, or explicitly cancelled), matching the
    watch's real lifecycle instead of inventing a lingering "done" state
    for something that no longer exists on the ZeroClaw side either.
    Everything else — which entity/state it's waiting for, its message,
    whether it repeats, when it was armed, when it last fired — lives in
    `extra_state_attributes`, visible in Developer Tools → States or any
    dashboard card that shows entity attributes.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:eye-outline"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, watch: Watch) -> None:
        self._entry = entry
        self._watch = watch
        self._attr_unique_id = f"{entry.entry_id}_watch_{watch.watch_id}"
        self._attr_name = f"Watch: {_watched_entity_name(hass, watch.entity_id)}"

    @property
    def device_info(self):
        # Same `identifiers` as the conversation entity (conversation.py)
        # — Home Assistant merges device metadata by identifier, so this
        # entity groups under that same "ZeroClaw (<agent>)" device without
        # needing to repeat its name/manufacturer here.
        return {"identifiers": {(DOMAIN, self._entry.entry_id)}}

    @property
    def native_value(self) -> str:
        return "armed"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "watch_id": self._watch.watch_id,
            "entity_id": self._watch.entity_id,
            "to_state": self._watch.to_state,
            "message": self._watch.message,
            "notification": self._watch.notification or self._watch.message,
            "recurring": self._watch.recurring,
            "created_at": self._watch.created_at,
            "last_triggered": self._watch.last_triggered,
        }

    @callback
    def async_update_from_watch(self, watch: Watch) -> None:
        """Called for a recurring watch after it fires (a one-shot watch
        is removed instead, see `_on_removed` above) — refreshes
        `last_triggered` and the rest of the attribute set in place."""
        self._watch = watch
        self.async_write_ha_state()
