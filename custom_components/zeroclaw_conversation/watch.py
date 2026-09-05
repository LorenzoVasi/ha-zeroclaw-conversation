"""Lightweight, integration-owned "watch": notify a ZeroClaw agent when a
Home Assistant entity reaches a given state — the event-driven alternative
to a token-costing heartbeat poll (see docs/DECISIONS.md, "Scheduling and
event-driven triggers").

Deliberately NOT a real Home Assistant automation entity: having an LLM
author raw automation YAML/JSON through the config API was considered and
rejected (see docs/DECISIONS.md) — a watch here is plain Python state owned
by this integration, armed with `async_track_state_change_event`. Nothing
shows up in Settings → Automations; instead, each watch gets its own
`sensor` entity (see `sensor.py`) grouped under its agent's device, so
"what's armed, when did it last fire" is visible in Home Assistant itself,
not just answerable by asking the agent via `list_watches`. Persisted via
`Store` so a watch created before an HA restart is still armed after one.

A watch fires once and deactivates itself by default (`recurring=False`) —
explicit user requirement: asking "tell me when the washing machine
finishes" without saying "every time" or giving another recurrence
shouldn't silently keep re-triggering forever. `recurring=True` is the
opt-in for "every time this happens," and stays armed after firing.

A watch only fires for changes with **no attached HA user**
(`new_state.context.user_id is None`) — another explicit user requirement:
notify for a light turned off by a physical/Zigbee switch or another
automation, but not for one the household just turned off themselves via
the dashboard, or that ZeroClaw itself just turned off because it was
asked to via Assist (both of those authenticate as a real HA user, so
they're indistinguishable from each other by `user_id` alone — but both
are equally "the person already knows," which is the actual distinction
that matters here). See `_arm`'s `_on_change` for the exact check and
docs/DECISIONS.md for why this is the right signal to filter on.

Firing a watch **guarantees** a household notification
(`webhook.async_notify_household`, the same path `{"type": "notify"}`
uses) — it does not just forward `message` to the agent and hope it
decides to notify on its own. The agent is *additionally* told too (still
via a webhook call, best-effort), so it can act further if the message
implies an action ("...then start the dryer"), but the notification itself
no longer depends on the agent's own judgment to actually happen — see
docs/DECISIONS.md for the report that led to this (a fired, disarmed watch
that never actually notified the household).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import ZeroClawError, async_call_webhook, webhook_secret_for
from .const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HOST,
    SIGNAL_WATCH_ADDED,
    SIGNAL_WATCH_REMOVED,
    SIGNAL_WATCH_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = "zeroclaw_conversation_watches"


@dataclass
class Watch:
    """One armed watch. `asdict(...)` is the exact storage/wire shape."""

    watch_id: str
    entry_id: str
    entity_id: str
    to_state: str
    message: str
    recurring: bool = False
    created_at: str = ""
    last_triggered: str | None = None
    notification: str | None = None
    """What the household is actually told (see `webhook.async_notify_
    household`) — kept separate from `message` (what's sent to the agent
    to act on) per explicit user request: the two serve different
    audiences and don't read the same way. `message` might be a bare
    instruction ("accendi le luci delle scale"); `notification` is meant
    to be a natural sentence a person would want to read ("Si sono spente
    le luci in camera di Lorenzo, accendo le luci delle scale come
    richiesto"). Falls back to `message` when not given, so an agent that
    forgets to set it separately still notifies something rather than
    nothing."""


class WatchManager:
    """One instance shared by every config entry (see `__init__.py`,
    `hass.data[DOMAIN][DATA_WATCH_MANAGER]`) — watches aren't scoped to a
    single entry's lifecycle since Home Assistant state changes aren't
    either.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._watches: dict[str, Watch] = {}
        self._unsubs: dict[str, callback] = {}

    async def async_load(self) -> None:
        """Restore watches persisted from a previous run and re-arm them.
        Call once, at first setup — safe to call again (idempotent: a
        second load would just re-read the same file into the same dict,
        harmlessly re-arming already-armed watches), but `__init__.py`
        guards against that anyway. Does NOT dispatch `SIGNAL_WATCH_ADDED`
        — `sensor.py`'s own `async_setup_entry` builds initial entities
        directly from `list_for_entry` instead, since platform setup runs
        after this and would miss a signal sent before it was listening.
        """
        data = await self._store.async_load() or {}
        for raw in data.get("watches", []):
            watch = Watch(**raw)
            self._watches[watch.watch_id] = watch
            self._arm(watch)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"watches": [asdict(w) for w in self._watches.values()]}
        )

    def _arm(self, watch: Watch) -> None:
        @callback
        def _on_change(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None or new_state.state != watch.to_state:
                return
            if new_state.context.user_id is not None:
                # A person (via the dashboard) or ZeroClaw itself (via
                # Assist, or a watch's own triggered follow-up action) made
                # this change — both authenticate as a real HA user
                # (a dashboard session, or the home_assistant_token's
                # owning user for anything ZeroClaw does), and either way
                # whoever caused it already knows. Only changes with no
                # attached user — a Zigbee/ZHA device report, another
                # automation, anything not directly attributable to a
                # person acting through HA or to ZeroClaw itself — should
                # actually notify. Confirmed by reading
                # `homeassistant/core.py`: every user-initiated service
                # call (dashboard click or REST API call with a bearer
                # token, no difference between them) gets a `Context` with
                # `user_id` set to the authenticated user; a device
                # integration calling `states.async_set` directly (no
                # service call, no authenticated caller) does not.
                return
            self.hass.async_create_task(self._async_fire(watch))

        self._unsubs[watch.watch_id] = async_track_state_change_event(
            self.hass, [watch.entity_id], _on_change
        )

    async def _async_fire(self, watch: Watch) -> None:
        entry = self.hass.config_entries.async_get_entry(watch.entry_id)
        if entry is None:
            # The config entry that created this watch is gone (integration
            # removed/reconfigured) — nothing sensible to notify, drop it.
            await self.async_cancel(watch.watch_id)
            return

        # Import here, not at module level: webhook.py doesn't import this
        # module, so there's no real cycle, but keeping the import local
        # keeps the "who depends on whom" story obvious from a quick read
        # of each file's own top-of-file imports.
        from .webhook import async_notify_household

        try:
            await async_notify_household(
                self.hass, entry, watch.notification or watch.message
            )
        except Exception as err:  # noqa: BLE001 - a watch firing must not crash the event loop
            _LOGGER.warning(
                "Watch '%s' on %s fired, but notifying the household failed: %s",
                watch.watch_id,
                watch.entity_id,
                err,
            )

        try:
            await async_call_webhook(
                self.hass,
                entry.data[CONF_HOST].rstrip("/"),
                entry.data.get(CONF_API_TOKEN) or "",
                watch.message,
                uuid.uuid4().hex,
                agent=entry.data.get(CONF_AGENT) or "",
                webhook_secret=webhook_secret_for(entry),
            )
        except ZeroClawError as err:
            _LOGGER.warning(
                "Watch '%s' on %s fired, but telling the agent (for any "
                "follow-up action) failed: %s",
                watch.watch_id,
                watch.entity_id,
                err,
            )

        watch.last_triggered = dt_util.utcnow().isoformat()

        if watch.recurring:
            await self._async_save()
            async_dispatcher_send(
                self.hass, SIGNAL_WATCH_UPDATED.format(entry_id=watch.entry_id), watch
            )
        else:
            await self.async_cancel(watch.watch_id)

    async def async_create(
        self,
        entry_id: str,
        entity_id: str,
        to_state: str,
        message: str,
        recurring: bool,
        notification: str | None = None,
    ) -> str:
        """Arm a new watch and persist it. Returns its `watch_id`."""
        watch_id = uuid.uuid4().hex
        watch = Watch(
            watch_id=watch_id,
            entry_id=entry_id,
            entity_id=entity_id,
            to_state=to_state,
            message=message,
            recurring=recurring,
            created_at=dt_util.utcnow().isoformat(),
            notification=notification,
        )
        self._watches[watch_id] = watch
        self._arm(watch)
        await self._async_save()
        async_dispatcher_send(self.hass, SIGNAL_WATCH_ADDED.format(entry_id=entry_id), watch)
        return watch_id

    async def async_cancel(self, watch_id: str) -> bool:
        """Disarm and forget a watch. Returns whether it existed."""
        unsub = self._unsubs.pop(watch_id, None)
        if unsub is not None:
            unsub()
        watch = self._watches.pop(watch_id, None)
        existed = watch is not None
        if existed:
            await self._async_save()
            async_dispatcher_send(
                self.hass, SIGNAL_WATCH_REMOVED.format(entry_id=watch.entry_id), watch_id
            )
        return existed

    def list_for_entry(self, entry_id: str) -> list[Watch]:
        """Every watch owned by one config entry (one agent) — what
        `list_watches` reports back, so an agent only sees its own."""
        return [w for w in self._watches.values() if w.entry_id == entry_id]
