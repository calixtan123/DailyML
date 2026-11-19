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


def test_opening_original_dispute_excludes_its_review_evidence_and_retains_target(learner):
    """Open a dispute without changing completion credit or retaining objection in public request data."""
    client, headers, db, _ = learner
    _configure_model(db)
    client.app.state.grading.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
    item = _today(client, headers)["selections"][0]["item"]
    submission = _submit(client, headers, item["id"], "An answer to challenge.").json()["submission"]

    response = client.post(
        f"/api/practice/items/{item['id']}/disputes", headers=headers,
        json={"request_id": submission["request"]["id"], "objection": "The rubric was applied incorrectly."},
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["credited"] is True
    assert saved["disputes"][0]["status"] == "open"
    assert saved["disputes"][0]["target_request_id"] == submission["request"]["id"]
    assert saved["disputes"][0]["objection"] == "The rubric was applied incorrectly."
    assert "objection" not in saved["request"]
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_evidence_selection").fetchone() == (0,)


def test_resolving_original_dispute_with_reassessment_selects_only_its_saved_version(learner):
    """Reassessment stays on its exact version and restores selected review evidence without new credit."""
    client, headers, db, _ = learner
    _configure_model(db)
    client.app.state.grading.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
    item = _today(client, headers)["selections"][0]["item"]
    original = _submit(client, headers, item["id"], "The exact answer.").json()["submission"]
    opened = client.post(f"/api/practice/items/{item['id']}/disputes", headers=headers, json={
        "request_id": original["request"]["id"], "objection": "Please assess this reasoning again.",
    }).json()

    reassessed = client.post(
        f"/api/practice/items/{item['id']}/disputes/{opened['disputes'][0]['id']}/reassess", headers=headers,
    )

    assert reassessed.status_code == 202
    dispute = client.get(f"/api/practice/items/{item['id']}/submission").json()["disputes"][0]
    reassessment = next(entry for entry in dispute["assessments"] if entry["id"] != original["request"]["id"])
    assert reassessment["state"] == "assessed"
    resolved = client.post(
        f"/api/practice/items/{item['id']}/disputes/{dispute['id']}/resolve", headers=headers,
        json={"assessment_request_id": reassessment["id"]},
    )

    assert resolved.status_code == 200
    assert resolved.json()["disputes"][0]["selected_request_id"] == reassessment["id"]
    assert resolved.json()["credited"] is True
    with db.connect() as connection:
        assert connection.execute("""
            SELECT evidence.grading_request_id FROM review_evidence_selection AS selection
            JOIN review_evidence AS evidence ON evidence.original_item_id = selection.original_item_id
              AND evidence.id = selection.evidence_id
        """).fetchone() == (reassessment["id"],)
        assert connection.execute("SELECT submitted_text FROM practice_evaluated_requests WHERE id = ?", (reassessment["id"],)).fetchone() == ("The exact answer.",)


def test_assisted_revision_dispute_keeps_original_review_selection_and_survives_backup(learner, tmp_path):
    """An assisted dispute is retained independently and never removes original scheduling evidence."""
    client, headers, db, _ = learner
    _configure_model(db)
    client.app.state.grading.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
    item = _today(client, headers)["selections"][0]["item"]
    original = _submit(client, headers, item["id"], "The original answer.").json()["submission"]
    revision = client.post(f"/api/practice/items/{item['id']}/revise", headers=headers, json={
        "request_id": original["request"]["id"], "text": "The assisted revision.", "version": 0,
    })
    assert revision.status_code == 202
    revised = revision.json()["revision"]

    opened = client.post(f"/api/practice/items/{item['id']}/disputes", headers=headers, json={
        "request_id": revised["request"]["id"], "objection": "The revision should receive different feedback.",
    })

    assert opened.status_code == 200
    assert opened.json()["disputes"][0]["role"] == "revision"
    with db.connect() as connection:
        assert connection.execute("SELECT evidence_id FROM review_evidence_selection").fetchone() == (original["request"]["id"],)
    restored = Database(tmp_path / "restored-dispute")
    with db.connect() as source, restored.connect() as destination:
        source.backup(destination)
    restored_practice = Practice(restored, answer_limit=8000)
    assert restored_practice.submission(item["id"])["disputes"][0]["target_request_id"] == revised["request"]["id"]


def test_dispute_rejects_an_assessment_from_another_saved_answer(learner):
    """Resolution cannot select feedback that is not a member of its immutable dispute."""
    client, headers, db, _ = learner
    _configure_model(db)
    client.app.state.grading.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
    first, second = [entry["item"] for entry in _today(client, headers)["selections"]]
    original = _submit(client, headers, first["id"], "First assessed answer.").json()["submission"]
    unrelated = _submit(client, headers, second["id"], "Second assessed answer.").json()["submission"]
    dispute = client.post(f"/api/practice/items/{first['id']}/disputes", headers=headers, json={
        "request_id": original["request"]["id"], "objection": "Please reconsider.",
    }).json()["disputes"][0]

    response = client.post(f"/api/practice/items/{first['id']}/disputes/{dispute['id']}/resolve", headers=headers,
                           json={"assessment_request_id": unrelated["request"]["id"]})

    assert response.status_code == 409
    assert client.get(f"/api/practice/items/{first['id']}/submission").json()["disputes"][0]["status"] == "open"


def test_late_reassessment_is_retained_without_overriding_original_resolution(learner):
    """A reassessment finishing after a chosen original remains history, not a new decision."""
    client, headers, db, _ = learner
    _configure_model(db)
    jobs = client.app.state.grading
    jobs.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
    item = _today(client, headers)["selections"][0]["item"]
    original = _submit(client, headers, item["id"], "An answer with a late reassessment.").json()["submission"]
    dispute = client.post(f"/api/practice/items/{item['id']}/disputes", headers=headers, json={
        "request_id": original["request"]["id"], "objection": "Reassess after I choose the original.",
    }).json()["disputes"][0]

    async def park(_request_id):
        """Leave the durable reassessment queued until the test explicitly completes it."""
    jobs.run_pending = park
    queued = client.post(f"/api/practice/items/{item['id']}/disputes/{dispute['id']}/reassess", headers=headers)
    assert queued.status_code == 202
    reassessment_id = next(entry["id"] for entry in queued.json()["disputes"][0]["assessments"]
                           if entry["id"] != original["request"]["id"])
    assert client.post(f"/api/practice/items/{item['id']}/disputes/{dispute['id']}/resolve", headers=headers,
                       json={"assessment_request_id": original["request"]["id"]}).status_code == 200

    import asyncio
    asyncio.run(type(client.app.state.grading).run_pending(jobs, reassessment_id))
    saved = client.get(f"/api/practice/items/{item['id']}/submission").json()["disputes"][0]
    assert saved["selected_request_id"] == original["request"]["id"]
    assert any(entry["id"] == reassessment_id and entry["state"] == "assessed" for entry in saved["assessments"])


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


def test_clarification_draft_survives_restore_and_cannot_be_overwritten_or_sent_by_stale_tab(learner, tmp_path):
    client, headers, db, instants = learner
    _configure_model(db)
    calls = []

    def clarify(outbound):
        calls.append(outbound)
        return response_stream(terminal(content=[{
            "type": "output_text", "annotations": [],
            "text": '{"feedback":{"status":"needs_clarification","explanation":"What was held out?"}}',
        }]))

    client.app.state.grading.provider = provider(clarify)
    first, second = [entry["item"] for entry in _today(client, headers)["selections"]]
    original = "Keep separate data."
    _submit(client, headers, first["id"], original)
    _submit(client, headers, second["id"], "A second original.")
    base = f"/api/practice/items/{first['id']}"
    submission = client.get(f"{base}/submission").json()
    request_id = submission["request"]["id"]

    assert submission["clarification_draft"] == {"text": original, "version": 0}
    assert _today(client, headers)["selections"][0]["item"]["grading_status"] == "needs_clarification"
    draft = "  Hold back a test set before fitting.  "
    saved = client.put(f"{base}/clarification-draft", headers=headers,
                       json={"request_id": request_id, "text": draft, "version": 0})
    assert saved.status_code == 200
    assert saved.json() == {"text": draft, "version": 1}
    assert client.get(f"{base}/submission").json()["clarification_draft"] == saved.json()

    restored = Database(tmp_path / "restored-draft")
    with db.connect() as source, restored.connect() as destination:
        source.backup(destination)
    after_restart = Practice(restored, answer_limit=8000, clock=lambda: instants[0])
    assert after_restart.submission(first["id"])["clarification_draft"] == saved.json()

    stale = {"request_id": request_id, "version": 0, "text": "Other tab's older edit."}
    conflict = client.put(f"{base}/clarification-draft", headers=headers, json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["saved"] == saved.json()
    stale_send = client.post(f"{base}/retry", headers=headers, json={**stale, "idempotency_key": "stale"})
    assert stale_send.status_code == 409
    assert stale_send.json()["saved"] == saved.json()
    assert len(calls) == 2

    too_long = client.put(f"{base}/clarification-draft", headers=headers,
                          json={**stale, "version": 1, "text": "🧪" * 8001})
    assert too_long.status_code == 422
    sent = client.post(f"{base}/retry", headers=headers, json={
        "request_id": request_id, "version": 1, "text": draft, "idempotency_key": "confirmed",
    })
    assert sent.status_code == 202
    assert sent.json()["assessed_text"] == draft
    assert sent.json()["text"] == original
    assert sent.json()["request"]["retry_of"] == request_id
    assert client.get(f"{base}/submission").json()["clarification_draft"] == {"text": draft, "version": 0}
    assert _today(client, headers)["completion_counts"] == {"2026-03-28": 2}
    assert len(calls) == 3

    stale_request = client.put(f"{base}/clarification-draft", headers=headers,
                               json={**stale, "version": 1})
    assert stale_request.status_code == 409
    assert client.get(f"{base}/submission").json()["clarification_draft"] == {"text": draft, "version": 0}


def _needs_clarification(explanation):
    """Return a schema-valid clarification at the external transport boundary."""
    import json

    return response_stream(terminal(content=[{
        "type": "output_text", "annotations": [],
        "text": json.dumps({"feedback": {"status": "needs_clarification", "explanation": explanation}}),
    }]))


def test_multiple_clarifications_preserve_exact_versions_and_one_review(learner):
    client, headers, db, _ = learner
    _configure_model(db)
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) <= 2:
            return _needs_clarification(f"Explain part {len(calls)}.")
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    item_id = _today(client, headers)["selections"][0]["item"]["id"]
    _submit(client, headers, item_id, "First answer.")
    url = f"/api/practice/items/{item_id}/submission"
    first = client.get(url).json()
    _retry(client, headers, item_id, first["request"]["id"], "Second answer.")
    second = client.get(url).json()
    _retry(client, headers, item_id, second["request"]["id"], "Third answer.")
    final = client.get(url).json()

    versions = final.get("versions", [])
    assert [(v["text"], v["kind"], v["role"], v["supported"]) for v in versions] == [
        ("First answer.", "initial", "original", False),
        ("Second answer.", "clarification", "original", False),
        ("Third answer.", "clarification", "original", False),
    ]
    assert [v["request"]["feedback"]["explanation"] for v in versions[:2]] == [
        "Explain part 1.", "Explain part 2.",
    ]
    assert [v["parent_version_id"] for v in versions] == [None, versions[0]["id"], versions[1]["id"]]
    assert final["submission_version_id"] == versions[2]["id"]
    assert client.get(f"/api/requests/{final['request']['id']}").json()["submission_version_id"] == versions[2]["id"]
    assert client.get("/api/usage").json() == {"attempts_24h": 3}
    with db.connect() as connection:
        assert connection.execute("SELECT grading_request_id FROM review_evidence").fetchall() == [
            (final["request"]["id"],),
        ]


def test_one_assisted_revision_keeps_credit_and_schedule_through_retry_and_backup(learner, tmp_path):
    client, headers, db, instants = learner
    _configure_model(db)
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) == 2:
            return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    item_id = _today(client, headers)["selections"][0]["item"]["id"]
    base = f"/api/practice/items/{item_id}"
    original_text = "Use a held-out test set."
    _submit(client, headers, item_id, original_text)
    original = client.get(f"{base}/submission").json()
    with db.connect() as connection:
        schedule = connection.execute("SELECT * FROM concept_review_state").fetchall()
    instants[0] = datetime(2026, 4, 8, 12, tzinfo=UTC)
    assert item_id in {item["id"] for item in _today(client, headers)["submitted"]}
    action = {"request_id": original["request"]["id"], "version": 0, "text": "Use nested evaluation."}
    draft = client.put(f"{base}/revision-draft", headers=headers, json=action)
    assert draft.status_code == 200
    assert client.get(f"{base}/submission").json()["revision_draft"] == {"text": action["text"], "version": 1}
    stale = {**action, "text": "Stale tab text."}
    for response in [
        client.put(f"{base}/revision-draft", headers=headers, json=stale),
        client.post(f"{base}/revise", headers=headers, json=stale),
    ]:
        assert response.status_code == 409
        assert response.json()["saved"] == draft.json()
    action["version"] = 1
    assert client.post(f"{base}/revise", headers=headers, json=action).status_code == 202
    failed = client.get(f"{base}/submission").json()
    assert failed["revision"]["request"]["state"] == "awaiting_allowance"
    assert failed["revision"]["supported"] is True
    assert _retry(client, headers, item_id, failed["revision"]["request"]["id"]).status_code == 202
    result = client.get(f"{base}/submission").json()
    revision = result["revision"]
    assert revision["request"]["state"] == "assessed"
    assert revision["submission_version_id"] == failed["revision"]["submission_version_id"]
    assert result["request"] == original["request"]
    assert result["assessed_text"] == original_text
    assert revision["assessed_text"] == action["text"]
    assert result["revision_draft"] is None
    assert client.post(f"{base}/revise", headers=headers, json=action).status_code == 202
    response = client.post(f"{base}/revise", headers=headers, json={**action, "text": "Second revision."})
    assert response.status_code == 409
    assert result["versions"][-1]["parent_version_id"] == original["submission_version_id"]
    assert client.get("/api/usage").json() == {"attempts_24h": 3}
    assert item_id in {item["id"] for item in _today(client, headers)["submitted"]}
    instants[0] = datetime(2026, 4, 20, 12, tzinfo=UTC)
    still_openable = {item["id"]: item["grading_status"] for item in _today(client, headers)["submitted"]}
    assert still_openable[item_id] == "assessed"

    restored = Database(tmp_path / "restored-revision")
    with db.connect() as source, restored.connect() as destination:
        source.backup(destination)
    practice = Practice(restored, answer_limit=8000, clock=lambda: instants[0])
    assert practice.submission(item_id) == result
    with restored.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT * FROM concept_review_state").fetchall() == schedule
        assert connection.execute("SELECT local_date FROM practice_credits").fetchall() == [("2026-03-28",)]
        assert connection.execute("SELECT COUNT(*) FROM review_evidence").fetchone() == (1,)


