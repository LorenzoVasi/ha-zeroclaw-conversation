"""Precedence rules for the bearer token, editable from the options flow.

Same trap as `test_webhook_secret.py`: falling back to the setup-time
value when the operator *clears* the field would keep sending a token the
add-on no longer accepts. See docs/DECISIONS.md, "API token in the
options flow".
"""

from dataclasses import dataclass, field

from custom_components.zeroclaw_conversation.api import api_token_for
from custom_components.zeroclaw_conversation.const import CONF_API_TOKEN


@dataclass
class FakeEntry:
    """Just the two mappings `api_token_for` reads."""

    data: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)


def test_value_captured_at_setup_is_used():
    assert api_token_for(FakeEntry(data={CONF_API_TOKEN: "abc"})) == "abc"


def test_no_token_anywhere_is_empty():
    assert api_token_for(FakeEntry(data={"host": "http://x"})) == ""


def test_options_override_setup_value():
    """The case this exists for: set up with pairing off, token added later."""
    entry = FakeEntry(data={CONF_API_TOKEN: ""}, options={CONF_API_TOKEN: "new"})
    assert api_token_for(entry) == "new"


def test_clearing_via_options_actually_clears_it():
    entry = FakeEntry(data={CONF_API_TOKEN: "old"}, options={CONF_API_TOKEN: ""})
    assert api_token_for(entry) == ""


def test_unrelated_options_do_not_shadow_the_setup_value():
    """An entry whose options only ever held the webhook secret."""
    entry = FakeEntry(data={CONF_API_TOKEN: "abc"}, options={"webhook_secret": "s"})
    assert api_token_for(entry) == "abc"
