"""End-to-end tests driven through real Home Assistant.

Everything in the path is real except the model: Home Assistant core (the
same harness the rest of the suite uses), this integration, and a real
ZeroClaw daemon built from the `addon-zeroclaw` repository's own
Dockerfile — talking to `fake_llm.py` instead of a paid, non-deterministic
provider. The gateway's own contract is pinned separately in
`test_gateway_contract.py`; this file is about what Home Assistant does
with it. See `tests/e2e/README.md` for how the stack is brought up.

Skipped unless `ZEROCLAW_E2E_HOST` points at a running gateway, so the
ordinary `pytest` run stays hermetic and fast.

Why this exists: this project's bug history is assumptions that only a
running instance disproved — a header that was never read, an endpoint
with no session concept, a prompt carrying a Python repr where a schema
was meant, an import that made a whole platform vanish. Unit tests saw
none of them.
"""

from __future__ import annotations

import os
import uuid

import aiohttp
import pytest
import voluptuous as vol
from homeassistant.components import ai_task, conversation, persistent_notification
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeroclaw_conversation.api import (
    async_delete_session,
    async_fetch_agents,
    async_fetch_configured_providers,
    async_list_sessions,
)
from custom_components.zeroclaw_conversation.const import (
    CONF_AGENT,
    CONF_API_TOKEN,
    CONF_HA_URL,
    CONF_HOST,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_SECRET,
    DATA_WATCH_MANAGER,
    DOMAIN,
)

HOST = os.environ.get("ZEROCLAW_E2E_HOST", "")
SECURED_HOST = os.environ.get("ZEROCLAW_E2E_SECURED_HOST", "")
FAKE_LLM = os.environ.get("ZEROCLAW_E2E_FAKE_LLM", "")
TOKEN = os.environ.get("ZEROCLAW_E2E_TOKEN", "e2e-token")
SECRET = os.environ.get("ZEROCLAW_E2E_WEBHOOK_SECRET", "e2e-webhook-secret")
AGENT = os.environ.get("ZEROCLAW_E2E_AGENT", "default")

WEBHOOK_ID = "e" * 32
WATCHED = "light.camera_lorenzo"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not HOST, reason="ZEROCLAW_E2E_HOST not set; see tests/e2e/README.md"
    ),
]

needs_secured = pytest.mark.skipif(
    not SECURED_HOST, reason="ZEROCLAW_E2E_SECURED_HOST not set"
)
needs_recorder = pytest.mark.skipif(
    not FAKE_LLM, reason="ZEROCLAW_E2E_FAKE_LLM not set"
)


# --- helpers ----------------------------------------------------------


