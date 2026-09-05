"""Structured `ai_task.generate_data` output.

Reported from a real instance (2026-09-05): Home Assistant's built-in AI
suggestions failed with `ZeroClaw did not return valid JSON for this
structured task: unexpected character: line 1 column 1 (char 0)`.

Two separate causes, both covered here — the reply not being bare JSON,
and the *request* describing the wanted shape as a `vol.Schema` repr
rather than as JSON Schema. See docs/DECISIONS.md.
"""

import json

import pytest
import voluptuous as vol
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig

from custom_components.zeroclaw_conversation.ai_task import (
    _parse_structured_reply,
    _schema_for_prompt,
)


class _FakeChatLog:
    """Only the attribute `_schema_for_prompt` touches."""

    llm_api = None


EXPECTED = {"suggestions": ["turn off the hall light"], "confidence": 0.8}
PAYLOAD = json.dumps(EXPECTED)


def test_clean_json_still_works():
    assert _parse_structured_reply(PAYLOAD) == EXPECTED


def test_surrounding_whitespace():
    assert _parse_structured_reply(f"\n\n  {PAYLOAD}  \n") == EXPECTED


def test_utf8_bom():
    """A BOM reads as "unexpected character" at position 0 — the exact
    wording of the reported failure."""
    assert _parse_structured_reply("﻿" + PAYLOAD) == EXPECTED


@pytest.mark.parametrize("fence", ["```json", "```JSON", "```"])
def test_markdown_code_fences(fence):
    assert _parse_structured_reply(f"{fence}\n{PAYLOAD}\n```") == EXPECTED


def test_prose_before():
    assert _parse_structured_reply(f"Ecco il JSON richiesto:\n{PAYLOAD}") == EXPECTED


def test_prose_after():
    assert _parse_structured_reply(f"{PAYLOAD}\n\nSpero sia utile!") == EXPECTED


def test_prose_both_sides():
    """What a warm, chatty household agent actually tends to produce."""
    reply = f"Certo! Ecco i suggerimenti:\n\n{PAYLOAD}\n\nFammi sapere."
    assert _parse_structured_reply(reply) == EXPECTED


def test_fenced_and_prose_together():
    reply = f"Ecco:\n```json\n{PAYLOAD}\n```\nTutto qui."
    assert _parse_structured_reply(reply) == EXPECTED


def test_top_level_array():
    assert _parse_structured_reply('Ecco:\n["a", "b"]\nfine') == ["a", "b"]


def test_pure_prose_still_fails_loudly():
    """Tolerating mess must not become inventing data — a reply with no
    JSON in it at all has to raise, not return something plausible."""
    with pytest.raises(ValueError):
        _parse_structured_reply("Non ho capito la richiesta, puoi ripetere?")


def test_empty_reply_fails_loudly():
    with pytest.raises(ValueError):
        _parse_structured_reply("")


def test_schema_is_rendered_as_json_schema_not_a_python_repr():
    """The bug that made this hard for the model in the first place: a
    `vol.Schema` interpolated into a prompt is a Python repr."""
    schema = vol.Schema(
        {vol.Required("title"): str, vol.Optional("count"): int}
    )
    rendered = _schema_for_prompt(schema, _FakeChatLog())

    parsed = json.loads(rendered)
    assert parsed["type"] == "object"
    assert parsed["properties"]["title"]["type"] == "string"
    assert parsed["properties"]["count"]["type"] == "integer"
    assert parsed["required"] == ["title"]
    assert "vol.Schema" not in rendered and "Required(" not in rendered


def test_schema_with_a_selector_is_serialized():
    """Home Assistant's own structures are full of selectors, which plain
    `convert` cannot serialize — hence `llm.selector_serializer`. Without
    it this raises rather than producing a schema."""
    schema = vol.Schema(
        {vol.Required("level"): NumberSelector(NumberSelectorConfig(min=0, max=10))}
    )
    parsed = json.loads(_schema_for_prompt(schema, _FakeChatLog()))
    assert "level" in parsed["properties"]