def test_competing_clarifications_and_revisions_commit_only_one_action(learner):
    client, headers, db, _ = learner
    _configure_model(db)
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) == 1:
            return _needs_clarification("Explain how the test set is used.")
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    item_id = _today(client, headers)["selections"][0]["item"]["id"]
    base = f"/api/practice/items/{item_id}"
    _submit(client, headers, item_id, "A test set is useful.")
    original = client.get(f"{base}/submission").json()
    original_request_id = original["request"]["id"]

    def race(send, competing_texts):
        barrier = Barrier(2)

        def send_after_barrier(text):
            barrier.wait(timeout=4)
            return send(text)

        with ThreadPoolExecutor(max_workers=2) as workers:
            return list(workers.map(send_after_barrier, competing_texts))

    clarification = race(
        lambda text: _retry(client, headers, item_id, original_request_id, text),
        ("Clarified A.", "Clarified B."),
    )
    assert sorted(response.status_code for response in clarification) == [202, 409]
    assessed = client.get(f"{base}/submission").json()
    action = {"request_id": assessed["request"]["id"], "version": 0}
    revisions = race(
        lambda text: client.post(f"{base}/revise", headers=headers, json={**action, "text": text}),
        ("Revised A.", "Revised B."),
    )
    assert sorted(response.status_code for response in revisions) == [202, 409]
    result = client.get(f"{base}/submission").json()
    assert [v["kind"] for v in result["versions"]] == ["initial", "clarification", "revision"]

    # Replaying an already accepted clarification after a revision returns the saved result.
    assert _retry(client, headers, item_id, original_request_id, assessed["assessed_text"]).json() == result
    assert _retry(client, headers, item_id, original_request_id).status_code == 409
    assert client.get("/api/usage").json() == {"attempts_24h": 3}
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_credits").fetchone() == (1,)
        assert connection.execute("SELECT grading_request_id FROM review_evidence").fetchall() == [
            (assessed["request"]["id"],),
        ]


