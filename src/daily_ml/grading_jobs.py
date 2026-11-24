"""Record and claim local grading work, preserving uncertainty across retries and restarts."""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from uuid import uuid4

from .config import Settings
from .database import Database, PreferenceConflict
from .grading import PROMPT_VERSION, SCHEMA_VERSION, GradingContext, calculate_grade
from .provider import MESSAGES, DispatchCancelled, Provider, ProviderFailure
from .scheduling import record_initial_assessment

logger = logging.getLogger(__name__)
DAY_SECONDS = 24 * 60 * 60
# Yield between bounded SQLite busy waits so Settings and other workers can run.
LOCAL_WRITE_RETRY_SECONDS = 0.1
ERROR_STATES = {"access": "awaiting_access", "allowance": "awaiting_allowance", "outcome_unknown": "outcome_unknown"}
PRACTICE_MESSAGES = {
    "unsent": "Grading was queued. You can send this saved answer when ready.",
    "model_required": "Choose a model in Settings before sending your saved answer.",
    "refusal": "The model declined to assess this answer. You can try again.",
}
UNSENT_MESSAGE = "The reference test could not be sent. You can run it again."


class RetryConflict(ValueError):
    """The requested parent is missing, successful, or belongs to a different model."""


def practice_grading_status(state: str, error: str | None) -> str:
    """Summarize the learner-visible state of a practice grading request.

    Parameters
    ----------
    state : str
        Request state stored in the grading ledger.
    error : str or None
        Saved recovery reason, when present.

    Returns
    -------
    str
        A status for the practice selector, including a recovered unsent queue entry.
    """
    if state == "queued" and error == "unsent":
        return "ready_to_send"
    if state == "preparing":
        return "in_flight"
    return state


def public_request(row: sqlite3.Row) -> dict:
    """Read only display-safe fields from the ledger."""
    result = {key: row[key] for key in ("id", "state", "model_id", "grade", "error", "retry_of", "schema_mode",
                                        "prompt_version", "schema_version", "created_at", "finished_at", "latency_ms")}
    result["feedback"] = json.loads(row["feedback_json"]) if row["feedback_json"] else None
    context = json.loads(row["context_json"])
    result["reference"] = {key: context[key] for key in ("question", "answer", "sources")}
    result["usage"] = json.loads(row["usage_json"]) if row["usage_json"] else None
    if row["kind"] == "practice":
        if "submission_version_id" in row.keys():
            result.update(
                submission_version_id=row["submission_version_id"], role=row["role"], supported=bool(row["supported"]),
            )
        result["model_id"] = row["model_id"] or None
        status = practice_grading_status(row["state"], row["error"])
        result["can_send"] = status == "ready_to_send"
        result["state"] = "queued" if result["can_send"] else status
        result["reference"].pop("answer")
        result["message"] = PRACTICE_MESSAGES.get(row["error"], MESSAGES.get(row["error"]))
        return result
    result["message"] = UNSENT_MESSAGE if row["error"] == "unsent" else MESSAGES.get(row["error"])
    return result


