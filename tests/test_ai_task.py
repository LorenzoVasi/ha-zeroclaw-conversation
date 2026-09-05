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
    _to_openapi_converter,
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
    conversion cannot serialize — hence `llm.selector_serializer`. Without
    it this raises rather than producing a schema."""
    schema = vol.Schema(
        {vol.Required("level"): NumberSelector(NumberSelectorConfig(min=0, max=10))}
    )
    parsed = json.loads(_schema_for_prompt(schema, _FakeChatLog()))
    assert "level" in parsed["properties"]


# --- the converter lookup itself -------------------------------------
#
# Home Assistant renamed this dependency (`voluptuous_openapi` →
# `probatio`). 0.2.1 imported the old name at module scope, which raised
# on a newer instance and took the whole ai_task platform down with it —
# the entity simply stopped existing. These pin the behaviour on every
# combination, including "neither is installed", which no pinned test
# harness can reproduce on its own.


def _hide_modules(monkeypatch, *names: str) -> None:
    """Make `import <name>` raise ImportError, as on an instance that
    ships the other one."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in names:
            raise ImportError(f"simulated: no module named {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_converter_found_under_the_current_name(monkeypatch):
    """Current core: `probatio`. The harness doesn't ship it, so it's
    injected — the point is the lookup order, not the library."""
    import sys
    import types

    sentinel = object()
    module = types.ModuleType("probatio")
    module.to_openapi = sentinel
    monkeypatch.setitem(sys.modules, "probatio", module)

    assert _to_openapi_converter() is sentinel


def test_converter_falls_back_to_the_older_name(monkeypatch):
    """Core 2026.2.3 and earlier: `voluptuous_openapi`."""
    _hide_modules(monkeypatch, "probatio")
    converter = _to_openapi_converter()
    assert converter is not None
    assert converter.__module__.startswith("voluptuous_openapi")


def test_neither_package_available_returns_none(monkeypatch):
    _hide_modules(monkeypatch, "probatio", "voluptuous_openapi")
    assert _to_openapi_converter() is None


def test_prompt_still_built_when_no_converter_exists(monkeypatch):
    """The regression that mattered: with no converter, describing the
    schema must degrade, not raise — a worse prompt beats a platform that
    won't load."""
    _hide_modules(monkeypatch, "probatio", "voluptuous_openapi")
    schema = vol.Schema({vol.Required("title"): str})

    rendered = _schema_for_prompt(schema, _FakeChatLog())

    assert isinstance(rendered, str)
    assert "title" in rendered


def test_a_converter_that_raises_does_not_fail_the_task(monkeypatch):
    """Same guarantee for a converter that exists but chokes on the
    schema."""
    import sys
    import types

    module = types.ModuleType("probatio")

    def _explode(*_args, **_kwargs):
        raise TypeError("cannot serialize that")

    module.to_openapi = _explode
    monkeypatch.setitem(sys.modules, "probatio", module)

    rendered = _schema_for_prompt(vol.Schema({vol.Required("a"): str}), _FakeChatLog())
    assert isinstance(rendered, str)


def test_module_imports_even_with_neither_package_installed(monkeypatch):
    """The actual 0.2.1 failure, reproduced.

    The old code imported `voluptuous_openapi` at module scope. On an
    instance that ships `probatio` instead, that raised at import time,
    so the `ai_task` platform never set up and Home Assistant reported
    `AI Task entity ... not found` — the feature disappeared rather than
    degrading. Re-importing the module with both names hidden is the
    closest thing to that instance this pinned harness can produce.
    """
    import importlib
    import sys

    _hide_modules(monkeypatch, "probatio", "voluptuous_openapi")
    name = "custom_components.zeroclaw_conversation.ai_task"
    monkeypatch.delitem(sys.modules, name, raising=False)

    module = importlib.import_module(name)

    # And it is still usable, not merely importable.
    assert module._to_openapi_converter() is None
    assert isinstance(
        module._schema_for_prompt(vol.Schema({vol.Required("a"): str}), _FakeChatLog()),
        str,
    )
