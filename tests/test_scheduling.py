"""Exercise the review policy with hand-calculated dates before persistence."""

from datetime import UTC, date, datetime

import pytest

from daily_ml.scheduling import LearningEvent, ReviewState, calculate_review, replay_reviews

INTERVALS = (1, 3, 7, 14, 30)


def evidence(day, grade, *, event_id="answer", accepted=None, supported=False, timestamp=None):
    """Build one immutable original with an independently specified date."""
    submitted = date.fromisoformat(day)
    instant = timestamp or f"{day}T12:00:00+00:00"
    return LearningEvent(
        event_id, datetime.fromisoformat(instant).astimezone(UTC).timestamp(),
        submitted, grade, supported, date.fromisoformat(accepted or day),
    )


def test_independent_perfect_reviews_progress_and_cap_at_stage_four():
    state = ReviewState()
    for submitted, stage, due in [
        ("2026-11-17", 1, "2026-11-18"),
        ("2026-11-18", 2, "2026-11-21"),
        ("2026-11-21", 3, "2026-11-28"),
        ("2026-11-28", 4, "2026-12-12"),
        ("2026-12-12", 4, "2027-01-11"),
        ("2027-01-11", 4, "2027-02-10"),
    ]:
        state = calculate_review(state, evidence(submitted, 3), False, INTERVALS)
        assert state == ReviewState(stage, date.fromisoformat(due))


@pytest.mark.parametrize(("grade", "supported", "due"), [
    (0, False, "2026-11-18"),
    (1, False, "2026-11-18"),
    (2, False, "2026-11-20"),
    (2, True, "2026-11-18"),
    (3, True, "2026-11-18"),
    (None, False, "2026-11-18"),  # Viewed, ungraded self-reported gap.
])
def test_non_independent_or_incomplete_outcomes_reset_progression(grade, supported, due):
    state = calculate_review(ReviewState(4, date(2026, 11, 30)),
                             evidence("2026-11-17", grade, supported=supported), False, INTERVALS)
    assert state == ReviewState(0, date.fromisoformat(due))


def test_clamp_overdue_but_keep_unexpired_dates_anchored_to_submission():
    state = ReviewState()
    overdue = evidence("2026-11-17", 3, accepted="2026-11-24")
    unexpired = evidence("2026-11-17", 2, accepted="2026-11-18")
    assert calculate_review(state, overdue, False, INTERVALS) == ReviewState(1, date(2026, 11, 24))
    assert calculate_review(state, unexpired, False, INTERVALS) == ReviewState(0, date(2026, 11, 20))


def test_replay_can_use_an_interval_that_extends_past_the_last_representable_date():
    """An accepted interval cannot interrupt replay when its due date is out of range."""
    events = [evidence(f"2026-11-{day}", 3, event_id=str(day)) for day in range(17, 22)]

    state = replay_reviews(events, (1, 3, 7, 14, 3_000_000))

    assert state == ReviewState(4, date.max)


def test_same_day_perfect_keeps_date_and_late_weaker_evidence_cannot_move_it_later():
    first = evidence("2026-11-17", 3, event_id="a")
    perfect = evidence("2026-11-17", 3, event_id="b", accepted="2026-11-21")
    weaker = evidence("2026-11-17", 2, event_id="c", accepted="2026-11-22")
    assert replay_reviews([perfect, first], INTERVALS) == ReviewState(1, date(2026, 11, 18))
    assert replay_reviews([weaker, perfect, first], INTERVALS) == ReviewState(0, date(2026, 11, 18))


@pytest.mark.parametrize("grade,supported", [(0, False), (1, False), (None, False), (3, True)])
def test_same_day_gap_or_help_preserves_earlier_due_date_and_resets(grade, supported):
    first = evidence("2026-11-17", 3, event_id="a")
    weaker = evidence("2026-11-17", grade, event_id="b", accepted="2026-11-24", supported=supported)
    assert replay_reviews([first, weaker], INTERVALS) == ReviewState(0, date(2026, 11, 18))


def test_revisiting_same_civil_date_after_timezone_change_cannot_advance_twice():
    first = evidence("2026-11-17", 3, event_id="a", timestamp="2026-11-17T06:00:00+00:00")
    next_day = evidence("2026-11-18", 3, event_id="b", timestamp="2026-11-17T13:00:00+00:00")
    previous_day_elsewhere = evidence("2026-11-17", 3, event_id="c", timestamp="2026-11-17T14:00:00+00:00")
    assert replay_reviews([next_day, previous_day_elsewhere, first], INTERVALS) == ReviewState(2, date(2026, 11, 21))


def test_replay_orders_by_utc_instant_before_stable_id():
    early = evidence("2026-11-17", 2, event_id="z", timestamp="2026-11-17T05:30:00-05:00")
    late = evidence("2026-11-17", 3, event_id="a", timestamp="2026-11-17T14:00:00+00:00")
    assert replay_reviews([late, early], INTERVALS) == ReviewState(0, date(2026, 11, 20))


def test_empty_review_evidence_has_no_stage_progress_or_due_date():
    assert replay_reviews([], INTERVALS) == ReviewState()
