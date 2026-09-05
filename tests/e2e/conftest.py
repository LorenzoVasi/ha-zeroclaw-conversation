"""Fixtures specific to the end-to-end suite.

Home Assistant's test harness blocks real sockets on purpose — an
ordinary component test that reaches the network is a bug, and the
harness additionally pins outbound connections to localhost. These tests
are the deliberate exception: talking to a real ZeroClaw daemon is the
entire point.

Rather than switching the guard off wholesale, the gateway's own host is
added to pytest-socket's allowlist, so an accidental call to anywhere
else still fails loudly here too.
"""

import os
from unittest.mock import patch
from urllib.parse import urlparse

import aiohttp
import pytest
import pytest_socket


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled):
    """Allow real network access, but only to the gateway under test."""
    host = urlparse(os.environ.get("ZEROCLAW_E2E_HOST", "")).hostname
    allowed = ["127.0.0.1", "localhost"]
    if host:
        allowed.append(host)
    pytest_socket.socket_allow_hosts(allowed, allow_unix_socket=True)


@pytest.fixture
async def real_client_session():
    """Give `api.py` a genuine aiohttp session.

    The Home Assistant test harness swaps `async_get_clientsession` for
    one that cannot reach the network — it nulls the DNS resolver and
    binds to a different event loop, which surfaces as
    `'NoneType' object has no attribute 'getaddrinfo'` rather than
    anything that mentions mocking. Creating the session inside the
    running test binds it to Home Assistant's own loop, which is what the
    integration's calls will execute on.
    """
    async with aiohttp.ClientSession() as session:
        with patch(
            "custom_components.zeroclaw_conversation.api.async_get_clientsession",
            return_value=session,
        ):
            yield session