def test_revision_clarification_keeps_assisted_role_and_original_unchanged(learner):
    client, headers, db, _ = learner
    _configure_model(db)
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) == 2:
            return _needs_clarification("Please name the comparison.")
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    item_id = _today(client, headers)["selections"][0]["item"]["id"]
    base = f"/api/practice/items/{item_id}"
    _submit(client, headers, item_id, "Original.")
    original = client.get(f"{base}/submission").json()
    action = {"request_id": original["request"]["id"], "text": "My comparison.", "version": 0}
    assert client.post(f"{base}/revise", headers=headers, json=action).status_code == 202
    current = client.get(f"{base}/submission").json()["revision"]
    assert current["clarification_draft"] == {"text": action["text"], "version": 0}
    assert _today(client, headers)["selections"][0]["item"]["grading_status"] == "needs_clarification"
    revision_request_id = current["request"]["id"]
    clarified = "Compare on unseen data."
    update = {"request_id": revision_request_id, "text": clarified, "version": 0}
    assert client.put(f"{base}/clarification-draft", headers=headers, json=update).status_code == 200
    assert _retry(client, headers, item_id, revision_request_id, clarified).status_code == 409
    update["version"] = 1
    response = client.post(f"{base}/retry", headers=headers, json={**update, "idempotency_key": "clarify-revision"})
    assert response.status_code == 202
    result = client.get(f"{base}/submission").json()
    assert result["revision"]["assessed_text"] == clarified
    assert result["revision"]["request"]["state"] == "assessed"
    assert result["request"] == original["request"]
    assert [(v["role"], v["kind"], v["supported"]) for v in result["versions"]] == [
        ("original", "initial", False),
        ("revision", "revision", True),
        ("revision", "clarification", True),
    ]
    assert client.post(f"{base}/revise", headers=headers, json=action).status_code == 202
    assert client.post(f"{base}/revise", headers=headers, json={**action, "text": clarified}).status_code == 409


