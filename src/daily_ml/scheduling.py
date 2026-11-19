"""Derive concept reviews from accepted evidence and saved learner requests."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

NEXT_DAY = timedelta(days=1)
MOSTLY_CORRECT_DELAY = timedelta(days=3)
INDEPENDENT_CORRECT_GRADE = 3
MOSTLY_CORRECT_GRADE = 2

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS practice_submission_support (
        item_id TEXT PRIMARY KEY REFERENCES practice_submissions(item_id),
        supported INTEGER NOT NULL CHECK (supported IN (0, 1))
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS original_support_immutable
    BEFORE UPDATE ON practice_submission_support
    BEGIN
        SELECT RAISE(ABORT, 'Original pre-answer support is immutable');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS review_evidence (
        id TEXT PRIMARY KEY,
        original_item_id TEXT NOT NULL REFERENCES practice_items(id),
        grading_request_id TEXT UNIQUE REFERENCES grading_requests(id),
        concept_id TEXT NOT NULL,
        submitted_at REAL NOT NULL,
        submitted_date TEXT NOT NULL,
        grade INTEGER CHECK (grade BETWEEN 0 AND 3),
        supported INTEGER NOT NULL CHECK (supported IN (0, 1)),
        accepted_date TEXT NOT NULL,
        CHECK ((grading_request_id IS NULL) = (grade IS NULL)),
        UNIQUE (original_item_id, id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_evidence_immutable
    BEFORE UPDATE ON review_evidence
    BEGIN
        SELECT RAISE(ABORT, 'Accepted learning evidence is immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS review_evidence_retained
    BEFORE DELETE ON review_evidence
    BEGIN
        SELECT RAISE(ABORT, 'Accepted learning evidence must be retained');
    END
    """,
    "CREATE INDEX IF NOT EXISTS review_evidence_by_concept ON review_evidence(concept_id)",
    """
    CREATE TABLE IF NOT EXISTS review_evidence_selection (
        original_item_id TEXT PRIMARY KEY,
        evidence_id TEXT NOT NULL,
        FOREIGN KEY (original_item_id, evidence_id) REFERENCES review_evidence(original_item_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS concept_review_state (
        concept_id TEXT PRIMARY KEY,
        stage INTEGER NOT NULL CHECK (stage BETWEEN 0 AND 4),
        evidence_due_date TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_sooner_requests (
        idempotency_key TEXT PRIMARY KEY,
        concept_id TEXT NOT NULL,
        requested_at REAL NOT NULL,
        due_date TEXT NOT NULL
    )
    """,
)

_ORIGINAL_ASSESSMENTS = """
    SELECT submission.item_id, request.id, json_extract(item.snapshot_json, '$.concept_id'),
           submission.completed_at, submission.completed_date, request.grade,
           COALESCE(support.supported, 0), request.finished_at, submission.completed_timezone
    FROM practice_submissions AS submission
    JOIN practice_items AS item ON item.id = submission.item_id
    JOIN grading_requests AS request ON request.id = submission.current_request_id
    LEFT JOIN practice_submission_support AS support ON support.item_id = submission.item_id
    WHERE request.state = 'assessed' AND request.grade IS NOT NULL AND request.finished_at IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM review_evidence AS evidence WHERE evidence.original_item_id = submission.item_id
      )
"""


@dataclass(frozen=True)
class LearningEvent:
    """One eligible original's frozen time, date, outcome and support.

    A grade of None means a completed self-reported gap. An unaccepted
    assessment is absent from the evidence.
    """

    original_item_id: str
    submitted_at: float
    submitted_date: date
    grade: int | None
    supported: bool
    accepted_date: date


@dataclass(frozen=True)
class ReviewState:
    """A concept's progression and current effective review date."""

    stage: int = 0
    due_date: date | None = None
    awaiting_assessment: bool = False


def calculate_review(
    state: ReviewState, event: LearningEvent, already_applied_on_submission_date: bool,
    intervals: tuple[int, ...],
) -> ReviewState:
    """Apply one accepted original to the current evidence-derived state.

    Parameters
    ----------
    state : ReviewState
        State from the preceding originals in submission order.
    event : LearningEvent
        Next eligible original with frozen date and support.
    already_applied_on_submission_date : bool
        Whether the concept has earlier eligible evidence on this local date.
    intervals : tuple of int
        Configured delays for successive independently correct answers.

    Returns
    -------
    ReviewState
        The recomputed stage and due date after this original.
    """
    independent_correct = event.grade == INDEPENDENT_CORRECT_GRADE and not event.supported
    if already_applied_on_submission_date and independent_correct:
        return state
    if independent_correct:
        delay_days = intervals[state.stage]
        stage = min(state.stage + 1, len(intervals) - 1)
    else:
        mostly_correct = event.grade == MOSTLY_CORRECT_GRADE and not event.supported
        delay_days = MOSTLY_CORRECT_DELAY.days if mostly_correct else NEXT_DAY.days
        stage = 0
    remaining_days = (date.max - event.submitted_date).days
    scheduled_date = event.submitted_date + timedelta(days=min(delay_days, remaining_days))
    due_date = max(scheduled_date, event.accepted_date)
    if already_applied_on_submission_date and state.due_date is not None:
        due_date = min(due_date, state.due_date)
    return ReviewState(stage, due_date)


