"""Personality-file rendering.

`build_personality_files` runs `.format()` over `TOOLS.md`, whose body is
full of literal JSON braces — a single unescaped `{` there raises at
agent-creation time, in the middle of the config flow, on a code path
nobody exercises until a user creates an agent. These tests are the cheap
guard for that, plus a check that the instructions the rest of the
project depends on actually survive into the rendered files.
"""

import pytest

from custom_components.zeroclaw_conversation.personality import (
    build_personality_files,
    default_system_prompt,
    sanitize_agent_alias,
)

# IDENTITY.md is customized by rewriting ZeroClaw's own
# "- **Label:** value" lines in place, so the fixture has to carry them
# for that path to be exercised at all — a bare heading would silently
# pass through unchanged and prove nothing.
IDENTITY_TEMPLATE = "\n".join(
    [
        "# IDENTITY.md",
        "",
        "- **Name:** Assistant",
        "- **vibe:** helpful",
        "- **Emoji:** 🤖",
    ]
)

TEMPLATES = [
    {"filename": "SOUL.md", "content": "# base soul"},
    {"filename": "IDENTITY.md", "content": IDENTITY_TEMPLATE},
    {"filename": "USER.md", "content": "# base user"},
    {"filename": "TOOLS.md", "content": "# base tools"},
    {"filename": "AGENTS.md", "content": "# untouched"},
]

WEBHOOK_URL = "http://homeassistant:8123/api/webhook/abc123"


def _render(language, url=WEBHOOK_URL):
    files = build_personality_files(TEMPLATES, "Mario", language=language, notify_webhook_url=url)
    return {f["filename"]: f["content"] for f in files}


# `en` and `it` are hand-translated; anything else falls back to English
# content with a "respond in <language>" directive, so one of those is
# worth covering too.
@pytest.mark.parametrize("language", ["en", "it", "de"])
def test_renders_without_format_errors(language):
    out = _render(language)
    assert set(out) == {f["filename"] for f in TEMPLATES}


@pytest.mark.parametrize("language", ["en", "it", "de"])
def test_tools_md_carries_the_literal_webhook_url(language):
    """The `{url}` placeholder is the only thing `.format()` should
    substitute — the surrounding `{{...}}` JSON examples must survive as
    literal braces."""
    tools = _render(language)["TOOLS.md"]
    assert WEBHOOK_URL in tools
    assert '"type": "create_watch"' in tools
    assert '"type": "notify"' in tools


@pytest.mark.parametrize("language", ["en", "it", "de"])
def test_tools_md_keeps_the_entity_id_resolution_instruction(language):
    """Added after a watch was armed against a guessed-at entity name;
    see docs/DECISIONS.md."""
    assert "entity_id" in _render(language)["TOOLS.md"]


@pytest.mark.parametrize("language", ["en", "it", "de"])
def test_soul_md_keeps_the_safety_critical_tool_routing(language):
    """Covers must never be driven through the generic on/off tools —
    those skip the confirmation the dedicated ones trigger. This is the
    one instruction in SOUL.md that is genuinely safety-relevant, so it
    gets pinned explicitly."""
    soul = _render(language)["SOUL.md"]
    assert "HassOpenCover" in soul
    assert "HassCloseCover" in soul


def test_untouched_templates_pass_through_unchanged():
    assert _render("en")["AGENTS.md"] == "# untouched"


def test_no_notify_url_means_no_tools_addition():
    """Leaving Home Assistant's URL blank switches the whole notify/watch
    feature off; TOOLS.md must then be left exactly as ZeroClaw shipped
    it rather than gaining a section pointing at a URL that doesn't
    exist."""
    out = build_personality_files(TEMPLATES, "Mario", language="en", notify_webhook_url=None)
    tools = next(f["content"] for f in out if f["filename"] == "TOOLS.md")
    assert tools == "# base tools"


def test_identity_uses_the_display_name():
    identity = _render("en")["IDENTITY.md"]
    assert "- **Name:** Mario" in identity
    assert "Assistant" not in identity


def test_identity_label_match_is_case_insensitive():
    """The template's label casing is ZeroClaw's to choose, not ours —
    `- **vibe:**` must be rewritten just like `- **Name:**` is."""
    identity = _render("it")["IDENTITY.md"]
    assert "helpful" not in identity
    assert "**Vibe:**" in identity


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mario", "mario"),
        ("Casa Vasirani", "casa_vasirani"),
        ("  spaced  out  ", "spaced_out"),
        ("weird-hyphens", "weird_hyphens"),
    ],
)
def test_sanitize_agent_alias(raw, expected):
    """ZeroClaw rejects aliases that aren't lowercase ASCII with single
    underscores — confirmed against its own validator's rejection
    messages, see docs/DECISIONS.md."""
    assert sanitize_agent_alias(raw) == expected


def test_default_system_prompt_is_language_aware():
    assert default_system_prompt("Mario", language="it") != default_system_prompt(
        "Mario", language="en"
    )
