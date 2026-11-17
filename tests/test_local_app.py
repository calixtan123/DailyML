"""Verify the local application origin and installed launch constraints."""

import re
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from daily_ml.app import create_app
from daily_ml.cli import LaunchError, bind_loopback, own_data_directory
from daily_ml.config import load_settings

ORIGIN = "http://127.0.0.1:49152"


@pytest.fixture
def assets(tmp_path):
    """Create a minimal compiled asset directory for API tests."""
    root = tmp_path / "compiled"
    (root / "assets").mkdir(parents=True)
    (root / "assets/main.js").write_text("export {};")
    (root / "index.html").write_text('<script type="module" src="/assets/main.js"></script>')
    return root


@pytest.fixture
def client(tmp_path, assets):
    """Run the production ASGI interface at a fixed test origin."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path / "Data With Spaces")})
    with TestClient(create_app(settings, port=49152, assets_dir=assets), base_url=ORIGIN) as browser:
        yield browser


def bootstrap(client):
    """Establish a session and return headers for protected browser requests."""
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Domain=" not in cookie
    assert "session" not in response.json()
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_shell_assets_and_protected_connection_flow(client):
    """Serve compiled files and hand the browser a one-use local launch path."""
    assert client.get("/").status_code == 200
    assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]
    assert client.get("/assets/main.js").text == "export {};"
    assert client.get("/api/connection").status_code == 403
    headers = bootstrap(client)
    assert client.get("/api/connection").json()["state"] == "signed_out"
    authorization = client.post("/api/connection/sign-in", headers=headers)
    assert authorization.status_code == 200
    assert set(authorization.json()) == {"launch_path"}
    path = authorization.json()["launch_path"]
    assert re.fullmatch(r"/api/connection/authorize/[\w-]+", path)
    redirect = client.get(path, follow_redirects=False)
    assert redirect.status_code == 303
    assert urlsplit(redirect.headers["location"]).netloc == "auth.openai.com"
    assert client.get(path, follow_redirects=False).status_code == 409
    assert client.get("/api/connection").json()["state"] == "signing_in"
    assert client.post("/api/connection/cancel", headers=headers).json()["state"] == "cancelled"
    assert client.post("/api/connection/sign-out", headers=headers).json()["state"] == "signed_out"


@pytest.mark.parametrize("host", ["localhost:49152", "127.0.0.1:49153", "127.0.0.1", "example.test"])
def test_actual_host_required_on_api_and_assets(client, host):
    """Reject wrong hosts on every route, even when the local origin is present."""
    headers = {"Host": host, "Origin": ORIGIN}
    assert client.get("/", headers=headers).status_code == 400
    assert client.get("/api/session", headers=headers).status_code == 400


@pytest.mark.parametrize("origin", ["null", "https://example.test", "http://127.0.0.1:49153", "http://localhost:49152"])
def test_exact_origin_required(client, origin):
    """Reject cross-origin reads, mutations and preflights."""
    headers = {**bootstrap(client), "Origin": origin}
    assert client.get("/api/session", headers=headers).status_code == 403
    assert client.post("/api/connection/sign-in", headers=headers).status_code == 403
    preflight = client.options("/api/connection/sign-in", headers=headers)
    assert preflight.status_code == 403
    assert "access-control-allow-origin" not in preflight.headers


def test_mutations_require_session_origin_and_csrf(client):
    """Reject missing and stale local request credentials."""
    headers = bootstrap(client)
    for rejected in (
        {"Origin": ORIGIN},
        {"X-CSRF-Token": headers["X-CSRF-Token"]},
        {**headers, "X-CSRF-Token": "stale-instance"},
    ):
        assert client.post("/api/connection/sign-out", headers=rejected).status_code == 403
    client.cookies.clear()
    assert client.post("/api/connection/sign-out", headers=headers).status_code == 403
    assert "access-control-allow-origin" not in client.get("/api/session").headers


def test_instance_credentials_do_not_survive_backend_restart(client, tmp_path, assets):
    """A new process instance cannot accept a previous cookie or CSRF token."""
    prior = bootstrap(client)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path / "Other Data")})
    with TestClient(create_app(settings, port=49152, assets_dir=assets), base_url=ORIGIN) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.post("/api/connection/sign-out", headers=prior).status_code == 403
        bootstrap(restarted)
        assert restarted.post("/api/connection/sign-out", headers=prior).status_code == 403


def test_preference_mutations_are_versioned_and_hide_invalid_payloads(client):
    """Display effective timezone, save once, and reject stale or invalid edits."""
    headers = bootstrap(client)
    assert client.get("/api/settings").json() == {
        "timezone": "Europe/London", "version": 0, "source": "default"
    }
    edit = {"timezone": "UTC", "version": 0}
    assert client.put("/api/settings", headers=headers, json=edit).json() == {
        "timezone": "UTC", "version": 1, "source": "preferences"
    }
    assert client.put("/api/settings", headers=headers, json=edit).status_code == 409
    response = client.put("/api/settings", headers=headers, json={"timezone": ["secret"], "version": 1})
    assert response.status_code == 422
    assert "secret" not in response.text
    assert client.get("/api/settings").json()["timezone"] == "UTC"


def test_settings_exposes_override_provenance(tmp_path, assets):
    """Show the effective installed timezone and identify why it cannot be edited."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path), "DML_TIMEZONE": "Asia/Tokyo"})
    with TestClient(create_app(settings, port=49152, assets_dir=assets), base_url=ORIGIN) as client:
        headers = bootstrap(client)
        assert client.get("/api/settings").json() == {
            "timezone": "Asia/Tokyo", "version": 0, "source": "environment"
        }
        assert client.put("/api/settings", headers=headers, json={"timezone": "UTC", "version": 0}).status_code == 409


def test_launcher_binds_loopback_free_port_and_reports_port_conflict():
    """Reserve the actual port and diagnose conflict before starting a worker."""
    with bind_loopback(0) as listener:
        assert listener.family == socket.AF_INET
        host, port = listener.getsockname()
        assert host == "127.0.0.1" and port
        listener.listen()
        with pytest.raises(LaunchError, match="port"):
            bind_loopback(port)


def test_data_directory_owner_is_exclusive_across_processes(tmp_path):
    """A second process cannot own the same data directory until the first exits."""
    data = tmp_path / "Shared Data"
    code = """from pathlib import Path
import sys
from daily_ml.cli import LaunchError, own_data_directory

try:
    with own_data_directory(Path(sys.argv[1])):
        pass
except LaunchError as error:
    raise SystemExit(str(error))
"""
    command = [sys.executable, "-c", code, str(data)]
    with own_data_directory(data):
        busy = subprocess.run(command, capture_output=True, text=True, timeout=3)
        assert busy.returncode == 1
        assert "already running" in busy.stderr
    free = subprocess.run(command, capture_output=True, text=True, timeout=3)
    assert free.returncode == 0


def test_missing_frontend_assets_has_recovery_message(tmp_path):
    """Fail before serving, with an actionable build or reinstall instruction."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    with pytest.raises(LaunchError, match="npm.*build"):
        create_app(settings, port=49152, assets_dir=tmp_path / "missing")