def replay_reviews(events: Iterable[LearningEvent], intervals: tuple[int, ...]) -> ReviewState:
    """Replay the selected originals for one concept in stable UTC order.

    Parameters
    ----------
    events : iterable of LearningEvent
        One currently eligible result per original item.
    intervals : tuple of int
        Configured delays for independently correct answers.

    Returns
    -------
    ReviewState
        Current stage and evidence-derived due date, if available.
    """
    state = ReviewState()
    submitted_dates: set[date] = set()
    for event in sorted(events, key=lambda event: (event.submitted_at, event.original_item_id)):
        state = calculate_review(state, event, event.submitted_date in submitted_dates, intervals)
        submitted_dates.add(event.submitted_date)
    return state


def initialize_reviews(connection: sqlite3.Connection, intervals: tuple[int, ...]) -> None:
    """Create review records and recover accepted pre-scheduler assessments.

    Parameters
    ----------
    connection : sqlite3.Connection
        The caller's locked schema transaction.
    intervals : tuple of int
        Configured intervals for replay.

    Raises
    ------
    sqlite3.Error
        If the additive migration or backfill cannot commit.
    """
    for statement in _SCHEMA:
        connection.execute(statement)
    for entry in connection.execute(_ORIGINAL_ASSESSMENTS).fetchall():
        _insert_assessment(connection, entry, entry[-1])
    for concept_id, in connection.execute("SELECT DISTINCT concept_id FROM review_evidence").fetchall():
        recompute_concept(connection, concept_id, intervals)


