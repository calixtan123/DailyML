"""Verify persisted credential protection, renewal and sign-out ordering."""

import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from daily_ml.config import load_settings, save_user_settings
from daily_ml.connection import Connection, ConnectionError
from oauth_support import CLIENT, Provider, callback, start, wait_for


@pytest.fixture
def connected(tmp_path):
    """Create a verified session and two independent instances over its files."""
    provider = Provider()
    provider.access_lifetime = 20
    with httpx.Client(transport=httpx.MockTransport(provider.transport)) as http:
        first = Connection(tmp_path, http=http)
        second = Connection(tmp_path, http=http)
        params = start(first, provider)
        callback(params)
        wait_for(first, "connected")
        try:
            yield first, second, provider, params
        finally:
            first.close()
            second.close()


def test_private_storage_and_stable_registration_after_sign_out(connected):
    """Remove renewable credentials while retaining the installation and learning database."""
    first, second, provider, params = connected
    root = first.data_dir / "credentials"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "session.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "installation.json").stat().st_mode) == 0o600
    settings = load_settings(environ={"DML_DATA_DIR": str(first.data_dir)})
    save_user_settings(settings, {"timezone": "UTC"}, expected_version=0)

    result = first.sign_out()
    assert result.state == "signed_out"
    assert len(provider.revoke_calls) == 1
    assert provider.revoke_calls[0] == {"token": ["original-refresh"],
                                        "token_type_hint": ["refresh_token"], "client_id": [CLIENT]}
    assert "original-access" not in (root / "session.json").read_text()
    assert "original-refresh" not in (root / "session.json").read_text()
    assert load_settings(environ={"DML_DATA_DIR": str(first.data_dir)}).timezone == "UTC"
    with pytest.raises(ConnectionError):
        second.get_access_token()
    again = start(second, provider)
    assert again["client_id"] == CLIENT
    assert again["ext_agent_host_id"] == params["ext_agent_host_id"]
    assert "id_token_hint" not in again


def test_concurrent_refresh_rotates_once_across_independent_connections(connected):
    """Both callers get the same rotated access token from a single refresh grant."""
    first, second, provider, _ = connected
    barrier = threading.Barrier(2)

    def renew(connection):
        barrier.wait(timeout=2)
        return connection.get_access_token()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(renew, (first, second)))
    assert results == ["rotated-access", "rotated-access"]
    assert provider.refresh_calls == [{"grant_type": ["refresh_token"], "client_id": [CLIENT],
                                       "refresh_token": ["original-refresh"],
                                       "resource": ["https://api.openai.com/v1"]}]
    assert "rotated-refresh" in (first.data_dir / "credentials/session.json").read_text()


def test_two_python_processes_share_one_refresh_rotation(connected, tmp_path):
    """POSIX locks serialize independent processes that read the same expiring session."""
    first, _, _, _ = connected
    events = tmp_path / "refresh-events.log"
    worker = r"""
import sys
import time
from pathlib import Path

import httpx

from daily_ml.connection import Connection

data, log, ready, peer_ready = map(Path, sys.argv[1:])

def transport(request):
    assert request.url.path == "/api/accounts/oauth/token"
    with log.open("a") as stream:
        stream.write("refresh\n")
    time.sleep(0.2)
    return httpx.Response(200, json={
        "access_token": "rotated-access", "refresh_token": "rotated-refresh",
        "token_type": "Bearer", "expires_in": 3600,
    })

with httpx.Client(transport=httpx.MockTransport(transport)) as http:
    connection = Connection(data, http=http)
    try:
        ready.touch()
        deadline = time.monotonic() + 3
        while not peer_ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert connection.get_access_token() == "rotated-access"
    finally:
        connection.close()
"""
    markers = (tmp_path / "first-ready", tmp_path / "second-ready")

    def run_worker(index):
        command = [
            sys.executable, "-c", worker, str(first.data_dir), str(events),
            str(markers[index]), str(markers[1 - index]),
        ]
        return subprocess.run(command, capture_output=True, text=True, timeout=6)

    with ThreadPoolExecutor(max_workers=2) as executor:
        for result in executor.map(run_worker, range(2)):
            assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == ["refresh"]


