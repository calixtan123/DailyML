"""Verify the local application origin and installed launch constraints."""

import re
import socket
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from daily_ml.app import create_app
from daily_ml.cli import LaunchError, bind_loopback, own_data_directory
from daily_ml.config import load_settings
from daily_ml.practice import Practice

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


def test_settings_backup_download_requires_local_session_and_returns_sqlite(client):
    """Settings creates a verified attachment only for the local signed-in browser session."""
    assert client.post("/api/backup").status_code == 403

    response = client.post("/api/backup", headers=bootstrap(client))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.sqlite3")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"SQLite format 3")


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


def test_reference_test_api_saves_model_validates_and_reuses_request(client, monkeypatch):
    """Protected Settings actions share one persisted assessment across reads."""
    import httpx
    from test_provider import catalog, response_stream, terminal
    import json

    def handle(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=catalog())
        assert json.loads(request.content)["model"] == "a"
        return response_stream(terminal())
    assert client.get("/api/usage").status_code == 403
    headers = bootstrap(client)
    monkeypatch.setattr(client.app.state.connection, "get_access_token", lambda: "fixture-token")
    client.app.state.grading.provider.transport = httpx.MockTransport(handle)
    models = client.get("/api/connection/models").json()
    assert [m["slug"] for m in models["models"]] == ["z", "a"]
    data = {"model_id": "a", "version": models["version"]}
    assert client.put("/api/connection/model", json=data).status_code == 403
    assert client.put("/api/connection/model", headers=headers, json=data).status_code == 200
    assert client.get("/api/connection/models").json()["selected"] == "a"
    assert client.get("/api/usage").json()["attempts_24h"] == 0
    body = {"idempotency_key": "my-action", "model_id": "a"}
    submitted = client.post("/api/connection/test", headers=headers, json=body)
    assert submitted.status_code == 202
    path = "/api/requests/" + submitted.json()["id"]
    result = client.get(path).json()
    assert result["state"] == "assessed" and result["grade"] == 1
    assert client.post("/api/connection/test", headers=headers, json=body).json() == result
    assert client.get("/api/connection/test").json()["latest"] == result
    assert client.get("/api/usage").json()["attempts_24h"] == 1
    assert client.get("/api/requests/missing").status_code == 404


def test_signed_out_reference_test_preserves_recovery_without_usage(client):
    """A lost grant before send leaves the local reference test recoverable."""
    headers = bootstrap(client)
    assert client.get("/api/connection/models").status_code == 409
    reply = client.post("/api/connection/test", headers=headers, json={"model_id": "listed", "idempotency_key": "lost"})
    assert client.get("/api/requests/" + reply.json()["id"]).json()["state"] == "awaiting_access"
    assert client.get("/api/usage").json()["attempts_24h"] == 0


