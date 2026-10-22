"""Ask a person on their phone before ZeroClaw runs a gated tool.

ZeroClaw's `supervised` level pauses a turn on any tool that's in
`always_ask` (this add-on puts the cover tools there, so the agent can't
open the gate unattended) or not in `auto_approve`, and sends `/ws/chat` an
`approval_request`. Nobody in an Assist dialog can answer that, so until
now it was always refused (see `api.async_stream_ws_chat`) — which made
those tools simply unusable from Assist rather than confirmed.

This sends an actionable notification (Allow / Deny) to the mobile_app
devices of whoever is talking — the same user → device join
`person_notify.py` uses — and waits for the tap. No device to ask means
`None`, which keeps the old refuse-and-explain behaviour; no answer in
time means a refusal. Nothing is ever approved without a person tapping
Allow.

Actionable notifications go through the legacy per-device service
`notify.mobile_app_<device name>` (it takes `data.actions`; the newer
`notify.send_message` entity action only takes message and title), which
mobile_app registers per push-capable device: `targets` keyed by
`device_name` in `mobile_app/notify.py`, read at the 2026.10.2 tag. The tap
comes back as a `mobile_app_notification_action` event fired by the
Companion app (documented by the app, not core), carrying our `action`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import slugify

from .const import DATA_LAST_USER_ID, DOMAIN
from .person_notify import MOBILE_APP_DOMAIN

_LOGGER = logging.getLogger(__name__)

EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"

MAX_WAIT_SECONDS = 90
"""Longest a turn waits for a tap. Kept under ZeroClaw's own 120s approval
timeout (`WS_APPROVAL_TIMEOUT_SECS`) so the refusal is ours, sent while
the request is still pending, not ZeroClaw's after it gave up."""

MIN_WAIT_SECONDS = 5

_TEXT = {
    "it": ("{agent} vuole usare «{tool}»{summary}. Consenti?", "Consenti", "Nega"),
    "en": ("{agent} wants to use “{tool}”{summary}. Allow it?", "Allow", "Deny"),
}


def _notify_services_for_user(hass: HomeAssistant, user_id: str) -> list[str]:
    """`notify` service names (without the domain) of this user's
    push-capable mobile_app devices."""
    services = []
    for entry in hass.config_entries.async_entries(MOBILE_APP_DOMAIN):
        if entry.data.get("user_id") != user_id:
            continue
        name = f"mobile_app_{slugify(entry.data.get('device_name') or '')}"
        if hass.services.has_service("notify", name):
            services.append(name)
    return services


def _readable_tool(tool: str) -> str:
    """`home_assistant__cover__HassOpenCover` → `HassOpenCover`."""
    return tool.rsplit("__", 1)[-1]


async def async_ask_on_phone(
    hass: HomeAssistant,
    entry: ConfigEntry,
    user_id: str | None,
    tool: str,
    summary: str,
    timeout_secs: int,
) -> bool | None:
    """Ask `user_id` (or, failing that, whoever last talked to this
    entry's agent) to allow `tool`. `True`/`False` is their answer — a
    missing answer is `False` — and `None` means there was no device to
    ask."""
    user_id = user_id or hass.data.get(DOMAIN, {}).get(DATA_LAST_USER_ID, {}).get(
        entry.entry_id
    )
    services = _notify_services_for_user(hass, user_id) if user_id else []
    if not services:
        return None

    request = uuid.uuid4().hex
    allow, deny = f"ZEROCLAW_ALLOW_{request}", f"ZEROCLAW_DENY_{request}"
    template, allow_title, deny_title = _TEXT.get(hass.config.language, _TEXT["en"])
    message = template.format(
        agent=entry.title,
        tool=_readable_tool(tool),
        summary=f" ({summary})" if summary else "",
    )

    answer: asyncio.Future[bool] = hass.loop.create_future()

    @callback
    def _on_action(event: Event) -> None:
        action = event.data.get("action")
        if action in (allow, deny) and not answer.done():
            answer.set_result(action == allow)

    unsub = hass.bus.async_listen(EVENT_NOTIFICATION_ACTION, _on_action)
    try:
        sent = 0
        for service in services:
            try:
                await hass.services.async_call(
                    "notify",
                    service,
                    {
                        "title": entry.title,
                        "message": message,
                        "data": {
                            "tag": request,
                            "actions": [
                                {"action": allow, "title": allow_title},
                                {"action": deny, "title": deny_title},
                            ],
                        },
                    },
                    blocking=True,
                )
                sent += 1
            except Exception as err:  # noqa: BLE001 - one dead phone mustn't stop the rest
                _LOGGER.warning("Could not ask for approval on %s: %s", service, err)
        if not sent:
            return None

        wait = max(MIN_WAIT_SECONDS, min(timeout_secs - 10, MAX_WAIT_SECONDS))
        try:
            async with asyncio.timeout(wait):
                return await answer
        except TimeoutError:
            _LOGGER.info("No answer to the approval for %s in %ss; refused", tool, wait)
            return False
    finally:
        unsub()
        # Take the question off every phone it went to, answered or not.
        for service in services:
            try:
                await hass.services.async_call(
                    "notify",
                    service,
                    {"message": "clear_notification", "data": {"tag": request}},
                    blocking=False,
                )
            except Exception as err:  # noqa: BLE001 - best effort
                _LOGGER.debug("Could not clear the approval on %s: %s", service, err)
