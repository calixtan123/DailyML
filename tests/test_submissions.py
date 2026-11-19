"""Exercise original submissions, calendar credit and grading recovery over the local API."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient

from daily_ml.app import create_app
from daily_ml.config import load_settings
from daily_ml.database import Database
from daily_ml.practice import Practice
from test_local_app import ORIGIN, assets, bootstrap
from test_provider import provider, response_stream, terminal


@pytest.fixture
def learner(tmp_path, assets, monkeypatch):
    """Create a learner with a controllable Python clock and no configured account."""
    data = tmp_path / "learning"
    instants = [datetime(2026, 3, 28, 23, 58, tzinfo=UTC)]
    monkeypatch.setattr("daily_ml.app.Practice", partial(
        Practice, clock=lambda: instants[0], choose=lambda candidates: candidates[0],
    ))
    settings = load_settings(environ={"DML_DATA_DIR": str(data)})
    app = create_app(settings, port=49152, assets_dir=assets)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, bootstrap(client), Database(data), instants


def _today(client, headers):
    """Prepare the current recommendations through the protected local API."""
    response = client.post("/api/practice/today", headers=headers)
    assert response.status_code == 200
    return response.json()


def _submit(client, headers, item_id, text, *, version=0, key="submit"):
    """Send the displayed editor version and text."""
    return client.post(f"/api/practice/items/{item_id}/submit", headers=headers,
                       json={"text": text, "version": version, "idempotency_key": key})


def test_submitting_after_london_dst_rollover_persists_original_and_calendar(learner):
    client, headers, db, instants = learner
    first = _today(client, headers)["selections"][0]["item"]
    text = "  Sampling variation means valid models can disagree.  "
    instants[0] = datetime(2026, 3, 29, 23, 1, tzinfo=UTC)

    result = _submit(client, headers, first["id"], text)

    assert result.status_code == 202
    saved = result.json()["submission"]
    assert saved["text"] == text
    assert saved["completed_date"] == "2026-03-30"
    assert saved["completed_timezone"] == "Europe/London"
    assert saved["request"]["state"] == "awaiting_access"
    assert client.get("/api/usage").json() == {"attempts_24h": 0}
    assert result.json()["today"]["completion_counts"] == {"2026-03-30": 1}
    assert result.json()["today"]["first_use_date"] == "2026-03-28"
    with db.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("invalid", ["   ", "a" * 8001])
def test_invalid_answer_creates_neither_completion_nor_request(learner, invalid):
    client, headers, _, _ = learner
    first = _today(client, headers)["selections"][0]["item"]

    response = _submit(client, headers, first["id"], invalid)

    assert response.status_code == 422
    assert invalid not in response.text
    assert _today(client, headers)["completion_counts"] == {}
    assert client.get("/api/connection/test").json()["latest"] is None


def test_concurrent_tabs_with_different_action_keys_save_one_original(learner):
    client, headers, _, _ = learner
    first = _today(client, headers)["selections"][0]["item"]
    barrier = Barrier(2)

    def submit(key):
        barrier.wait(timeout=4)
        return _submit(client, headers, first["id"], "A complete original.", key=key)

    with ThreadPoolExecutor(max_workers=2) as workers:
        left, right = workers.map(submit, ("first", "second"))

    assert left.status_code == right.status_code == 202
    assert left.json()["submission"] == right.json()["submission"]
    assert _today(client, headers)["completion_counts"] == {"2026-03-28": 1}
    conflicting = _submit(client, headers, first["id"], "A competing answer.", key="third")
    assert conflicting.status_code == 409
    assert "A competing answer." not in conflicting.text


def test_two_items_for_same_question_credit_only_once_on_completion_date(learner):
    client, headers, _, instants = learner
    first = _today(client, headers)["selections"][0]["item"]
    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    current = _today(client, headers)["selections"][0]["item"]
    assert current["question"]["id"] == first["question"]["id"] and current["id"] != first["id"]

    for item, key in [(first, "first"), (current, "second")]:
        assert _submit(client, headers, item["id"], "An original on each saved item.", key=key).status_code == 202

    assert _today(client, headers)["completion_counts"] == {"2026-03-29": 1}


def _configure_model(db):
    """Give the learner the same saved model preference used by Settings."""
    saved = db.get_preferences()
    db.save_preferences({**saved.values, "model_id": "z"}, expected_version=saved.version)


def _retry(client, headers, item_id, request_id, text=None):
    """Act explicitly on the request state displayed by the learner's tab."""
    return client.post(f"/api/practice/items/{item_id}/retry", headers=headers,
                       json={"request_id": request_id, "text": text, "idempotency_key": "retry"})


def _valid_terminal(http_request):
    """Construct feedback for the exact question rubric that traversed the SDK."""
    import json
    from test_grading import example_feedback

    context = json.loads(json.loads(http_request.content)["input"][0]["content"])
    answer = example_feedback()
    answer["feedback"]["criteria"] = [
        {"criterion_id": entry["id"], "status": "met", "justification": "Explains the criterion."}
        for entry in context["criteria"]
    ]
    answer["feedback"]["source_ids"] = [next(iter(context["sources"]))]
    return terminal(content=[{"type": "output_text", "text": json.dumps(answer), "annotations": []}])


