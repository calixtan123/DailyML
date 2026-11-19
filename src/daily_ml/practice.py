"""Select, snapshot and save local practice independently of model access."""

import json
import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone as utc
from random import choice
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .bank import SectionNotFound
from .database import Database
from .grading import GradingContext
from .grading_jobs import enqueue_practice_request, practice_grading_status, public_request

DAILY_GOAL = 2
SelectionKind = Literal["review", "variety", "extra", "focused"]

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
    """
    CREATE TABLE IF NOT EXISTS practice_credits (
        question_id TEXT NOT NULL,
        local_date TEXT NOT NULL REFERENCES practice_days(local_date),
        item_id TEXT NOT NULL UNIQUE REFERENCES practice_submissions(item_id),
        FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id),
        PRIMARY KEY (question_id, local_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS practice_credits_by_date ON practice_credits(local_date)",
    """
    CREATE TABLE IF NOT EXISTS practice_clarification_drafts (
        request_id TEXT PRIMARY KEY REFERENCES grading_requests(id),
        text TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0)
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


@dataclass(frozen=True)
class PracticeSelection:
    """A saved recommendation or requested addition within today's session."""

    kind: SelectionKind
    item: PracticeItem


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


def _no_due_concepts(_connection: sqlite3.Connection, _date: str) -> Collection[str]:
    """Leave real review scheduling to the existing due-concept seam."""
    return ()


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
        the current ISO local date. The initial implementation has no due dates.
    """

    def __init__(
        self,
        database: Database,
        *,
        answer_limit: int,
        clock: Callable[[], datetime] = _now,
        choose: Callable[[Sequence[_Candidate]], _Candidate] = choice,
        due_concepts: Callable[[sqlite3.Connection, str], Collection[str]] = _no_due_concepts,
    ) -> None:
        self.database = database
        self.answer_limit = answer_limit
        self._clock = clock
        self._choose = choose
        self._due_concepts = due_concepts
        database.initialize_grading()
        with self.database.connect(immediate=True) as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

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
                "SELECT 1 FROM practice_submissions WHERE item_id = ?", (item_id,)
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
            dispatch_id = None
            self._prepare_today(connection, day)
            if original is None:
                if version != expected_version:
                    raise DraftConflict(Draft(draft_text, version))
                request_id = enqueue_practice_request(
                    connection, self._grading_context(snapshot, text), model_id, day.timestamp,
                )
                connection.execute(
                    "INSERT INTO practice_submissions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, text, day.timestamp, day.local_date, day.timezone, request_id, request_id),
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
            if not connection.execute("SELECT 1 FROM practice_items WHERE id = ?", (item_id,)).fetchone():
                raise PracticeItemNotFound(item_id)
            return self._read_submission(connection, item_id)

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
            current = self._current_request(connection, item_id)
            if current["id"] != request_id or current["state"] != "needs_clarification":
                raise SubmissionConflict("This clarification is no longer open. Copy your text before refreshing grading.")
            assessed_text = json.loads(current["context_json"])["answer"]
            saved = self._clarification_draft(connection, request_id, assessed_text)
            if saved.version != expected_version:
                raise DraftConflict(saved)
            if saved.text == text:
                return saved
            result = Draft(text, saved.version + 1)
            connection.execute(
                """INSERT INTO practice_clarification_drafts (request_id, text, version)
                   VALUES (?, ?, ?)
                   ON CONFLICT(request_id) DO UPDATE SET text = excluded.text, version = excluded.version""",
                (request_id, result.text, result.version),
            )
        return result

    @staticmethod
    def _current_request(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row:
        """Read the exact request that currently owns grading for this original."""
        connection.row_factory = sqlite3.Row
        current = connection.execute(
            """SELECT r.* FROM practice_submissions s
               JOIN grading_requests r ON r.id = s.current_request_id WHERE s.item_id = ?""",
            (item_id,),
        ).fetchone()
        if current is None:
            raise PracticeItemNotFound(item_id)
        return current

    @staticmethod
    def _clarification_draft(connection: sqlite3.Connection, request_id: str, assessed_text: str) -> Draft:
        """Load the draft, using the previously sent answer as the first editable version."""
        row = connection.execute(
            "SELECT text, version FROM practice_clarification_drafts WHERE request_id = ?", (request_id,),
        ).fetchone()
        return Draft(*row) if row is not None else Draft(assessed_text, 0)

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
            current = self._current_request(connection, item_id)
            if current["id"] != request_id:
                if current["retry_of"] == request_id and (
                    text is None or json.loads(current["context_json"])["answer"] == text
                ):
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
                draft = self._clarification_draft(connection, request_id, context.answer)
                if draft.version != expected_version:
                    raise DraftConflict(draft)
                context = context.model_copy(update={"answer": text})
            use_json = model_id == current["model_id"] and (
                current["schema_mode"] == "json" or current["error"] == "schema_unsupported"
            )
            new_id = enqueue_practice_request(
                connection, context, model_id, self._clock().timestamp(),
                retry_of=request_id, mode="json" if use_json else "schema",
            )
            connection.execute(
                "UPDATE practice_submissions SET current_request_id=? WHERE item_id=?", (new_id, item_id),
            )
            if text is not None:
                connection.execute("DELETE FROM practice_clarification_drafts WHERE request_id = ?", (request_id,))
            return self._read_submission(connection, item_id), new_id if model_id else None

    @staticmethod
    def _can_retry(request: sqlite3.Row) -> bool:
        """Only an unsent recovered queue entry or a retryable terminal state can resume."""
        return request["state"] in (
            "awaiting_access", "awaiting_allowance", "outcome_unknown", "failed_recoverable",
        ) or (request["state"] == "queued" and request["error"] == "unsent")

    @staticmethod
    def _read_submission(connection: sqlite3.Connection, item_id: str) -> dict | None:
        """Project a submission and its current request from the same SQLite snapshot."""
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT r.*, s.original_text, s.completed_at, s.completed_date, s.completed_timezone,
                      EXISTS (SELECT 1 FROM practice_credits c WHERE c.item_id = s.item_id) AS credited
               FROM practice_submissions s JOIN grading_requests r ON r.id = s.current_request_id
               WHERE s.item_id = ?""", (item_id,),
        ).fetchone()
        if row is None:
            return None
        assessed_text = json.loads(row["context_json"])["answer"]
        clarification = (
            Practice._clarification_draft(connection, row["id"], assessed_text)
            if row["state"] == "needs_clarification" else None
        )
        return {
            "text": row["original_text"],
            "completed_at": row["completed_at"],
            "completed_date": row["completed_date"],
            "completed_timezone": row["completed_timezone"],
            "credited": bool(row["credited"]),
            "assessed_text": assessed_text,
            "clarification_draft": asdict(clarification) if clarification else None,
            "request": public_request(row),
        }

    @staticmethod
    def _grading_context(snapshot: str, text: str) -> GradingContext:
        """Freeze only the stored question, rubric, references and answer for transport."""
        record = json.loads(snapshot)
        return GradingContext(
            version=f"v{record['version']}",
            question=record["prompt"],
            answer=text,
            reference=record["reference_explanation"],
            criteria=record["criteria"],
            critical_misconceptions={
                f"misconception-{index}": value
                for index, value in enumerate(record["critical_misconceptions"], start=1)
            },
            sources={source["id"]: source["url"] for source in record["sources"]},
        )

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
                   WHERE selection.local_date = ? AND submission.item_id IS NULL""",
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
                """SELECT selection.kind, item.id, item.local_date, item.section_title,
                          item.snapshot_json, item.draft_text, item.draft_version,
                          request.state, request.error
                   FROM practice_selections AS selection
                   JOIN practice_items AS item ON item.id = selection.item_id
                   LEFT JOIN practice_submissions AS submission ON submission.item_id = item.id
                   LEFT JOIN grading_requests AS request ON request.id = submission.current_request_id
                   WHERE selection.local_date = ?
                   ORDER BY selection.slot""",
                (local_date,),
            )
        ]
        unfinished = [
            self._item(row)
            for row in connection.execute(
                """SELECT item.id, item.local_date, item.section_title, item.snapshot_json,
                          item.draft_text, item.draft_version, NULL, NULL
                   FROM practice_items AS item
                   WHERE trim(item.draft_text) <> ''
                     AND item.id NOT IN (SELECT item_id FROM practice_submissions)
                     AND NOT EXISTS (
                       SELECT 1 FROM practice_selections WHERE local_date = ? AND item_id = item.id
                   )
                   ORDER BY item.local_date DESC, item.created_at DESC, item.id""",
                (local_date,),
            )
        ]
        calendar_date = date.fromisoformat(local_date)
        week_start = (calendar_date - timedelta(days=calendar_date.weekday())).isoformat()
        counts = dict(connection.execute(
            """SELECT local_date, COUNT(*) FROM practice_credits
               WHERE local_date BETWEEN ? AND ? GROUP BY local_date""",
            (week_start, local_date),
        ))
        first_use_date, = connection.execute("SELECT MIN(local_date) FROM practice_days").fetchone()
        submitted = [
            self._item(row)
            for row in connection.execute(
                """SELECT item.id, item.local_date, item.section_title,
                          item.snapshot_json, item.draft_text, item.draft_version, request.state, request.error
                   FROM practice_submissions AS submission
                   JOIN practice_items AS item ON item.id = submission.item_id
                   JOIN grading_requests AS request ON request.id = submission.current_request_id
                   WHERE (submission.completed_date >= ? OR request.state <> 'assessed')
                     AND NOT EXISTS (
                        SELECT 1 FROM practice_selections WHERE local_date = ? AND item_id = item.id
                     )
                   ORDER BY submission.completed_at DESC, item.id""",
                (week_start, local_date),
            )
        ]
        recommended = sum(selection.kind in ("review", "variety") for selection in selections)
        shortage = None
        if recommended < DAILY_GOAL:
            shortage = f"Only {recommended} distinct recommendations were available. Your daily goal is {DAILY_GOAL}."
        return PracticeToday(
            local_date, original_timezone, day.timezone, DAILY_GOAL, self.answer_limit, shortage, selections, unfinished,
            submitted, counts, first_use_date,
        )

    @staticmethod
    def _item(row: Sequence) -> PracticeItem:
        """Project an immutable stored snapshot onto learner-visible prompt fields."""
        item_id, local_date, section_title, snapshot_json, text, version, request_state, request_error = row
        record = json.loads(snapshot_json)
        question = PracticeQuestion(
            record["id"], record["version"], record["section_id"], section_title,
            record["concept_id"], record["title"], record["prompt"], record["objective"],
            record["difficulty"], tuple(record["expected_answer_words"]),
        )
        grading_status = practice_grading_status(request_state, request_error) if request_state else None
        return PracticeItem(item_id, local_date, question, Draft(text, version), request_state is not None, grading_status)
