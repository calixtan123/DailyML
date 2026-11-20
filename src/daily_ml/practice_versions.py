"""Store immutable answer versions and bind each grading request to exact text."""

import sqlite3
from typing import Literal

SubmissionRole = Literal["original", "revision"]
SubmissionKind = Literal["initial", "clarification", "revision"]

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS practice_submission_versions (
        id TEXT PRIMARY KEY REFERENCES grading_requests(id),
        item_id TEXT NOT NULL REFERENCES practice_submissions(item_id),
        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
        role TEXT NOT NULL CHECK (role IN ('original', 'revision')),
        kind TEXT NOT NULL CHECK (kind IN ('initial', 'clarification', 'revision')),
        parent_version_id TEXT,
        text TEXT NOT NULL,
        supported INTEGER NOT NULL CHECK (supported IN (0, 1)),
        created_at REAL NOT NULL,
        FOREIGN KEY (item_id, parent_version_id) REFERENCES practice_submission_versions(item_id, id),
        UNIQUE (item_id, id),
        UNIQUE (item_id, ordinal),
        CHECK ((kind = 'initial') = (parent_version_id IS NULL)),
        CHECK (role <> 'revision' OR supported = 1),
        CHECK (kind <> 'revision' OR role = 'revision')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_grading_versions (
        request_id TEXT PRIMARY KEY REFERENCES grading_requests(id),
        version_id TEXT NOT NULL REFERENCES practice_submission_versions(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS practice_grading_versions_by_version ON practice_grading_versions(version_id)",
    """
    CREATE TABLE IF NOT EXISTS practice_revisions (
        item_id TEXT PRIMARY KEY REFERENCES practice_submissions(item_id),
        initial_request_id TEXT NOT NULL UNIQUE REFERENCES practice_grading_versions(request_id),
        current_request_id TEXT NOT NULL UNIQUE REFERENCES practice_grading_versions(request_id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS practice_revision_identity_immutable
    BEFORE UPDATE OF item_id, initial_request_id ON practice_revisions
    BEGIN
        SELECT RAISE(ABORT, 'The revision identity is immutable');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS practice_revision_drafts (
        request_id TEXT PRIMARY KEY REFERENCES practice_grading_versions(request_id),
        text TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0)
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS practice_evaluated_requests AS
    SELECT request.*, version.id AS submission_version_id, version.item_id,
           version.role, version.kind AS version_kind, version.parent_version_id,
           version.text AS submitted_text, version.supported, version.ordinal,
           version.created_at AS version_created_at
    FROM practice_grading_versions AS link
    JOIN grading_requests AS request ON request.id = link.request_id
    JOIN practice_submission_versions AS version ON version.id = link.version_id
    """,
)


def initialize_versions(connection: sqlite3.Connection) -> None:
    """Create version storage and backfill legacy originals and clarification chains.

    Parameters
    ----------
    connection : sqlite3.Connection
        Practice initialization's locked schema transaction. Existing request IDs
        serve as stable version IDs, and the parent's clarification state
        distinguishes legacy text submissions from retries, even for equal text.

    Raises
    ------
    sqlite3.Error
        If schema creation or backfill fails, so initialization rolls back.
    """
    for statement in _SCHEMA:
        connection.execute(statement)
    for table, operations in (
        ("practice_submission_versions", ("UPDATE", "DELETE")),
        ("practice_grading_versions", ("UPDATE", "DELETE")),
        ("practice_revisions", ("DELETE",)),
    ):
        for operation in operations:
            connection.execute(f"""
                CREATE TRIGGER IF NOT EXISTS {table}_retain_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'Saved practice evidence is immutable');
                END
            """)
    originals = connection.execute("""
        SELECT submission.item_id, submission.original_request_id, submission.original_text,
               submission.completed_at, COALESCE(support.supported, 0)
        FROM practice_submissions AS submission
        LEFT JOIN practice_submission_support AS support ON support.item_id = submission.item_id
        LEFT JOIN practice_grading_versions AS link ON link.request_id = submission.original_request_id
        WHERE link.request_id IS NULL
    """).fetchall()
    for item_id, request_id, text, created_at, supported in originals:
        version_id = request_id
        save_answer_version(
            connection, item_id, version_id, text, "original", "initial", supported, created_at,
        )
        children = connection.execute("""
            WITH RECURSIVE chain(id, position) AS (
                VALUES (?, 0)
                UNION ALL
                SELECT request.id, chain.position + 1 FROM chain
                JOIN grading_requests AS request ON request.retry_of = chain.id
                WHERE request.kind = 'practice'
            )
            SELECT request.id, json_extract(request.context_json, '$.answer'),
                   request.created_at, parent.state
            FROM chain
            JOIN grading_requests AS request ON request.id = chain.id
            JOIN grading_requests AS parent ON parent.id = request.retry_of
            WHERE chain.position > 0
            ORDER BY chain.position
        """, (request_id,)).fetchall()
        for request_id, answer, timestamp, parent_state in children:
            if parent_state == "needs_clarification":
                save_answer_version(
                    connection, item_id, request_id, answer, "original", "clarification",
                    supported, timestamp, parent_version_id=version_id,
                )
                version_id = request_id
            else:
                bind_grading_version(connection, request_id, version_id)


def save_answer_version(
    connection: sqlite3.Connection,
    item_id: str,
    request_id: str,
    text: str,
    role: SubmissionRole,
    kind: SubmissionKind,
    supported: bool,
    created_at: float,
    *,
    parent_version_id: str | None = None,
) -> None:
    """Freeze one new answer under the UUID of its first grading request.

    Parameters
    ----------
    connection : sqlite3.Connection
        The same transaction that saves the grading request.
    item_id : str
        Original practice item owning every version.
    request_id : str
        First grading request evaluating this exact text.
    text : str
        Exact answer the learner submitted.
    role : {'original', 'revision'}
        Schedule-owning original track or follow-on assisted practice.
    kind : {'initial', 'clarification', 'revision'}
        Action that produced this answer version.
    supported : bool
        Whether the learner had support when writing this version.
    created_at : float
        UTC submission timestamp in seconds.
    parent_version_id : str or None
        Preceding version, or the original being revised.
    """
    connection.execute("""
        INSERT INTO practice_submission_versions
            (id, item_id, ordinal, role, kind, parent_version_id, text, supported, created_at)
        SELECT ?, ?, COALESCE(MAX(ordinal), 0) + 1, ?, ?, ?, ?, ?, ?
        FROM practice_submission_versions WHERE item_id = ?
    """, (request_id, item_id, role, kind, parent_version_id, text, supported, created_at, item_id))
    bind_grading_version(connection, request_id, request_id)


def bind_grading_version(connection: sqlite3.Connection, request_id: str, version_id: str) -> None:
    """Link an outbound grading request to the existing immutable answer it evaluates.

    Parameters
    ----------
    connection : sqlite3.Connection
        The same transaction that saves the grading request.
    request_id : str
        Grading request identity.
    version_id : str
        Stable identity of the exact answer being evaluated.
    """
    connection.execute("INSERT INTO practice_grading_versions VALUES (?, ?)", (request_id, version_id))
