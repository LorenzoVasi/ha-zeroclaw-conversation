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

E2E_HOST_VARS = (
    "ZEROCLAW_E2E_HOST",
    "ZEROCLAW_E2E_SECURED_HOST",
    "ZEROCLAW_E2E_FAKE_LLM",
)


# NOTE: every test here skips itself when its host isn't configured,
# which is right on a developer machine — you may only have brought half
# the stack up. In CI that same behaviour is a trap: a workflow missing an
# environment variable goes green while silently testing nothing. The
# guard for that lives in the workflows as an explicit shell check before
# pytest runs, deliberately rather than as a pytest hook here: a check
# that runs before the suite and is obviously correct by reading beats a
# cleverer one whose behaviour I could not reproduce locally.


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled):
    """Allow real network access, but only to the hosts under test.

    Every host the suite is configured with has to be listed: pytest-socket
    reports a missed one as `SocketConnectBlockedError` against a bare IP,
    which looks like a network problem rather than a fixture that needs
    updating.
    """
    allowed = ["127.0.0.1", "localhost"]
    for var in E2E_HOST_VARS:
        host = urlparse(os.environ.get(var, "")).hostname
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