async def _setup_entry(
    hass: HomeAssistant,
    *,
    host: str = "",
    webhook_secret: str = "",
    ha_url: str = "",
    webhook_id: str = "",
) -> MockConfigEntry:
    """Configure the integration against a live gateway."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"ZeroClaw ({AGENT})",
        data={
            CONF_HOST: host or HOST,
            CONF_API_TOKEN: TOKEN,
            CONF_WEBHOOK_SECRET: webhook_secret,
            CONF_AGENT: AGENT,
            CONF_HA_URL: ha_url,
            CONF_WEBHOOK_ID: webhook_id,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, domain: str) -> str:
    """Look the entity up rather than constructing its id — the id is
    derived from the entry title, so hardcoding it makes a test fail for
    reasons unrelated to what it checks."""
    registry = er.async_get(hass)
    matches = [
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == domain
    ]
    assert len(matches) == 1, matches
    return matches[0]


async def _converse(hass: HomeAssistant, agent_id: str, text: str, **kwargs):
    return await conversation.async_converse(
        hass, text, context=Context(), agent_id=agent_id, **kwargs
    )


async def _generate(hass: HomeAssistant, entry, instructions: str, structure=None):
    return await ai_task.async_generate_data(
        hass,
        task_name="e2e",
        entity_id=_entity_id(hass, entry, "ai_task"),
        instructions=instructions,
        structure=structure,
    )


async def _recorded_prompts() -> str:
    """Everything the fake model has been asked since the last reset."""
    async with aiohttp.ClientSession() as session, session.get(
        f"{FAKE_LLM}/_requests", timeout=aiohttp.ClientTimeout(total=15)
    ) as resp:
        payload = await resp.json()
    return "\n".join(
        str(message.get("content", ""))
        for request in payload["requests"]
        for message in request["messages"]
    )


async def _reset_recorder() -> None:
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{FAKE_LLM}/_reset", timeout=aiohttp.ClientTimeout(total=15)
        )


# --- Assist ------------------------------------------------------------


async def test_assist_turn(hass: HomeAssistant, real_client_session):
    """A real Assist conversation, handled by this integration's
    conversation entity, answered by a real ZeroClaw."""
    entry = await _setup_entry(hass)
    result = await _converse(
        hass, _entity_id(hass, entry, "conversation"), "ping", conversation_id=None
    )
    assert result.response.speech["plain"]["speech"] == "pong"


async def test_assist_reply_reflects_the_prompt(
    hass: HomeAssistant, real_client_session
):
    """A second phrase, so a pass can't come from a hardcoded "pong"
    reached by some other route."""
    entry = await _setup_entry(hass)
    result = await _converse(
        hass,
        _entity_id(hass, entry, "conversation"),
        "la lavatrice ha finito?",
        conversation_id=None,
    )
    assert "asciugatrice" in result.response.speech["plain"]["speech"]


async def test_assist_keeps_one_conversation_across_turns(
    hass: HomeAssistant, real_client_session
):
    """The same `conversation_id` must survive a turn — the whole reason
    Assist was moved off the stateless `/webhook`."""
    entry = await _setup_entry(hass)
    agent_id = _entity_id(hass, entry, "conversation")

    first = await _converse(hass, agent_id, "ping", conversation_id=None)
    conversation_id = first.conversation_id
    assert conversation_id

    second = await _converse(hass, agent_id, "ping", conversation_id=conversation_id)
    assert second.conversation_id == conversation_id


@needs_recorder
async def test_speaker_name_reaches_the_model(
    hass: HomeAssistant, real_client_session
):
    """The greeting-by-name feature, proven at the far end.

    Asserting on the reply alone would prove nothing — the model could
    greet by name for its own reasons. This checks the name actually
    travelled in the prompt, which is the part this integration owns.
    """
    await _reset_recorder()
    user_id = uuid.uuid4().hex
    hass.states.async_set(
        "person.lorenzo", "home", {"user_id": user_id, "friendly_name": "Lorenzo"}
    )

    entry = await _setup_entry(hass)
    await conversation.async_converse(
        hass,
        "ciao",
        conversation_id=None,  # a brand-new conversation is what triggers it
        context=Context(user_id=user_id),
        agent_id=_entity_id(hass, entry, "conversation"),
    )

    assert "Lorenzo" in await _recorded_prompts()


@needs_recorder
async def test_no_speaker_context_when_the_user_is_unknown(
    hass: HomeAssistant, real_client_session
):
    """No linked person means no name — and specifically no guessing."""
    await _reset_recorder()
    entry = await _setup_entry(hass)
    await conversation.async_converse(
        hass,
        "ciao",
        conversation_id=None,
        context=Context(user_id=uuid.uuid4().hex),  # nobody owns this id
        agent_id=_entity_id(hass, entry, "conversation"),
    )

    assert "Home Assistant context" not in await _recorded_prompts()


# --- AI Task -----------------------------------------------------------


async def test_ai_task_unstructured(hass: HomeAssistant, real_client_session):
    entry = await _setup_entry(hass)
    result = await _generate(hass, entry, "ping")
    assert result.data == "pong"


async def test_ai_task_structured_survives_a_chatty_reply(
    hass: HomeAssistant, real_client_session
):
    """The failure Home Assistant's AI suggestions hit on a real instance:
    correct JSON wrapped in prose and a markdown code fence."""
    entry = await _setup_entry(hass)
    result = await _generate(
        hass,
        entry,
        "This is an automated data request: suggest one thing to do.",
        vol.Schema({vol.Required("suggestions"): [str]}),
    )
    assert result.data == {"suggestions": ["Spegni le luci del corridoio"]}


async def test_ai_task_structured_with_a_clean_reply(
    hass: HomeAssistant, real_client_session
):
    """The easy path, kept separate so a regression in fence-stripping
    can't hide behind it."""
    entry = await _setup_entry(hass)
    result = await _generate(
        hass,
        entry,
        "richiesta pulita",
        vol.Schema({vol.Required("suggestions"): [str]}),
    )
    assert result.data == {"suggestions": ["Chiudi il garage"]}