def _insert_assessment(connection: sqlite3.Connection, entry: tuple, acceptance_timezone: str) -> str:
    """Save and select the first assessment tied to this original."""
    item_id, request_id, concept_id, timestamp, local_date, grade, supported, finished_at, _ = entry
    accepted_date = datetime.fromtimestamp(finished_at, UTC).astimezone(ZoneInfo(acceptance_timezone)).date()
    connection.execute(
        """INSERT INTO review_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, item_id, request_id, concept_id, timestamp, local_date, grade, supported, accepted_date.isoformat()),
    )
    connection.execute("INSERT INTO review_evidence_selection VALUES (?, ?)", (item_id, request_id))
    return concept_id


def record_initial_assessment(
    connection: sqlite3.Connection, request_id: str, acceptance_timezone: str, intervals: tuple[int, ...],
) -> None:
    """Record first accepted grading evidence within the grading transaction.

    Parameters
    ----------
    connection : sqlite3.Connection
        The transaction that has just finalized feedback.
    request_id : str
        The accepted request ID. Clarification names its original via the
        current request; revisions and already accepted originals are skipped.
    acceptance_timezone : str
        Effective learning timezone at acceptance.
    intervals : tuple of int
        Configured intervals for replay.
    """
    original = connection.execute(_ORIGINAL_ASSESSMENTS + " AND request.id = ?", (request_id,)).fetchone()
    if original is not None:
        concept_id = _insert_assessment(connection, original, acceptance_timezone)
        recompute_concept(connection, concept_id, intervals)


def record_gap(
    connection: sqlite3.Connection, item_id: str, completed_at: float, completed_date: str,
    intervals: tuple[int, ...],
) -> None:
    """Select a viewed self-reported gap as this original's next-day evidence.

    Parameters
    ----------
    connection : sqlite3.Connection
        The caller's transaction recording explanation-view completion.
    item_id : str
        Stable practice item identity. Repeated calls for the same original
        preserve the first completion.
    completed_at : float
        Explanation-view timestamp as UTC seconds since the Unix epoch.
    completed_date : str
        Frozen local date of the explanation view in ISO format.
    intervals : tuple of int
        Configured intervals for replay.

    Raises
    ------
    LookupError
        If the practice item is missing.
    """
    item = connection.execute(
        "SELECT json_extract(snapshot_json, '$.concept_id') FROM practice_items WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        raise LookupError("The practice item is unavailable.")
    concept_id, = item
    inserted = connection.execute(
        """INSERT INTO review_evidence
           SELECT ?, ?, NULL, ?, ?, ?, NULL, 0, ?
           WHERE NOT EXISTS (SELECT 1 FROM review_evidence WHERE original_item_id = ?)""",
        (item_id, item_id, concept_id, completed_at, completed_date, completed_date, item_id),
    )
    if inserted.rowcount:
        connection.execute("INSERT INTO review_evidence_selection VALUES (?, ?)", (item_id, item_id))
        recompute_concept(connection, concept_id, intervals)


def select_review_evidence(
    connection: sqlite3.Connection, original_item_id: str, evidence_id: str | None,
    intervals: tuple[int, ...],
) -> None:
    """Exclude or reselect evidence for one original and replay the concept.

    Parameters
    ----------
    connection : sqlite3.Connection
        The transaction changing the original's eligibility.
    original_item_id : str
        Original practice identity whose evidence is changing.
    evidence_id : str or None
        A saved evidence ID for that original, or None to exclude it.
    intervals : tuple of int
        Configured intervals for replay.

    Raises
    ------
    LookupError
        The original has no saved evidence, or the requested evidence belongs
        to a different original or is unavailable.
    """
    row = connection.execute(
        """SELECT concept_id FROM review_evidence
           WHERE original_item_id = ? AND (? IS NULL OR id = ?) LIMIT 1""",
        (original_item_id, evidence_id, evidence_id),
    ).fetchone()
    if row is None:
        raise LookupError("Accepted evidence is unavailable for this original.")
    if evidence_id is None:
        connection.execute("DELETE FROM review_evidence_selection WHERE original_item_id = ?", (original_item_id,))
    else:
        connection.execute(
            """INSERT INTO review_evidence_selection VALUES (?, ?)
               ON CONFLICT(original_item_id) DO UPDATE SET evidence_id = excluded.evidence_id""",
            (original_item_id, evidence_id),
        )
    recompute_concept(connection, row[0], intervals)


def recompute_concept(connection: sqlite3.Connection, concept_id: str, intervals: tuple[int, ...]) -> None:
    """Replace a concept's evidence-derived stage/date in the caller's transaction.

    Parameters
    ----------
    connection : sqlite3.Connection
        The transaction selecting this concept's eligible evidence.
    concept_id : str
        Stable primary concept identity.
    intervals : tuple of int
        Configured intervals for replay.
    """
    selected = connection.execute(
        """SELECT evidence.original_item_id, evidence.submitted_at, evidence.submitted_date,
                  evidence.grade, evidence.supported, evidence.accepted_date
           FROM review_evidence AS evidence
           JOIN review_evidence_selection AS selection ON evidence.id = selection.evidence_id
           WHERE evidence.concept_id = ?""",
        (concept_id,),
    )
    events = (
        LearningEvent(item_id, submitted_at, date.fromisoformat(submitted), grade, bool(supported),
                      date.fromisoformat(accepted))
        for item_id, submitted_at, submitted, grade, supported, accepted in selected
    )
    state = replay_reviews(events, intervals)
    connection.execute(
        """INSERT INTO concept_review_state VALUES (?, ?, ?)
           ON CONFLICT(concept_id) DO UPDATE SET
             stage = excluded.stage, evidence_due_date = excluded.evidence_due_date""",
        (concept_id, state.stage, state.due_date.isoformat() if state.due_date else None),
    )


def read_reviews(connection: sqlite3.Connection) -> dict[str, ReviewState]:
    """Read concept review dates overlaid with outstanding earlier requests.

    Parameters
    ----------
    connection : sqlite3.Connection
        The caller's local practice transaction.

    Returns
    -------
    dict of str to ReviewState
        Effective review snapshot by primary concept.
    """
    states = {
        concept_id: ReviewState(stage, date.fromisoformat(due_date) if due_date else None, due_date is None)
        for concept_id, stage, due_date in connection.execute("SELECT * FROM concept_review_state")
    }
    requests = connection.execute("""
        SELECT request.concept_id, MIN(request.due_date)
        FROM review_sooner_requests AS request
        WHERE NOT EXISTS (
            SELECT 1 FROM review_evidence AS evidence
            JOIN review_evidence_selection AS selection ON selection.evidence_id = evidence.id
            WHERE evidence.concept_id = request.concept_id
              AND evidence.submitted_at > request.requested_at
              AND evidence.submitted_date >= request.due_date
        )
        GROUP BY request.concept_id
    """)
    for concept_id, requested in requests:
        state = states.get(concept_id, ReviewState())
        requested_date = date.fromisoformat(requested)
        due_date = min(requested_date, state.due_date) if state.due_date else requested_date
        states[concept_id] = ReviewState(state.stage, due_date, state.awaiting_assessment)
    return states


def due_concepts(connection: sqlite3.Connection, local_date: str) -> set[str]:
    """Return concept identities that can be reviewed on the given local date.

    Parameters
    ----------
    connection : sqlite3.Connection
        The transaction preparing the day's saved recommendations.
    local_date : str
        Today's local date in ISO format.

    Returns
    -------
    set of str
        Concepts with effective review dates at or before the given date.
    """
    today = date.fromisoformat(local_date)
    return {
        concept_id for concept_id, review in read_reviews(connection).items()
        if review.due_date is not None and review.due_date <= today
    }