def test_backfill_legacy_clarification_chain_and_transport_retries(learner, tmp_path):
    client, headers, db, instants = learner
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) < 3:
            return _needs_clarification("Please be specific.")
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    item_id = _today(client, headers)["selections"][0]["item"]["id"]
    url = f"/api/practice/items/{item_id}/submission"
    original_answer = "Keep test data separate."
    initial = _submit(client, headers, item_id, original_answer).json()["submission"]["request"]
    _configure_model(db)
    _retry(client, headers, item_id, initial["id"])
    original_retry = client.get(url).json()["request"]
    _retry(client, headers, item_id, original_retry["id"], original_answer)
    clarified = client.get(url).json()["request"]
    _retry(client, headers, item_id, clarified["id"], "Keep an untouched test set until final evaluation.")
    result = client.get(url).json()

    legacy = Database(tmp_path / "legacy-restore")
    with db.connect() as source, legacy.connect() as destination:
        source.backup(destination)
        # Remove the additive DML-8 schema while preserving the legacy records.
        destination.execute("DROP VIEW practice_evaluated_requests")
        for table in ("practice_revision_drafts", "practice_revisions", "practice_grading_versions",
                      "practice_submission_versions"):
            destination.execute(f"DROP TABLE {table}")
    after_restart = Practice(legacy, answer_limit=8000, clock=lambda: instants[0])
    assert after_restart.submission(item_id) == result
    assert [v["id"] for v in result["versions"]] == [initial["id"], clarified["id"], result["request"]["id"]]
    assert result["versions"][0]["request"]["id"] == original_retry["id"]
    with legacy.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_today_reopens_both_older_completed_comparisons_for_the_same_question(learner):
    client, headers, db, instants = learner
    _configure_model(db)
    client.app.state.grading.provider = provider(lambda request: response_stream(_valid_terminal(request)))
    client.app.state.grading.clock = lambda: instants[0].timestamp()
    first = _today(client, headers)["selections"][0]["item"]

    def complete_revision(item):
        base = f"/api/practice/items/{item['id']}"
        assert _submit(client, headers, item["id"], "Original answer.").status_code == 202
        current = client.get(f"{base}/submission").json()
        action = {"request_id": current["request"]["id"], "text": "Revised answer.", "version": 0}
        assert client.post(f"{base}/revise", headers=headers, json=action).status_code == 202

    complete_revision(first)
    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    second = _today(client, headers)["selections"][0]["item"]
    assert second["id"] != first["id"]
    assert second["question"]["id"] == first["question"]["id"]
    complete_revision(second)

    instants[0] = datetime(2026, 4, 20, 12, tzinfo=UTC)
    submitted = [item["id"] for item in _today(client, headers)["submitted"]]
    assert submitted == [second["id"], first["id"]]
    for item in (first, second):
        comparison = client.get(f"/api/practice/items/{item['id']}/submission").json()
        assert comparison["revision"]["assessed_text"] == "Revised answer."
        assert comparison["revision"]["request"]["state"] == "assessed"


