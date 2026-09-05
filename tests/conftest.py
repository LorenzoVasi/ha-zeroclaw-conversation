"""Shared fixtures.

Two things every test here needs, both autouse because no test in this
suite wants either switched off:

- `enable_custom_integrations` is what lets Home Assistant's own test
  harness load `custom_components/zeroclaw_conversation` at all; without
  it, starting a flow for this domain fails to find the integration.
- The base `homeassistant` component has to be set up explicitly. Real
  Home Assistant always has it; the harness starts with nothing, and the
  `conversation` component this integration depends on reads
  `hass.data[DATA_EXPOSED_ENTITIES]` during its own setup — which that
  component owns. Without it every test dies on a bare
  `KeyError: 'homeassistant.exposed_entities'` that points nowhere near
  the actual cause.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom component loadable in every test."""


@pytest.fixture(autouse=True)
async def setup_base_component(hass: HomeAssistant):
    """Set up the `homeassistant` component that `conversation` needs."""
    assert await async_setup_component(hass, "homeassistant", {})
