"""Exercise scheduling at the practice, SQLite and grading boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier

import pytest

from daily_ml.database import Database
from daily_ml.practice import Practice
from daily_ml.scheduling import read_reviews, record_gap, select_review_evidence
from test_local_app import assets  # noqa: F401 - fixture used by learner
from test_provider import provider, response_stream, terminal
from test_submissions import _configure_model, _retry, _submit, _today, _valid_terminal, learner  # noqa: F401

INTERVALS = (1, 3, 7, 14, 30)


def _assessment_clock(learner):
    """Use the same mutable UTC clock for practice and accepted feedback."""
    client, _, db, instants = learner
    _configure_model(db)
    jobs = client.app.state.grading
    jobs.clock = lambda: instants[0].timestamp()
    jobs.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
    return jobs


def _review(client, headers, item):
    """Return current scheduling state for a saved question's primary concept."""
    return _today(client, headers)["reviews"].get(item["question"]["concept_id"])


def test_finalization_once_from_two_tabs_updates_today_due_selection_and_calendar(learner):
    client, headers, db, instants = learner
    _assessment_clock(learner)
    item = _today(client, headers)["selections"][0]["item"]
    barrier = Barrier(2)

    def send(key):
        barrier.wait(timeout=4)
        return _submit(client, headers, item["id"], "An independent original answer.", key=key)

    with ThreadPoolExecutor(max_workers=2) as workers:
        left, right = workers.map(send, ("left", "right"))
    assert left.status_code == right.status_code == 202

    saved = _today(client, headers)
    assert saved["completion_counts"] == {"2026-03-28": 1}
    assert saved["reviews"][item["question"]["concept_id"]] == {
        "stage": 1, "due_date": "2026-03-29", "awaiting_assessment": False,
    }
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_evidence").fetchone() == (1,)
    assert client.get("/api/usage").json() == {"attempts_24h": 1}
    assert saved["selections"][0]["kind"] == "variety"

    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    next_day = _today(client, headers)
    assert next_day["selections"][0]["kind"] == "review"
    assert next_day["selections"][0]["item"]["question"]["concept_id"] == item["question"]["concept_id"]
    assert next_day["selections"][1]["kind"] == "variety"
    assert next_day["due_reviews"] == [{
        "question_id": item["question"]["id"], "section_id": item["question"]["section_id"],
        "title": item["question"]["title"],
    }]


def test_replay_uses_original_order_even_after_late_feedback(learner):
    client, headers, _, instants = learner
    jobs = _assessment_clock(learner)
    run = jobs.run_pending

    async def park(_request_id):
        pass

    jobs.run_pending = park

    earlier = _today(client, headers)["selections"][0]["item"]
    early_request = _submit(client, headers, earlier["id"], "Older answer.").json()["submission"]["request"]["id"]
    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    response = client.post("/api/practice/items", headers=headers,
                           json={"question_id": earlier["question"]["id"]})
    assert response.status_code == 200
    later_id = response.json()["item_id"]
    assert later_id != earlier["id"]
    late_request = _submit(client, headers, later_id, "Newer answer.").json()["submission"]["request"]["id"]
    assert _review(client, headers, earlier) is None

    asyncio.run(run(late_request))
    assert _review(client, headers, earlier)["due_date"] == "2026-03-30"
    instants[0] = datetime(2026, 4, 2, 12, tzinfo=UTC)
    asyncio.run(run(early_request))
    # Older answer advanced first. The newer answer used three days, anchored to March 29.
    assert _review(client, headers, earlier) == {
        "stage": 2, "due_date": "2026-04-01", "awaiting_assessment": False,
    }
    asyncio.run(run(early_request))
    assert _review(client, headers, earlier)["stage"] == 2


def test_review_sooner_is_idempotent_and_survives_late_replay_and_restart(learner, tmp_path):
    client, headers, db, instants = learner
    jobs = _assessment_clock(learner)
    run = jobs.run_pending

    async def park(_request_id):
        pass

    jobs.run_pending = park
    item = _today(client, headers)["selections"][0]["item"]
    job = _submit(client, headers, item["id"], "An answer awaiting assessment.").json()["submission"]["request"]["id"]

    concept = item["question"]["concept_id"]
    action = {"idempotency_key": "earlier-review"}
    path = f"/api/practice/reviews/{concept}/sooner"
    assert client.post(path, headers=headers, json=action).json()["due_date"] == "2026-03-29"
    instants[0] = datetime(2026, 4, 2, 12, tzinfo=UTC)
    asyncio.run(run(job))
    assert client.post(path, headers=headers, json=action).json()["due_date"] == "2026-03-29"
    assert client.post(path, headers=headers, json={"idempotency_key": "new-key"}).json()["due_date"] == "2026-03-29"
    assert _review(client, headers, item)["due_date"] == "2026-03-29"

    restored = Database(tmp_path / "restored-reviews")
    with db.connect() as source, restored.connect() as destination:
        source.backup(destination)
    practice = Practice(restored, answer_limit=8000, clock=lambda: instants[0])
    preserved = practice.today("America/Los_Angeles").reviews[concept]
    assert preserved.due_date.isoformat() == "2026-03-29"
    assert preserved.stage == 1
    assert practice.submission(item["id"])["completed_date"] == "2026-03-28"
    with restored.connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("replacement", ["needs_clarification", "failed_recoverable"])