def test_access_recovery_commits_feedback_and_reopening_is_read_only(learner):
    client, headers, db, _ = learner
    first = _today(client, headers)["selections"][0]["item"]
    text = "I would quantify diversity and compare validation results."
    initial = _submit(client, headers, first["id"], text).json()["submission"]
    _configure_model(db)
    sends = []

    def assess(outbound):
        sends.append(outbound)
        # Prove the data commits before transport and the transport owns no write lock.
        with db.connect(immediate=True) as connection:
            assert connection.execute("SELECT original_text FROM practice_submissions").fetchone() == (text,)
            assert connection.execute("SELECT COUNT(*) FROM practice_credits").fetchone() == (1,)
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    retried = _retry(client, headers, first["id"], initial["request"]["id"])

    assert retried.status_code == 202
    url = f"/api/practice/items/{first['id']}/submission"
    reopened = client.get(url).json()
    assert reopened == client.get(url).json()
    assert reopened["request"]["state"] == "assessed"
    assert reopened["request"]["grade"] == 3
    assert reopened["request"]["feedback"]["model_answer"]
    assert client.get("/api/usage").json()["attempts_24h"] == len(sends) == 1


@pytest.mark.parametrize(("retry_after", "code", "expected_calls"), [
    ("0", "rate_limit_exceeded", 2),
    ("6", "rate_limit_exceeded", 1),
    ("0", "subscription_sharing_usage_limit_exceeded", 1),
    ("malformed", "rate_limit_exceeded", 1),
])
def test_only_confirmed_short_transient_rate_limit_retries_once(learner, retry_after, code, expected_calls):
    client, headers, db, _ = learner
    _configure_model(db)
    calls = []

    def rate_limited(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after}, json={
            "error": {"code": code, "message": "temporary capacity unavailable"},
        })

    client.app.state.grading.provider = provider(rate_limited)
    first = _today(client, headers)["selections"][0]["item"]
    assert _submit(client, headers, first["id"], "Save before sending.").status_code == 202

    saved = client.get(f"/api/practice/items/{first['id']}/submission").json()
    assert saved["request"]["state"] == "awaiting_allowance"
    assert saved["credited"] is True
    assert len(calls) == client.get("/api/usage").json()["attempts_24h"] == expected_calls


def test_restart_keeps_only_unsent_work_queued_and_requires_an_explicit_send(learner):
    client, headers, db, _ = learner
    _configure_model(db)
    jobs = client.app.state.grading
    # Park dispatch to simulate an abrupt shutdown after HTTP acknowledgement.
    run = jobs.run_pending
    async def park(_request_id):
        pass
    jobs.run_pending = park
    first, second = [choice["item"] for choice in _today(client, headers)["selections"]]
    unsent = _submit(client, headers, first["id"], "Saved but not sent.").json()["submission"]["request"]["id"]
    sent = _submit(client, headers, second["id"], "Saved before interruption.").json()["submission"]["request"]["id"]
    assert jobs._claim(sent)["id"] == sent
    jobs._reserve(sent)
    jobs.run_pending = run

    jobs.recover_interrupted()
    sends = []
    def assess(outbound):
        sends.append(outbound)
        return response_stream(_valid_terminal(outbound))
    jobs.provider = provider(assess)
    asyncio.run(jobs.run_pending())

    assert jobs.get_request(unsent)["state"] == "queued"
    assert jobs.get_request(unsent)["can_send"]
    assert jobs.get_request(sent)["state"] == "outcome_unknown"
    assert "may have used" in jobs.get_request(sent)["message"]
    assert jobs.usage_count() == 1 and not sends
    assert _retry(client, headers, first["id"], unsent).status_code == 202
    assert _retry(client, headers, second["id"], sent).status_code == 202
    assert jobs.usage_count() == 3 and len(sends) == 2
    assert _today(client, headers)["completion_counts"] == {"2026-03-28": 2}


def test_clarification_keeps_original_date_and_credit_through_backup(learner, tmp_path):
    client, headers, db, instants = learner
    _configure_model(db)
    calls = []
    def assess(outbound):
        calls.append(outbound)
        if len(calls) == 1:
            return response_stream(terminal(content=[{
                "type": "output_text", "annotations": [],
                "text": '{"feedback":{"status":"needs_clarification","explanation":"Which data is held out?"}}',
            }]))
        return response_stream(_valid_terminal(outbound))
    client.app.state.grading.provider = provider(assess)
    first = _today(client, headers)["selections"][0]["item"]
    result = _submit(client, headers, first["id"], "Use separate data.").json()
    original = result["submission"]["request"]["id"]
    url = f"/api/practice/items/{first['id']}/submission"
    assert client.get(url).json()["request"]["state"] == "needs_clarification"
    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    assert _retry(client, headers, first["id"], original, "Hold test data back from tuning.").status_code == 202

    restored = Database(tmp_path / "restored")
    with db.connect() as source, restored.connect() as destination:
        source.backup(destination)
    practice = Practice(restored, answer_limit=8000, clock=lambda: instants[0])
    submission = practice.submission(first["id"])
    assert submission["text"] == "Use separate data."
    assert submission["assessed_text"] == "Hold test data back from tuning."
    assert submission["completed_date"] == "2026-03-28"
    assert submission["request"]["state"] == "assessed"
    assert submission["request"]["retry_of"] == original
    assert practice.today("Europe/London").completion_counts == {"2026-03-28": 1}
    assert len(calls) == 2
    with restored.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
