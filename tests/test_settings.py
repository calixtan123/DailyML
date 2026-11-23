"""Verify preferences and observed attempts through the protected local interface."""

import json
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from daily_ml.app import create_app
from daily_ml.config import load_settings, save_user_settings
from test_local_app import ORIGIN, assets, bootstrap, client  # noqa: F401
from test_provider import catalog, response_stream, terminal
from test_submissions import _retry, _submit, _today, _valid_terminal, learner  # noqa: F401


def test_signed_out_settings_resolve_model_and_timezone_sources_independently(tmp_path, assets):
    """Expose only effective editable values and provenance without account or provider access."""
    environ = {"DML_DATA_DIR": str(tmp_path)}
    initial = load_settings(environ=environ)
    save_user_settings(initial, {"timezone": "UTC", "model_id": "saved-model"}, expected_version=0)
    config = tmp_path / "install.toml"
    config.write_text('timezone = "America/Los_Angeles"\nmodel_id = "installed-model"\n')
    settings = load_settings(config, environ={**environ, "DML_MODEL_ID": "environment-model"})

    with TestClient(create_app(settings, port=49152, assets_dir=assets), base_url=ORIGIN) as browser:
        headers = bootstrap(browser)
        assert browser.get("/api/settings").json() == {
            "timezone": "America/Los_Angeles", "source": "installation", "version": 1,
            "model_id": "environment-model", "model_source": "environment",
        }
        edit = browser.put("/api/connection/model", headers=headers, json={"model_id": "z", "version": 1})
        assert edit.status_code == 409
        assert load_settings(environ=environ).preferences.values == {"timezone": "UTC", "model_id": "saved-model"}


def test_catalog_removal_keeps_selection_until_explicit_replacement(client, monkeypatch):
    """A catalog read cannot change stored preferences or spend a model attempt."""
    headers = bootstrap(client)
    monkeypatch.setattr(client.app.state.connection, "get_access_token", lambda: "offline-token")
    entries = catalog()["models"]
    client.app.state.grading.provider.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"models": entries}),
    )

    chosen = client.put("/api/connection/model", headers=headers, json={"model_id": "z", "version": 0})
    assert chosen.status_code == 200
    assert chosen.json()["settings"] == client.get("/api/settings").json()
    assert chosen.json()["settings"]["model_id"] == "z"

    entries.pop(0)
    refreshed = client.get("/api/connection/models").json()
    assert refreshed["selected"] == "z" and refreshed["version"] == 1
    assert [model["slug"] for model in refreshed["models"]] == ["a"]
    assert client.put("/api/connection/model", headers=headers, json={"model_id": "z", "version": 1}).status_code == 409

    saved_timezone = client.put("/api/settings", headers=headers, json={"timezone": "UTC", "version": 1})
    assert saved_timezone.status_code == 200
    assert client.put("/api/connection/model", headers=headers, json={"model_id": "a", "version": 1}).status_code == 409
    replacement = client.put("/api/connection/model", headers=headers, json={"model_id": "a", "version": 2})
    assert replacement.json()["settings"] == {
        "timezone": "UTC", "source": "preferences", "version": 3,
        "model_id": "a", "model_source": "preferences",
    }
    assert client.get("/api/usage").json() == {"attempts_24h": 0}


def test_old_connection_action_recovers_after_new_installation_override(tmp_path, assets, monkeypatch):
    """Recover the exact acknowledged request before enforcing current selection for new sends."""
    environ = {"DML_DATA_DIR": str(tmp_path / "learning")}
    first = load_settings(environ=environ)
    body = {"model_id": "z", "idempotency_key": "original-action"}
    with TestClient(create_app(first, port=49152, assets_dir=assets), base_url=ORIGIN) as browser:
        headers = bootstrap(browser)
        jobs = browser.app.state.grading
        monkeypatch.setattr(browser.app.state.connection, "get_access_token", lambda: "offline-token")
        jobs.provider.transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=catalog()) if request.url.path.endswith("/models")
            else response_stream(terminal()),
        )
        queued = browser.post("/api/connection/test", headers=headers, json=body)
        assert queued.status_code == 202
        saved = browser.get(f"/api/requests/{queued.json()['id']}").json()
        assert saved["state"] == "assessed"

    second = load_settings(environ={**environ, "DML_MODEL_ID": "a"})
    with TestClient(create_app(second, port=49152, assets_dir=assets), base_url=ORIGIN) as browser:
        headers = bootstrap(browser)
        replay = browser.post("/api/connection/test", headers=headers, json=body)
        assert replay.status_code == 202 and replay.json() == saved
        stale_new = browser.post("/api/connection/test", headers=headers, json={**body, "idempotency_key": "new"})
        assert stale_new.status_code == 409
        assert browser.get("/api/usage").json() == {"attempts_24h": 1}