def test_unaccepted_attempt_cannot_replace_prior_review(learner, replacement):
    client, headers, _, instants = learner
    jobs = _assessment_clock(learner)
    item = _today(client, headers)["selections"][0]["item"]
    assert _submit(client, headers, item["id"], "An assessed answer.").status_code == 202

    if replacement == "needs_clarification":
        jobs.provider = provider(lambda _: response_stream(terminal(content=[{
            "type": "output_text", "annotations": [],
            "text": '{"feedback":{"status":"needs_clarification","explanation":"What was held out?"}}',
        }])))
    else:
        jobs.provider = provider(lambda _: response_stream(terminal(content=[])))
    instants[0] = datetime(2026, 3, 29, 12, tzinfo=UTC)
    later = _today(client, headers)["selections"][0]["item"]
    assert _submit(client, headers, later["id"], "A subsequent answer.").status_code == 202

    saved = client.get(f"/api/practice/items/{later['id']}/submission").json()
    assert saved["request"]["state"] == replacement
    assert _review(client, headers, later)["due_date"] == "2026-03-29"
    if replacement == "needs_clarification":
        jobs.provider = provider(lambda outbound: response_stream(_valid_terminal(outbound)))
        instants[0] = datetime(2026, 4, 2, 12, tzinfo=UTC)
        assert _retry(client, headers, later["id"], saved["request"]["id"], "Clarified answer.").status_code == 202
        assert _review(client, headers, later) == {
            "stage": 2, "due_date": "2026-04-02", "awaiting_assessment": False,
        }


def test_fall_dst_completion_is_frozen_and_acceptance_uses_current_timezone(learner):
    client, headers, _, instants = learner
    jobs = _assessment_clock(learner)
    run = jobs.run_pending

    async def park(_request_id):
        pass

    jobs.run_pending = park
    # The second occurrence of 01:30 local follows the London rollback.
    instants[0] = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    item = _today(client, headers)["selections"][0]["item"]
    submitted = _submit(client, headers, item["id"], "An autumn practice answer.").json()["submission"]
    assert submitted["completed_date"] == "2026-10-25"

    version = client.get("/api/settings").json()["version"]
    response = client.put("/api/settings", headers=headers, json={
        "timezone": "America/Los_Angeles", "version": version,
    })
    assert response.status_code == 200
    assert _today(client, headers)["local_date"] == "2026-10-24"
    instants[0] = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)
    assert _today(client, headers)["completion_counts"] == {"2026-10-25": 1}

    instants[0] = datetime(2026, 10, 28, 0, 30, tzinfo=UTC)
    asyncio.run(run(submitted["request"]["id"]))

    saved = client.get(f"/api/practice/items/{item['id']}/submission").json()
    assert saved["completed_date"] == "2026-10-25"
    assert _review(client, headers, item)["due_date"] == "2026-10-27"


def test_pre_scheduler_assessment_backfills_once_from_saved_dates(learner, monkeypatch):
    client, headers, db, instants = learner
    _assessment_clock(learner)
    monkeypatch.setattr("daily_ml.grading_jobs.record_initial_assessment", lambda *_: None)
    item = _today(client, headers)["selections"][0]["item"]
    instants[0] = datetime(2026, 3, 29, 23, 30, tzinfo=UTC)
    assert _submit(client, headers, item["id"], "An old saved assessment.").status_code == 202
    assert _review(client, headers, item) is None

    instants[0] = datetime(2026, 4, 5, 12, tzinfo=UTC)
    for _ in range(2):
        practice = Practice(db, answer_limit=8000, clock=lambda: instants[0])
        state = practice.today("America/Los_Angeles").reviews[item["question"]["concept_id"]]
        assert state.stage == 1
        assert state.due_date == date(2026, 3, 31)
    with db.connect() as connection:
        assert connection.execute("SELECT submitted_date, accepted_date FROM review_evidence").fetchall() == [
            ("2026-03-30", "2026-03-30"),
        ]


def test_excluding_and_reselecting_evidence_keeps_earlier_review_and_original_credit(learner):
    client, headers, db, instants = learner
    _assessment_clock(learner)
    item = _today(client, headers)["selections"][0]["item"]
    assessment = _submit(client, headers, item["id"], "My assessed answer.").json()["submission"]["request"]["id"]
    concept = item["question"]["concept_id"]
    client.post(f"/api/practice/reviews/{concept}/sooner", headers=headers,
                json={"idempotency_key": "keep-this-review"}).raise_for_status()

    with db.connect(immediate=True) as connection:
        select_review_evidence(connection, item["id"], None, INTERVALS)
    practice = Practice(db, answer_limit=8000, clock=lambda: instants[0])
    assert practice.today("Europe/London").reviews[concept].awaiting_assessment
    assert _review(client, headers, item) == {
        "stage": 0, "due_date": "2026-03-29", "awaiting_assessment": True,
    }
    assert _today(client, headers)["completion_counts"] == {"2026-03-28": 1}

    with db.connect(immediate=True) as connection:
        with pytest.raises(LookupError, match="Accepted evidence"):
            select_review_evidence(connection, item["id"], "unrelated-evidence", INTERVALS)
        select_review_evidence(connection, item["id"], assessment, INTERVALS)
    assert _review(client, headers, item) == {
        "stage": 1, "due_date": "2026-03-29", "awaiting_assessment": False,
    }


def test_record_gap_creates_one_next_day_review_without_grading(learner):
    client, headers, db, instants = learner
    item = _today(client, headers)["selections"][0]["item"]
    with db.connect(immediate=True) as connection:
        record_gap(connection, item["id"], instants[0].timestamp(), "2026-03-28", INTERVALS)
        record_gap(connection, item["id"], instants[0].timestamp(), "2026-03-29", INTERVALS)
        assert connection.execute("SELECT COUNT(*) FROM review_evidence").fetchone() == (1,)
        assert read_reviews(connection)[item["question"]["concept_id"]].due_date == date(2026, 3, 29)
        with pytest.raises(LookupError, match="practice item"):
            record_gap(connection, "missing", instants[0].timestamp(), "2026-03-28", INTERVALS)
    assert client.get("/api/usage").json() == {"attempts_24h": 0}
