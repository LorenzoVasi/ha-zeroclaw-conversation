"""Precedence rules for the optional `X-Webhook-Secret` value.

These exist because the obvious implementation is wrong in a way review
did not catch: `entry.options.get(X) or entry.data.get(X)` falls back to
the setup-time value when the operator *clears* the field, so removing a
secret would leave the integration still sending the stale one and
getting 401 on every `/webhook` call while the UI showed an empty field.
See docs/DECISIONS.md, "Optional `X-Webhook-Secret` on `/webhook`".
"""

from dataclasses import dataclass, field

from custom_components.zeroclaw_conversation.api import webhook_secret_for
from custom_components.zeroclaw_conversation.const import CONF_WEBHOOK_SECRET


@dataclass
class FakeEntry:
    """Just the two mappings `webhook_secret_for` reads.

    A real `MockConfigEntry` would work too, but this keeps the test
    honest about how little of a config entry the function is allowed to
    depend on.
    """

    data: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)


def test_entry_predating_the_field_sends_no_header():
    """Every config entry created before this feature existed."""
    assert webhook_secret_for(FakeEntry(data={"host": "http://x"})) is None


def test_value_captured_at_setup_is_used():
    assert webhook_secret_for(FakeEntry(data={CONF_WEBHOOK_SECRET: "abc"})) == "abc"


def test_blank_at_setup_means_not_configured():
    """An empty string is "no secret", not "a secret that is empty"."""
    assert webhook_secret_for(FakeEntry(data={CONF_WEBHOOK_SECRET: ""})) is None


def test_options_override_setup_value():
    entry = FakeEntry(
        data={CONF_WEBHOOK_SECRET: "old"}, options={CONF_WEBHOOK_SECRET: "new"}
    )
    assert webhook_secret_for(entry) == "new"


def test_clearing_via_options_actually_disables_the_header():
    """The regression this whole module exists for."""
    entry = FakeEntry(
        data={CONF_WEBHOOK_SECRET: "old"}, options={CONF_WEBHOOK_SECRET: ""}
    )
    assert webhook_secret_for(entry) is None


def test_options_only_entry():
    assert webhook_secret_for(FakeEntry(options={CONF_WEBHOOK_SECRET: "opt"})) == "opt"


def test_unrelated_options_do_not_shadow_the_setup_value():
    """Presence of *other* option keys must not be read as "cleared"."""
    entry = FakeEntry(data={CONF_WEBHOOK_SECRET: "abc"}, options={"something_else": 1})
    assert webhook_secret_for(entry) == "abc"
