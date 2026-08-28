"""Inbound HA webhook: the one path a ZeroClaw agent uses to act on Home
Assistant *from its own side* — proactively notifying the household, and
arming/managing "watches" (see `watch.py`) for event-driven triggers.

Registered once per config entry (one webhook per agent), at a URL only
this integration and the agent it was taught to (via `TOOLS.md`, see
`personality.py`/`config_flow.py`) actually know. The webhook ID itself is
the credential, the same security model Home Assistant's own webhook
system already uses everywhere (an unguessable 64-hex-char ID,
`homeassistant.components.webhook.async_generate_id()`); there is no
separate auth header to check.

One webhook, dispatched on a `"type"` field (`notify` / `create_watch` /
`cancel_watch` / `list_watches`) rather than one webhook per capability —
keeps the URL an agent has to remember to exactly one, and this module the
one place new request types get added.

`local_only=True`: matches every other network assumption this integration
and the companion `zeroclaw` add-on already make (ZeroClaw reaches Home
Assistant, and vice versa, over the local/internal network only, never the
public internet) — see both repos' docs/DECISIONS.md.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from aiohttp import web
from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_WEBHOOK_ID, DATA_LAST_USER_ID, DATA_WATCH_MANAGER, DOMAIN
from .person_notify import async_notify_targets_for_user

_LOGGER = logging.getLogger(__name__)


async def async_register_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register this entry's webhook, if it was given one.

    Older entries created before this feature existed have no
    `CONF_WEBHOOK_ID` and simply don't get one — no retroactive migration;
    the user can remove and re-add the integration to opt in, same
    retrofit story as every personality-file change in docs/DECISIONS.md.
    """
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if not webhook_id:
        return
    webhook.async_register(
        hass,
        DOMAIN,
        f"ZeroClaw ({entry.title})",
        webhook_id,
        _handle_webhook,
        local_only=True,
    )


async def async_unregister_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unregister this entry's webhook, if it had one."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID)
    if webhook_id:
        webhook.async_unregister(hass, webhook_id)


def _entry_for_webhook(hass: HomeAssistant, webhook_id: str) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_WEBHOOK_ID) == webhook_id:
            return entry
    return None


async def _handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "JSON body must be an object"}, status=400)

    entry = _entry_for_webhook(hass, webhook_id)
    if entry is None:
        return web.json_response({"error": "unknown webhook"}, status=404)

    msg_type = data.get("type", "notify")
    if msg_type == "notify":
        return await _handle_notify(hass, entry, data)
    if msg_type == "create_watch":
        return await _handle_create_watch(hass, entry, data)
    if msg_type == "cancel_watch":
        return await _handle_cancel_watch(hass, entry, data)
    if msg_type == "list_watches":
        return _handle_list_watches(hass, entry)
    return web.json_response({"error": f"unknown \"type\": {msg_type!r}"}, status=400)


async def _handle_notify(
    hass: HomeAssistant, entry: ConfigEntry, data: dict
) -> web.Response:
    """`{"type": "notify", "message": "..."}` — create a household
    notification (a fresh one each call — `notification_id` left unset so
    Home Assistant generates a new one rather than overwriting the last
    notification from this same agent). Additionally pushes to whoever
    most recently talked to this entry's agent (see `person_notify.py`,
    `DATA_LAST_USER_ID`) — every `notify.*` entity belonging to *their*
    mobile_app devices, resolved fresh on every call rather than a fixed
    target configured once at setup. A failure pushing is logged, not
    fatal — the persistent notification already landed either way; so does
    having no known user yet (nobody's talked to this agent since Home
    Assistant last restarted) or a known user with no registered device —
    both are normal, not errors.
    """
    message = data.get("message")
    if not message or not isinstance(message, str):
        return web.json_response(
            {"error": '"message" (a non-empty string) is required'}, status=400
        )

    persistent_notification.async_create(hass, message, title=entry.title)

    last_user_id = hass.data.get(DOMAIN, {}).get(DATA_LAST_USER_ID, {}).get(
        entry.entry_id
    )
    if last_user_id:
        targets = async_notify_targets_for_user(hass, last_user_id)
        if targets:
            try:
                await hass.services.async_call(
                    "notify",
                    "send_message",
                    {"message": message, "title": entry.title},
                    target={"entity_id": targets},
                    blocking=True,
                )
            except Exception as err:  # noqa: BLE001 - report, don't fail the webhook over it
                _LOGGER.warning(
                    "ZeroClaw notify webhook: persistent notification created, "
                    "but pushing to %s failed: %s",
                    targets,
                    err,
                )

    return web.json_response({"status": "ok"})


