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

import logging
from json import JSONDecodeError

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.json import json_loads

from .api import ZeroClawError, async_call_webhook, webhook_secret_for
from .const import CONF_AGENT, CONF_API_TOKEN, CONF_HOST, DOMAIN

_LOGGER = logging.getLogger(__name__)


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
            prompt = (
                f"{prompt}\n\nRespond with ONLY a single JSON object (no other "
                f"text, no markdown code fences) whose fields match this "
                f"schema: {task.structure}"
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
            data = json_loads(reply_text)
        except JSONDecodeError as err:
            _LOGGER.error(
                "ZeroClaw did not return valid JSON for a structured task: %s. Response: %s",
                err,
                reply_text,
            )
            raise HomeAssistantError(
                f"ZeroClaw did not return valid JSON for this structured task: {err}"
            ) from err

        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )
