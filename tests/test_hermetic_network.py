"""Guard: no test reaches a host off this machine, and no test finds cloud credentials.

An unmocked call to the résumé engine's Gemini client used to build a real client:
`config.gemini_auth()` defaults to Vertex, and a developer machine with gcloud's
application default credentials hands those to `genai.Client(vertexai=True)`.
conftest now scrubs the tailor's own key, points gcloud's config and
GOOGLE_APPLICATION_CREDENTIALS at an empty sandbox, and refuses every connection
that leaves loopback. These tests fail loudly if any of that is removed.
"""
import asyncio
import os
import socket
from pathlib import Path

import pytest

import conftest

_OFF_MACHINE = ("192.0.2.1", 80)        # TEST-NET-1: documentation only, never routed


def test_a_connection_off_the_machine_is_refused():
    with pytest.raises(conftest.OffMachineConnectionError, match="192.0.2.1"):
        socket.create_connection(_OFF_MACHINE, timeout=1)


def test_connect_ex_off_the_machine_is_refused_too():
    with socket.socket() as s, pytest.raises(conftest.OffMachineConnectionError):
        s.connect_ex(_OFF_MACHINE)


def test_an_asyncio_connection_off_the_machine_is_refused():
    async def _open():
        await asyncio.open_connection(*_OFF_MACHINE)
    with pytest.raises(conftest.OffMachineConnectionError):
        asyncio.run(_open())


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_still_connects(host):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection((host, port), timeout=5):
            pass


def test_ipv6_loopback_still_connects():
    if not socket.has_ipv6:
        pytest.skip("no IPv6 on this machine")
    with socket.socket(socket.AF_INET6) as server:
        try:
            server.bind(("::1", 0))
        except OSError:
            pytest.skip("::1 is not configured on this machine")
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("::1", port), timeout=5):
            pass


def test_an_asyncio_loopback_connection_still_works():
    async def _roundtrip():
        async def _echo(reader, writer):
            writer.write(await reader.read(5))
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(_echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"hello")
            await writer.drain()
            got = await reader.read(5)
            writer.close()
            return got
    assert asyncio.run(_roundtrip()) == b"hello"


@pytest.mark.parametrize("address,loopback", [
    (("127.0.0.1", 80), True), (("127.8.9.10", 80), True), (("localhost", 80), True),
    (("::1", 80, 0, 0), True), (("[::1]", 80), True), (("app.localhost", 80), True),
    (("LOCALHOST", 80), True), ("/tmp/some.sock", True),
    (("192.0.2.1", 80), False), (("example.com", 443), False),
    (("generativelanguage.googleapis.com", 443), False), (("169.254.169.254", 80), False),
])
def test_only_loopback_addresses_pass(address, loopback):
    assert conftest._is_loopback(address) is loopback


@pytest.mark.parametrize("env,on", [
    ({}, True), ({"AUTO_APPLY_TEST_JEV": "fake"}, True), ({"AUTO_APPLY_TEST_JEV": "replay"}, True),
    ({"AUTO_APPLY_TEST_JEV": "record"}, False), ({"AUTO_APPLY_TEST_JEV": " RECORD "}, False),
    ({"AUTO_APPLY_CAPTURE_JEV": "record"}, False),
])
def test_the_guard_is_off_only_for_a_jev_recording(env, on):
    assert conftest._network_guarded(env) is on


def test_the_tailors_gemini_key_is_scrubbed():
    assert "RESUME_TAILOR_GEMINI_API_KEY" not in os.environ


def test_gcloud_config_is_an_empty_sandbox():
    cfg = Path(os.environ["CLOUDSDK_CONFIG"])
    assert "inployed-test-gcloud" in str(cfg).lower()
    assert cfg.is_dir() and not any(cfg.iterdir())


def test_google_application_credentials_names_no_file():
    creds = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    assert creds.parent == Path(os.environ["CLOUDSDK_CONFIG"])
    assert not creds.exists()


def test_google_auth_finds_no_credentials():
    """What an unmocked `genai.Client(vertexai=True)` asks first: no credential
    file, no gcloud login, and the metadata server is off the machine."""
    auth = pytest.importorskip("google.auth")
    from google.auth import exceptions
    with pytest.raises(exceptions.DefaultCredentialsError):
        auth.default()