def test_question_browser_routes_only_expose_public_fields(client):
    """Read actual SQLite prompts with a session and protect assessment content."""
    assert client.get("/api/sections").status_code == 403
    assert client.get("/api/questions", params={"section_id": "fundamentals"}).status_code == 403
    bootstrap(client)
    sections = client.get("/api/sections")
    assert sections.status_code == 200
    assert sections.headers["cache-control"] == "no-store"
    assert [(row["id"], row["question_count"]) for row in sections.json()] == [
        ("fundamentals", 3), ("forests", 2), ("other-ensembles", 2),
        ("deep-learning", 2), ("deployment", 1), ("mlops", 2),
    ]
    response = client.get("/api/questions", params={"section_id": "forests"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {row["id"] for row in response.json()} == {"forest-mechanics", "tree-diversity"}
    for row in response.json():
        assert set(row) == {"id", "version", "title", "prompt", "objective", "difficulty", "concept_id"}
        assert row["prompt"] and row["objective"] and row["difficulty"] == "introductory"
        assert row["concept_id"]
    assert client.get("/api/questions", params={"section_id": "missing"}).status_code == 404
    assert client.get("/api/questions").status_code == 422
    assert client.get("/api/questions", params={"section_id": "https://invalid.test"}).status_code == 422
    assert client.get("/api/sections", headers={"Origin": "https://invalid.test"}).status_code == 403


def test_offline_validate_bank_runs_from_outside_checkout_without_assets(tmp_path, monkeypatch, capsys):
    """Exercise the installed CLI command with sockets and UI assets unavailable."""
    import os
    from shutil import copytree

    from daily_ml import cli

    def forbidden(*args, **kwargs):
        pytest.fail("Offline validation accessed runtime configuration, assets, or network")

    with monkeypatch.context() as isolated:
        isolated.chdir(tmp_path)
        isolated.setenv("DML_DATA_DIR", str(tmp_path / "unused-data"))
        isolated.setattr(socket, "socket", forbidden)
        isolated.setattr(cli, "resolve_assets", forbidden)
        isolated.setattr(cli, "resolve_installation", forbidden)
        assert cli.main(["validate-bank"]) == 0
    assert "12 questions across 6 sections" in capsys.readouterr().out
    assert not (tmp_path / "unused-data").exists()

    executable = Path(sys.executable).with_name("daily-ml")
    environment = {**os.environ, "DML_CONFIG": str(tmp_path / "missing.toml")}
    result = subprocess.run(
        [executable, "validate-bank"], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "12 questions across 6 sections" in result.stdout

    from importlib.resources import files

    candidate = copytree(files("daily_ml").joinpath("resources"), tmp_path / "bad bank")
    (candidate / "questions" / "generalisation.json").write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        cli.main(["validate-bank", "--bank-dir", str(candidate)])
    assert raised.value.code == 1
    assert "generalisation.json" in capsys.readouterr().err


def test_practice_routes_require_csrf_and_allow_signed_out_drafts(client):
    """Save offline practice using the local session while hiding assessment content."""
    path = "/api/practice/today"
    assert client.post(path).status_code == 403
    headers = bootstrap(client)
    assert client.post(path, headers={"Origin": ORIGIN}).status_code == 403

    today = client.post(path, headers=headers)
    assert today.status_code == 200
    assert today.headers["cache-control"] == "no-store"
    assert today.json()["answer_limit"] == 8000
    first = today.json()["selections"][0]["item"]
    assert set(first["question"]) == {
        "id", "version", "title", "prompt", "objective", "difficulty", "section_id",
        "section_title", "concept_id", "expected_answer_words",
    }
    draft_path = f"/api/practice/items/{first['id']}/draft"
    draft = {"text": "Train on the training set.", "version": 0}
    assert client.put(draft_path, headers={"Origin": ORIGIN}, json=draft).status_code == 403

    saved = client.put(draft_path, headers=headers, json=draft)
    assert saved.status_code == 200
    assert saved.json() == {**draft, "version": 1}
    stale = client.put(draft_path, headers=headers, json={"text": "Stale text", "version": 0})
    assert stale.status_code == 409
    assert stale.json()["saved"] == saved.json()
    assert "Stale text" not in stale.text
    invalid = client.put(draft_path, headers=headers, json={"text": "🧪" * 8001, "version": 1})
    assert invalid.status_code == 422
    assert "🧪" not in invalid.text
    assert client.post(path, headers=headers).json()["selections"][0]["item"]["draft"] == saved.json()
    assert client.post("/api/practice/items", headers=headers, json={"section_id": "deployment"}).status_code == 200
    assert client.post("/api/practice/items", headers=headers, json={"section_id": "missing"}).status_code == 404
    assert client.put(
        "/api/practice/items/9d095c57-2a4f-40bd-89ed-5673d8a38fbd/draft", headers=headers, json=draft
    ).status_code == 404


def test_exact_practice_route_resumes_a_draft_saved_after_midnight(tmp_path, assets, monkeypatch):
    """Return the older draft's snapshot while preserving the next day's saved pair."""
    now = datetime(2026, 11, 16, 23, 59, tzinfo=UTC)
    practice_factory = partial(Practice, clock=lambda: now, choose=lambda options: options[0])
    monkeypatch.setattr("daily_ml.app.Practice", practice_factory)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})

    with TestClient(create_app(settings, port=49152, assets_dir=assets), base_url=ORIGIN) as client:
        headers = bootstrap(client)
        monday = client.post("/api/practice/today", headers=headers).json()
        older = monday["selections"][0]["item"]
        now = datetime(2026, 11, 17, 0, 1, tzinfo=UTC)
        tuesday = client.post("/api/practice/today", headers=headers).json()
        newer = tuesday["selections"][0]["item"]
        assert monday["local_date"] == "2026-11-16"
        assert tuesday["local_date"] == "2026-11-17"
        assert older["question"] == newer["question"]
        assert older["id"] != newer["id"]
        saved = client.put(
            f"/api/practice/items/{older['id']}/draft",
            headers=headers,
            json={"text": "Keep my earlier answer.", "version": 0},
        )
        assert saved.status_code == 200

        reopened = client.post(
            "/api/practice/items", headers=headers, json={"question_id": older["question"]["id"]}
        )

    assert reopened.status_code == 200
    assert reopened.json()["item_id"] == older["id"]
    assert reopened.json()["today"]["selections"] == tuesday["selections"]
    assert reopened.json()["today"]["unfinished"] == [{**older, "draft": saved.json()}]
