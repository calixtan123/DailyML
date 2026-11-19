"""Read immutable, locally saved practice history in stable pages."""

import base64
import json
from dataclasses import dataclass
from datetime import date

from .database import Database
from .practice import Practice, PracticeItemNotFound
from .scheduling import ReviewState, read_reviews

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


class HistoryCursorInvalid(ValueError):
    """The requested history cursor cannot safely identify a next page."""


@dataclass(frozen=True)
class HistoryEntry:
    """A compact, saved-history row that can open its exact practice item.

    Parameters
    ----------
    item_id : str
        Immutable practice item UUID for the history detail or resume route.
    local_date : str
        Saved practice date in ISO format.
    created_at : float
        Immutable UTC timestamp that participates in stable keyset ordering.
    section_id : str
        Saved curriculum section identifier.
    section_title : str
        Saved section title from the item snapshot.
    question_id : str
        Stable question identity recorded by the practice item.
    title : str
        Saved question title from the item snapshot.
    status : str
        Current durable work state suitable for an index label.
    draft_text : str
        Current locally saved draft text, if any.
    completed_date : str or None
        Original completion date, if the learner submitted an answer.
    """

    item_id: str
    local_date: str
    created_at: float
    section_id: str
    section_title: str
    question_id: str
    title: str
    status: str
    draft_text: str
    completed_date: str | None