def enqueue_practice_request(
    db: sqlite3.Connection,
    context: GradingContext,
    model_id: str | None,
    timestamp: float,
    *,
    retry_of: str | None = None,
    mode: str = "schema",
) -> str:
    """Insert one logical request within the caller's open transaction.

    Parameters
    ----------
    db : sqlite3.Connection
        The transaction writing the practice answer or explicit recovery.
    context : GradingContext
        The exact versioned material that can be sent for assessment.
    model_id : str or None
        Effective model at this action, or None until the learner selects one.
    timestamp : float
        UTC seconds for this action.
    retry_of : str or None
        The preceding grading request, when applicable.
    mode : str
        Schema or explicitly authorized JSON fallback mode.

    Returns
    -------
    str
        New logical grading request identity.
    """
    request_id = str(uuid4())
    db.execute(
        """INSERT INTO grading_requests
           (id, kind, state, model_id, created_at, retry_of, schema_mode, prompt_version,
            schema_version, context_json, error)
           VALUES (?, 'practice', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, "queued" if model_id else "awaiting_access", model_id or "", timestamp,
         retry_of, mode, PROMPT_VERSION, SCHEMA_VERSION, context.model_dump_json(),
         None if model_id else "model_required"),
    )
    return request_id


def _is_write_contention(error: sqlite3.OperationalError) -> bool:
    """Use the primary SQLite code, including extended BUSY and LOCKED variants."""
    primary_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    return primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


async def _write_when_available(operation: Callable):
    """Retain ownership of local work while a competing SQLite writer finishes."""
    while True:
        try:
            return await asyncio.to_thread(operation)
        except sqlite3.OperationalError as error:
            if not _is_write_contention(error):
                raise
            await asyncio.sleep(LOCAL_WRITE_RETRY_SECONDS)


class GradingJobs:
    """Coordinate persisted logical requests and chargeable or uncertain attempts."""

    def __init__(self, database: Database, provider: Provider, settings: Settings, *, clock: Callable = time.time):
        self.database = database
        self.provider = provider
        self.settings = settings
        self.clock = clock
        database.initialize_grading()

    def recover_interrupted(self) -> None:
        """At exclusive application startup, stop pending jobs and preserve sent uncertainty."""
        with self.database.connect() as db:
            db.execute("""UPDATE grading_requests SET state='outcome_unknown', error='outcome_unknown', finished_at=?
                          WHERE state='in_flight'""", (self.clock(),))
            db.execute("""UPDATE grading_requests SET state='failed_recoverable', error='unsent', finished_at=?
                          WHERE kind='connection_test' AND state IN ('queued','preparing')""", (self.clock(),))
            db.execute("""UPDATE grading_requests SET state='queued', error='unsent'
                          WHERE kind='practice' AND state IN ('queued','preparing')""")
            # Evaluation work is explicitly resumed by its locked CLI invocation. Unlike an
            # in-flight request, a preparing request has no pre-send reservation and is safe to
            # return to the queue after a process crash.
            db.execute("""UPDATE grading_requests SET state='queued', error=NULL, finished_at=NULL
                          WHERE kind='evaluation' AND state='preparing'
                          AND NOT EXISTS (SELECT 1 FROM model_attempts WHERE request_id=grading_requests.id)""")

    def enqueue_connection_test(self, idempotency_key: str, model_id: str, *, retry_of: str | None = None) -> dict:
        """Save a reference test or reuse a previous click or active test.

        Parameters
        ----------
        idempotency_key : str
            Stable identifier per browser action, including uncertain HTTP delivery.
        model_id : str
            Selected account model.
        retry_of : str or None
            Optional failed parent. An explicit schema rejection enables JSON mode.

        Returns
        -------
        dict
            The authoritative snapshot associated with this click.

        Raises
        ------
        RetryConflict
            If a retry does not identify recoverable work for this model.
        PreferenceConflict
            If a new action names a model different from the effective selection or active test.
        """
        with self.database.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("""SELECT r.* FROM grading_action_keys k JOIN grading_requests r
                                  ON r.id=k.request_id WHERE k.action_key=?""", (idempotency_key,)).fetchone()
            if prior:
                return public_request(prior)
            selected, _ = self.settings.effective_model(self.database.read_preferences(db))
            if selected is not None and model_id != selected:
                raise PreferenceConflict("The selected model changed. Refresh the model catalog before testing.")
            active = db.execute("""SELECT * FROM grading_requests WHERE kind='connection_test'
                                   AND state IN ('queued','preparing','in_flight') LIMIT 1""").fetchone()
            mode = "schema"
            if retry_of:
                parent = db.execute("SELECT * FROM grading_requests WHERE id=? AND kind='connection_test'", (retry_of,)).fetchone()
                if not parent or not parent["error"] or parent["model_id"] != model_id:
                    raise RetryConflict("Choose a failed test for this model to retry.")
                if parent["error"] == "schema_unsupported" or parent["schema_mode"] == "json":
                    mode = "json"
                child = db.execute("SELECT * FROM grading_requests WHERE retry_of=?", (retry_of,)).fetchone()
                active = child or active
            if active:
                if active["model_id"] != model_id:
                    raise PreferenceConflict(
                        "A connection test for another model is pending. Wait for it to finish and try again."
                    )
                request_id = active["id"]
            else:
                request_id = str(uuid4())
                context = parent["context_json"] if retry_of else GradingContext.connection_test().model_dump_json()
                db.execute("""INSERT INTO grading_requests
                              (id,kind,state,model_id,created_at,retry_of,schema_mode,prompt_version,
                               schema_version,context_json) VALUES (?,'connection_test','queued',?,?,?,?,?,?,?)""",
                           (request_id, model_id, self.clock(), retry_of, mode, PROMPT_VERSION, SCHEMA_VERSION,
                            context))
            db.execute("INSERT INTO grading_action_keys VALUES (?,?)", (idempotency_key, request_id))
            return public_request(db.execute("SELECT * FROM grading_requests WHERE id=?", (request_id,)).fetchone())

    def enqueue_evaluation(
        self,
        context: GradingContext,
        model_id: str,
        *,
        retry_of: str | None = None,
        mode: str = "schema",
        connection: sqlite3.Connection | None = None,
    ) -> str:
        """Persist one fixed evaluation case before it can be sent.

        Parameters
        ----------
        context : GradingContext
            Immutable reviewed case context, including its fixed answer.
        model_id : str
            Account model selected for the explicit evaluation invocation.
        retry_of : str or None
            Previous evaluation request when resuming a request that was already admitted.
        mode : str
            The saved output mode from the previous request.

        Returns
        -------
        str
            The durable ledger request identifier.
        """
        request_id = str(uuid4())
        def insert(db: sqlite3.Connection) -> None:
            """Write the evaluation request into the caller's transaction."""
            db.execute(
                """INSERT INTO grading_requests
                   (id,kind,state,model_id,created_at,retry_of,schema_mode,prompt_version,schema_version,context_json)
                   VALUES (?,'evaluation','queued',? ,?,?,?, ?,?,?)""",
                (request_id, model_id, self.clock(), retry_of, mode, PROMPT_VERSION, SCHEMA_VERSION,
                 context.model_dump_json()),
            )
        if connection is None:
            with self.database.connect() as db:
                insert(db)
        else:
            insert(connection)
        return request_id

    def get_request(self, request_id: str) -> dict | None:
        """Return the saved result without contacting the provider.

        Parameters
        ----------
        request_id : str
            Logical grading request identifier.

        Returns
        -------
        dict or None
            Saved snapshot, or None for an unknown request.
        """
        with self.database.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM grading_requests WHERE id=?", (request_id,)).fetchone()
            if row is not None and row["kind"] == "practice":
                row = db.execute("SELECT * FROM practice_evaluated_requests WHERE id=?", (request_id,)).fetchone()
            return public_request(row) if row else None

    def latest_connection_test(self) -> dict | None:
        """Return the latest connection test for Settings after navigation or restart.

        Returns
        -------
        dict or None
            Latest test snapshot, or None before the first test.
        """
        with self.database.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT * FROM grading_requests WHERE kind='connection_test'
                                ORDER BY created_at DESC, rowid DESC LIMIT 1""").fetchone()
            return public_request(row) if row else None

    def usage_count(self) -> int:
        """Count outbound reservations in the rolling UTC window (now - 24h, now].

        Returns
        -------
        int
            Locally observed model attempts, including uncertain outcomes.
        """
        now = self.clock()
        with self.database.connect() as db:
            return db.execute(
                """SELECT
                       (SELECT COUNT(*) FROM model_attempts WHERE sent_at>:start AND sent_at<=:end)
                     + (SELECT COUNT(*) FROM model_rate_limit_retries WHERE sent_at>:start AND sent_at<=:end)""",
                {"start": now - DAY_SECONDS, "end": now},
            ).fetchone()[0]

    def _claim(self, request_id: str | None) -> sqlite3.Row | None:
        """Claim unsent work transactionally before starting authorization or transport."""
        with self.database.connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                """SELECT * FROM grading_requests WHERE state='queued' AND error IS NULL
                   AND (kind IN ('connection_test','practice') OR id=?)
                   AND (? IS NULL OR id=?) ORDER BY rowid LIMIT 1""", (request_id, request_id, request_id)
            ).fetchone()
            if job:
                db.execute("UPDATE grading_requests SET state='preparing' WHERE id=?", (job["id"],))
            return job

    def _reserve(self, request_id: str) -> None:
        """Reserve only a live claim, including when a deadline races this transaction."""
        with self.database.connect() as db:
            changed = db.execute("""UPDATE grading_requests SET state='in_flight'
                                    WHERE id=? AND state='preparing'""", (request_id,))
            if changed.rowcount != 1:
                # Finalization acquired SQLite first. A late worker cannot reopen a terminal job.
                raise ProviderFailure("unavailable", confirmed_unsent=True)
            db.execute("INSERT INTO model_attempts(request_id,sent_at) VALUES (?,?)", (request_id, self.clock()))

    def _reserve_retry(self, request_id: str) -> None:
        """Reserve the single automatic resend slot on this still-active practice request."""
        with self.database.connect() as db:
            changed = db.execute(
                """INSERT INTO model_rate_limit_retries
                   SELECT id, ? FROM grading_requests
                   WHERE id=? AND kind='practice' AND state='in_flight'
                   ON CONFLICT(request_id) DO NOTHING""",
                (self.clock(), request_id),
            )
            if changed.rowcount != 1:
                raise ProviderFailure("unavailable", confirmed_unsent=True)

    async def run_pending(self, request_id: str | None = None) -> None:
        """Consume available queued work with exactly one send per durable claim.

        Parameters
        ----------
        request_id : str or None
            Process only this request when invoked by an explicit browser action.

        Raises
        ------
        sqlite3.Error
            Persistence failed. The existing durable state records whether a send was possible.
        """
        while (row := await _write_when_available(lambda: self._claim(request_id))) is not None:
            started = time.monotonic()
            claimed_id = row["id"]
            context = GradingContext.model_validate_json(row["context_json"])
            result = None
            grade = None
            error = None
            confirmed_unsent = False
            cancelled = None
            for automatic_retry in (False, True):
                reserve = self._reserve_retry if automatic_retry else self._reserve
                try:
                    result = await self.provider.grade(context, row["model_id"], mode=row["schema_mode"],
                                                       before_send=lambda: reserve(claimed_id))
                    grade = calculate_grade(result.feedback, context, self.settings)
                    state = result.feedback.feedback.status
                except ProviderFailure as failure:
                    error = failure.category
                    confirmed_unsent = failure.confirmed_unsent
                    state = ERROR_STATES.get(error, "failed_recoverable")
                    if row["kind"] == "practice" and not automatic_retry and failure.retry_after is not None:
                        try:
                            await asyncio.sleep(failure.retry_after)
                        except asyncio.CancelledError as failure:
                            # The first response was confirmed and no retry had been reserved.
                            cancelled = failure
                        else:
                            error = None
                            continue
                except DispatchCancelled as failure:
                    cancelled = failure
                    confirmed_unsent = not failure.admitted
                    error = "unsent" if confirmed_unsent else "outcome_unknown"
                    state = ERROR_STATES.get(error, "failed_recoverable")
                except sqlite3.OperationalError as failure:
                    # A busy reservation rolled back before the SDK could send.
                    if not _is_write_contention(failure):
                        raise
                    error = "unsent"
                    state = "failed_recoverable"
                break
            latency_ms = round((time.monotonic() - started) * 1000)

            def finalize() -> None:
                """Commit this result and release a proven unsent reservation together."""
                with self.database.connect() as db:
                    changed = db.execute(
                        """UPDATE grading_requests SET state=?, error=?, grade=?, feedback_json=?, response_id=?,
                           usage_json=?, finished_at=?, latency_ms=?
                           WHERE id=? AND state IN ('preparing','in_flight')""",
                        (state, error, grade, result.feedback.feedback.model_dump_json() if result else None,
                         result.response_id if result else None, json.dumps(result.usage) if result else None,
                         self.clock(), latency_ms, claimed_id),
                    )
                    if changed.rowcount == 1 and state == "assessed" and row["kind"] == "practice":
                        timezone, _ = self.settings.effective_timezone(self.database.read_preferences(db))
                        record_initial_assessment(db, claimed_id, timezone, self.settings.review_intervals)
                    if changed.rowcount == 1 and confirmed_unsent:
                        if automatic_retry:
                            db.execute("DELETE FROM model_rate_limit_retries WHERE request_id=?", (claimed_id,))
                        else:
                            db.execute("DELETE FROM model_attempts WHERE request_id=?", (claimed_id,))

            await _write_when_available(finalize)
            if cancelled is not None:
                raise cancelled
            logger.info("Grading state=%s category=%s model=%s latency_ms=%d tokens=%s",
                        state, error, row["model_id"], latency_ms, result.usage if result else None)
