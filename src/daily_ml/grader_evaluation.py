"""Run the reviewed fixed grader set through the durable production grading path.

The module keeps fixture text in package data and stores only safe outcome metadata in
the evaluation tables.  Individual fixed answers remain in the immutable package
fixture and are never copied to operational logs or the public report.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from importlib.resources import files
from typing import Iterable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .bank import validate_bank
from .config import Settings
from .database import Database
from .grading import GradingContext, PROMPT_VERSION, SCHEMA_VERSION
from .grading_jobs import GradingJobs
from .provider import Provider

RELEASE_CASE_COUNT = 18
RELEASE_MINIMUM_AGREEMENT = 16
TERMINAL_STATES = {"assessed", "needs_clarification"}
PAUSE_STATES = {"awaiting_access", "awaiting_allowance", "outcome_unknown", "failed_recoverable"}
ACTIVE_STATES = {"queued", "preparing", "in_flight"}
FIXTURE_VERSION = "fixed-v1"
CANONICAL_CASE_IDS = frozenset({
    "leakage-correct", "leakage-concise", "leakage-critical", "metrics-correct", "metrics-critical",
    "metrics-alternative", "bagging-correct", "bagging-gap", "backprop-correct", "batch-online-correct",
    "boosting-tradeoffs-correct", "forest-mechanics-correct", "generalisation-correct", "monitoring-correct",
    "preprocessing-correct", "tree-diversity-correct", "deep-overfit-clarify", "manipulation-resistance",
})


def _suite_fingerprint(cases: tuple["EvaluationCase", ...]) -> str:
    """Return a stable identity for the exact ordered evaluation suite content."""
    encoded = json.dumps(
        [case.model_dump(mode="json") for case in cases], sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvaluationCase(BaseModel):
    """One reviewed, immutable expected outcome for a starter-bank answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    question_id: str
    question_version: int = Field(ge=1)
    rubric_version: str = Field(min_length=1)
    categories: tuple[str, ...] = Field(min_length=1)
    answer: str = Field(min_length=1)
    expected_grades: tuple[int, ...] = ()
    expected_states: tuple[str, ...] = ("assessed",)
    expected_critical_ids: tuple[str, ...] = ()
    required_corrections: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationSummary:
    """Public, sanitized summary for one saved fixed-set run."""

    run_id: str
    completed_count: int
    resumed_count: int
    usage_count: int
    agreement_count: int
    critical_coverage_count: int
    release_passed: bool
    paused_state: str | None


def load_fixed_cases() -> tuple[EvaluationCase, ...]:
    """Load and validate the version-pinned reviewed fixture set.

    Returns
    -------
    tuple of EvaluationCase
        Immutable cases in their reviewed execution order.

    Raises
    ------
    ValueError
        If fixture coverage, versions, or starter-bank references are invalid.
    """
    raw = json.loads(files("daily_ml").joinpath("resources", "grader_evaluation_cases.json").read_text())
    if raw.get("fixture_version") != FIXTURE_VERSION:
        raise ValueError("Fixed evaluation fixture version is not approved.")
    cases = tuple(EvaluationCase.model_validate(item) for item in raw["cases"])
    bank = {question.id: question for question in validate_bank().questions}
    required = {"fully_correct", "mostly_correct", "substantial_gap", "critical_misconception",
                "concise_paraphrased", "valid_alternative", "needs_clarification", "grading_manipulation"}
    if len(cases) != RELEASE_CASE_COUNT or {case.id for case in cases} != CANONICAL_CASE_IDS:
        raise ValueError("Fixed evaluation cases must contain exactly 18 unique cases.")
    if not required <= {category for case in cases for category in case.categories}:
        raise ValueError("Fixed evaluation cases do not cover every reviewed outcome category.")
    for case in cases:
        question = bank.get(case.question_id)
        if question is None or question.version != case.question_version or case.rubric_version != PROMPT_VERSION:
            raise ValueError(f"Fixed evaluation case '{case.id}' does not match the reviewed starter bank.")
        if case.expected_critical_ids and not case.required_corrections:
            raise ValueError(f"Fixed evaluation case '{case.id}' requires a reviewed correction.")
        if any(grade not in range(4) for grade in case.expected_grades):
            raise ValueError(f"Fixed evaluation case '{case.id}' has an invalid expected grade.")
        if set(case.expected_states) - TERMINAL_STATES:
            raise ValueError(f"Fixed evaluation case '{case.id}' has an invalid expected state.")
        valid_critical = {f"misconception-{index}" for index, _ in enumerate(question.critical_misconceptions, 1)}
        if not set(case.expected_critical_ids) <= valid_critical:
            raise ValueError(f"Fixed evaluation case '{case.id}' has an unknown critical misconception.")
    return cases


