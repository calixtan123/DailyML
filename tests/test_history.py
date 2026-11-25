"""Verify stable, snapshot-backed history index queries."""

from dataclasses import asdict
from datetime import datetime, timezone
from random import Random

import pytest

from daily_ml.bank import import_bank, validate_bank
from daily_ml.database import Database
from daily_ml.history import History, HistoryCursorInvalid
from daily_ml.practice import Practice, PracticeItemNotFound
from test_local_app import assets
from test_provider import provider, response_stream
from test_submissions import _configure_model, _needs_clarification, _retry, _submit, _today, _valid_terminal, learner

LONDON = "Europe/London"
MONDAY = datetime(2026, 11, 16, 23, 59, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path):
    """Create the saved practice schema with the reviewed starter bank."""
    database = Database(tmp_path / "live")
    import_bank(database, validate_bank())
    Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    return database


def test_history_returns_empty_page_before_practice_starts(database):
    """History has a usable empty result before the learner saves work."""
    today = Practice(database, answer_limit=8000, clock=lambda: MONDAY).today(LONDON)
    with pytest.raises(PracticeItemNotFound):
        History(database).detail(today.selections[0].item.id)
    assert History(database).sections() == ()
    page = History(database).list()

    assert page.entries == ()
    assert page.next_cursor is None


def test_history_paginates_tied_timestamps_with_item_id(database):
    """A cursor cannot skip or duplicate entries created at the same instant."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    items = practice.today(LONDON).selections
    addition = practice.add_item(LONDON, question_id="backpropagation")

    for item_id in [selection.item.id for selection in items] + [addition.item_id]:
        practice.view_help(item_id, "explanation")

    first_page = History(database).list(limit=2)
    second_page = History(database).list(cursor=first_page.next_cursor)

    actual_ids = [entry.item_id for entry in (*first_page.entries, *second_page.entries)]
    expected_ids = [item.item.id for item in items] + [addition.item_id]
    assert set(actual_ids) == set(expected_ids)
    assert len(actual_ids) == len(set(actual_ids)) == 3
    assert second_page.next_cursor is None


def test_history_filters_saved_section_and_inclusive_practice_dates(database):
    """Filters use saved practice metadata rather than active bank content."""
    monday = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    monday.today(LONDON)
    later = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    tuesday = Practice(database, answer_limit=8000, clock=lambda: later, choose=Random(9).choice)
    chosen = tuesday.add_item(LONDON, question_id="backpropagation")
    tuesday.view_help(chosen.item_id, "explanation")

    page = History(database).list(section_id="deep-learning", start_date="2026-11-17", end_date="2026-11-17")

    assert [(entry.question_id, entry.local_date) for entry in page.entries] == [
        ("backpropagation", "2026-11-17"),
    ]


def test_history_lists_sections_from_saved_practice_items(database):
    """Section filters list the immutable metadata recorded by saved items."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    for selection in practice.today(LONDON).selections:
        practice.view_help(selection.item.id, "explanation")

    sections = History(database).sections()

    assert {(section.id, section.title) for section in sections} == {
        ("fundamentals", "Fundamentals"),
        ("other-ensembles", "Other ensembles"),
    }


def test_calendar_finds_an_older_question_by_its_completion_date(database):
    """A resumed answer appears on the day that earned calendar credit."""
    monday = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    item = monday.today(LONDON).selections[0].item
    monday.view_help(item.id, "hint")
    later = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    tuesday = Practice(database, answer_limit=8000, clock=lambda: later)
    tuesday.submit(item.id, "Saved on a later date.", expected_version=0,
                   resolve_settings=lambda _connection: (LONDON, "gpt-test"))
    with database.connect() as connection:
        connection.execute("UPDATE grading_requests SET error='unsent' WHERE kind='practice'")

    history = History(database)
    entry, = history.list(activity_date="2026-11-24").entries
    assert entry.item_id == item.id
    assert entry.local_date == "2026-11-16"
    assert entry.status == "ready_to_send"
    assert history.detail(item.id)["support"]["reveals"] == ["hint"]
    assert history.list(start_date="2026-11-24").entries == ()
    assert tuesday.today(LONDON).completion_counts == {"2026-11-24": 1}


def test_history_reports_saved_drafts_and_original_assessment_state(database):
    """Index status comes from the durable draft and request records."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    first, second = (selection.item for selection in practice.today(LONDON).selections)
    practice.save_draft(first.id, "Saved to finish later.", expected_version=0, timezone=LONDON)
    practice.submit(
        second.id,
        "An answer to assess.",
        expected_version=0,
        resolve_settings=lambda _connection: (LONDON, None),
    )

    entries = {entry.item_id: entry for entry in History(database).list().entries}

    assert entries[first.id].status == "unfinished"
    assert "draft_text" not in asdict(entries[first.id])
    assert History(database).detail(first.id)["draft"]["text"] == "Saved to finish later."
    assert entries[second.id].status == "awaiting_access"
    assert entries[second.id].completed_date == "2026-11-16"


@pytest.mark.parametrize("cursor", ("not-a-cursor", "eyJ0IjoxfQ"))
def test_history_rejects_malformed_or_stale_cursor(database, cursor):
    """A malformed cursor produces a specific recoverable query error."""
    with pytest.raises(HistoryCursorInvalid):
        History(database).list(cursor=cursor)


def test_history_detail_reads_saved_question_and_resumable_draft(database):
    """The exact stored question and draft remain available independently of selection."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    item = practice.today(LONDON).selections[0].item
    practice.save_draft(item.id, "A saved draft.", expected_version=0, timezone=LONDON)

    saved = History(database).detail(item.id)

    assert saved["question"]["prompt"] == item.question.prompt
    assert saved["question"]["version"] == item.question.version
    assert saved["question"]["criteria"]
    assert saved["draft"] == {"text": "A saved draft.", "version": 1}
    assert saved["submission"] is None
    assert saved["item"]["id"] == item.id


def test_history_detail_includes_saved_submission_evidence(database):
    """A completed item's exact original and version are present before grading access."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    item = practice.today(LONDON).selections[0].item
    practice.submit(item.id, "A saved answer.", expected_version=0, resolve_settings=lambda _: (LONDON, None))

    saved = History(database).detail(item.id)

    assert saved["submission"]["completed_date"] == "2026-11-16"
    assert saved["submission"]["text"] == saved["submission"]["versions"][0]["text"] == "A saved answer."
    assert saved["assessed_at"] is None
    assert saved["item"]["submitted"] is True


def test_history_help_only_and_cross_day_gap_completion(database):
    """Reveal events start history and a later acknowledgement preserves both dates."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY)
    item = practice.today(LONDON).selections[0].item
    practice.begin_gap(item.id)
    history = History(database)
    entry, = history.list().entries
    assert entry.status == "support_viewed"
    assert entry.completed_date is None
    assert history.detail(item.id)["support"] == {"reveals": ["gap_explanation"], "gap_completed": False}

    tuesday = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    practice = Practice(database, answer_limit=8000, clock=lambda: tuesday)
    practice.acknowledge_gap(item.id, "gap-action", resolve_timezone=lambda _: LONDON)
    entry, = history.list().entries
    assert (entry.status, entry.completed_date, entry.local_date) == ("self_reported_gap", "2026-11-17", "2026-11-16")
    detail = history.detail(item.id)
    assert detail["submission"]["request"]["state"] == "self_reported_gap"
    assert detail["submission"]["completed_date"] == entry.completed_date
    assert detail["support"] == {"reveals": ["gap_explanation"], "gap_completed": True}
    assert detail["assessed_at"] is None


def test_history_section_rename_uses_latest_saved_label(database):
    """One filter option covers attempts made before and after a section rename."""
    monday = Practice(database, answer_limit=8000, clock=lambda: MONDAY)
    first = monday.add_item(LONDON, question_id="backpropagation")
    monday.submit(first.item_id, "First answer.", expected_version=0, resolve_settings=lambda _: (LONDON, None))
    with database.connect() as connection:
        connection.execute("UPDATE bank_sections SET title = 'Updated title' WHERE id = 'deep-learning'")
    later = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    tuesday = Practice(database, answer_limit=8000, clock=lambda: later)
    second = tuesday.add_item(LONDON, question_id="backpropagation")
    tuesday.view_help(second.item_id, "explanation")

    history = History(database)
    section, = history.sections()
    assert (section.id, section.title) == ("deep-learning", "Updated title")
    entries = history.list(section_id=section.id).entries
    assert [entry.item_id for entry in entries] == [second.item_id, first.item_id]
    assert entries[1].section_title != section.title


@pytest.mark.parametrize("invalid", ("20261116", "2026-W47-1", "2026-02-31"))
def test_history_rejects_non_calendar_date_filters(database, invalid):
    """Filter values follow the calendar input format."""
    with pytest.raises(HistoryCursorInvalid):
        History(database).list(start_date=invalid)


def test_bank_update_and_retirement_preserve_gap_and_resumable_draft(database):
    """Import changed content twice and reopen the original evidence without signing in."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY)
    gap, draft = [selection.item for selection in practice.today(LONDON).selections]
    practice.begin_gap(gap.id)
    practice.acknowledge_gap(gap.id, "record-gap", resolve_timezone=lambda _: LONDON)
    practice.save_draft(draft.id, "A saved draft.", expected_version=0, timezone=LONDON)
    history = History(database)
    before = {item.id: history.detail(item.id) for item in (gap, draft)}
    credit = practice.today(LONDON).completion_counts

    bank = validate_bank()
    for version, retired in ((2, False), (3, True)):
        changes = tuple(
            question.model_copy(update={"version": version, "retired": retired, "prompt": "Updated question?"})
            for question in bank.questions if question.id in {gap.question.id, draft.question.id}
        )
        import_bank(database, bank.model_copy(update={"release": False, "questions": changes}))

        assert history.detail(gap.id) == before[gap.id]
        assert history.detail(draft.id) == before[draft.id]
        assert practice.saved_item(draft.id).draft.text == "A saved draft."
        assert practice.today(LONDON).completion_counts == credit


def test_history_api_before_sign_in_resumes_exact_retired_work(learner):
    """Local routes expose saved work and reject malformed filters before sign-in."""
    client, headers, database, _ = learner
    first, second = [entry["item"] for entry in _today(client, headers)["selections"]]
    item_id = first["id"]
    practice_path = f"/api/practice/items/{item_id}"
    assert client.get("/api/history").json() == {"entries": [], "next_cursor": None}
    assert client.get(f"/api/history/{second['id']}").status_code == 404
    assert client.put(f"{practice_path}/draft", headers=headers,
                      json={"text": "Resume this version.", "version": 0}).status_code == 200
    bank = validate_bank()
    original = next(q for q in bank.questions if q.id == first["question"]["id"])
    retired = original.model_copy(update={"version": 2, "retired": True, "prompt": "Updated prompt."})
    import_bank(database, bank.model_copy(update={"release": False, "questions": (retired,)}))

    section, = client.get("/api/history/sections").json()
    page = client.get("/api/history", params={"section_id": section["id"], "limit": 1}).json()
    entry, = page["entries"]
    assert (entry["item_id"], entry["status"]) == (item_id, "unfinished")
    assert "draft_text" not in entry
    detail = client.get(f"/api/history/{item_id}").json()
    assert detail["question"]["prompt"] == original.prompt
    assert detail["draft"]["text"] == "Resume this version."
    assert client.get(practice_path).json()["draft"] == detail["draft"]
    assert client.get("/api/history", params={"cursor": "invalid"}).status_code == 422
    assert client.get("/api/history", params={"start_date": "2026-03-29", "end_date": "2026-03-28"}).status_code == 422


def test_history_api_reads_feedback_for_each_clarification_version(learner):
    """Retain the prior clarification explanation after the next version receives a grade."""
    client, headers, database, _ = learner
    _configure_model(database)
    calls = []

    def assess(request):
        calls.append(request)
        if len(calls) == 1:
            return _needs_clarification("Which data is held out?")
        return response_stream(_valid_terminal(request))

    client.app.state.grading.provider = provider(assess)
    item = _today(client, headers)["selections"][0]["item"]
    submission = _submit(client, headers, item["id"], "Use separate data.").json()["submission"]
    assert _retry(client, headers, item["id"], submission["request"]["id"],
                  "Hold out test data from tuning.").status_code == 202
    detail = client.get(f"/api/history/{item['id']}").json()
    first, second = detail["submission"]["versions"]
    assert first["text"] == "Use separate data."
    assert first["request"]["feedback"]["explanation"] == "Which data is held out?"
    assert second["text"] == "Hold out test data from tuning."
    assert second["parent_version_id"] == first["id"]
    assert second["request"]["feedback"]["status"] == "assessed"
    assert detail["assessed_at"] == second["request"]["finished_at"]