async def test_ai_task_structured_failure_is_reported_not_invented(
    hass: HomeAssistant, real_client_session
):
    """A reply with no JSON at all must fail loudly, and the error must
    quote what came back — otherwise the next occurrence is undiagnosable
    from the notification alone."""
    entry = await _setup_entry(hass)
    with pytest.raises(HomeAssistantError) as err:
        await _generate(
            hass,
            entry,
            "richiesta impossibile",
            vol.Schema({vol.Required("suggestions"): [str]}),
        )
    assert "non ho capito" in str(err.value).lower()


@needs_recorder
async def test_ai_task_sends_a_real_json_schema(
    hass: HomeAssistant, real_client_session
):
    """The 0.2.1 bug that no assertion on the *reply* could have caught:
    the prompt described the wanted shape as a `vol.Schema` Python repr,
    so the model was answering a badly-phrased question correctly."""
    await _reset_recorder()
    entry = await _setup_entry(hass)
    await _generate(
        hass,
        entry,
        "richiesta pulita",
        vol.Schema({vol.Required("suggestions"): [str]}),
    )

    prompts = await _recorded_prompts()
    assert '"type": "object"' in prompts or '"type":"object"' in prompts
    assert "suggestions" in prompts
    # The tell-tales of a leaked Python object.
    assert "vol.Schema" not in prompts
    assert "Required(" not in prompts


# --- the agent-facing webhook and watches ------------------------------


async def test_notify_agent_service_reaches_the_gateway(
    hass: HomeAssistant, real_client_session
):
    """HA → agent: an automation's action, telling the agent something
    happened. A 401 or a gateway error would raise."""
    entry = await _setup_entry(hass)
    await hass.services.async_call(
        DOMAIN,
        "notify_agent",
        {
            "entity_id": _entity_id(hass, entry, "conversation"),
            "message": "automazione",
        },
        blocking=True,
    )


async def test_watch_fires_and_notifies_the_household(
    hass: HomeAssistant, real_client_session, hass_client_no_auth
):
    """The full event-driven path: the agent arms a watch through the
    inbound webhook, an external device changes the entity, the watch
    fires, and the household is notified directly — not left to the agent
    deciding to relay it."""
    await async_setup_component(hass, "webhook", {})
    entry = await _setup_entry(
        hass, ha_url="http://homeassistant:8123", webhook_id=WEBHOOK_ID
    )
    hass.states.async_set(WATCHED, "on")

    client = await hass_client_no_auth()
    resp = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        json={
            "type": "create_watch",
            "entity_id": WATCHED,
            "to_state": "spento",  # deliberately not English
            "message": "accendi light.luci_scale",
            "notification": "Si sono spente le luci in camera di Lorenzo.",
        },
    )
    assert resp.status == 200
    assert (await resp.json())["to_state"] == "off"  # normalized on the way in

    manager = hass.data[DOMAIN][DATA_WATCH_MANAGER]
    assert len(manager.list_for_entry(entry.entry_id)) == 1

    # An external change: nobody caused it through Home Assistant.
    hass.states.async_set(WATCHED, "off", context=Context(user_id=None))
    await hass.async_block_till_done()

    notifications = hass.data.get(persistent_notification.DOMAIN, {})
    assert any(
        "Si sono spente le luci" in n["message"] for n in notifications.values()
    ), notifications

    # One-shot by default: it disarms itself.
    assert manager.list_for_entry(entry.entry_id) == []


