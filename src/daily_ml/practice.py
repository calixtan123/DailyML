"""Select, snapshot and save local practice independently of model access."""

import json
import sqlite3
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone as utc
from random import choice
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .bank import SectionNotFound
from .database import Database

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
            else:
                item_id = self._existing_focused_item(candidates, existing) if section_id or question_id else None
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
        if len(text) > self.answer_limit:
            raise DraftInvalid(f"Keep your answer to {self.answer_limit:,} characters or fewer.")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise DraftInvalid("The draft contains an invalid Unicode character.") from None

        day = self._day(timezone)
        with self.database.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT draft_text, draft_version FROM practice_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise PracticeItemNotFound(item_id)
            saved = Draft(*row)
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
    def _append(
        connection: sqlite3.Connection, day: _Day, candidate: _Candidate, slot: int, kind: SelectionKind
    ) -> str:
        """Link an unfinished answer when present or create a new immutable snapshot."""
        saved = connection.execute(
            """SELECT id FROM practice_items
               WHERE question_id = ? AND trim(draft_text) <> ''
               ORDER BY created_at DESC, id LIMIT 1""",
            (candidate.question_id,),
        ).fetchone()
        if saved:
            item_id = saved[0]
        else:
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
    def _existing_focused_item(candidates: Sequence[_Candidate], existing: dict[str, str]) -> str | None:
        """Reopen a day's existing item when focused choices have been exhausted."""
        return next((existing[item.question_id] for item in candidates if item.question_id in existing), None)

    @staticmethod
    def _exhausted_message(section_id: str | None) -> str:
        """Tell the learner when their requested pool has no additional question."""
        if section_id:
            return "No questions are available in this section. Choose another section."
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
                          item.snapshot_json, item.draft_text, item.draft_version
                   FROM practice_selections AS selection
                   JOIN practice_items AS item ON item.id = selection.item_id
                   WHERE selection.local_date = ?
                   ORDER BY selection.slot""",
                (local_date,),
            )
        ]
        unfinished = [
            self._item(row)
            for row in connection.execute(
                """SELECT item.id, item.local_date, item.section_title, item.snapshot_json,
                          item.draft_text, item.draft_version
                   FROM practice_items AS item
                   WHERE trim(item.draft_text) <> '' AND NOT EXISTS (
                       SELECT 1 FROM practice_selections WHERE local_date = ? AND item_id = item.id
                   )
                   ORDER BY item.local_date DESC, item.created_at DESC, item.id""",
                (local_date,),
            )
        ]
        recommended = sum(selection.kind in ("review", "variety") for selection in selections)
        shortage = None
        if recommended < DAILY_GOAL:
            shortage = f"Only {recommended} distinct recommendations were available. Your daily goal is {DAILY_GOAL}."
        return PracticeToday(
            local_date, original_timezone, day.timezone, DAILY_GOAL, self.answer_limit, shortage, selections, unfinished
        )

    @staticmethod
    def _item(row: Sequence) -> PracticeItem:
        """Project an immutable stored snapshot onto learner-visible prompt fields."""
        item_id, local_date, section_title, snapshot_json, text, version = row
        record = json.loads(snapshot_json)
        question = PracticeQuestion(
            record["id"], record["version"], record["section_id"], section_title,
            record["concept_id"], record["title"], record["prompt"], record["objective"],
            record["difficulty"], tuple(record["expected_answer_words"]),
        )
        return PracticeItem(item_id, local_date, question, Draft(text, version))
