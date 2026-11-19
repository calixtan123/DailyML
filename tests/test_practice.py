"""Verify saved daily practice across connections, dates and content releases."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from random import Random
from threading import Barrier

import pytest

from daily_ml.bank import import_bank, validate_bank
from daily_ml.database import Database
from daily_ml.practice import DraftConflict, DraftInvalid, Practice

MONDAY = datetime(2026, 11, 16, 23, 59, tzinfo=timezone.utc)
TUESDAY = datetime(2026, 11, 17, 0, 1, tzinfo=timezone.utc)
LONDON = "Europe/London"


@pytest.fixture
def database(tmp_path):
    """Open a clean database with the complete reviewed starter bank."""
    db = Database(tmp_path / "live")
    import_bank(db, validate_bank())
    return db


def service(database, *, now=MONDAY, seed=4, due=()):
    """Control time, random selection and the source of review candidates."""
    return Practice(database, answer_limit=8000, clock=lambda: now, choose=Random(seed).choice,
                    due_concepts=lambda _connection, _date: due)


def test_first_access_and_restart_return_the_same_distinct_pair(database):
    monday = service(database).today(LONDON)

    restarted = service(Database(database.path.parent), seed=7).today(LONDON)

    assert restarted == monday
    assert monday.local_date == "2026-11-16"
    assert monday.goal == 2
    first, second = monday.selections
    assert first.kind == second.kind == "variety"
    assert first.item.question.id != second.item.question.id
    assert first.item.question.section_id != second.item.question.section_id
    assert first.item.question.concept_id != second.item.question.concept_id


def test_due_review_randomises_only_over_eligible_concepts(database):
    due = ("generalisation", "tree-diversity", "unknown-concept")
    choices = []

    def choose(candidates):
        choices.append([candidate.question_id for candidate in candidates])
        return candidates[-1]

    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=choose,
                        due_concepts=lambda _connection, _date: due)

    today = practice.today(LONDON)

    review, varied = today.selections
    assert choices[0] == ["generalisation", "tree-diversity"]
    assert review.kind == "review"
    assert review.item.question.id == "tree-diversity"
    assert varied.kind == "variety"
    assert varied.item.question.section_id != "forests"


def test_variety_favours_sections_without_recent_saved_work(database):
    monday = service(database)
    extra = monday.add_item(LONDON, question_id="backpropagation")
    monday.save_draft(extra.item_id, "Practised backpropagation.", expected_version=0, timezone=LONDON)

    tuesday = service(database, now=TUESDAY).today(LONDON)

    assert all(pick.item.question.section_id != "deep-learning" for pick in tuesday.selections)


def test_two_connections_open_one_day_once(database):
    first = service(database)
    second = service(Database(database.path.parent), seed=7)
    barrier = Barrier(2)

    def open_today(practice):
        barrier.wait(timeout=5)
        return practice.today(LONDON)

    with ThreadPoolExecutor(max_workers=2) as workers:
        left = workers.submit(open_today, first)
        right = workers.submit(open_today, second)

    assert left.result() == right.result()
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_items").fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM practice_days").fetchone() == (1,)


def test_partial_pair_is_rolled_back_when_selection_is_interrupted(database):
    calls = 0

    def interrupted(candidates):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted selection")
        return candidates[0]

    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=interrupted)
    with pytest.raises(RuntimeError, match="interrupted selection"):
        practice.today(LONDON)

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM practice_days").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM practice_items").fetchone() == (0,)
    assert len(service(database).today(LONDON).selections) == 2


def test_snapshot_and_draft_survive_new_content_midnight_and_sqlite_backup(database, tmp_path):
    started = service(database)
    today = started.today(LONDON)
    original = today.selections[0].item
    started.save_draft(original.id, "Monday answer", expected_version=0, timezone=LONDON)
    bank = validate_bank()
    old_record = next(question for question in bank.questions if question.id == original.question.id)
    new_record = old_record.model_copy(update={
        "version": 2, "prompt": "An updated prompt.",
        "criteria": tuple(criterion.model_copy(update={"description": "Updated criterion."})
                          for criterion in old_record.criteria),
        "sources": tuple(source.model_copy(update={"title": "Updated source."}) for source in old_record.sources),
    })
    import_bank(database, bank.model_copy(update={"release": False, "questions": (new_record,)}))
    backup = Database(tmp_path / "backup")
    restored = Database(tmp_path / "restored")

    with database.connect() as source, backup.connect() as destination:
        source.backup(destination)
    with backup.connect() as source, restored.connect() as destination:
        source.backup(destination)
    tomorrow = service(restored, now=TUESDAY)
    tuesday = tomorrow.today(LONDON)
    visible_items = [selection.item for selection in tuesday.selections] + tuesday.unfinished
    resumed = next(item for item in visible_items if item.id == original.id)
    saved = tomorrow.save_draft(original.id, "Tuesday edit", expected_version=1, timezone=LONDON)

    assert len(tuesday.selections) == 2
    assert tuesday.local_date == "2026-11-17"
    assert resumed.local_date == "2026-11-16"
    assert resumed.question == original.question
    assert resumed.draft.text == "Monday answer"
    assert saved.version == 2
    with restored.connect() as connection:
        stored = connection.execute(
            "SELECT snapshot_json FROM practice_items WHERE id = ?", (original.id,)
        ).fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert json.loads(stored) == old_record.model_dump(mode="json")


def test_extra_and_focused_practice_save_and_resume_without_duplicate_items(database):
    practice = service(database)
    today = practice.today(LONDON)

    extra = practice.add_item(LONDON)
    focused = practice.add_item(LONDON, section_id="deployment")
    again = practice.add_item(LONDON, section_id="deployment")
    selected = practice.add_item(LONDON, question_id="batch-vs-online")

    assert extra.item_id not in {choice.item.id for choice in today.selections}
    assert extra.today.selections[2].kind == "extra"
    assert focused.item_id == again.item_id == selected.item_id
    picked = next(choice.item for choice in selected.today.selections if choice.item.id == selected.item_id)
    assert picked.question.section_id == "deployment"
    assert sum(choice.item.question.id == "batch-vs-online" for choice in selected.today.selections) == 1
    assert practice.today(LONDON) == selected.today


def test_shortage_keeps_the_goal_and_explains_exhaustion(database):
    bank = validate_bank()
    remaining = bank.questions[0].id
    retired = tuple(
        question.model_copy(update={"retired": True, "version": 2})
        for question in bank.questions if question.id != remaining
    )
    import_bank(database, bank.model_copy(update={"release": False, "questions": retired}))
    practice = service(database)

    today = practice.today(LONDON)
    extra = practice.add_item(LONDON)

    assert today.goal == 2
    assert len(today.selections) == 1
    assert today.shortage
    assert extra.item_id is None
    assert extra.message
    assert extra.today == today


def test_two_connection_draft_race_preserves_the_winner(database):
    first = service(database)
    second = service(Database(database.path.parent))
    item_id = first.today(LONDON).selections[0].item.id
    barrier = Barrier(2)

    def save(practice, text):
        barrier.wait(timeout=5)
        try:
            return practice.save_draft(item_id, text, expected_version=0, timezone=LONDON)
        except DraftConflict as conflict:
            return conflict

    with ThreadPoolExecutor(max_workers=2) as workers:
        left = workers.submit(save, first, "First tab")
        right = workers.submit(save, second, "Second tab")

    result = [left.result(), right.result()]
    conflict, = [value for value in result if isinstance(value, DraftConflict)]
    saved, = [value for value in result if not isinstance(value, DraftConflict)]
    assert saved.version == 1
    assert conflict.saved == saved
    assert first.today(LONDON).selections[0].item.draft == saved


def test_unicode_code_point_limit_rejects_oversize_without_changing_saved_text(database):
    practice = service(database)
    item_id = practice.today(LONDON).selections[0].item.id
    text = "🧪" * 8000

    saved = practice.save_draft(item_id, text, expected_version=0, timezone=LONDON)
    with pytest.raises(DraftInvalid):
        practice.save_draft(item_id, text + "é", expected_version=1, timezone=LONDON)
    with pytest.raises(DraftInvalid):
        practice.save_draft(item_id, "\ud800", expected_version=1, timezone=LONDON)

    assert saved.text == text
    assert practice.today(LONDON).selections[0].item.draft == saved


def test_learning_timezone_controls_rollover_and_keeps_original_session_timezone(database):
    practice = service(database)
    london = practice.today(LONDON)

    paris = practice.today("Europe/Paris")
    returned = service(database, now=TUESDAY).today(LONDON)

    assert london.local_date == "2026-11-16"
    assert paris.local_date == "2026-11-17"
    assert returned.selections == paris.selections
    assert returned.timezone == "Europe/Paris"
    assert returned.effective_timezone == LONDON


def test_long_imported_editorial_snapshot_can_be_submitted_after_bank_update(database):
    bank = validate_bank()
    source = bank.questions[0]
    prompt = "Practice prompt. " * 130
    reference = "Reference explanation. " * 100
    description = "Check the explanation. " * 100
    long_record = source.model_copy(update={
        "version": 2, "prompt": prompt.strip(), "reference_explanation": reference.strip(),
        "criteria": (source.criteria[0].model_copy(update={"description": description.strip()}),
                     *source.criteria[1:]),
    })
    import_bank(database, bank.model_copy(update={"release": False, "questions": (long_record,)}))
    practice = service(database)
    selected = practice.add_item(LONDON, question_id=source.id)
    shorter = source.model_copy(update={"version": 3})
    import_bank(database, bank.model_copy(update={"release": False, "questions": (shorter,)}))

    original = "  My exact, independently entered answer.  "
    result = service(database).submit(
        selected.item_id, original, expected_version=0, resolve_settings=lambda _db: (LONDON, None),
    )

    assert result.submission["text"] == original
    assert result.submission["credited"] is True
    assert result.submission["request"]["state"] == "awaiting_access"
    assert result.today.completion_counts == {"2026-11-16": 1}
    with database.connect() as connection:
        row = connection.execute(
            "SELECT kind, context_json FROM grading_requests WHERE id = ?",
            (result.submission["request"]["id"],),
        ).fetchone()
    assert row[0] == "practice"
    context = json.loads(row[1])
    assert context["version"] == "v2"
    assert context["question"] == prompt.strip()
    assert context["reference"] == reference.strip()
    assert context["criteria"][0]["description"] == description.strip()
    assert context["answer"] == original


def test_focused_section_resumes_unfinished_draft_after_another_question_is_submitted(database):
    practice = service(database)
    finished = practice.add_item(LONDON, question_id="forest-mechanics").item_id
    unfinished = practice.add_item(LONDON, question_id="tree-diversity").item_id
    practice.submit(finished, "Original.", expected_version=0, resolve_settings=lambda _db: (LONDON, None))
    practice.save_draft(unfinished, "An unfinished answer.", expected_version=0, timezone=LONDON)

    focused = practice.add_item(LONDON, section_id="forests")

    assert focused.item_id == unfinished
    assert focused.message is None
    item = next(choice.item for choice in focused.today.selections if choice.item.id == unfinished)
    assert item.draft.text == "An unfinished answer."


def test_focused_section_reports_when_no_further_questions_are_editable(database):
    practice = service(database)
    for question_id in ("forest-mechanics", "tree-diversity"):
        item_id = practice.add_item(LONDON, question_id=question_id).item_id
        practice.submit(item_id, "Original.", expected_version=0, resolve_settings=lambda _db: (LONDON, None))

    focused = practice.add_item(LONDON, section_id="forests")

    assert focused.item_id is None
    assert "no more editable questions" in focused.message.lower()
    assert focused.today.goal == 2