def test_sign_out_racing_blocked_refresh_revokes_latest_and_cannot_resurrect(connected):
    """Sign-out and renewal serialize on the same file lock."""
    first, second, provider, _ = connected
    entered, release, signing_out = threading.Event(), threading.Event(), threading.Event()

    def blocked_refresh():
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json={
            "access_token": "rotated-access", "refresh_token": "rotated-refresh",
            "token_type": "Bearer", "expires_in": 3600,
        })

    def sign_out():
        signing_out.set()
        return second.sign_out()

    provider.refresh_response = blocked_refresh
    with ThreadPoolExecutor(max_workers=2) as executor:
        refreshed = executor.submit(first.get_access_token)
        try:
            assert entered.wait(1)
            signed_out = executor.submit(sign_out)
            assert signing_out.wait(1)
        finally:
            release.set()
        assert refreshed.result(timeout=3) == "rotated-access"
        assert signed_out.result(timeout=3).state == "signed_out"
    assert provider.revoke_calls[0]["token"] == ["rotated-refresh"]
    assert first.get_status().state == "signed_out"
    with pytest.raises(ConnectionError):
        first.get_access_token()


@pytest.mark.parametrize("terminal", [True, False])
def test_refresh_recovery_preserves_only_retryable_credentials(connected, terminal):
    """Provider invalid_grant clears tokens and a transient outage keeps them for retry."""
    first, second, provider, _ = connected
    provider.refresh_response = httpx.Response(
        400 if terminal else 503, json={"error": "invalid_grant", "detail": "sensitive-upstream"}
    )
    with pytest.raises(ConnectionError) as raised:
        first.get_access_token()
    assert "sensitive-upstream" not in str(raised.value)
    if terminal:
        assert second.get_status().state == "reconnect"
        assert "original-refresh" not in (first.data_dir / "credentials/session.json").read_text()
    else:
        assert "original-refresh" in (first.data_dir / "credentials/session.json").read_text()
        provider.refresh_response = None
        assert second.get_access_token() == "rotated-access"


def test_unconfirmed_revocation_clears_local_tokens_and_reports_it(connected):
    """Even an unavailable revoke endpoint must leave the app signed out."""
    first, second, provider, _ = connected
    provider.revoke_response = httpx.Response(503)
    assert first.sign_out().remote_revocation_unconfirmed
    assert len(provider.revoke_calls) == 2
    assert second.get_status().state == "signed_out"
    assert second.get_status().remote_revocation_unconfirmed


def test_reject_symlink_to_credentials(connected, tmp_path):
    """Opening a tampered credential path never follows a link to another file."""
    first, _, _, _ = connected
    path = first.data_dir / "credentials/session.json"
    target = tmp_path / "unrelated"
    target.write_text("separate")
    path.unlink()
    os.symlink(target, path)
    with pytest.raises(ConnectionError, match="credential storage"):
        first.get_status()
    assert target.read_text() == "separate"


def test_rejected_refresh_client_recovers_after_sign_out_and_restart(connected):
    """A confirmed client rejection remains distinct from ordinary session renewal."""
    first, second, provider, initial = connected
    provider.refresh_response = httpx.Response(401, json={"error": "invalid_client"})
    with pytest.raises(ConnectionError):
        first.get_access_token()
    assert second.get_status().state == "reconnect"
    assert second.get_access_token() == "original-access"
    assert len(provider.refresh_calls) == 1
    assert first.sign_out().state == "signed_out"

    replacement = start(second, provider)
    assert replacement["client_id"] == "dynamic_agent_client"
    assert replacement["ext_agent_host_id"] == initial["ext_agent_host_id"]
    callback(replacement, client_id="oaiapp_replacement")
    wait_for(second, "connected")
    assert start(first, provider)["client_id"] == "oaiapp_replacement"