@dataclass(frozen=True)
class HistoryPage:
    """One bounded page of saved history and its optional continuation cursor."""

    entries: tuple[HistoryEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class HistorySection:
    """One saved section available for a history filter."""

    id: str
    title: str


class History:
    """Query saved practice rows without consulting the mutable question bank."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list(
        self,
        *,
        section_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> HistoryPage:
        """Return one stable page of rows ordered by saved creation time and ID.

        Parameters
        ----------
        section_id : str or None
            Optional saved section filter.
        start_date : str or None
            Inclusive ISO practice-date lower bound.
        end_date : str or None
            Inclusive ISO practice-date upper bound.
        cursor : str or None
            Opaque continuation returned by a previous call with these filters.
        limit : int
            Number of rows to return, from one through ``MAX_PAGE_SIZE``.

        Returns
        -------
        HistoryPage
            Rows backed by immutable practice snapshots and a next-page cursor.

        Raises
        ------
        HistoryCursorInvalid
            If a cursor, date bound, or page size is invalid.
        """
        start = self._validate_date(start_date)
        end = self._validate_date(end_date)
        if start is not None and end is not None and start > end:
            raise HistoryCursorInvalid("History start date must be on or before its end date.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise HistoryCursorInvalid(f"History page size must be between 1 and {MAX_PAGE_SIZE}.")
        marker = self._decode_cursor(cursor) if cursor is not None else None

        predicates = []
        parameters: dict[str, object] = {"limit": limit + 1}
        if section_id is not None:
            predicates.append("item.section_id = :section_id")
            parameters["section_id"] = section_id
        if start is not None:
            predicates.append("item.local_date >= :start_date")
            parameters["start_date"] = start.isoformat()
        if end is not None:
            predicates.append("item.local_date <= :end_date")
            parameters["end_date"] = end.isoformat()
        if marker is not None:
            predicates.append("(item.created_at < :cursor_time OR (item.created_at = :cursor_time AND item.id < :cursor_id))")
            parameters.update(cursor_time=marker[0], cursor_id=marker[1])
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        with self.database.connect() as connection:
            rows = connection.execute(f"""
                SELECT item.id, item.local_date, item.created_at, item.section_id, item.section_title,
                       item.question_id, json_extract(item.snapshot_json, '$.title'), item.draft_text,
                       submission.completed_date, request.state
                FROM practice_items AS item
                LEFT JOIN practice_submissions AS submission ON submission.item_id = item.id
                LEFT JOIN grading_requests AS request ON request.id = submission.current_request_id
                {where}
                ORDER BY item.created_at DESC, item.id DESC
                LIMIT :limit
            """, parameters).fetchall()
        has_next = len(rows) > limit
        entries = tuple(self._entry(row) for row in rows[:limit])
        next_cursor = self._encode_cursor(entries[-1]) if has_next else None
        return HistoryPage(entries, next_cursor)

    def sections(self) -> tuple[HistorySection, ...]:
        """Return sections from saved practice items, ordered for a filter control.

        Returns
        -------
        tuple of HistorySection
            Distinct immutable section identifiers and saved display titles.
        """
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT section_id, section_title
                FROM practice_items
                GROUP BY section_id, section_title
                ORDER BY section_title COLLATE NOCASE, section_id
            """).fetchall()
        return tuple(HistorySection(*row) for row in rows)

    def detail(self, item_id: str) -> dict:
        """Return one saved item's immutable question and learning evidence.

        Parameters
        ----------
        item_id : str
            Stable practice item identity.

        Returns
        -------
        dict
            Snapshot-backed question, latest saved draft, submission evidence,
            support-use record, and current review state.

        Raises
        ------
        PracticeItemNotFound
            If no saved practice item has the requested identity.
        """
        with self.database.connect() as connection:
            connection.row_factory = None
            row = connection.execute("""
                SELECT id, local_date, section_title, snapshot_json, draft_text, draft_version
                FROM practice_items WHERE id = ?
            """, (item_id,)).fetchone()
            if row is None:
                raise PracticeItemNotFound(item_id)
            saved_id, local_date, section_title, snapshot_json, draft_text, draft_version = row
            question = json.loads(snapshot_json)
            question["section_title"] = section_title
            submission = Practice._read_submission(connection, saved_id)
            review = read_reviews(connection).get(question["concept_id"], ReviewState())
            reveals = [kind for kind, in connection.execute("""
                SELECT kind FROM practice_help_events WHERE item_id = ? ORDER BY viewed_at, kind
            """, (saved_id,))]
            gap_completed = connection.execute(
                "SELECT 1 FROM practice_gap_completions WHERE item_id = ?", (saved_id,),
            ).fetchone() is not None
        request = submission["request"] if submission is not None else None
        return {
            "item": {"id": saved_id, "local_date": local_date, "submitted": submission is not None},
            "item_id": saved_id,
            "local_date": local_date,
            "question": question,
            "draft": {"text": draft_text, "version": draft_version},
            "submission": submission,
            "support": {"reveals": reveals, "gap_completed": gap_completed} if reveals or gap_completed else None,
            "assessed_at": request["finished_at"] if request and request["state"] == "assessed" else None,
            "review": {
                "due_date": review.due_date.isoformat() if review.due_date else None,
                "awaiting_assessment": review.awaiting_assessment,
            },
        }

    @staticmethod
    def _entry(row: tuple) -> HistoryEntry:
        """Convert one SQLite history row to its small index projection."""
        item_id, local_date, created_at, section_id, section_title, question_id, title, draft, completed, state = row
        if completed is None:
            status = "unfinished" if draft.strip() else "not_started"
        elif state == "assessed":
            status = "assessed"
        else:
            status = "pending_assessment"
        return HistoryEntry(item_id, local_date, created_at, section_id, section_title, question_id, title, status, draft, completed)

    @staticmethod
    def _validate_date(value: str | None) -> date | None:
        """Parse an optional strict ISO date bound."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise HistoryCursorInvalid("History dates must use ISO YYYY-MM-DD text.")
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise HistoryCursorInvalid("History dates must use ISO YYYY-MM-DD text.") from error

    @staticmethod
    def _encode_cursor(entry: HistoryEntry) -> str:
        """Encode the final stable ordering key without exposing query syntax."""
        payload = json.dumps([entry.created_at, entry.item_id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[float, str]:
        """Decode and validate a cursor's immutable ordering key."""
        if not isinstance(value, str) or not value:
            raise HistoryCursorInvalid("History cursor is invalid.")
        try:
            padding = "=" * (-len(value) % 4)
            timestamp, item_id = json.loads(base64.urlsafe_b64decode(value + padding))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise HistoryCursorInvalid("History cursor is invalid.") from error
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not isinstance(item_id, str) or not item_id:
            raise HistoryCursorInvalid("History cursor is invalid.")
        return float(timestamp), item_id
