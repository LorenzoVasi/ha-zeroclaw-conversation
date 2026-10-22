"""Approving a gated tool from the speaker's phone.

The property that matters most: nothing is approved unless a person taps
Allow on *this* request — no answer, a different request's answer, or no
phone at all never turns into a yes.
"""

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeroclaw_conversation.approval import (
    EVENT_NOTIFICATION_ACTION,
    async_ask_on_phone,
)
from custom_components.zeroclaw_conversation.const import DOMAIN

USER = "user-1"
TOOL = "home_assistant__cover__HassOpenCover"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="ZeroClaw (mario)", data={})
    entry.add_to_hass(hass)
    return entry


def _phone(hass: HomeAssistant, tap: str | None, user_id: str = USER) -> list[dict]:
    """A push-capable mobile_app device for `user_id` whose owner taps
    `tap` ("allow"/"deny", or None to ignore it). Returns what it was sent."""
    MockConfigEntry(
        domain="mobile_app", data={"user_id": user_id, "device_name": "Pixel 9"}
    ).add_to_hass(hass)
    received: list[dict] = []

    async def _notify(call: ServiceCall) -> None:
        received.append(dict(call.data))
        actions = call.data.get("data", {}).get("actions")
        if tap and actions:
            # A stale tap on some other request first: must be ignored.
            hass.bus.async_fire(EVENT_NOTIFICATION_ACTION, {"action": "ZEROCLAW_ALLOW_other"})
            chosen = actions[0] if tap == "allow" else actions[1]
            hass.bus.async_fire(EVENT_NOTIFICATION_ACTION, {"action": chosen["action"]})

    hass.services.async_register("notify", "mobile_app_pixel_9", _notify)
    return received


async def _ask(hass, entry, user_id=USER, timeout_secs=120):
    return await async_ask_on_phone(
        hass, entry, user_id, TOOL, "name: Cancello", timeout_secs
    )


async def test_allow_tap_approves(hass: HomeAssistant, entry):
    received = _phone(hass, "allow")
    assert await _ask(hass, entry) is True

    question = received[0]
    assert "HassOpenCover" in question["message"]
    assert "Cancello" in question["message"]


async def test_deny_tap_refuses(hass: HomeAssistant, entry):
    _phone(hass, "deny")
    assert await _ask(hass, entry) is False


async def test_no_answer_refuses(hass: HomeAssistant, entry):
    _phone(hass, None)
    with (
        patch("custom_components.zeroclaw_conversation.approval.MIN_WAIT_SECONDS", 0.01),
        patch("custom_components.zeroclaw_conversation.approval.MAX_WAIT_SECONDS", 0.01),
    ):
        assert await _ask(hass, entry) is False


async def test_question_is_cleared_afterwards(hass: HomeAssistant, entry):
    received = _phone(hass, "allow")
    await _ask(hass, entry)
    await hass.async_block_till_done()

    assert received[-1]["message"] == "clear_notification"
    assert received[-1]["data"]["tag"] == received[0]["data"]["tag"]


async def test_no_device_means_nobody_could_be_asked(hass: HomeAssistant, entry):
    assert await _ask(hass, entry) is None


async def test_someone_elses_phone_is_not_asked(hass: HomeAssistant, entry):
    received = _phone(hass, "allow", user_id="someone-else")
    assert await _ask(hass, entry) is None
    assert received == []


async def test_falls_back_to_the_last_speaker(hass: HomeAssistant, entry):
    """A voice satellite turn can carry no user; the agent's last speaker
    is who gets asked then."""
    from custom_components.zeroclaw_conversation.const import DATA_LAST_USER_ID

    hass.data.setdefault(DOMAIN, {})[DATA_LAST_USER_ID] = {entry.entry_id: USER}
    _phone(hass, "allow")
    assert await _ask(hass, entry, user_id=None) is True