def test_supported_original_is_frozen_through_clarification_and_revision(learner):
    client, headers, db, _ = learner
    _configure_model(db)
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) == 1:
            return _needs_clarification("Name the validation data.")
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    item_id = _today(client, headers)["selections"][0]["item"]["id"]
    base = f"/api/practice/items/{item_id}"
    with db.connect() as connection:
        # Simulate a persisted pre-answer support record in the submission transaction.
        connection.execute("""
            CREATE TRIGGER record_test_original_support AFTER INSERT ON practice_submissions
            BEGIN
                INSERT INTO practice_submission_support (item_id, supported) VALUES (NEW.item_id, 1);
            END
        """)
    response = _submit(client, headers, item_id, "I used the reference example.")
    assert response.status_code == 202
    original = client.get(f"{base}/submission").json()
    assert original["supported"] is True
    assert _retry(client, headers, item_id, original["request"]["id"], "I used a held-out sample.").status_code == 202
    assessed = client.get(f"{base}/submission").json()
    revision = {"request_id": assessed["request"]["id"], "text": "I used a held-out dataset.", "version": 0}
    assert client.post(f"{base}/revise", headers=headers, json=revision).status_code == 202
    result = client.get(f"{base}/submission").json()
    assert [(v["role"], v["supported"], v["request"]["supported"]) for v in result["versions"]] == [
        ("original", True, True), ("original", True, True), ("revision", True, True),
    ]
    with db.connect() as connection:
        assert connection.execute("SELECT grading_request_id, supported FROM review_evidence").fetchall() == [
            (assessed["request"]["id"], 1),
        ]


