"""`_normalize_state` — the safeguard for a real, silent watch failure.

A watch created with `to_state: "spento"` never fired, ever: Home
Assistant's entity states are a fixed English vocabulary, so the stored
string was compared byte-for-byte against `"off"` and never matched, with
no error at creation time. See docs/DECISIONS.md, "Fix watches never
firing". These cases pin the alias table down so the fix can't be
regressed by a later edit.
"""

import pytest

from custom_components.zeroclaw_conversation.webhook import _normalize_state


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The exact bug that was reported.
        ("spento", "off"),
        ("spenta", "off"),
        ("spente", "off"),
        ("acceso", "on"),
        ("accese", "on"),
        # Covers, which are the security-sensitive domain.
        ("aperto", "open"),
        ("chiusa", "closed"),
        # Locks.
        ("bloccato", "locked"),
        ("sbloccata", "unlocked"),
        # Presence.
        ("casa", "home"),
        ("fuori", "not_home"),
    ],
)
def test_italian_state_words_map_to_home_assistant_states(raw, expected):
    assert _normalize_state(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", "off"),
        ("ON", "on"),
        ("  Off  ", "off"),
        ("not home", "not_home"),
    ],
)
def test_already_english_states_are_normalized_not_mangled(raw, expected):
    """Casing/whitespace is normalized; the value itself passes through."""
    assert _normalize_state(raw) == expected


def test_unknown_values_pass_through_lowercased():
    """An unrecognized word is still lowercased/underscored rather than
    dropped — HA's own state strings are conventionally lowercase
    snake_case, so that guess is more often right than preserving
    whatever the caller sent."""
    assert _normalize_state("Playing") == "playing"


def test_numeric_sensor_values_are_untouched():
    """A numeric threshold has no case to normalize and must survive."""
    assert _normalize_state("21.5") == "21.5"
