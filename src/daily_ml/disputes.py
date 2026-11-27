"""Persist assessment disputes without changing immutable grading evidence."""

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .grading_jobs import public_request
from .practice_versions import bind_grading_version
from .scheduling import select_review_evidence

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS practice_disputes (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES practice_submissions(item_id),
        target_request_id TEXT NOT NULL UNIQUE REFERENCES practice_grading_versions(request_id),
        target_version_id TEXT NOT NULL REFERENCES practice_submission_versions(id),
        role TEXT NOT NULL CHECK (role IN ('original', 'revision')),
        objection TEXT NOT NULL,
        opened_at REAL NOT NULL,
        resolved_at REAL,
        selected_request_id TEXT REFERENCES practice_grading_versions(request_id),
        CHECK ((resolved_at IS NULL) = (selected_request_id IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_dispute_reassessments (
        request_id TEXT PRIMARY KEY REFERENCES practice_grading_versions(request_id),
        dispute_id TEXT NOT NULL REFERENCES practice_disputes(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS practice_dispute_reassessments_by_dispute ON practice_dispute_reassessments(dispute_id)",
    """
    CREATE TABLE IF NOT EXISTS practice_dispute_reassessment_actions (
        action_key TEXT PRIMARY KEY,
        dispute_id TEXT NOT NULL REFERENCES practice_disputes(id),
        request_id TEXT NOT NULL UNIQUE REFERENCES practice_dispute_reassessments(request_id)
    )
    """,
)


# Summary queries join the current revision or original request. A resolved
# dispute may choose a different assessment for that request.
CURRENT_ASSESSMENT_JOIN = """
    LEFT JOIN practice_disputes AS disputed ON disputed.target_request_id = request.id
    LEFT JOIN grading_requests AS accepted ON accepted.id = disputed.selected_request_id
"""
CURRENT_ASSESSMENT_SUMMARY = """
    CASE WHEN request.state = 'assessed' THEN COALESCE(accepted.grade, request.grade) END,
    EXISTS (
        SELECT 1 FROM practice_disputes AS pending
        WHERE pending.target_request_id IN (submission.current_request_id, revision.current_request_id)
          AND pending.selected_request_id IS NULL
    ),
    EXISTS (
        SELECT 1 FROM practice_revision_drafts AS draft
        WHERE draft.request_id = submission.current_request_id
          AND revision.item_id IS NULL
    )
"""


def initialize_disputes(connection: sqlite3.Connection) -> None:
    """Create additive immutable dispute storage in the active schema transaction."""
    for statement in _SCHEMA:
        connection.execute(statement)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS practice_dispute_identity_immutable
        BEFORE UPDATE OF item_id, target_request_id, target_version_id, role, objection, opened_at
        ON practice_disputes BEGIN
            SELECT RAISE(ABORT, 'A dispute target and objection are immutable');
        END
    """)


def dispute_projection(connection: sqlite3.Connection, item_id: str, dispute_id: str | None = None) -> dict | None:
    """Return one current feedback comparison for an item's named or latest dispute."""
    connection.row_factory = sqlite3.Row
    if dispute_id is None:
        dispute = connection.execute("""
            SELECT * FROM practice_disputes WHERE item_id = ? ORDER BY opened_at DESC, rowid DESC LIMIT 1
        """, (item_id,)).fetchone()
    else:
        dispute = connection.execute(
            "SELECT * FROM practice_disputes WHERE item_id = ? AND id = ?", (item_id, dispute_id),
        ).fetchone()
    if dispute is None:
        return None
    assessments = connection.execute("""
        SELECT request.* FROM practice_evaluated_requests AS request
        WHERE request.id = ? OR request.id IN (
            SELECT request_id FROM practice_dispute_reassessments WHERE dispute_id = ?
        ) ORDER BY request.created_at, request.id
    """, (dispute["target_request_id"], dispute["id"])).fetchall()
    return {
        "id": dispute["id"], "status": "resolved" if dispute["selected_request_id"] else "open",
        "target_request_id": dispute["target_request_id"], "target_version_id": dispute["target_version_id"],
        "role": dispute["role"], "objection": dispute["objection"], "selected_request_id": dispute["selected_request_id"],
        "assessments": [public_request(row) for row in assessments],
    }


def disputes_projection(connection: sqlite3.Connection, item_id: str) -> list[dict]:
    """Return every dispute for both submission roles in newest-first order."""
    identifiers = connection.execute(
        "SELECT id FROM practice_disputes WHERE item_id = ? ORDER BY opened_at DESC, rowid DESC", (item_id,),
    ).fetchall()
    return [entry for dispute_id, in identifiers if (entry := dispute_projection(connection, item_id, dispute_id))]


def register_reassessment(
    connection: sqlite3.Connection, dispute_id: str, request_id: str,
) -> None:
    """Bind an already-saved grading request to exactly one open dispute."""
    bind_grading_version(
        connection, request_id,
        connection.execute("SELECT target_version_id FROM practice_disputes WHERE id = ?", (dispute_id,)).fetchone()[0],
    )
    connection.execute("INSERT INTO practice_dispute_reassessments VALUES (?, ?)", (request_id, dispute_id))


def selected_reassessment_evidence(
    connection: sqlite3.Connection, dispute_id: str, request_id: str, *, accepted_at: float, timezone: str,
) -> str:
    """Create immutable review evidence for the chosen completed reassessment."""
    row = connection.execute("""
        SELECT dispute.item_id, request.id, json_extract(item.snapshot_json, '$.concept_id'),
               submission.completed_at, submission.completed_date, request.grade, version.supported,
               request.finished_at
        FROM practice_disputes AS dispute
        JOIN practice_dispute_reassessments AS reassessment ON reassessment.dispute_id = dispute.id
        JOIN grading_requests AS request ON request.id = reassessment.request_id
        JOIN practice_submission_versions AS version ON version.id = dispute.target_version_id
        JOIN practice_items AS item ON item.id = dispute.item_id
        JOIN practice_submissions AS submission ON submission.item_id = dispute.item_id
        WHERE dispute.id = ? AND request.id = ? AND dispute.role = 'original'
          AND request.state = 'assessed' AND request.grade IS NOT NULL AND request.finished_at IS NOT NULL
    """, (dispute_id, request_id)).fetchone()
    if row is None:
        raise LookupError("Choose an assessed reassessment for this dispute.")
    item_id, evidence_id, concept_id, submitted_at, submitted_date, grade, supported, _finished_at = row
    accepted = datetime.fromtimestamp(accepted_at, UTC).astimezone(ZoneInfo(timezone)).date().isoformat()
    connection.execute("""
        INSERT OR IGNORE INTO review_evidence
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (evidence_id, item_id, evidence_id, concept_id, submitted_at, submitted_date, grade, supported, accepted))
    return evidence_id


def resolve_dispute(
    connection: sqlite3.Connection, item_id: str, dispute_id: str, assessment_request_id: str,
    intervals: tuple[int, ...], now: float, timezone: str,
) -> None:
    """Atomically validate and choose one assessment, then replay original evidence."""
    connection.row_factory = sqlite3.Row
    dispute = connection.execute(
        "SELECT * FROM practice_disputes WHERE id = ? AND item_id = ?", (dispute_id, item_id),
    ).fetchone()
    if dispute is None or dispute["selected_request_id"] is not None:
        raise LookupError("This dispute is no longer open.")
    chosen = connection.execute("""
        SELECT request.id FROM grading_requests AS request
        WHERE request.id = ? AND request.state = 'assessed' AND (
            request.id = ? OR EXISTS (
                SELECT 1 FROM practice_dispute_reassessments
                WHERE dispute_id = ? AND request_id = request.id
            )
        )
    """, (assessment_request_id, dispute["target_request_id"], dispute_id)).fetchone()
    if chosen is None:
        raise LookupError("Choose an available assessment from this dispute.")
    selected = assessment_request_id
    if dispute["role"] == "original":
        if selected != dispute["target_request_id"]:
            selected = selected_reassessment_evidence(
                connection, dispute_id, selected, accepted_at=now, timezone=timezone,
            )
        select_review_evidence(connection, dispute["item_id"], selected, intervals)
    updated = connection.execute("""
        UPDATE practice_disputes SET selected_request_id = ?, resolved_at = ?
        WHERE id = ? AND selected_request_id IS NULL
    """, (selected, now, dispute_id))
    if updated.rowcount != 1:
        raise LookupError("This dispute is no longer open.")