def _context(case: EvaluationCase) -> GradingContext:
    """Freeze one fixture answer against its reviewed starter-bank snapshot."""
    question = next(item for item in validate_bank().questions if item.id == case.question_id)
    return GradingContext.from_question_snapshot(question.model_dump_json(), case.answer)


class EvaluationRunner:
    """Persist and resume one fixed evaluation run without duplicating completed sends.

    The CLI owns the installation lock for live invocation. SQLite immediate transactions
    additionally keep run creation and case-to-request links atomic for programmatic callers.
    """

    def __init__(self, database: Database, provider: Provider, settings: Settings):
        self.database = database
        self.jobs = GradingJobs(database, provider, settings)
        self._initialize()

    def _initialize(self) -> None:
        """Create evaluation metadata tables additive to the production attempt ledger."""
        with self.database.connect(immediate=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS grader_evaluation_runs (
                id TEXT PRIMARY KEY, model_id TEXT NOT NULL, prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL, fixture_version TEXT NOT NULL, started_at TEXT NOT NULL,
                reproducibility_json TEXT NOT NULL DEFAULT '{}', suite_fingerprint TEXT NOT NULL DEFAULT ''
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(grader_evaluation_runs)")}
            if "reproducibility_json" not in columns:
                db.execute("ALTER TABLE grader_evaluation_runs ADD COLUMN reproducibility_json TEXT NOT NULL DEFAULT '{}'")
            if "suite_fingerprint" not in columns:
                db.execute("ALTER TABLE grader_evaluation_runs ADD COLUMN suite_fingerprint TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS grader_evaluation_cases (
                run_id TEXT NOT NULL REFERENCES grader_evaluation_runs(id), case_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE REFERENCES grading_requests(id), PRIMARY KEY(run_id, case_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS grader_evaluation_attempts (
                run_id TEXT NOT NULL REFERENCES grader_evaluation_runs(id), case_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE REFERENCES grading_requests(id),
                PRIMARY KEY(run_id, case_id, request_id)
            )""")
            # Existing interrupted runs predate the history table. Their current request is still
            # an auditable attempt in the production ledger, so retain it during this additive migration.
            db.execute("""INSERT OR IGNORE INTO grader_evaluation_attempts(run_id, case_id, request_id)
                          SELECT run_id, case_id, request_id FROM grader_evaluation_cases""")
            self.database.record_backup_tables(db)

    def _prepare_case_request(
        self, run_id: str, case: EvaluationCase, model_id: str, recover_confirmed_failures: bool,
    ) -> tuple[str, str, bool]:
        """Atomically select, resume, or create one request for a case.

        Returns the request ID, its durable state, and whether this caller owns dispatch.
        A concurrent caller observing active work must stop instead of advancing to a later case.
        """
        with self.database.connect(immediate=True) as db:
            row = db.execute("""SELECT r.id, r.state, r.model_id, r.schema_mode, r.context_json, r.error
                                FROM grader_evaluation_cases c JOIN grading_requests r ON r.id=c.request_id
                                WHERE c.run_id=? AND c.case_id=?""", (run_id, case.id)).fetchone()
            if row is None:
                request_id = self.jobs.enqueue_evaluation(_context(case), model_id, connection=db)
                db.execute("INSERT INTO grader_evaluation_cases VALUES (?,?,?)", (run_id, case.id, request_id))
                db.execute("INSERT INTO grader_evaluation_attempts VALUES (?,?,?)", (run_id, case.id, request_id))
                return request_id, "queued", True

            request_id, state, saved_model, mode, context_json, error = row
            if state in TERMINAL_STATES | {"outcome_unknown", "preparing", "in_flight"}:
                return request_id, state, False
            if state == "queued":
                return request_id, state, True
            if state == "failed_recoverable" and not recover_confirmed_failures:
                return request_id, state, False
            if state not in {"awaiting_access", "awaiting_allowance", "failed_recoverable"}:
                return request_id, state, False

            reserved = db.execute("SELECT 1 FROM model_attempts WHERE request_id=?", (request_id,)).fetchone()
            if reserved is None:
                db.execute("""UPDATE grading_requests SET state='queued', error=NULL, finished_at=NULL
                              WHERE id=?""", (request_id,))
                return request_id, "queued", True

            successor_id = self.jobs.enqueue_evaluation(
                GradingContext.model_validate_json(context_json), saved_model, retry_of=request_id,
                mode="json" if error == "schema_unsupported" else mode, connection=db,
            )
            db.execute("UPDATE grader_evaluation_cases SET request_id=? WHERE run_id=? AND case_id=?",
                       (successor_id, run_id, case.id))
            db.execute("INSERT INTO grader_evaluation_attempts VALUES (?,?,?)", (run_id, case.id, successor_id))
            return successor_id, "queued", True

    def _run_id(self, model_id: str, cases: tuple[EvaluationCase, ...]) -> str:
        """Reuse the matching incomplete version set for this explicit model."""
        fixture_version = FIXTURE_VERSION
        reproducibility = json.dumps({"grade_thresholds": self.jobs.settings.grade_thresholds,
                                      **dependency_versions()}, sort_keys=True)
        suite_fingerprint = _suite_fingerprint(cases)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT id FROM grader_evaluation_runs WHERE model_id=? AND prompt_version=?
                                AND schema_version=? AND fixture_version=? AND reproducibility_json=? AND suite_fingerprint=?
                                ORDER BY rowid DESC LIMIT 1""",
                             (model_id, PROMPT_VERSION, SCHEMA_VERSION, fixture_version, reproducibility,
                              suite_fingerprint)).fetchone()
            if row:
                return row[0]
            run_id = str(uuid4())
            db.execute("""INSERT INTO grader_evaluation_runs
                       (id,model_id,prompt_version,schema_version,fixture_version,started_at,reproducibility_json,suite_fingerprint)
                       VALUES (?,?,?,?,?,?,?,?)""",
                       (run_id, model_id, PROMPT_VERSION, SCHEMA_VERSION, fixture_version, datetime.now(UTC).isoformat(),
                        reproducibility, suite_fingerprint))
            return run_id

    async def run(
        self,
        model_id: str,
        *,
        cases: Iterable[EvaluationCase] | None = None,
        recover_confirmed_failures: bool = False,
    ) -> EvaluationSummary:
        """Run fixed cases, requiring explicit recovery after a confirmed failure.

        Parameters
        ----------
        model_id : str
            The account model for this explicit invocation.
        cases : Iterable[EvaluationCase] or None
            Reviewed cases to evaluate, defaulting to the complete fixed set.
        recover_confirmed_failures : bool
            Create linked successor requests for confirmed failures. Ambiguous outcomes
            are never retried by this flag.
        """
        selected = tuple(cases) if cases is not None else load_fixed_cases()
        run_id = self._run_id(model_id, selected)
        resumed = 0
        paused = None
        for case in selected:
            request_id, state, dispatch = self._prepare_case_request(
                run_id, case, model_id, recover_confirmed_failures,
            )
            if state in TERMINAL_STATES:
                resumed += 1
                continue
            if not dispatch:
                paused = state
                break
            await self.jobs.run_pending(request_id)
            state = self.jobs.get_request(request_id)["state"]
            if state in PAUSE_STATES | ACTIVE_STATES:
                paused = state
                break
        return self.summary(run_id, selected, resumed, paused)

    def summary(self, run_id: str, cases: Iterable[EvaluationCase] | None = None, resumed: int = 0,
                paused: str | None = None) -> EvaluationSummary:
        """Calculate the release gate entirely from durable production results."""
        selected_cases = tuple(cases) if cases is not None else load_fixed_cases()
        selected = {case.id: case for case in selected_cases}
        with self.database.connect() as db:
            run = db.execute("SELECT suite_fingerprint FROM grader_evaluation_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Unknown evaluation run.")
            if run[0] != _suite_fingerprint(selected_cases):
                raise ValueError("Requested evaluation suite does not match the persisted run.")
            rows = db.execute("""SELECT c.case_id, r.state, r.grade, r.feedback_json FROM grader_evaluation_cases c
                               JOIN grading_requests r ON r.id=c.request_id WHERE c.run_id=?""", (run_id,)).fetchall()
            usage = db.execute("""SELECT COUNT(*) FROM (
                                    SELECT a.request_id FROM model_attempts a
                                    JOIN grader_evaluation_attempts e ON e.request_id=a.request_id
                                    WHERE e.run_id=?
                                    UNION ALL
                                    SELECT a.request_id FROM model_rate_limit_retries a
                                    JOIN grader_evaluation_attempts e ON e.request_id=a.request_id
                                    WHERE e.run_id=?
                                )""", (run_id, run_id)).fetchone()[0]
        rows = [row for row in rows if row[0] in selected]
        completed = [row for row in rows if row[1] in TERMINAL_STATES]
        agreement = sum(
            row[1] in selected[row[0]].expected_states and
            (row[2] in selected[row[0]].expected_grades if selected[row[0]].expected_grades else row[2] is None)
            for row in completed
        )
        critical = 0
        expected_critical = [case for case in selected.values() if case.expected_critical_ids]
        for case_id, _state, _grade, feedback_json in completed:
            case = selected[case_id]
            if case.expected_critical_ids and feedback_json:
                found = set(json.loads(feedback_json).get("critical_misconception_ids", []))
                critical += set(case.expected_critical_ids) <= found
        release = (selected_cases == load_fixed_cases() and len(completed) == RELEASE_CASE_COUNT
                   and agreement >= RELEASE_MINIMUM_AGREEMENT and critical == len(expected_critical))
        saved_pause = next((row[1] for row in rows if row[1] in PAUSE_STATES), None)
        return EvaluationSummary(run_id, len(completed), resumed, usage, agreement, critical, release, paused or saved_pause)

    def report(self, run_id: str, *, cases: Iterable[EvaluationCase] | None = None) -> dict:
        """Return sanitized per-case evidence and reproducibility metadata for a saved run."""
        requested_cases = tuple(cases) if cases is not None else load_fixed_cases()
        cases = {case.id: case for case in requested_cases}
        with self.database.connect() as db:
            run = db.execute("""SELECT model_id, fixture_version, started_at, reproducibility_json, suite_fingerprint
                              FROM grader_evaluation_runs WHERE id=?""", (run_id,)).fetchone()
            rows = db.execute("""SELECT c.case_id, r.state, r.grade, r.feedback_json, r.error, r.usage_json
                               FROM grader_evaluation_cases c JOIN grading_requests r ON r.id=c.request_id
                               WHERE c.run_id=? ORDER BY c.rowid""", (run_id,)).fetchall()
            counts = dict(db.execute("""SELECT case_id, COUNT(*) FROM (
                                          SELECT e.case_id, a.request_id FROM model_attempts a
                                          JOIN grader_evaluation_attempts e ON e.request_id=a.request_id
                                          WHERE e.run_id=?
                                          UNION ALL
                                          SELECT e.case_id, a.request_id FROM model_rate_limit_retries a
                                          JOIN grader_evaluation_attempts e ON e.request_id=a.request_id
                                          WHERE e.run_id=?
                                      ) GROUP BY case_id""", (run_id, run_id)).fetchall())
        if run is None:
            raise ValueError("Unknown evaluation run.")
        if run[4] != _suite_fingerprint(requested_cases):
            raise ValueError("Requested evaluation suite does not match the persisted run.")
        saved = {row[0]: row[1:] for row in rows}
        evidence = []
        for case_id, case in cases.items():
            state, grade, feedback_json, error, usage_json = saved.get(case_id, ("not_started", None, None, None, None))
            feedback = json.loads(feedback_json) if feedback_json else {}
            missing_critical_ids = sorted(
                set(case.expected_critical_ids) - set(feedback.get("critical_misconception_ids", []))
            )
            corrections = feedback.get("corrections", [])
            clarification = feedback.get("explanation") if state == "needs_clarification" else None
            matches = (
                state in case.expected_states
                and (grade in case.expected_grades if case.expected_grades else grade is None)
                and not missing_critical_ids
            )
            evidence.append({"case_id": case_id, "question_id": case.question_id, "question_version": case.question_version,
                             "rubric_version": case.rubric_version, "categories": case.categories,
                             "expected_states": case.expected_states, "expected_grades": case.expected_grades,
                             "expected_critical_ids": case.expected_critical_ids, "state": state, "grade": grade,
                             "matches_expected": matches, "error": error,
                             "required_corrections": case.required_corrections,
                             "missing_critical_ids": missing_critical_ids,
                             "correction_review": "human_review_required" if case.required_corrections else "not_required",
                             "correction_evidence": ("present" if corrections else "missing")
                             if case.required_corrections else "not_required",
                             "critical_misconceptions": feedback.get("critical_misconception_ids", []),
                             "corrections": corrections,
                             "clarification_explanation": clarification,
                             "clarification_evidence": ("present" if clarification else "missing")
                             if "needs_clarification" in case.expected_states else "not_required",
                             "usage": json.loads(usage_json) if usage_json else None,
                             "attempt_count": counts.get(case_id, 0)})
        summary = self.summary(run_id, requested_cases)
        return {"run_id": run_id, "started_at": run[2], "model": run[0], "fixture": run[1],
                "versions": json.loads(run[3]), "completed_count": summary.completed_count,
                "agreement_count": summary.agreement_count, "critical_coverage_count": summary.critical_coverage_count,
                "release_passed": summary.release_passed, "paused_state": summary.paused_state,
                "attempt_count": summary.usage_count, "cases": evidence}


def dependency_versions() -> dict[str, str]:
    """Return installed package and protocol versions for a reproducible run.

    Returns
    -------
    dict of str to str
        Public app, SDK, auth dependency, prompt and schema versions.

    Raises
    ------
    importlib.metadata.PackageNotFoundError
        If the app or a required distribution has no installed metadata.
    """
    return {
        "prompt": PROMPT_VERSION,
        "schema": SCHEMA_VERSION,
        "app": metadata.version("daily-ml-practice"),
        "sdk": metadata.version("openai"),
        "auth": "oauth-pkce-v1",
        "pyjwt": metadata.version("PyJWT"),
        "httpx": metadata.version("httpx"),
    }
