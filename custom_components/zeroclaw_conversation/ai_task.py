"""ZeroClaw AI Task platform.

Implements Home Assistant's `ai_task` building block
(https://www.home-assistant.io/integrations/ai_task/) so automations and
scripts can call `ai_task.generate_data` against ZeroClaw, the same way
they would against any other configured AI provider. Uses the same
`/webhook` call as the `conversation` platform (see api.py).

Verified against the real `AITaskEntity` base class and a real provider
implementation (home-assistant/core: homeassistant/components/ai_task/
entity.py, homeassistant/components/anthropic/ai_task.py) rather than only
the developer docs — the `conversation` platform in this same integration
was bitten once by an abstract-method gap the docs didn't mention (see
docs/DECISIONS.md), so this one was checked against actual core source
first. `AITaskEntity.state` / `.supported_features` have concrete default
implementations in the base class (unlike `ConversationEntity.
supported_languages`), so no equivalent trap here.
"""

from __future__ import annotations

import json
import logging
import re
from json import JSONDecodeError
from typing import Any

import voluptuous as vol
from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.json import json_loads
from voluptuous_openapi import convert

from .api import ZeroClawError, async_call_webhook, webhook_secret_for
from .const import CONF_AGENT, CONF_API_TOKEN, CONF_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)

# ```json … ``` or ``` … ``` around the whole reply. Models emit these
# constantly, and a bare "reply with only JSON" instruction does not
# reliably stop it.
_CODE_FENCE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL
)


def _schema_for_prompt(
    structure: vol.Schema, chat_log: conversation.ChatLog
) -> str:
    """Render `task.structure` as JSON Schema the model can actually read.

    `structure` is a `vol.Schema` object; interpolating it into a prompt
    yields a Python repr, and for the schemas Home Assistant's own
    features build — which are full of selectors — that repr is close to
    meaningless. `voluptuous_openapi.convert` is what core's own LLM
    integrations use for this, with `llm.selector_serializer` for exactly
    the selector case (see `homeassistant/components/anthropic/entity.py`).
    """
    serializer = (
        chat_log.llm_api.custom_serializer
        if chat_log.llm_api
        else llm.selector_serializer
    )
    return json.dumps(convert(structure, custom_serializer=serializer))


def _parse_structured_reply(reply_text: str) -> Any:
    """Parse a model reply that is *supposed* to be nothing but JSON.

    Raises `JSONDecodeError` if nothing usable can be found, so the
    caller still fails loudly rather than inventing data.

    Three shapes are tolerated beyond clean JSON, all of them things
    models do routinely no matter how the prompt is worded — and this
    integration talks to a household agent whose whole personality file
    tells it to be warm and conversational, which makes prose around the
    answer more likely here than for a bare API call:

    1. a UTF-8 BOM (which reads as "unexpected character" at position 0),
    2. a markdown code fence around the whole reply,
    3. a sentence before and/or after the JSON.
    """
    text = reply_text.lstrip("﻿").strip()

    try:
        return json_loads(text)
    except (JSONDecodeError, ValueError):
        pass

    if fenced := _CODE_FENCE.match(text):
        text = fenced.group("body").strip()
        try:
            return json_loads(text)
        except (JSONDecodeError, ValueError):
            pass

    # Last resort: the outermost {...} or [...] in the reply. Deliberately
    # a span rather than a balanced scan — it is enough for "here is your
    # JSON: {...}, hope that helps", and anything it gets wrong still
    # fails the parse below instead of returning something plausible but
    # wrong.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json_loads(text[start : end + 1])
            except (JSONDecodeError, ValueError):
                continue

    # Nothing worked; re-raise from the original text so the error message
    # describes what actually came back.
    return json_loads(text)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ZeroClaw AI Task entity."""
    async_add_entities([ZeroClawAITaskEntity(entry)])


class ZeroClawAITaskEntity(ai_task.AITaskEntity):
    """An AI Task entity that delegates to a ZeroClaw gateway.

    Declares GENERATE_DATA only:
    - No SUPPORT_ATTACHMENTS — `/webhook` takes `{"message": "<text>"}`,
      there's no file-upload mechanism to hand attachments to ZeroClaw
      through this endpoint.
    - No GENERATE_IMAGE — ZeroClaw has no dedicated image-generation
      endpoint exposed through the gateway.
    - Structured (`task.structure`) output is best-effort only: the schema
      is described in the prompt text and the reply is parsed as JSON,
      since `/webhook` has no native JSON-schema-constrained decoding.
      Whether this actually works depends on the underlying model ZeroClaw
      is configured with — some models follow a "reply with only JSON"
      instruction reliably, some don't.
    """

    _attr_has_entity_name = True
    _attr_supported_features = ai_task.AITaskEntityFeature.GENERATE_DATA

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ai_task"
        self._host: str = entry.data[CONF_HOST].rstrip("/")
        self._token: str = entry.data.get(CONF_API_TOKEN) or ""
        self._agent: str = entry.data.get(CONF_AGENT) or ""
        # See the matching comment in conversation.py — disambiguates
        # multiple config entries against the same gateway/different agents.
        self._attr_name = f"ZeroClaw ({self._agent})" if self._agent else "ZeroClaw"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._attr_name,
            "manufacturer": "ZeroClaw Labs",
            "entry_type": "service",
        }

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Run one data-generation task against ZeroClaw."""
        prompt = task.instructions
        if task.structure:
            # The agent on the other end is a household assistant whose
            # own personality files tell it to be warm, to confirm what
            # it did, and to answer in the household's language — all
            # actively unhelpful here. Saying plainly that this is not a
            # conversation does more than repeating "only JSON" louder.
            prompt = (
                f"{prompt}\n\n"
                "This is an automated data request from Home Assistant, not "
                "a conversation. Reply with a single JSON value and nothing "
                "else: no greeting, no explanation, no markdown code fences, "
                "no text before or after it. It must match this JSON schema:\n"
                f"{_schema_for_prompt(task.structure, chat_log)}"
            )

        try:
            reply_text = await async_call_webhook(
                self.hass,
                self._host,
                self._token,
                prompt,
                chat_log.conversation_id,
                agent=self._agent,
                webhook_secret=webhook_secret_for(self._entry),
            )
        except ZeroClawError as err:
            _LOGGER.warning("ZeroClaw AI Task call failed: %s", err)
            raise HomeAssistantError(str(err)) from err

        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id, data=reply_text
            )

        try:
            data = _parse_structured_reply(reply_text)
        except (JSONDecodeError, ValueError) as err:
            _LOGGER.error(
                "ZeroClaw did not return valid JSON for a structured task: %s. Response: %s",
                err,
                reply_text,
            )
            raise HomeAssistantError(
                "ZeroClaw did not return valid JSON for this structured task "
                f"({err}). The agent replied: {reply_text[:200]!r}"
            ) from err

        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )
