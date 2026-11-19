"""Verify stable, snapshot-backed history index queries."""

from datetime import datetime, timezone
from random import Random

import pytest

from daily_ml.bank import import_bank, validate_bank
from daily_ml.database import Database
from daily_ml.history import History, HistoryCursorInvalid
from daily_ml.practice import Practice

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
    page = History(database).list()

    assert page.entries == ()
    assert page.next_cursor is None


def test_history_paginates_tied_timestamps_with_item_id(database):
    """A cursor cannot skip or duplicate entries created at the same instant."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    items = practice.today(LONDON).selections
    addition = practice.add_item(LONDON, question_id="backpropagation")

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
    tuesday.add_item(LONDON, question_id="backpropagation")

    page = History(database).list(section_id="deep-learning", start_date="2026-11-17", end_date="2026-11-17")

    assert [(entry.question_id, entry.local_date) for entry in page.entries] == [
        ("backpropagation", "2026-11-17"),
    ]


def test_history_lists_sections_from_saved_practice_items(database):
    """Section filters list the immutable metadata recorded by saved items."""
    practice = Practice(database, answer_limit=8000, clock=lambda: MONDAY, choose=Random(4).choice)
    practice.today(LONDON)

    sections = History(database).sections()

    assert {(section.id, section.title) for section in sections} == {
        ("fundamentals", "Fundamentals"),
        ("other-ensembles", "Other ensembles"),
    }


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
    assert entries[first.id].draft_text == "Saved to finish later."
    assert entries[second.id].status == "pending_assessment"
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