def test_today_reopens_each_older_original_with_an_unused_revision(learner):
    client, headers, db, instants = learner
    _configure_model(db)
    calls = []

    def assess(outbound):
        calls.append(outbound)
        if len(calls) == 3:
            return _needs_clarification("Name the evaluation criterion.")
        return response_stream(_valid_terminal(outbound))

    client.app.state.grading.provider = provider(assess)
    client.app.state.grading.clock = lambda: instants[0].timestamp()
    first = _today(client, headers)["selections"][0]["item"]
    assert _submit(client, headers, first["id"], "The first answer.").status_code == 202

    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    second, pending = [entry["item"] for entry in _today(client, headers)["selections"]]
    assert second["question"]["id"] == first["question"]["id"]
    assert second["id"] != first["id"]
    assert _submit(client, headers, second["id"], "The later answer.").status_code == 202
    assert _submit(client, headers, pending["id"], "An answer needing clarification.").status_code == 202

    instants[0] = datetime(2026, 4, 20, 12, tzinfo=UTC)
    submitted = {item["id"]: item["grading_status"] for item in _today(client, headers)["submitted"]}
    assert submitted == {
        first["id"]: "assessed", second["id"]: "assessed", pending["id"]: "needs_clarification",
    }
    for item in (first, second):
        base = f"/api/practice/items/{item['id']}"
        saved = client.get(f"{base}/submission").json()
        edit = {"request_id": saved["request"]["id"], "text": "My revised answer.", "version": 0}
        assert saved["revision_draft"] is not None
        assert client.post(f"{base}/revise", headers=headers, json=edit).status_code == 202
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_credits").fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM review_evidence").fetchone() == (2,)
