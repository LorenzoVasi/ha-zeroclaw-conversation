"""The inbound webhook an agent uses to notify and to arm watches.

This is where the watch bugs in docs/DECISIONS.md actually lived: a watch
armed on an entity that doesn't exist, a `to_state` in the household's
language that could never match, a notification that was really the raw
instruction meant for the agent. All of those are silent failures at
creation time — the request succeeds and the watch simply never fires —
so they're exactly the kind of thing that needs a test rather than a
careful reading.
"""

from unittest.mock import patch

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.zeroclaw_conversation.const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HA_URL,
    CONF_HOST,
    CONF_WEBHOOK_ID,
    DOMAIN,
)

WEBHOOK_ID = "a" * 32
WATCHED = "light.camera_lorenzo"


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured entry with the notify/watch webhook registered."""
    await async_setup_component(hass, "webhook", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ZeroClaw (mario)",
        data={
            CONF_HOST: "http://local-zeroclaw:42617",
            CONF_API_TOKEN: "tok",
            CONF_AGENT: "mario",
            CONF_HA_URL: "http://homeassistant:8123",
            CONF_WEBHOOK_ID: WEBHOOK_ID,
        },
    )
    entry.add_to_hass(hass)
    # The platforms make live HTTP calls on setup; the webhook and the
    # WatchManager are what these tests are about, so stub the rest out.
    with patch(
        "custom_components.zeroclaw_conversation.PLATFORMS", []
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.fixture
async def post(hass: HomeAssistant, entry, hass_client_no_auth: ClientSessionGenerator):
    """POST a JSON body to this entry's webhook."""
    client = await hass_client_no_auth()

    async def _post(payload: dict):
        return await client.post(f"/api/webhook/{WEBHOOK_ID}", json=payload)

    return _post


async def _watches(hass: HomeAssistant, entry) -> list:
    from custom_components.zeroclaw_conversation.const import DATA_WATCH_MANAGER

    return hass.data[DOMAIN][DATA_WATCH_MANAGER].list_for_entry(entry.entry_id)


async def test_create_watch_rejects_an_entity_that_does_not_exist(post):
    """The one structural check that can be made at creation time — an
    agent that guessed at an entity name gets told immediately instead of
    arming a watch that could never fire."""
    resp = await post(
        {
            "type": "create_watch",
            "entity_id": "light.does_not_exist",
            "to_state": "off",
            "message": "do something",
        }
    )
    assert resp.status == 400
    assert "no such entity" in (await resp.json())["error"]


async def test_create_watch_normalizes_a_non_english_state(
    hass: HomeAssistant, post, entry
):
    """"spento" is what the household says; "off" is the only thing Home
    Assistant will ever compare equal to."""
    hass.states.async_set(WATCHED, "on")
    resp = await post(
        {
            "type": "create_watch",
            "entity_id": WATCHED,
            "to_state": "spento",
            "message": "accendi light.luci_scale",
        }
    )
    assert resp.status == 200
    assert (await resp.json())["to_state"] == "off"

    watch = (await _watches(hass, entry))[0]
    assert watch.to_state == "off"


async def test_watch_defaults_to_firing_once(hass: HomeAssistant, post, entry):
    """"tell me when X happens" means once. Only an explicit "every time"
    should arm something permanent."""
    hass.states.async_set(WATCHED, "on")
    await post(
        {
            "type": "create_watch",
            "entity_id": WATCHED,
            "to_state": "off",
            "message": "do something",
        }
    )
    assert (await _watches(hass, entry))[0].recurring is False


async def test_notification_falls_back_to_the_message(
    hass: HomeAssistant, post, entry
):
    """Without an explicit `notification`, the household would read the
    agent's raw instruction verbatim — the fallback is deliberate, but it
    has to actually be the fallback and not a blank."""
    hass.states.async_set(WATCHED, "on")
    await post(
        {
            "type": "create_watch",
            "entity_id": WATCHED,
            "to_state": "off",
            "message": "accendi light.luci_scale",
        }
    )
    watch = (await _watches(hass, entry))[0]
    assert watch.notification is None
    assert watch.message == "accendi light.luci_scale"


async def test_notification_is_kept_separate_when_given(
    hass: HomeAssistant, post, entry
):
    hass.states.async_set(WATCHED, "on")
    await post(
        {
            "type": "create_watch",
            "entity_id": WATCHED,
            "to_state": "off",
            "message": "accendi light.luci_scale",
            "notification": "Si sono spente le luci in camera di Lorenzo.",
        }
    )
    watch = (await _watches(hass, entry))[0]
    assert watch.notification == "Si sono spente le luci in camera di Lorenzo."
    assert watch.message == "accendi light.luci_scale"


async def test_notify_creates_a_persistent_notification(hass: HomeAssistant, post):
    resp = await post({"type": "notify", "message": "La lavatrice ha finito."})
    assert resp.status == 200

    notifications = hass.data.get(persistent_notification.DOMAIN, {})
    assert any(
        n["message"] == "La lavatrice ha finito." for n in notifications.values()
    )


async def test_notify_rejects_an_empty_message(post):
    assert (await post({"type": "notify", "message": ""})).status == 400


async def test_unknown_type_is_rejected(post):
    assert (await post({"type": "definitely_not_a_thing"})).status == 400


async def test_list_and_cancel_round_trip(hass: HomeAssistant, post, entry):
    hass.states.async_set(WATCHED, "on")
    created = await (
        await post(
            {
                "type": "create_watch",
                "entity_id": WATCHED,
                "to_state": "off",
                "message": "do something",
            }
        )
    ).json()
    watch_id = created["watch_id"]

    listed = await (await post({"type": "list_watches"})).json()
    assert [w["watch_id"] for w in listed["watches"]] == [watch_id]

    assert (await post({"type": "cancel_watch", "watch_id": watch_id})).status == 200
    assert await _watches(hass, entry) == []


async def test_cancelling_an_unknown_watch_is_a_404(post):
    resp = await post({"type": "cancel_watch", "watch_id": "nope"})
    assert resp.status == 404
