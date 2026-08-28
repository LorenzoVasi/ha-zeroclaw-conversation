"""Resolve notify targets, and now also display names, from Home
Assistant's own person/device linkage, instead of static config the user
has to pick and keep in sync by hand.

User request (2026-08-27): "quando una persona scrive su HA, zeroclaw-
conversation vada a vedere quali sono i device legati a quella persona" —
whoever is actually talking to an agent should be who gets notified, not a
notify target configured once at setup and never revisited.

`async_notify_targets_for_user` deliberately goes through the HA *user*
(`Context.user_id`, carried on every Assist turn — see `conversation.py`),
not the `person` integration: a mobile_app device is registered to a user
account directly (confirmed by reading
`homeassistant/components/mobile_app/__init__.py`'s own
`_handle_user_removed`, which filters mobile_app config entries with the
exact same `entry.data["user_id"] == user_id` check used below — not an
invented pattern, the same one core itself uses for account-deletion
cleanup), so `person.*` entities are an unnecessary extra hop for that
specific lookup: user_id already *is* the join key mobile_app uses.

`async_resolve_person_name` (2026-08-28, "voglio che riconosca chi sono")
*does* need `person.*` specifically — it's the only place a HA user_id maps
to a human-friendly display name. Confirmed by reading
`homeassistant/components/person/__init__.py`/`const.py`: a person entity
configured with a linked user account (`CONF_USER_ID`) carries that same
user_id back out as a plain state attribute
(`PersonEntityStateAttribute.USER_ID`, i.e. `"user_id"`) — the reverse
direction of the same join `async_notify_targets_for_user` already uses.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

MOBILE_APP_DOMAIN = "mobile_app"
PERSON_DOMAIN = "person"


def async_notify_targets_for_user(hass: HomeAssistant, user_id: str) -> list[str]:
    """Every `notify.*` entity belonging to a mobile_app device registered
    to `user_id`. Empty (not an error) when that person has no mobile_app
    device, or has one that's never enabled push (confirmed by reading
    `mobile_app/notify.py`: a `NotifyEntity` is only created per config
    entry when `supports_push()` is true for it, so a push-less device
    simply contributes no entity here, same as having none at all).
    """
    entity_reg = er.async_get(hass)
    targets: list[str] = []
    for entry in hass.config_entries.async_entries(MOBILE_APP_DOMAIN):
        if entry.data.get("user_id") != user_id:
            continue
        for entity in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
            if entity.domain == "notify":
                targets.append(entity.entity_id)
    return targets


def async_resolve_person_name(hass: HomeAssistant, user_id: str) -> str | None:
    """The display name of whichever `person.*` entity is linked to
    `user_id`, or `None` if no person entity is linked to that account
    (e.g. a guest/service account with no matching `person`, or a
    household that hasn't linked its `person` entities to HA user accounts
    at all — Home Assistant doesn't require that link, so this is a normal,
    silent miss, not an error).
    """
    for state in hass.states.async_all(PERSON_DOMAIN):
        if state.attributes.get("user_id") == user_id:
            return state.name
    return None
