"""Select, snapshot and save local practice independently of model access."""

import json
import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone as utc
from pathlib import Path
from random import choice
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .bank import SectionNotFound
from .config import bundled_defaults
from .database import Database
from .grading import GradingContext
from .grading_jobs import enqueue_practice_request, practice_grading_status, public_request
from .practice_versions import bind_grading_version, initialize_versions, save_answer_version
from .disputes import (
    CURRENT_ASSESSMENT_JOIN,
    CURRENT_ASSESSMENT_SUMMARY,
    disputes_projection,
    initialize_disputes,
    register_reassessment,
    resolve_dispute,
)
from .scheduling import (
    NEXT_DAY,
    ReviewState,
    due_concepts as scheduled_due_concepts,
    initialize_reviews,
    read_reviews,
    record_gap,
)

DAILY_GOAL = 2
SelectionKind = Literal["review", "variety", "extra", "focused"]

_CREDITS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS practice_credits (
        question_id TEXT NOT NULL,
        local_date TEXT NOT NULL REFERENCES practice_days(local_date),
        item_id TEXT NOT NULL UNIQUE REFERENCES practice_items(id),
        FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id),
        PRIMARY KEY (question_id, local_date)
    )
    """
_HELP_EVENTS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS practice_help_events (
        item_id TEXT NOT NULL REFERENCES practice_items(id),
        kind TEXT NOT NULL CHECK (kind IN ('hint', 'explanation', 'gap_explanation')),
        viewed_at REAL NOT NULL,
        PRIMARY KEY (item_id, kind)
    )
    """

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS practice_days (
        local_date TEXT PRIMARY KEY,
        timezone TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_items (
        id TEXT PRIMARY KEY,
        local_date TEXT NOT NULL REFERENCES practice_days(local_date),
        timezone TEXT NOT NULL,
        created_at REAL NOT NULL,
        question_id TEXT NOT NULL,
        question_version INTEGER NOT NULL,
        section_id TEXT NOT NULL,
        section_title TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        draft_text TEXT NOT NULL DEFAULT '',
        draft_version INTEGER NOT NULL DEFAULT 0 CHECK (draft_version >= 0),
        last_practised_date TEXT,
        FOREIGN KEY (question_id, question_version)
            REFERENCES bank_question_versions(question_id, version),
        UNIQUE (id, question_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_selections (
        local_date TEXT NOT NULL REFERENCES practice_days(local_date),
        slot INTEGER NOT NULL CHECK (slot > 0),
        kind TEXT NOT NULL CHECK (kind IN ('review', 'variety', 'extra', 'focused')),
        item_id TEXT NOT NULL,
        question_id TEXT NOT NULL,
        FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id),
        PRIMARY KEY (local_date, slot),
        UNIQUE (local_date, question_id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS practice_snapshot_immutable
    BEFORE UPDATE OF id, local_date, timezone, created_at, question_id, question_version,
                     section_id, section_title, snapshot_json ON practice_items
    BEGIN
        SELECT RAISE(ABORT, 'Practice item identity and snapshot are immutable');
    END
    """,
    "CREATE INDEX IF NOT EXISTS practice_items_by_question ON practice_items(question_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS practice_submissions (
        item_id TEXT PRIMARY KEY REFERENCES practice_items(id),
        original_text TEXT NOT NULL,
        completed_at REAL NOT NULL,
        completed_date TEXT NOT NULL REFERENCES practice_days(local_date),
        completed_timezone TEXT NOT NULL,
        original_request_id TEXT NOT NULL UNIQUE REFERENCES grading_requests(id),
        current_request_id TEXT NOT NULL UNIQUE REFERENCES grading_requests(id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS original_submission_immutable
    BEFORE UPDATE OF item_id, original_text, completed_at, completed_date, completed_timezone,
                     original_request_id ON practice_submissions
    BEGIN
        SELECT RAISE(ABORT, 'The original submission is immutable');
    END
    """,
    _CREDITS_SCHEMA,
    "CREATE INDEX IF NOT EXISTS practice_credits_by_date ON practice_credits(local_date)",
    """
    CREATE TABLE IF NOT EXISTS practice_gap_completions (
        item_id TEXT PRIMARY KEY REFERENCES practice_items(id),
        completed_at REAL NOT NULL,
        completed_date TEXT NOT NULL REFERENCES practice_days(local_date),
        completed_timezone TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_clarification_drafts (
        request_id TEXT PRIMARY KEY REFERENCES grading_requests(id),
        text TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0)
    )
    """,
    _HELP_EVENTS_SCHEMA,
    """
    CREATE TABLE IF NOT EXISTS practice_gap_actions (
        action_key TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES practice_items(id)
    )
    """,
)


@dataclass(frozen=True)
class Draft:
    """Saved answer text with the version required for its next edit."""

    text: str
    version: int


class DraftConflict(RuntimeError):
    """The edit was based on an older draft version."""

    def __init__(self, saved: Draft) -> None:
        super().__init__("This draft changed in another tab.")
        self.saved = saved


class DraftInvalid(ValueError):
    """Draft text cannot be saved under the current configuration."""


class SubmissionConflict(RuntimeError):
    """A different original has already been saved, or this action is stale."""


class PracticeItemNotFound(LookupError):
    """The requested practice item or available question could not be found."""


class SupportUnavailable(LookupError):
    """The saved question version lacks the requested support."""


@dataclass(frozen=True)
class PracticeQuestion:
    """The public prompt and metadata from an immutable item snapshot."""

    id: str
    version: int
    section_id: str
    section_title: str
    concept_id: str
    title: str
    prompt: str
    objective: str
    difficulty: str
    expected_answer_words: tuple[int, int]


@dataclass(frozen=True)
class PracticeItem:
    """One durable practice identity, its original local date and its current draft."""

    id: str
    local_date: str
    question: PracticeQuestion
    draft: Draft
    submitted: bool = False
    grading_status: str | None = None
    grade: int | None = None
    has_open_dispute: bool = False
    has_revision_draft: bool = False


@dataclass(frozen=True)
class PracticeSelection:
    """A saved recommendation or requested addition within today's session."""

    kind: SelectionKind
    item: PracticeItem


@dataclass(frozen=True)
class DueReview:
    """An active question that can start a due concept review."""

    question_id: str
    section_id: str
    title: str


@dataclass(frozen=True)
class PracticeToday:
    """Today's saved session, unfinished drafts and effective editing limit."""

    local_date: str
    timezone: str
    effective_timezone: str
    goal: int
    answer_limit: int
    shortage: str | None
    selections: list[PracticeSelection]
    unfinished: list[PracticeItem]
    submitted: list[PracticeItem]
    completion_counts: dict[str, int]
    first_use_date: str
    reviews: dict[str, ReviewState]
    due_reviews: list[DueReview]


@dataclass(frozen=True)
class SubmissionResult:
    """The saved original, current Today session and optional new work to dispatch."""

    submission: dict
    today: PracticeToday
    dispatch_id: str | None


@dataclass(frozen=True)
class PracticeAddition:
    """Result of asking for more practice, including any shortage message."""

    today: PracticeToday
    item_id: str | None
    message: str | None = None


@dataclass(frozen=True)
class _Candidate:
    """An available bank version that can start or resume practice."""

    question_id: str
    version: int
    section_id: str
    section_title: str
    concept_id: str
    snapshot_json: str


@dataclass(frozen=True)
class _Day:
    """A local date paired with the clock instant and timezone used to derive it."""

    local_date: str
    timezone: str
    timestamp: float


def _now() -> datetime:
    """Return the current timezone-aware instant."""
    return datetime.now(utc.utc)


class Practice:
    """Persist Today selection and drafts in short, isolated transactions.

    Parameters
    ----------
    database : Database
        Local database with the validated question bank already imported.
    answer_limit : int
        The maximum number of Unicode code points in an answer.
    clock : callable
        Returns the current timezone-aware instant.
    choose : callable
        Picks a candidate from a nonempty ranked sequence.
    due_concepts : callable
        Returns eligible due concept IDs using the same SQLite connection and
        the current ISO local date. By default, uses saved review dates.
    review_intervals : tuple of int or None
        Effective independent review intervals, or the bundled defaults.
    """

    def __init__(
        self,
        database: Database,
        *,
        answer_limit: int,
        clock: Callable[[], datetime] = _now,
        choose: Callable[[Sequence[_Candidate]], _Candidate] = choice,
        due_concepts: Callable[[sqlite3.Connection, str], Collection[str]] = scheduled_due_concepts,
        review_intervals: tuple[int, ...] | None = None,
    ) -> None:
        self.database = database
        self.answer_limit = answer_limit
        self._clock = clock
        self._choose = choose
        self._due_concepts = due_concepts
        self.review_intervals = review_intervals if review_intervals is not None else tuple(bundled_defaults()["review_intervals"])
        database.initialize_grading()
        with self.database.connect(immediate=True) as connection:
            self._migrate_practice_credits(connection)
            for statement in _SCHEMA:
                connection.execute(statement)
            initialize_reviews(connection, self.review_intervals)
            initialize_versions(connection)
            initialize_disputes(connection)
            self.database.record_backup_tables(connection)

    def _migrate_practice_credits(self, connection: sqlite3.Connection) -> None:
        """Move legacy credit rows to the item-level foreign key schema.

        Older installations tied a credit's item to ``practice_submissions``.
        Gap completions intentionally have no submission or grading request,
        so the credit must reference the immutable practice item instead.
        """
        foreign_keys = connection.execute("PRAGMA foreign_key_list(practice_credits)").fetchall()
        if not any(row[2] == "practice_submissions" for row in foreign_keys):
            return
        self.database.create_backup(prefix="daily-ml-before-gap-upgrade")
        connection.execute("DROP INDEX IF EXISTS practice_credits_by_date")
        connection.execute("ALTER TABLE practice_credits RENAME TO practice_credits_legacy")
        connection.execute(_CREDITS_SCHEMA)
        connection.execute(
            """INSERT INTO practice_credits (question_id, local_date, item_id)
               SELECT question_id, local_date, item_id FROM practice_credits_legacy"""
        )
        connection.execute("DROP TABLE practice_credits_legacy")

    def today(self, timezone: str) -> PracticeToday:
        """Get today's session, saving the recommendation pair if needed.

        Parameters
        ----------
        timezone : str
            The effective learning timezone.

        Returns
        -------
        PracticeToday
            Committed recommendation slots, extra selections and saved drafts.
        """
        day = self._day(timezone)
        with self.database.connect(immediate=True) as connection:
            self._prepare_today(connection, day)
            result = self._today(connection, day)
        return result

    def review_sooner(self, concept_id: str, idempotency_key: str, timezone: str) -> ReviewState:
        """Save an earlier review request and return the effective date.

        Parameters
        ----------
        concept_id : str
            Stable concept from the question bank or saved practice.
        idempotency_key : str
            The browser action ID. A retry keeps the original requested date.
        timezone : str
            Effective timezone when making the request.

        Returns
        -------
        ReviewState
            Current scheduling state for the requested concept.

        Raises
        ------
        PracticeItemNotFound
            The concept has no active question or saved review.
        SubmissionConflict
            The action ID already belongs to another concept.
        """
        day = self._day(timezone)
        with self.database.connect(immediate=True) as connection:
            reviews = read_reviews(connection)
            if concept_id not in reviews and not connection.execute(
                """SELECT 1 FROM bank_active_questions WHERE concept_id = ?
                   UNION ALL SELECT 1 FROM practice_items
                   WHERE json_extract(snapshot_json, '$.concept_id') = ? LIMIT 1""",
                (concept_id, concept_id),
            ).fetchone():
                raise PracticeItemNotFound(concept_id)
            existing = connection.execute(
                "SELECT concept_id FROM review_sooner_requests WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            state = reviews.get(concept_id, ReviewState())
            if existing is not None:
                if existing[0] != concept_id:
                    raise SubmissionConflict("This review request already belongs to a different concept.")
                return state
            tomorrow = date.fromisoformat(day.local_date) + NEXT_DAY
            due_date = min(tomorrow, state.due_date) if state.due_date else tomorrow
            connection.execute(
                "INSERT INTO review_sooner_requests VALUES (?, ?, ?, ?)",
                (idempotency_key, concept_id, day.timestamp, due_date.isoformat()),
            )
            return ReviewState(state.stage, due_date, state.awaiting_assessment)

    def add_item(
        self,
        timezone: str,
        *,
        section_id: str | None = None,
        question_id: str | None = None,
    ) -> PracticeAddition:
        """Resume or save a requested extra, section or specific question.

        Parameters
        ----------
        timezone : str
            The effective learning timezone.
        section_id : str or None
            Optional limit for focused practice.
        question_id : str or None
            The exact question selected while browsing.

        Returns
        -------
        PracticeAddition
            The committed session, selected item's ID if any, and shortage text.

        Raises
        ------
        SectionNotFound
            If the requested section does not exist.
        PracticeItemNotFound
            If the specific question is unavailable.
        """
        day = self._day(timezone)
        with self.database.connect(immediate=True) as connection:
            if section_id is not None and not connection.execute(
                "SELECT 1 FROM bank_sections WHERE id = ?", (section_id,)
            ).fetchone():
                raise SectionNotFound(section_id)
            self._prepare_today(connection, day)
            existing = dict(connection.execute(
                "SELECT question_id, item_id FROM practice_selections WHERE local_date = ?",
                (day.local_date,),
            ))
            candidates = self._candidates(connection, section_id, question_id)
            if question_id is not None and not candidates:
                raise PracticeItemNotFound(question_id)
            fresh = [candidate for candidate in candidates if candidate.question_id not in existing]
            if fresh:
                selected = self._varied(connection, day.local_date, fresh)
                kind: SelectionKind = "focused" if section_id or question_id else "extra"
                item_id = self._append(connection, day, selected, len(existing) + 1, kind)
                message = None
            elif question_id is not None:
                today_item_id = existing[question_id]
                item_id = self._unfinished_item_id(connection, question_id, today_item_id) or today_item_id
                message = None
            else:
                item_id = (
                    self._existing_focused_item(connection, day, candidates, existing) if section_id else None
                )
                message = None if item_id else self._exhausted_message(section_id)
            result = PracticeAddition(self._today(connection, day), item_id, message)
        return result

    def save_draft(
        self, item_id: str, text: str, *, expected_version: int, timezone: str
    ) -> Draft:
        """Conditionally persist the complete draft text.

        Parameters
        ----------
        item_id : str
            Stable practice item ID, including items from previous days.
        text : str
            Exact editor text to persist.
        expected_version : int
            Last saved version observed by this tab.
        timezone : str
            Current learning timezone, for tracking when this section was practised.

        Returns
        -------
        Draft
            Saved text and version after SQLite commits.

        Raises
        ------
        DraftConflict
            If a different version is currently saved.
        DraftInvalid
            If text exceeds the configured limit or cannot be encoded as UTF-8.
        PracticeItemNotFound
            If this practice identity does not exist.
        """
        self._validate_text(text)
        day = self._day(timezone)
        with self.database.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT draft_text, draft_version FROM practice_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise PracticeItemNotFound(item_id)
            saved = Draft(*row)
            if connection.execute(
                """SELECT 1 FROM practice_submissions WHERE item_id = ?
                   UNION ALL SELECT 1 FROM practice_gap_completions WHERE item_id = ?""",
                (item_id, item_id),
            ).fetchone():
                raise SubmissionConflict("This answer was already submitted. Its original cannot be edited.")
            if saved.version != expected_version:
                raise DraftConflict(saved)
            if saved.text == text:
                return saved
            connection.execute(
                """UPDATE practice_items
                   SET draft_text = ?, draft_version = draft_version + 1,
                       last_practised_date = CASE WHEN ? THEN ? ELSE last_practised_date END
                   WHERE id = ? AND draft_version = ?""",
                (text, bool(text.strip()), day.local_date, item_id, expected_version),
            )
            result = Draft(text, expected_version + 1)
        return result

    def submit(
        self,
        item_id: str,
        text: str,
        *,
        expected_version: int,
        resolve_settings: Callable[[sqlite3.Connection], tuple[str, str | None]],
    ) -> SubmissionResult:
        """Commit the original, its date, unique credit and grading request together.

        Parameters
        ----------
        item_id : str
            Stable identity of the selected or resumed practice item.
        text : str
            Exact answer text entered by the learner.
        expected_version : int
            The editor's last observed draft version.
        resolve_settings : callable
            Read the effective timezone and model from the submission transaction.

        Returns
        -------
        SubmissionResult
            Committed original and Today session, with a dispatch ID only for new work.

        Raises
        ------
        DraftInvalid
            The answer is blank, exceeds the configured limit or is invalid Unicode.
        DraftConflict
            The draft changed in another tab before submission.
        SubmissionConflict
            The item already has a different immutable original.
        PracticeItemNotFound
            The requested identity does not exist.
        """
        self._validate_text(text, submitted=True)
        with self.database.connect(immediate=True) as connection:
            timezone, model_id = resolve_settings(connection)
            day = self._day(timezone)
            item = connection.execute(
                """SELECT question_id, snapshot_json, draft_text, draft_version
                   FROM practice_items WHERE id = ?""", (item_id,)
            ).fetchone()
            if item is None:
                raise PracticeItemNotFound(item_id)
            question_id, snapshot, draft_text, version = item
            original = connection.execute(
                "SELECT original_text FROM practice_submissions WHERE item_id = ?", (item_id,)
            ).fetchone()
            gap = connection.execute(
                "SELECT 1 FROM practice_gap_completions WHERE item_id = ?", (item_id,)
            ).fetchone()
            dispatch_id = None
            self._prepare_today(connection, day)
            if original is None:
                if gap is not None:
                    raise SubmissionConflict("A self-reported gap for this question was already recorded.")
                if version != expected_version:
                    raise DraftConflict(Draft(draft_text, version))
                request_id = enqueue_practice_request(
                    connection, GradingContext.from_question_snapshot(snapshot, text), model_id, day.timestamp,
                )
                connection.execute(
                    "INSERT INTO practice_submissions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, text, day.timestamp, day.local_date, day.timezone, request_id, request_id),
                )
                supported = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM practice_help_events WHERE item_id = ?)", (item_id,)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO practice_submission_support VALUES (?, ?)",
                    (item_id, supported),
                )
                save_answer_version(
                    connection, item_id, request_id, text, "original", "initial", supported, day.timestamp,
                )
                connection.execute(
                    """INSERT INTO practice_credits VALUES (?, ?, ?)
                       ON CONFLICT(question_id, local_date) DO NOTHING""",
                    (question_id, day.local_date, item_id),
                )
                connection.execute(
                    "UPDATE practice_items SET last_practised_date = ? WHERE id = ?",
                    (day.local_date, item_id),
                )
                dispatch_id = request_id if model_id else None
            elif original[0] != text:
                raise SubmissionConflict("An original answer for this question was already submitted in another tab.")
            today = self._today(connection, day)
            submission = self._read_submission(connection, item_id)
        return SubmissionResult(submission, today, dispatch_id)

    def saved_item(self, item_id: str) -> PracticeItem:
        """Read an exact saved editor item without selecting it for today's practice."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"""SELECT item.id, item.local_date, item.section_title, item.snapshot_json,
                          item.draft_text, item.draft_version,
                          CASE WHEN gap.item_id IS NOT NULL THEN 'self_reported_gap' ELSE request.state END,
                          request.error, {CURRENT_ASSESSMENT_SUMMARY}
                   FROM practice_items AS item
                   LEFT JOIN practice_submissions AS submission ON submission.item_id = item.id
                   LEFT JOIN practice_gap_completions AS gap ON gap.item_id = item.id
                   LEFT JOIN practice_revisions AS revision ON revision.item_id = item.id
                   LEFT JOIN grading_requests AS request
                     ON request.id = COALESCE(revision.current_request_id, submission.current_request_id)
                   {CURRENT_ASSESSMENT_JOIN}
                   WHERE item.id = ?""", (item_id,),
            ).fetchone()
        if row is None:
            raise PracticeItemNotFound
        return self._item(row)

    def submission(self, item_id: str) -> dict | None:
        """Read the saved answer and current feedback without dispatching a request.

        Parameters
        ----------
        item_id : str
            Stable practice item identity.

        Returns
        -------
        dict or None
            Saved answer and grading state, or None if unsubmitted.

        Raises
        ------
        PracticeItemNotFound
            The requested practice identity does not exist.
        """
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            if not connection.execute("SELECT 1 FROM practice_items WHERE id = ?", (item_id,)).fetchone():
                raise PracticeItemNotFound(item_id)
            return self._read_submission(connection, item_id)

    def open_dispute(self, item_id: str, request_id: str, objection: str) -> dict:
        """Persist an objection and exclude only its original-track evidence.

        Raises
        ------
        SubmissionConflict
            If the displayed assessment is missing, unassessed, or already disputed.
        """
        if not objection.strip() or len(objection) > 1600:
            raise DraftInvalid("Write an objection of at most 1600 characters.")
        with self.database.connect(immediate=True) as connection:
            connection.row_factory = sqlite3.Row
            target = connection.execute("""
                SELECT assessed.* FROM practice_evaluated_requests AS assessed
                JOIN practice_submissions AS submission ON submission.item_id = assessed.item_id
                LEFT JOIN practice_revisions AS revision ON revision.item_id = assessed.item_id
                WHERE assessed.id = ? AND assessed.item_id = ? AND assessed.state = 'assessed'
                  AND (assessed.id = submission.current_request_id OR assessed.id = revision.current_request_id)
            """, (request_id, item_id)).fetchone()
            if target is None:
                raise SubmissionConflict("Choose an assessed saved answer to dispute.")
            if connection.execute("SELECT 1 FROM practice_disputes WHERE target_request_id = ?", (request_id,)).fetchone():
                raise SubmissionConflict("This assessment already has a dispute.")
            dispute_id = str(uuid4())
            connection.execute("""
                INSERT INTO practice_disputes
                (id, item_id, target_request_id, target_version_id, role, objection, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (dispute_id, item_id, request_id, target["submission_version_id"], target["role"], objection.strip(),
                  self._clock().timestamp()))
            if target["role"] == "original":
                from .scheduling import select_review_evidence
                select_review_evidence(connection, item_id, None, self.review_intervals)
            return self._read_submission(connection, item_id)

    def reassess_dispute(
        self, item_id: str, dispute_id: str, idempotency_key: str, *,
        resolve_settings: Callable[[sqlite3.Connection], tuple[str, str | None]],
    ) -> tuple[dict, str | None]:
        """Queue or reuse one reassessment of the immutable disputed submission."""
        with self.database.connect(immediate=True) as connection:
            connection.row_factory = sqlite3.Row
            dispute = connection.execute("""
                SELECT * FROM practice_disputes WHERE id = ? AND item_id = ? AND selected_request_id IS NULL
            """, (dispute_id, item_id)).fetchone()
            if dispute is None:
                raise SubmissionConflict("This dispute is no longer open.")
            action = connection.execute("""
                SELECT dispute_id FROM practice_dispute_reassessment_actions WHERE action_key = ?
            """, (idempotency_key,)).fetchone()
            if action is not None:
                if action["dispute_id"] != dispute_id:
                    raise SubmissionConflict("This reassessment action already belongs to another dispute.")
                return self._read_submission(connection, item_id), None
            latest = connection.execute("""
                SELECT request.* FROM practice_dispute_reassessments AS link
                JOIN grading_requests AS request ON request.id = link.request_id
                WHERE link.dispute_id = ? ORDER BY request.created_at DESC, request.rowid DESC LIMIT 1
            """, (dispute_id,)).fetchone()
            if latest is not None and (
                latest["state"] in ("preparing", "in_flight", "assessed")
                or (latest["state"] == "queued" and latest["error"] is None)
            ):
                return self._read_submission(connection, item_id), None
            _, model_id = resolve_settings(connection)
            if model_id is None:
                raise SubmissionConflict("Choose a model in Settings before requesting reassessment.")
            target = connection.execute("SELECT * FROM practice_evaluated_requests WHERE id = ?", (dispute["target_request_id"],)).fetchone()
            context = GradingContext.model_validate_json(target["context_json"]).model_copy(update={"objection": dispute["objection"]})
            request_id = enqueue_practice_request(
                connection, context, model_id, self._clock().timestamp(), retry_of=latest["id"] if latest else None,
                mode=self._request_mode(latest or target, model_id),
            )
            register_reassessment(connection, dispute_id, request_id)
            connection.execute(
                "INSERT INTO practice_dispute_reassessment_actions VALUES (?, ?, ?)",
                (idempotency_key, dispute_id, request_id),
            )
            return self._read_submission(connection, item_id), request_id

    def resolve_dispute(
        self, item_id: str, dispute_id: str, assessment_request_id: str, *,
        resolve_settings: Callable[[sqlite3.Connection], tuple[str, str | None]],
    ) -> dict:
        """Accept one assessment belonging to an open dispute in one transaction."""
        with self.database.connect(immediate=True) as connection:
            try:
                timezone, _ = resolve_settings(connection)
                resolve_dispute(
                    connection, item_id, dispute_id, assessment_request_id, self.review_intervals,
                    self._clock().timestamp(), timezone,
                )
            except LookupError as error:
                raise SubmissionConflict(str(error)) from None
            return self._read_submission(connection, item_id)

    def view_help(self, item_id: str, kind: Literal["hint", "explanation"]) -> str:
        """Record help use before returning the selected stored teaching text.

        Parameters
        ----------
        item_id : str
            Stable practice item identity.
        kind : {"hint", "explanation"}
            Stored support to reveal.

        Returns
        -------
        str
            The requested hint or reference explanation.

        Raises
        ------
        PracticeItemNotFound
            If the practice item does not exist.
        ValueError
            If ``kind`` is not a supported help type.
        SupportUnavailable
            If the saved question version has no matching teaching content.
        """
        if kind not in ("hint", "explanation"):
            raise ValueError("Help must be a hint or explanation.")
        with self.database.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM practice_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise PracticeItemNotFound(item_id)
            record = json.loads(row[0])
            content_key = "hint" if kind == "hint" else "reference_explanation"
            content = record.get(content_key)
            if not content:
                raise SupportUnavailable(f"This question has no {kind} available.")
            connection.execute(
                "INSERT OR IGNORE INTO practice_help_events VALUES (?, ?, ?)",
                (item_id, kind, self._clock().timestamp()),
            )
        return content

    def begin_gap(self, item_id: str) -> dict[str, object]:
        """Reveal and persist the explanation required before a gap is recorded.

        Parameters
        ----------
        item_id : str
            Stable practice item identity.

        Returns
        -------
        dict[str, object]
            Explanation text and the acknowledgement requirement.

        Raises
        ------
        PracticeItemNotFound
            If the practice item does not exist.
        SubmissionConflict
            If the item already has a competing submitted answer.
        SupportUnavailable
            If the saved question version has no reference explanation.
        """
        with self.database.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM practice_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise PracticeItemNotFound(item_id)
            if connection.execute(
                """SELECT 1 FROM practice_submissions WHERE item_id = ?
                   UNION ALL SELECT 1 FROM practice_gap_completions WHERE item_id = ?""",
                (item_id, item_id),
            ).fetchone():
                raise SubmissionConflict("This question was already submitted.")
            explanation = json.loads(row[0]).get("reference_explanation")
            if not explanation:
                raise SupportUnavailable("This question has no explanation available.")
            connection.execute(
                "INSERT OR IGNORE INTO practice_help_events VALUES (?, 'gap_explanation', ?)",
                (item_id, self._clock().timestamp()),
            )
        return {
            "item_id": item_id,
            "explanation": explanation,
            "acknowledgement_required": True,
        }

    def acknowledge_gap(
        self, item_id: str, idempotency_key: str, *,
        resolve_timezone: Callable[[sqlite3.Connection], str],
    ) -> SubmissionResult:
        """Save a viewed self-reported gap and its local completion credit once.

        Parameters
        ----------
        item_id : str
            Stable practice item identity.
        idempotency_key : str
            Browser action identity used to make acknowledgement retries safe.
        resolve_timezone : callable
            Read the effective learning timezone from the completion transaction.

        Returns
        -------
        SubmissionResult
            The local self-reported-gap submission and updated Today session.

        Raises
        ------
        PracticeItemNotFound
            If the practice item does not exist.
        SubmissionConflict
            If the explanation was not viewed or another answer won the race.
        """
        with self.database.connect(immediate=True) as connection:
            day = self._day(resolve_timezone(connection))
            item = connection.execute(
                "SELECT question_id FROM practice_items WHERE id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise PracticeItemNotFound(item_id)

            prior = connection.execute(
                "SELECT item_id FROM practice_gap_actions WHERE action_key = ?", (idempotency_key,)
            ).fetchone()
            if prior is not None:
                if prior[0] != item_id:
                    raise SubmissionConflict("This gap acknowledgement already belongs to another question.")
                self._prepare_today(connection, day)
                submission = self._read_submission(connection, item_id)
                if submission is None:
                    raise SubmissionConflict("The saved gap acknowledgement is unavailable.")
                return SubmissionResult(submission, self._today(connection, day), None)

            if connection.execute(
                "SELECT 1 FROM practice_gap_completions WHERE item_id = ?", (item_id,)
            ).fetchone():
                connection.execute(
                    "INSERT INTO practice_gap_actions VALUES (?, ?)", (idempotency_key, item_id)
                )
                self._prepare_today(connection, day)
                submission = self._read_submission(connection, item_id)
                return SubmissionResult(submission, self._today(connection, day), None)

            if connection.execute(
                "SELECT 1 FROM practice_submissions WHERE item_id = ?", (item_id,)
            ).fetchone():
                raise SubmissionConflict("An answer for this question was already submitted.")

            if not connection.execute(
                "SELECT 1 FROM practice_help_events WHERE item_id = ? AND kind = 'gap_explanation'", (item_id,)
            ).fetchone():
                raise SubmissionConflict("View the explanation before recording a self-reported gap.")

            self._prepare_today(connection, day)
            question_id = item[0]
            connection.execute(
                "INSERT INTO practice_gap_completions VALUES (?, ?, ?, ?)",
                (item_id, day.timestamp, day.local_date, day.timezone),
            )
            connection.execute(
                """INSERT INTO practice_credits VALUES (?, ?, ?)
                   ON CONFLICT(question_id, local_date) DO NOTHING""",
                (question_id, day.local_date, item_id),
            )
            connection.execute(
                "UPDATE practice_items SET last_practised_date = ? WHERE id = ?",
                (day.local_date, item_id),
            )
            record_gap(connection, item_id, day.timestamp, day.local_date, self.review_intervals)
            connection.execute(
                "INSERT INTO practice_gap_actions VALUES (?, ?)", (idempotency_key, item_id)
            )
            submission = self._read_submission(connection, item_id)
            today = self._today(connection, day)
        return SubmissionResult(submission, today, None)

    def save_clarification_draft(
        self, item_id: str, request_id: str, text: str, *, expected_version: int,
    ) -> Draft:
        """Save an edit for the current clarification request without sending it.

        Parameters
        ----------
        item_id : str
            Stable practice item ID.
        request_id : str
            Grading request that asked for clarification.
        text : str
            Exact editor text.
        expected_version : int
            The editor's last observed clarification draft version.

        Returns
        -------
        Draft
            Committed text and version.

        Raises
        ------
        DraftInvalid
            Text exceeds the configured limit or is invalid Unicode.
        PracticeItemNotFound
            The practice item has no saved original.
        SubmissionConflict
            The displayed request no longer accepts a clarification.
        DraftConflict
            The draft changed in another tab.
        """
        self._validate_text(text)
        with self.database.connect(immediate=True) as connection:
            current = self._current_request(connection, item_id, displayed_id=request_id)
            if current["id"] != request_id or current["state"] != "needs_clarification":
                raise SubmissionConflict("This clarification is no longer open. Your submitted answers are saved.")
            return self._save_follow_on_draft(connection, current, text, expected_version, kind="clarification")

    @staticmethod
    def _current_request(
        connection: sqlite3.Connection, item_id: str, *, displayed_id: str | None = None,
    ) -> sqlite3.Row:
        """Resolve the current request on the original or the displayed request's revision track."""
        connection.row_factory = sqlite3.Row
        original = connection.execute("""
            SELECT r.* FROM practice_submissions AS submission
            JOIN practice_evaluated_requests AS r ON r.id = submission.current_request_id
            WHERE submission.item_id = ?
        """, (item_id,)).fetchone()
        if original is None:
            raise PracticeItemNotFound(item_id)
        if displayed_id is None or displayed_id == original["id"]:
            return original
        displayed = connection.execute("""
            SELECT role FROM practice_evaluated_requests WHERE id = ? AND item_id = ?
        """, (displayed_id, item_id)).fetchone()
        if displayed is None:
            raise SubmissionConflict("Choose a grading request for this saved answer.")
        if displayed["role"] == "original":
            return original
        return connection.execute("""
            SELECT r.* FROM practice_revisions AS revision
            JOIN practice_evaluated_requests AS r ON r.id = revision.current_request_id
            WHERE revision.item_id = ?
        """, (item_id,)).fetchone()

    @staticmethod
    def _follow_on_draft(
        connection: sqlite3.Connection, request: sqlite3.Row, kind: Literal["clarification", "revision"],
    ) -> Draft:
        """Load the saved draft or prefill it with the exact evaluated answer."""
        row = connection.execute(
            f"SELECT text, version FROM practice_{kind}_drafts WHERE request_id = ?", (request["id"],),
        ).fetchone()
        return Draft(*row) if row is not None else Draft(request["submitted_text"], 0)

    def _save_follow_on_draft(
        self,
        connection: sqlite3.Connection,
        request: sqlite3.Row,
        text: str,
        expected_version: int,
        *,
        kind: Literal["clarification", "revision"],
    ) -> Draft:
        """Save an edit only if its version still matches the latest draft."""
        saved = self._follow_on_draft(connection, request, kind)
        if saved.version != expected_version:
            raise DraftConflict(saved)
        if saved.text == text:
            return saved
        result = Draft(text, saved.version + 1)
        connection.execute(
            f"""INSERT INTO practice_{kind}_drafts (request_id, text, version)
                VALUES (?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET text = excluded.text, version = excluded.version""",
            (request["id"], result.text, result.version),
        )
        return result

    @staticmethod
    def _check_revision_available(original: sqlite3.Row, request_id: str, revision_exists: bool) -> None:
        """Require the current assessed original and an unused revision slot."""
        if revision_exists:
            raise SubmissionConflict("This answer already has its one revision.")
        if original["id"] != request_id or original["state"] != "assessed":
            raise SubmissionConflict("Open the current original assessment before revising.")

    def save_revision_draft(self, item_id: str, request_id: str, text: str, *, expected_version: int) -> Draft:
        """Save editable revision text without grading or spending the revision.

        Parameters
        ----------
        item_id : str
            Stable practice item ID.
        request_id : str
            Assessed original request shown beside the editor.
        text : str
            Exact editor text.
        expected_version : int
            Draft version last observed by the editor.

        Returns
        -------
        Draft
            Committed text and version.

        Raises
        ------
        DraftInvalid
            The draft exceeds the limit or contains invalid Unicode.
        DraftConflict
            The draft changed in another tab.
        SubmissionConflict
            The assessment cannot accept a revision or a revision already exists.
        PracticeItemNotFound
            The item has no saved original.
        """
        self._validate_text(text)
        with self.database.connect(immediate=True) as connection:
            original = self._current_request(connection, item_id)
            revision_exists = connection.execute("SELECT 1 FROM practice_revisions WHERE item_id = ?", (item_id,))
            self._check_revision_available(original, request_id, revision_exists.fetchone() is not None)
            return self._save_follow_on_draft(connection, original, text, expected_version, kind="revision")

    def revise(
        self,
        item_id: str,
        request_id: str,
        text: str,
        *,
        expected_version: int,
        resolve_settings: Callable[[sqlite3.Connection], tuple[str, str | None]],
    ) -> tuple[dict, str | None]:
        """Commit the one assisted revision and its grading request together.

        Parameters
        ----------
        item_id : str
            Stable practice item ID.
        request_id : str
            Assessed original request on which the revision is based.
        text : str
            Exact revised answer to preserve.
        expected_version : int
            Draft version last observed by the editor.
        resolve_settings : callable
            Read effective model settings within the active transaction.

        Returns
        -------
        tuple of dict and str or None
            Updated submission view and the new request ID if it can dispatch.

        Raises
        ------
        DraftInvalid
            The answer is blank, exceeds the limit or contains invalid Unicode.
        DraftConflict
            The draft changed in another tab.
        SubmissionConflict
            A different revision already exists or the original is not assessed.
        PracticeItemNotFound
            The item has no saved original.
        """
        self._validate_text(text, submitted=True)
        with self.database.connect(immediate=True) as connection:
            original = self._current_request(connection, item_id)
            earlier = connection.execute("""
                SELECT initial.submitted_text, initial.parent_version_id FROM practice_revisions AS revision
                JOIN practice_evaluated_requests AS initial ON initial.id = revision.initial_request_id
                WHERE revision.item_id = ?
            """, (item_id,)).fetchone()
            if earlier is not None and request_id == original["id"] and (
                earlier["parent_version_id"], earlier["submitted_text"]
            ) == (original["submission_version_id"], text):
                return self._read_submission(connection, item_id), None
            self._check_revision_available(original, request_id, earlier is not None)
            draft = self._follow_on_draft(connection, original, "revision")
            if draft.version != expected_version:
                raise DraftConflict(draft)
            _, model_id = resolve_settings(connection)
            timestamp = self._clock().timestamp()
            context = GradingContext.model_validate_json(original["context_json"]).model_copy(update={"answer": text})
            new_id = enqueue_practice_request(
                connection, context, model_id, timestamp, mode=self._request_mode(original, model_id),
            )
            save_answer_version(
                connection, item_id, new_id, text, "revision", "revision", True, timestamp,
                parent_version_id=original["submission_version_id"],
            )
            connection.execute("INSERT INTO practice_revisions VALUES (?, ?, ?)", (item_id, new_id, new_id))
            connection.execute("DELETE FROM practice_revision_drafts WHERE request_id = ?", (request_id,))
            return self._read_submission(connection, item_id), new_id if model_id else None

    @staticmethod
    def _request_mode(current: sqlite3.Row, model_id: str | None) -> str:
        """Carry forward fallback format only for the same model."""
        if model_id == current["model_id"] and (
            current["schema_mode"] == "json" or current["error"] == "schema_unsupported"
        ):
            return "json"
        return "schema"

    def retry_submission(
        self,
        item_id: str,
        request_id: str,
        text: str | None,
        *,
        expected_version: int = 0,
        resolve_settings: Callable[[sqlite3.Connection], tuple[str, str | None]],
    ) -> tuple[dict, str | None]:
        """Create one explicit continuation of the displayed grading request.

        Parameters
        ----------
        item_id : str
            The immutable original's practice item.
        request_id : str
            The exact request the learner chose to resume or clarify.
        text : str or None
            Replacement answer only when the model requested clarification.
        expected_version : int
            Last observed clarification draft version, zero before the first save.
        resolve_settings : callable
            Resolve effective settings using the active transaction.

        Returns
        -------
        tuple of dict and str or None
            Saved submission view and a new request ID when dispatch is needed.

        Raises
        ------
        DraftInvalid
            A supplied clarification violates the answer requirements.
        DraftConflict
            The clarification draft changed in another tab.
        SubmissionConflict
            The displayed request has changed or cannot accept the action.
        PracticeItemNotFound
            No original is available for the item.
        """
        if text is not None:
            self._validate_text(text, submitted=True)
        with self.database.connect(immediate=True) as connection:
            current = self._current_request(connection, item_id, displayed_id=request_id)
            if current["id"] != request_id:
                child = connection.execute("""
                    SELECT child.submission_version_id, child.submitted_text, parent.submission_version_id
                    FROM practice_evaluated_requests AS child
                    JOIN practice_evaluated_requests AS parent ON parent.id = child.retry_of
                    WHERE child.retry_of = ?
                """, (request_id,)).fetchone()
                if child is not None:
                    child_version, child_text, parent_version = child
                    same_action = (text == child_text) if child_version != parent_version else (text is None)
                    if same_action:
                        return self._read_submission(connection, item_id), None
                raise SubmissionConflict("Grading changed in another tab. Reload the saved answer before retrying.")
            if current["state"] == "needs_clarification":
                if text is None:
                    raise SubmissionConflict("Write a clarified answer before sending it.")
            elif text is not None or not self._can_retry(current):
                raise SubmissionConflict("This request is not available to retry.")
            _, model_id = resolve_settings(connection)
            if model_id is None and text is None:
                raise SubmissionConflict("Choose a model in Settings before sending your saved answer.")
            context = GradingContext.model_validate_json(current["context_json"])
            if text is not None:
                draft = self._follow_on_draft(connection, current, "clarification")
                if draft.version != expected_version:
                    raise DraftConflict(draft)
                context = context.model_copy(update={"answer": text})
            timestamp = self._clock().timestamp()
            new_id = enqueue_practice_request(
                connection, context, model_id, timestamp,
                retry_of=request_id, mode=self._request_mode(current, model_id),
            )
            if text is None:
                bind_grading_version(connection, new_id, current["submission_version_id"])
            else:
                save_answer_version(
                    connection, item_id, new_id, text, current["role"], "clarification",
                    bool(current["supported"]), timestamp, parent_version_id=current["submission_version_id"],
                )
                connection.execute("DELETE FROM practice_clarification_drafts WHERE request_id = ?", (request_id,))
            track_table = "practice_revisions" if current["role"] == "revision" else "practice_submissions"
            connection.execute(f"UPDATE {track_table} SET current_request_id=? WHERE item_id=?", (new_id, item_id))
            return self._read_submission(connection, item_id), new_id if model_id else None

    @staticmethod
    def _can_retry(request: sqlite3.Row) -> bool:
        """Only an unsent recovered queue entry or a retryable terminal state can resume."""
        return request["state"] in (
            "awaiting_access", "awaiting_allowance", "outcome_unknown", "failed_recoverable",
        ) or (request["state"] == "queued" and request["error"] == "unsent")

    @classmethod
    def _answer_view(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        """Project the current answer, feedback and clarification draft for a track."""
        clarification = (
            cls._follow_on_draft(connection, row, "clarification") if row["state"] == "needs_clarification" else None
        )
        return {
            "assessed_text": row["submitted_text"],
            "submission_version_id": row["submission_version_id"],
            "supported": bool(row["supported"]),
            "clarification_draft": asdict(clarification) if clarification else None,
            "request": public_request(row),
        }

    @classmethod
    def _read_submission(cls, connection: sqlite3.Connection, item_id: str) -> dict | None:
        """Project original and revision evidence from the same SQLite snapshot."""
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT r.*, s.original_text, s.completed_at, s.completed_date, s.completed_timezone,
                      revision.current_request_id AS revision_request_id,
                      EXISTS (SELECT 1 FROM practice_credits c WHERE c.item_id = s.item_id) AS credited
               FROM practice_submissions AS s
               JOIN practice_evaluated_requests AS r ON r.id = s.current_request_id
               LEFT JOIN practice_revisions AS revision ON revision.item_id = s.item_id
               WHERE s.item_id = ?""", (item_id,),
        ).fetchone()
        if row is None:
            return cls._read_gap(connection, item_id)
        revision = None
        revision_draft = None
        if row["revision_request_id"] is not None:
            revision_request = connection.execute(
                "SELECT * FROM practice_evaluated_requests WHERE id = ?", (row["revision_request_id"],),
            ).fetchone()
            revision = cls._answer_view(connection, revision_request)
        elif row["state"] == "assessed":
            revision_draft = asdict(cls._follow_on_draft(connection, row, "revision"))
        versions = [
            {
                "id": version["submission_version_id"],
                "role": version["role"],
                "kind": version["version_kind"],
                "parent_version_id": version["parent_version_id"],
                "text": version["submitted_text"],
                "supported": bool(version["supported"]),
                "created_at": version["version_created_at"],
                "request": public_request(version),
            }
            for version in connection.execute("""
                SELECT r.* FROM practice_evaluated_requests AS r
                JOIN grading_requests AS request ON request.id = r.id
                WHERE r.item_id = ? AND NOT EXISTS (
                    SELECT 1 FROM practice_evaluated_requests AS newer
                    JOIN grading_requests AS newer_request ON newer_request.id = newer.id
                    WHERE newer.submission_version_id = r.submission_version_id
                      AND (newer_request.created_at > request.created_at
                           OR (newer_request.created_at = request.created_at
                               AND newer_request.rowid > request.rowid))
                )
                ORDER BY r.ordinal
            """, (item_id,))
        ]
        return {
            "text": row["original_text"],
            "completed_at": row["completed_at"],
            "completed_date": row["completed_date"],
            "completed_timezone": row["completed_timezone"],
            "credited": bool(row["credited"]),
            "assisted": bool(row["supported"]),
            **cls._answer_view(connection, row),
            "versions": versions,
            "revision": revision,
            "revision_draft": revision_draft,
            "disputes": disputes_projection(connection, item_id),
        }

    @staticmethod
    def _read_gap(connection: sqlite3.Connection, item_id: str) -> dict | None:
        """Return the offline gap completion for an item, if present."""
        gap = connection.execute(
            """SELECT completed_at, completed_date, completed_timezone,
                      EXISTS (SELECT 1 FROM practice_credits c WHERE c.item_id = g.item_id) AS credited
               FROM practice_gap_completions AS g WHERE g.item_id = ?""",
            (item_id,),
        ).fetchone()
        if gap is None:
            return None
        completed_at, completed_date, completed_timezone, credited = gap
        return {
            "text": "I don't know",
            "completed_at": completed_at,
            "completed_date": completed_date,
            "completed_timezone": completed_timezone,
            "credited": bool(credited),
            "assisted": False,
            "versions": [],
            "revision": None,
            "revision_draft": None,
            "disputes": [],
            "assessed_text": "I don't know",
            "clarification_draft": None,
            "request": {
                "id": None, "state": "self_reported_gap", "model_id": None, "grade": None,
                "error": None, "retry_of": None, "schema_mode": None, "prompt_version": None,
                "schema_version": None, "created_at": completed_at, "finished_at": completed_at,
                "latency_ms": None, "feedback": None, "reference": None, "usage": None,
                "can_send": False, "message": None,
            },
        }

    def _validate_text(self, text: str, *, submitted: bool = False) -> None:
        """Apply a shared code point limit and reject blank original answers."""
        if len(text) > self.answer_limit:
            raise DraftInvalid(f"Keep your answer to {self.answer_limit:,} characters or fewer.")
        if submitted and not text.strip():
            raise DraftInvalid("Write an answer before submitting.")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise DraftInvalid("The answer contains an invalid Unicode character.") from None

    def _day(self, timezone: str) -> _Day:
        """Resolve the local date once for the whole transaction."""
        now = self._clock()
        return _Day(now.astimezone(ZoneInfo(timezone)).date().isoformat(), timezone, now.timestamp())

    def _prepare_today(self, connection: sqlite3.Connection, day: _Day) -> None:
        """Write a day and its distinct recommendation slots exactly once."""
        inserted = connection.execute(
            "INSERT OR IGNORE INTO practice_days VALUES (?, ?, ?)",
            (day.local_date, day.timezone, day.timestamp),
        )
        if inserted.rowcount == 0:
            return

        candidates = self._candidates(connection)
        if not candidates:
            return
        due = self._due_concepts(connection, day.local_date)
        due_candidates = [candidate for candidate in candidates if candidate.concept_id in due]
        if due_candidates:
            fresh_due = self._prefer_unpractised(connection, day.local_date, due_candidates)
            first = self._choose(fresh_due)
            kind: SelectionKind = "review"
        else:
            first = self._varied(connection, day.local_date, candidates)
            kind = "variety"
        self._append(connection, day, first, 1, kind)

        remaining = [candidate for candidate in candidates if candidate.question_id != first.question_id]
        if remaining:
            second = self._varied(connection, day.local_date, remaining, after=first)
            self._append(connection, day, second, 2, "variety")

    @staticmethod
    def _candidates(
        connection: sqlite3.Connection, section_id: str | None = None, question_id: str | None = None
    ) -> list[_Candidate]:
        """Read active versions in consistent order for deterministic random choices."""
        rows = connection.execute(
            """SELECT question.question_id, question.version, question.section_id, section.title,
                      question.concept_id, question.record_json
               FROM bank_active_questions AS question
               JOIN bank_sections AS section ON section.id = question.section_id
               WHERE (? IS NULL OR question.section_id = ?)
                 AND (? IS NULL OR question.question_id = ?)
               ORDER BY question.question_id""",
            (section_id, section_id, question_id, question_id),
        )
        return [_Candidate(*row) for row in rows]

    @staticmethod
    def _prefer_unpractised(
        connection: sqlite3.Connection, local_date: str, candidates: Sequence[_Candidate]
    ) -> list[_Candidate]:
        """Prefer questions without saved practice activity on this date."""
        practised = {row[0] for row in connection.execute(
            "SELECT question_id FROM practice_items WHERE last_practised_date = ?", (local_date,)
        )}
        return [item for item in candidates if item.question_id not in practised] or list(candidates)

    def _varied(
        self,
        connection: sqlite3.Connection,
        local_date: str,
        candidates: Sequence[_Candidate],
        *,
        after: _Candidate | None = None,
    ) -> _Candidate:
        """Choose an unpractised question and favour diversity and older sections."""
        recency = dict(connection.execute(
            """SELECT section_id, MAX(last_practised_date)
               FROM practice_items GROUP BY section_id"""
        ))
        eligible = self._prefer_unpractised(connection, local_date, candidates)

        def rank(candidate: _Candidate) -> tuple[bool, bool, str]:
            return (
                after is not None and candidate.section_id == after.section_id,
                after is not None and candidate.concept_id == after.concept_id,
                recency.get(candidate.section_id) or "",
            )

        best = min(map(rank, eligible))
        return self._choose([candidate for candidate in eligible if rank(candidate) == best])

    @staticmethod
    def _unfinished_item_id(
        connection: sqlite3.Connection, question_id: str, preferred_id: str | None = None
    ) -> str | None:
        """Find a saved draft, favouring the selected item if it already has work."""
        row = connection.execute(
            """SELECT id FROM practice_items
               WHERE question_id = ? AND trim(draft_text) <> ''
                 AND id NOT IN (SELECT item_id FROM practice_submissions)
                 AND id NOT IN (SELECT item_id FROM practice_gap_completions)
               ORDER BY (id = ?) DESC, created_at DESC, id LIMIT 1""",
            (question_id, preferred_id),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _append(
        connection: sqlite3.Connection, day: _Day, candidate: _Candidate, slot: int, kind: SelectionKind
    ) -> str:
        """Link an unfinished answer when present or create a new immutable snapshot."""
        item_id = Practice._unfinished_item_id(connection, candidate.question_id)
        if item_id is None:
            item_id = str(uuid4())
            connection.execute(
                """INSERT INTO practice_items (
                       id, local_date, timezone, created_at, question_id, question_version,
                       section_id, section_title, snapshot_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, day.local_date, day.timezone, day.timestamp, candidate.question_id,
                 candidate.version, candidate.section_id, candidate.section_title, candidate.snapshot_json),
            )
        connection.execute(
            "INSERT INTO practice_selections VALUES (?, ?, ?, ?, ?)",
            (day.local_date, slot, kind, item_id, candidate.question_id),
        )
        return item_id

    @staticmethod
    def _existing_focused_item(
        connection: sqlite3.Connection, day: _Day, candidates: Sequence[_Candidate], existing: dict[str, str],
    ) -> str | None:
        """Resume a saved draft first, then an editable selection in the section."""
        for item in candidates:
            draft_id = Practice._unfinished_item_id(connection, item.question_id, existing[item.question_id])
            if draft_id:
                return draft_id
        unsubmitted = {
            item_id for item_id, in connection.execute(
                """SELECT selection.item_id
                   FROM practice_selections AS selection
                   LEFT JOIN practice_submissions AS submission ON submission.item_id = selection.item_id
                   LEFT JOIN practice_gap_completions AS gap ON gap.item_id = selection.item_id
                   WHERE selection.local_date = ? AND submission.item_id IS NULL AND gap.item_id IS NULL""",
                (day.local_date,),
            )
        }
        return next((existing[item.question_id] for item in candidates if existing[item.question_id] in unsubmitted), None)

    @staticmethod
    def _exhausted_message(section_id: str | None) -> str:
        """Tell the learner when their requested pool has no additional question."""
        if section_id:
            return "There are no more editable questions in this section. Choose another section."
        return "There are no more distinct questions available for today."

    def _today(self, connection: sqlite3.Connection, day: _Day) -> PracticeToday:
        """Read today's committed-to-be session and the separately resumable work."""
        local_date = day.local_date
        original_timezone, = connection.execute(
            "SELECT timezone FROM practice_days WHERE local_date = ?", (local_date,)
        ).fetchone()
        selections = [
            PracticeSelection(kind, self._item(row))
            for kind, *row in connection.execute(
                f"""SELECT selection.kind, item.id, item.local_date, item.section_title,
                          item.snapshot_json, item.draft_text, item.draft_version,
                          CASE WHEN gap.item_id IS NOT NULL THEN 'self_reported_gap' ELSE request.state END,
                          request.error, {CURRENT_ASSESSMENT_SUMMARY}
                   FROM practice_selections AS selection
                   JOIN practice_items AS item ON item.id = selection.item_id
                   LEFT JOIN practice_submissions AS submission ON submission.item_id = item.id
                   LEFT JOIN practice_gap_completions AS gap ON gap.item_id = item.id
                   LEFT JOIN practice_revisions AS revision ON revision.item_id = item.id
                   LEFT JOIN grading_requests AS request
                     ON request.id = COALESCE(revision.current_request_id, submission.current_request_id)
                   {CURRENT_ASSESSMENT_JOIN}
                   WHERE selection.local_date = ?
                   ORDER BY selection.slot""",
                (local_date,),
            )
        ]
        unfinished = [
            self._item(row)
            for row in connection.execute(
                """SELECT item.id, item.local_date, item.section_title, item.snapshot_json,
                          item.draft_text, item.draft_version, NULL, NULL, NULL, 0, 0
                   FROM practice_items AS item
                   WHERE trim(item.draft_text) <> ''
                     AND item.id NOT IN (SELECT item_id FROM practice_submissions)
                     AND item.id NOT IN (SELECT item_id FROM practice_gap_completions)
                     AND NOT EXISTS (
                       SELECT 1 FROM practice_selections WHERE local_date = ? AND item_id = item.id
                   )
                   ORDER BY item.local_date DESC, item.created_at DESC, item.id""",
                (local_date,),
            )
        ]
        calendar_date = date.fromisoformat(local_date)
        week_start = calendar_date - timedelta(days=calendar_date.weekday())
        counts = dict(connection.execute(
            """SELECT local_date, COUNT(*) FROM practice_credits
               WHERE local_date <= ? GROUP BY local_date""",
            (local_date,),
        ))
        first_use_date, = connection.execute("SELECT MIN(local_date) FROM practice_days").fetchone()
        # Every saved submission can be reopened from Today across practice days.
        submitted = [
            self._item(row)
            for row in connection.execute(
                f"""SELECT item.id, item.local_date, item.section_title,
                          item.snapshot_json, item.draft_text, item.draft_version,
                          CASE WHEN gap.item_id IS NOT NULL THEN 'self_reported_gap' ELSE request.state END,
                          request.error, {CURRENT_ASSESSMENT_SUMMARY}
                   FROM practice_items AS item
                   LEFT JOIN practice_submissions AS submission ON submission.item_id = item.id
                   LEFT JOIN practice_gap_completions AS gap ON gap.item_id = item.id
                   LEFT JOIN practice_revisions AS revision ON revision.item_id = item.id
                   LEFT JOIN grading_requests AS request
                     ON request.id = COALESCE(revision.current_request_id, submission.current_request_id)
                   {CURRENT_ASSESSMENT_JOIN}
                   WHERE (gap.completed_date >= ? OR submission.item_id IS NOT NULL)
                     AND NOT EXISTS (
                        SELECT 1 FROM practice_selections WHERE local_date = ? AND item_id = item.id
                     )
                   ORDER BY CASE WHEN revision.item_id IS NOT NULL THEN request.created_at
                                 ELSE COALESCE(gap.completed_at, submission.completed_at) END DESC, item.id""",
                (week_start.isoformat(), local_date),
            )
        ]
        recommended = sum(selection.kind in ("review", "variety") for selection in selections)
        shortage = None
        if recommended < DAILY_GOAL:
            shortage = f"Only {recommended} distinct recommendations were available. Your daily goal is {DAILY_GOAL}."
        reviews = read_reviews(connection)
        due = {
            concept_id for concept_id, review in reviews.items()
            if review.due_date is not None and review.due_date <= calendar_date
        }
        due_reviews: dict[str, DueReview] = {}
        for candidate in self._candidates(connection) if due else ():
            if candidate.concept_id in due and candidate.concept_id not in due_reviews:
                due_reviews[candidate.concept_id] = DueReview(
                    candidate.question_id, candidate.section_id, json.loads(candidate.snapshot_json)["title"],
                )
        return PracticeToday(
            local_date, original_timezone, day.timezone, DAILY_GOAL, self.answer_limit, shortage, selections, unfinished,
            submitted, counts, first_use_date, reviews, list(due_reviews.values()),
        )

    @staticmethod
    def _item(row: Sequence) -> PracticeItem:
        """Project an immutable stored snapshot onto learner-visible prompt fields."""
        (
            item_id, local_date, section_title, snapshot_json, text, version,
            request_state, request_error, grade, has_open_dispute, has_revision_draft,
        ) = row
        record = json.loads(snapshot_json)
        question = PracticeQuestion(
            record["id"], record["version"], record["section_id"], section_title,
            record["concept_id"], record["title"], record["prompt"], record["objective"],
            record["difficulty"], tuple(record["expected_answer_words"]),
        )
        grading_status = practice_grading_status(request_state, request_error) if request_state else None
        return PracticeItem(
            item_id, local_date, question, Draft(text, version), request_state is not None, grading_status,
            grade, bool(has_open_dispute), bool(has_revision_draft),
        )