# Home Assistant's entity states are a fixed, English-only vocabulary
# ("off", not "spento") regardless of what language the agent (or the
# household) is speaking — confirmed the hard way: a watch created with
# `to_state: "spento"` never fires, ever, because the entity's actual
# `new_state.state` is always the literal string "off". `_on_change`
# (watch.py) compares strings exactly, so a mismatch here is silent and
# permanent — no error at creation time (any non-empty string is a
# syntactically valid `to_state`), just a watch that stays armed forever.
# This alias table is the technical safeguard for that failure mode,
# independent of whether the agent's own TOOLS.md instruction (also
# strengthened alongside this, see personality.py) is followed correctly —
# same belt-and-suspenders pattern as the cover/lock personality-file
# mitigation elsewhere in this project.
_STATE_ALIASES: dict[str, str] = {
    # on/off (light, switch, fan, etc.)
    "acceso": "on", "accesa": "on", "accesi": "on", "accese": "on",
    "spento": "off", "spenta": "off", "spenti": "off", "spente": "off",
    # cover (blinds, garage doors, gates)
    "aperto": "open", "aperta": "open", "aperti": "open", "aperte": "open",
    "chiuso": "closed", "chiusa": "closed", "chiusi": "closed", "chiuse": "closed",
    # lock
    "bloccato": "locked", "bloccata": "locked",
    "sbloccato": "unlocked", "sbloccata": "unlocked",
    # presence (device_tracker / person)
    "casa": "home", "a_casa": "home", "in_casa": "home",
    "fuori": "not_home", "assente": "not_home", "via": "not_home",
    # generic on/off availability
    "disponibile": "on", "non_disponibile": "unavailable",
}


def _normalize_state(raw: str) -> str:
    """Map a natural-language state word (any language the household or
    the agent might use) to Home Assistant's actual internal state string,
    via `_STATE_ALIASES` if it's a recognized alias. Otherwise falls back
    to the lowercased, underscore-joined form regardless (`"ON"` ->
    `"on"`) — Home Assistant's own state strings are conventionally
    lowercase snake_case across every standard domain, so normalizing
    casing even for an unrecognized word is more likely correct than
    preserving whatever case the caller happened to send; a numeric sensor
    value passes through unaffected either way (digits have no case).
    """
    key = raw.strip().lower().replace(" ", "_")
    return _STATE_ALIASES.get(key, key)


async def _handle_create_watch(
    hass: HomeAssistant, entry: ConfigEntry, data: dict
) -> web.Response:
    """`{"type": "create_watch", "entity_id", "to_state", "message",
    "recurring"?}` — arm a watch (see `watch.py`). `recurring` defaults to
    `false`: a watch fires once and disarms itself unless the caller
    explicitly asks to keep it armed.
    """
    entity_id = data.get("entity_id")
    to_state = data.get("to_state")
    message = data.get("message")
    recurring = bool(data.get("recurring", False))

    if not entity_id or not isinstance(entity_id, str):
        return web.json_response(
            {"error": '"entity_id" (a non-empty string) is required'}, status=400
        )
    if hass.states.get(entity_id) is None:
        return web.json_response(
            {"error": f"no such entity: {entity_id!r}"}, status=400
        )
    if not to_state or not isinstance(to_state, str):
        return web.json_response(
            {"error": '"to_state" (a non-empty string) is required'}, status=400
        )
    if not message or not isinstance(message, str):
        return web.json_response(
            {"error": '"message" (a non-empty string) is required'}, status=400
        )

    normalized_state = _normalize_state(to_state)

    manager = hass.data[DOMAIN][DATA_WATCH_MANAGER]
    watch_id = await manager.async_create(
        entry.entry_id, entity_id, normalized_state, message, recurring
    )
    return web.json_response(
        {"status": "ok", "watch_id": watch_id, "to_state": normalized_state}
    )


async def _handle_cancel_watch(
    hass: HomeAssistant, entry: ConfigEntry, data: dict
) -> web.Response:
    """`{"type": "cancel_watch", "watch_id"}` — disarm a watch. Only
    disarms watches owned by the calling entry's own agent — a `watch_id`
    belonging to a different agent is treated as not found, not as a
    cross-agent authorization error, to avoid confirming its existence."""
    watch_id = data.get("watch_id")
    if not watch_id or not isinstance(watch_id, str):
        return web.json_response(
            {"error": '"watch_id" (a non-empty string) is required'}, status=400
        )

    manager = hass.data[DOMAIN][DATA_WATCH_MANAGER]
    owned = {w.watch_id for w in manager.list_for_entry(entry.entry_id)}
    if watch_id not in owned or not await manager.async_cancel(watch_id):
        return web.json_response({"error": "no such watch_id"}, status=404)
    return web.json_response({"status": "ok"})


def _handle_list_watches(hass: HomeAssistant, entry: ConfigEntry) -> web.Response:
    """`{"type": "list_watches"}` — every watch this agent currently has
    armed, so it can check before creating a duplicate or answer "what are
    you watching for me right now?" truthfully instead of guessing."""
    manager = hass.data[DOMAIN][DATA_WATCH_MANAGER]
    watches = [asdict(w) for w in manager.list_for_entry(entry.entry_id)]
    return web.json_response({"status": "ok", "watches": watches})
