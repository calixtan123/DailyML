"""Verify offline help, assisted practice and self-reported gaps."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timezone
from threading import Barrier

import pytest

from daily_ml.bank import import_bank, validate_bank
from daily_ml.database import Database
from daily_ml.practice import Practice, PracticeItemNotFound, SubmissionConflict


MONDAY = datetime(2026, 11, 16, 23, 59, tzinfo=timezone.utc)
TUESDAY = datetime(2026, 11, 17, 0, 1, tzinfo=timezone.utc)
LONDON = "Europe/London"


def london_timezone(_connection):
    """Use the London timezone for the offline practice fixtures."""
    return LONDON


@pytest.fixture
def database(tmp_path):
    """Open a clean database with the reviewed starter bank."""
    db = Database(tmp_path / "live")
    import_bank(db, validate_bank())
    return db


def service(database, *, now=MONDAY):
    """Build a deterministic offline practice service."""
    return Practice(database, answer_limit=8000, clock=lambda: now, choose=lambda candidates: candidates[0])


def test_help_is_saved_before_hint_or_explanation_is_returned(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item

    hint = practice.view_help(item.id, "hint")
    explanation = practice.view_help(item.id, "explanation")

    assert hint
    assert explanation
    with database.connect() as connection:
        events = connection.execute(
            "SELECT kind FROM practice_help_events WHERE item_id = ? ORDER BY kind", (item.id,)
        ).fetchall()
    assert events == [("explanation",), ("hint",)]


def test_regular_explanation_access_does_not_authorize_a_gap(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item

    practice.view_help(item.id, "explanation")

    with pytest.raises(SubmissionConflict, match="View the explanation"):
        practice.acknowledge_gap(item.id, "gap-1", resolve_timezone=london_timezone)


def test_pre_submission_help_is_snapshot_as_assisted_practice(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    practice.view_help(item.id, "hint")

    result = practice.submit(
        item.id, "An answer written after reading the hint.", expected_version=0,
        resolve_settings=lambda _connection: (LONDON, None),
    )

    assert result.submission["assisted"] is True
    with database.connect() as connection:
        assert connection.execute(
            "SELECT supported FROM practice_submission_support WHERE item_id = ?", (item.id,)
        ).fetchone() == (1,)


def test_post_submission_help_preserves_original_independence(database):
    """Reading explanations after answering leaves the original status intact."""
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    result = practice.submit(
        item.id, "An independent answer.", expected_version=0,
        resolve_settings=lambda _connection: (LONDON, None),
    )

    assert practice.view_help(item.id, "explanation")

    assert result.submission["assisted"] is False
    assert practice.submission(item.id)["assisted"] is False


def test_gap_requires_viewed_explanation_then_creates_one_local_completion(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item

    preview = practice.begin_gap(item.id)
    assert preview["explanation"]
    assert preview["acknowledgement_required"] is True
    assert practice.submission(item.id) is None

    result = practice.acknowledge_gap(item.id, "gap-1", resolve_timezone=london_timezone)
    repeated = practice.acknowledge_gap(item.id, "gap-1", resolve_timezone=london_timezone)

    assert result.submission == repeated.submission
    assert result.submission["text"] == "I don't know"
    assert result.submission["request"]["state"] == "self_reported_gap"
    assert result.submission["credited"] is True
    assert result.today.completion_counts == {"2026-11-16": 1}
    assert result.today.reviews[item.question.concept_id].due_date == date(2026, 11, 17)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_submissions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM practice_gap_completions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM practice_credits").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM grading_requests").fetchone() == (0,)
        assert connection.execute(
            "SELECT grade, grading_request_id FROM review_evidence"
        ).fetchone() == (None, None)


def test_gap_cannot_complete_before_explanation_or_after_a_competing_answer(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item

    with pytest.raises(SubmissionConflict, match="View the explanation"):
        practice.acknowledge_gap(item.id, "gap-1", resolve_timezone=london_timezone)

    practice.submit(
        item.id, "An ordinary answer.", expected_version=0,
        resolve_settings=lambda _connection: (LONDON, None),
    )
    with pytest.raises(SubmissionConflict, match="already"):
        practice.acknowledge_gap(item.id, "gap-2", resolve_timezone=london_timezone)


def test_simultaneous_gap_acknowledgements_produce_one_original_and_credit(database):
    first = service(database)
    second = service(Database(database.path.parent))
    item = first.today(LONDON).selections[0].item
    first.begin_gap(item.id)
    barrier = Barrier(2)

    def acknowledge(practice, key):
        barrier.wait(timeout=5)
        return practice.acknowledge_gap(item.id, key, resolve_timezone=london_timezone)

    with ThreadPoolExecutor(max_workers=2) as workers:
        left, right = workers.map(lambda values: acknowledge(*values), ((first, "a"), (second, "b")))

    assert left.submission == right.submission
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_submissions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM practice_gap_completions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM practice_credits").fetchone() == (1,)


def test_answer_and_gap_race_has_one_completion_without_partial_records(database):
    first = service(database)
    second = service(Database(database.path.parent))
    item = first.today(LONDON).selections[0].item
    first.begin_gap(item.id)
    barrier = Barrier(2)

    def answer():
        barrier.wait(timeout=5)
        try:
            return first.submit(
                item.id, "An ordinary answer.", expected_version=0,
                resolve_settings=lambda _connection: (LONDON, None),
            )
        except SubmissionConflict:
            return None

    def gap():
        barrier.wait(timeout=5)
        try:
            return second.acknowledge_gap(item.id, "gap-race", resolve_timezone=london_timezone)
        except SubmissionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        answer_result, gap_result = workers.map(lambda operation: operation(), (answer, gap))

    assert (answer_result is None) != (gap_result is None)
    with database.connect() as connection:
        submissions = connection.execute("SELECT COUNT(*) FROM practice_submissions").fetchone()[0]
        gaps = connection.execute("SELECT COUNT(*) FROM practice_gap_completions").fetchone()[0]
        requests = connection.execute("SELECT COUNT(*) FROM grading_requests").fetchone()[0]
    assert submissions + gaps == 1
    assert requests == submissions


def test_gap_uses_the_date_explanation_is_acknowledged(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    practice.begin_gap(item.id)
    result = Practice(database, answer_limit=8000, clock=lambda: TUESDAY, choose=lambda candidates: candidates[0])\
        .acknowledge_gap(item.id, "gap-next-day", resolve_timezone=london_timezone)

    assert result.submission["completed_date"] == "2026-11-17"
    assert result.today.completion_counts == {"2026-11-17": 1}

    with database.connect() as connection:
        recency = connection.execute(
            "SELECT last_practised_date FROM practice_items WHERE id = ?", (item.id,),
        ).fetchone()
    assert recency == ("2026-11-17",)


def test_today_hides_prior_week_gap_but_keeps_unresolved_grading(database):
    saturday = datetime(2026, 11, 21, 23, 59, tzinfo=timezone.utc)
    sunday = datetime(2026, 11, 22, 0, 1, tzinfo=timezone.utc)
    monday = datetime(2026, 11, 23, 0, 1, tzinfo=timezone.utc)
    practice = service(database, now=saturday)
    gap, pending = [selection.item for selection in practice.today(LONDON).selections[:2]]
    practice.begin_gap(gap.id)
    practice.acknowledge_gap(gap.id, "sunday-gap", resolve_timezone=london_timezone)
    practice.submit(
        pending.id, "An answer awaiting grading.", expected_version=0,
        resolve_settings=lambda _connection: (LONDON, "test-model"),
    )

    sunday_today = service(database, now=sunday).today(LONDON)
    monday_today = service(database, now=monday).today(LONDON)

    assert {item.id for item in sunday_today.submitted} == {gap.id, pending.id}
    assert {item.id for item in monday_today.submitted} == {pending.id}
    assert practice.submission(gap.id)["request"]["state"] == "self_reported_gap"


def test_gap_retry_after_day_rollover_reuses_completion_and_prepares_today(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    practice.begin_gap(item.id)
    practice.acknowledge_gap(item.id, "gap-retry", resolve_timezone=london_timezone)

    retry = Practice(database, answer_limit=8000, clock=lambda: TUESDAY, choose=lambda candidates: candidates[0])\
        .acknowledge_gap(item.id, "gap-retry", resolve_timezone=london_timezone)

    assert retry.submission["completed_date"] == "2026-11-16"
    assert retry.today.local_date == "2026-11-17"


def test_existing_credit_schema_migrates_and_preserves_answer_credit(database):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    practice.submit(
        item.id, "An answer before the migration.", expected_version=0,
        resolve_settings=lambda _connection: (LONDON, None),
    )
    with database.connect(immediate=True) as connection:
        connection.execute("DROP INDEX practice_credits_by_date")
        connection.execute("ALTER TABLE practice_credits RENAME TO practice_credits_new")
        connection.execute(
            """CREATE TABLE practice_credits (
                question_id TEXT NOT NULL,
                local_date TEXT NOT NULL REFERENCES practice_days(local_date),
                item_id TEXT NOT NULL UNIQUE REFERENCES practice_submissions(item_id),
                FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id),
                PRIMARY KEY (question_id, local_date)
            )"""
        )
        connection.execute(
            "INSERT INTO practice_credits SELECT question_id, local_date, item_id FROM practice_credits_new"
        )
        connection.execute("DROP TABLE practice_credits_new")
        connection.execute("CREATE INDEX practice_credits_by_date ON practice_credits(local_date)")

    Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=lambda candidates: candidates[0])
    with database.connect() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_key_list(practice_credits)").fetchall()
        assert any(row[2] == "practice_items" for row in foreign_keys)
        assert connection.execute("SELECT COUNT(*) FROM practice_credits").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    [backup] = (database.path.parent / "backups").glob("daily-ml-before-gap-upgrade-*.sqlite3")
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert any(row[2] == "practice_submissions"
                   for row in connection.execute("PRAGMA foreign_key_list(practice_credits)"))
        assert connection.execute("SELECT item_id FROM practice_credits").fetchall() == [(item.id,)]


def test_interrupted_credit_migration_preserves_legacy_schema_and_data(database, monkeypatch):
    """A later initialization error rolls back the whole legacy-credit migration."""
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    practice.submit(
        item.id, "An answer before the interrupted migration.", expected_version=0,
        resolve_settings=lambda _connection: (LONDON, None),
    )
    with database.connect(immediate=True) as connection:
        connection.execute("DROP INDEX practice_credits_by_date")
        connection.execute("ALTER TABLE practice_credits RENAME TO practice_credits_new")
        connection.execute(
            """CREATE TABLE practice_credits (
                question_id TEXT NOT NULL,
                local_date TEXT NOT NULL REFERENCES practice_days(local_date),
                item_id TEXT NOT NULL UNIQUE REFERENCES practice_submissions(item_id),
                FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id),
                PRIMARY KEY (question_id, local_date)
            )"""
        )
        connection.execute(
            "INSERT INTO practice_credits SELECT question_id, local_date, item_id FROM practice_credits_new"
        )
        connection.execute("DROP TABLE practice_credits_new")
        connection.execute("CREATE INDEX practice_credits_by_date ON practice_credits(local_date)")

    def interrupt_initialization(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated initialization interruption")

    monkeypatch.setattr("daily_ml.practice.initialize_reviews", interrupt_initialization)

    with pytest.raises(sqlite3.OperationalError, match="interruption"):
        service(database)

    with database.connect() as connection:
        foreign_keys = connection.execute("PRAGMA foreign_key_list(practice_credits)").fetchall()
        assert any(row[2] == "practice_submissions" for row in foreign_keys)
        assert connection.execute("SELECT item_id FROM practice_credits").fetchall() == [(item.id,)]


def test_gap_records_survive_sqlite_backup_and_restore(database, tmp_path):
    practice = service(database)
    item = practice.today(LONDON).selections[0].item
    practice.begin_gap(item.id)
    practice.acknowledge_gap(item.id, "backup-gap", resolve_timezone=london_timezone)

    restored = Database(tmp_path / "restored")
    with database.connect() as source, restored.connect() as destination:
        source.backup(destination)

    reopened = Practice(restored, answer_limit=8000, clock=lambda: MONDAY, choose=lambda candidates: candidates[0])
    saved = reopened.submission(item.id)
    assert saved["request"]["state"] == "self_reported_gap"
    with restored.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_help_events").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM practice_gap_completions").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM practice_gap_actions").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