async def test_watch_ignores_a_change_home_assistant_itself_made(
    hass: HomeAssistant, real_client_session, hass_client_no_auth
):
    """Turning something off from the dashboard must not notify — the
    household already knows, they just did it."""
    await async_setup_component(hass, "webhook", {})
    entry = await _setup_entry(
        hass, ha_url="http://homeassistant:8123", webhook_id=WEBHOOK_ID
    )
    hass.states.async_set(WATCHED, "on")

    client = await hass_client_no_auth()
    await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        json={
            "type": "create_watch",
            "entity_id": WATCHED,
            "to_state": "off",
            "message": "irrelevant",
        },
    )

    # A user-attributed change is what a dashboard toggle looks like.
    hass.states.async_set(WATCHED, "off", context=Context(user_id=uuid.uuid4().hex))
    await hass.async_block_till_done()

    assert len(hass.data[DOMAIN][DATA_WATCH_MANAGER].list_for_entry(entry.entry_id)) == 1


# --- api.py's client functions, against the real gateway ---------------


async def test_api_client_reads_agents_and_providers(
    hass: HomeAssistant, real_client_session
):
    """`config_flow.py` builds its dropdowns from these."""
    agents = await async_fetch_agents(hass, HOST, TOKEN)
    assert AGENT in agents

    providers = await async_fetch_configured_providers(hass, HOST, TOKEN)
    assert "custom.fake" in providers


async def test_api_client_lists_and_deletes_sessions(
    hass: HomeAssistant, real_client_session
):
    """What `session_cleanup.py` runs on a timer."""
    entry = await _setup_entry(hass)
    result = await _converse(
        hass, _entity_id(hass, entry, "conversation"), "ping", conversation_id=None
    )
    conversation_id = result.conversation_id

    sessions = await async_list_sessions(hass, HOST, TOKEN)
    mine = [s for s in sessions if conversation_id in s.get("session_id", "")]
    assert mine, "the session this test just created is not listed"

    await async_delete_session(hass, HOST, TOKEN, mine[0]["session_key"])
    remaining = await async_list_sessions(hass, HOST, TOKEN)
    assert not [s for s in remaining if conversation_id in s.get("session_id", "")]

    # Idempotent: deleting a gone session is not an error.
    await async_delete_session(hass, HOST, TOKEN, mine[0]["session_key"])


# --- the webhook secret, end to end ------------------------------------


@needs_secured
async def test_matching_webhook_secret_works(hass: HomeAssistant, real_client_session):
    entry = await _setup_entry(hass, host=SECURED_HOST, webhook_secret=SECRET)
    result = await _generate(hass, entry, "ping")
    assert result.data == "pong"


@needs_secured
async def test_missing_webhook_secret_fails_clearly(
    hass: HomeAssistant, real_client_session
):
    """The mismatch an operator creates by setting the secret on the
    add-on and not here. It must surface as an error, not silence."""
    entry = await _setup_entry(hass, host=SECURED_HOST, webhook_secret="")
    with pytest.raises(HomeAssistantError):
        await _generate(hass, entry, "ping")


@needs_secured
async def test_assist_is_unaffected_by_a_missing_webhook_secret(
    hass: HomeAssistant, real_client_session
):
    """`gateway.webhook_secret` covers `/webhook` only. Assist goes over
    `/ws/chat`, so it keeps working even when the secret is absent — the
    difference between "AI Tasks broke" and "the assistant is down"."""
    entry = await _setup_entry(hass, host=SECURED_HOST, webhook_secret="")
    result = await _converse(
        hass, _entity_id(hass, entry, "conversation"), "ping", conversation_id=None
    )
    assert result.response.speech["plain"]["speech"] == "pong"