def test_new_preferences_apply_without_rewriting_existing_practice_and_attempts(learner, monkeypatch, caplog):
    """Keep the first snapshot and include practice, retry and connection sends in one UTC count."""
    client, headers, database, instants = learner
    jobs = client.app.state.grading
    jobs.clock = lambda: instants[0].timestamp()
    sends = []
    learner_text = "The original private explanation that stays out of Settings."
    connection_token = "offline-fixture-token"

    def handle(request):
        assert request.headers["Authorization"] == f"Bearer {connection_token}"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog())
        body = json.loads(request.content)
        sends.append(body["model"])
        if len(sends) == 1:
            return response_stream(terminal("failed", code="subscription_sharing_usage_limit_exceeded"))
        if len(sends) == 2:
            return response_stream(_valid_terminal(request))
        return response_stream(terminal())

    monkeypatch.setattr(client.app.state.connection, "get_access_token", lambda: connection_token)
    jobs.provider.transport = httpx.MockTransport(handle)
    assert client.put("/api/connection/model", headers=headers, json={"model_id": "z", "version": 0}).status_code == 200
    today = _today(client, headers)
    item = today["selections"][0]["item"]
    submitted = _submit(client, headers, item["id"], learner_text).json()["submission"]
    old_path = f"/api/requests/{submitted['request']['id']}"
    old_attempt = client.get(old_path).json()
    assert old_attempt["state"] == "awaiting_allowance"
    assert _today(client, headers)["completion_counts"] == {"2026-03-28": 1}

    instants[0] = datetime(2026, 3, 29, 23, 57, tzinfo=UTC)
    assert client.put("/api/settings", headers=headers, json={"timezone": "Asia/Tokyo", "version": 1}).status_code == 200
    assert client.put("/api/connection/model", headers=headers, json={"model_id": "a", "version": 2}).status_code == 200
    latest = _today(client, headers)
    assert latest["local_date"] == "2026-03-30" and latest["timezone"] == "Asia/Tokyo"
    with database.connect() as db:
        assert db.execute("SELECT local_date FROM practice_credits").fetchall() == [("2026-03-28",)]
        assert db.execute("SELECT timezone FROM practice_days WHERE local_date=?", (today["local_date"],)).fetchone() == (
            "Europe/London",
        )
    assert _retry(client, headers, item["id"], old_attempt["id"]).status_code == 202
    saved = client.get(f"/api/practice/items/{item['id']}/submission").json()
    assert saved["completed_timezone"] == "Europe/London"
    assert saved["completed_date"] == "2026-03-28"
    assert saved["request"]["model_id"] == "a"
    assert saved["request"]["reference"] == old_attempt["reference"]
    assert saved["request"]["state"] == "assessed"
    assert saved["request"]["feedback"]
    assert client.get(old_path).json() == old_attempt

    test = client.post("/api/connection/test", headers=headers,
                       json={"model_id": "a", "idempotency_key": "fixed-question"})
    assert test.status_code == 202
    assert sends == ["z", "a", "a"]
    assert client.get("/api/usage").json() == {"attempts_24h": 3}
    instants[0] = datetime(2026, 3, 29, 23, 58, tzinfo=UTC)
    assert client.get("/api/usage").json() == {"attempts_24h": 2}
    assert client.put("/api/settings", headers=headers, json={"timezone": "UTC", "version": 3}).status_code == 200
    assert client.put("/api/connection/model", headers=headers, json={"model_id": "z", "version": 4}).status_code == 200
    assert client.get(f"/api/practice/items/{item['id']}/submission").json() == saved
    for path in ("/api/settings", "/api/usage", "/api/connection", "/api/connection/models", "/api/connection/test", old_path):
        payload = client.get(path).text
        assert learner_text not in payload and connection_token not in payload
    assert learner_text not in caplog.text and connection_token not in caplog.text
