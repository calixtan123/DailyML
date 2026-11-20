"""Run the reviewed fixed grader set through the durable production grading path.

The module keeps fixture text in package data and stores only safe outcome metadata in
the evaluation tables.  Individual fixed answers remain in the immutable package
fixture and are never copied to operational logs or the public report.
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import Iterable
from uuid import uuid4

import openai
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
PAUSE_STATES = {"awaiting_access", "awaiting_allowance", "outcome_unknown"}


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
    cases = tuple(EvaluationCase.model_validate(item) for item in raw["cases"])
    bank = {question.id: question for question in validate_bank().questions}
    required = {"fully_correct", "mostly_correct", "substantial_gap", "critical_misconception",
                "concise_paraphrased", "valid_alternative", "needs_clarification", "grading_manipulation"}
    if len(cases) != RELEASE_CASE_COUNT or len({case.id for case in cases}) != len(cases):
        raise ValueError("Fixed evaluation cases must contain exactly 18 unique cases.")
    if not required <= {category for case in cases for category in case.categories}:
        raise ValueError("Fixed evaluation cases do not cover every reviewed outcome category.")
    for case in cases:
        question = bank.get(case.question_id)
        if question is None or question.version != case.question_version or case.rubric_version != PROMPT_VERSION:
            raise ValueError(f"Fixed evaluation case '{case.id}' does not match the reviewed starter bank.")
        if case.expected_critical_ids and not case.required_corrections:
            raise ValueError(f"Fixed evaluation case '{case.id}' requires a reviewed correction.")
    return cases


def _context(case: EvaluationCase) -> GradingContext:
    """Freeze one fixture answer against its reviewed starter-bank snapshot."""
    question = next(item for item in validate_bank().questions if item.id == case.question_id)
    return GradingContext(
        version=f"{question.id}-v{question.version}", question=question.prompt, answer=case.answer,
        reference=question.reference_explanation,
        criteria=[{"id": item.id, "description": item.description, "weight": item.weight} for item in question.criteria],
        critical_misconceptions={f"misconception-{index}": text for index, text in enumerate(question.critical_misconceptions, 1)},
        sources={source.id: source.url for source in question.sources},
    )


class EvaluationRunner:
    """Persist and resume one fixed evaluation run without duplicating completed sends."""

    def __init__(self, database: Database, provider: Provider, settings: Settings):
        self.database = database
        self.jobs = GradingJobs(database, provider, settings)
        self._initialize()

    def _initialize(self) -> None:
        """Create evaluation metadata tables additive to the production attempt ledger."""
        with self.database.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS grader_evaluation_runs (
                id TEXT PRIMARY KEY, model_id TEXT NOT NULL, prompt_version TEXT NOT NULL,
                schema_version TEXT NOT NULL, fixture_version TEXT NOT NULL, started_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS grader_evaluation_cases (
                run_id TEXT NOT NULL REFERENCES grader_evaluation_runs(id), case_id TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE REFERENCES grading_requests(id), PRIMARY KEY(run_id, case_id)
            )""")

    def _run_id(self, model_id: str, cases: tuple[EvaluationCase, ...]) -> str:
        """Reuse the matching incomplete version set for this explicit model."""
        fixture_version = "fixed-v1"
        with self.database.connect() as db:
            row = db.execute("""SELECT id FROM grader_evaluation_runs WHERE model_id=? AND prompt_version=?
                                AND schema_version=? AND fixture_version=? ORDER BY rowid DESC LIMIT 1""",
                             (model_id, PROMPT_VERSION, SCHEMA_VERSION, fixture_version)).fetchone()
            if row:
                return row[0]
            run_id = str(uuid4())
            db.execute("INSERT INTO grader_evaluation_runs VALUES (?,?,?,?,?,?)",
                       (run_id, model_id, PROMPT_VERSION, SCHEMA_VERSION, fixture_version, datetime.now(UTC).isoformat()))
            return run_id

    async def run(self, model_id: str, *, cases: Iterable[EvaluationCase] | None = None) -> EvaluationSummary:
        """Run unsent fixed cases sequentially and stop at an uncertain/access outcome."""
        selected = tuple(cases) if cases is not None else load_fixed_cases()
        run_id = self._run_id(model_id, selected)
        resumed = 0
        paused = None
        for case in selected:
            with self.database.connect() as db:
                row = db.execute("""SELECT r.id, r.state FROM grader_evaluation_cases c JOIN grading_requests r
                                    ON r.id=c.request_id WHERE c.run_id=? AND c.case_id=?""", (run_id, case.id)).fetchone()
            if row and row[1] in TERMINAL_STATES:
                resumed += 1
                continue
            if row and row[1] == "outcome_unknown":
                paused = row[1]
                break
            if row and row[1] in {"awaiting_access", "awaiting_allowance"}:
                with self.database.connect() as db:
                    changed = db.execute("""UPDATE grading_requests SET state='queued', error=NULL, finished_at=NULL
                                          WHERE id=? AND state IN ('awaiting_access','awaiting_allowance')""", (row[0],))
                if changed.rowcount != 1:
                    paused = row[1]
                    break
            if row is None:
                with self.database.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    request_id = self.jobs.enqueue_evaluation(_context(case), model_id, connection=db)
                    db.execute("INSERT INTO grader_evaluation_cases VALUES (?,?,?)", (run_id, case.id, request_id))
            else:
                request_id = row[0]
            await self.jobs.run_pending(request_id)
            state = self.jobs.get_request(request_id)["state"]
            if state in PAUSE_STATES:
                paused = state
                break
        return self.summary(run_id, selected, resumed, paused)

    def summary(self, run_id: str, cases: Iterable[EvaluationCase] | None = None, resumed: int = 0,
                paused: str | None = None) -> EvaluationSummary:
        """Calculate the release gate entirely from durable production results."""
        selected = {case.id: case for case in (tuple(cases) if cases is not None else load_fixed_cases())}
        with self.database.connect() as db:
            rows = db.execute("""SELECT c.case_id, r.state, r.grade, r.feedback_json FROM grader_evaluation_cases c
                               JOIN grading_requests r ON r.id=c.request_id WHERE c.run_id=?""", (run_id,)).fetchall()
            usage = db.execute("""SELECT COUNT(*) FROM model_attempts a JOIN grader_evaluation_cases c
                                ON c.request_id=a.request_id WHERE c.run_id=?""", (run_id,)).fetchone()[0]
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
        release = len(completed) == len(selected) and agreement >= RELEASE_MINIMUM_AGREEMENT and critical == len(expected_critical)
        return EvaluationSummary(run_id, len(completed), resumed, usage, agreement, critical, release, paused)

    def report(self, run_id: str, *, cases: Iterable[EvaluationCase] | None = None) -> dict:
        """Return sanitized per-case evidence and reproducibility metadata for a saved run."""
        cases = {case.id: case for case in (tuple(cases) if cases is not None else load_fixed_cases())}
        with self.database.connect() as db:
            run = db.execute("SELECT model_id, fixture_version, started_at FROM grader_evaluation_runs WHERE id=?", (run_id,)).fetchone()
            rows = db.execute("""SELECT c.case_id, r.state, r.grade, r.feedback_json, r.error, r.usage_json
                               FROM grader_evaluation_cases c JOIN grading_requests r ON r.id=c.request_id
                               WHERE c.run_id=? ORDER BY c.rowid""", (run_id,)).fetchall()
        if run is None:
            raise ValueError("Unknown evaluation run.")
        saved = {row[0]: row[1:] for row in rows}
        evidence = []
        for case_id, case in cases.items():
            state, grade, feedback_json, error, usage_json = saved.get(case_id, ("not_started", None, None, None, None))
            feedback = json.loads(feedback_json) if feedback_json else {}
            matches = state in case.expected_states and (grade in case.expected_grades if case.expected_grades else grade is None)
            evidence.append({"case_id": case_id, "question_id": case.question_id, "question_version": case.question_version,
                             "rubric_version": case.rubric_version, "categories": case.categories,
                             "expected_states": case.expected_states, "expected_grades": case.expected_grades,
                             "expected_critical_ids": case.expected_critical_ids, "state": state, "grade": grade,
                             "matches_expected": matches, "error": error,
                             "critical_misconceptions": feedback.get("critical_misconception_ids", []),
                             "corrections": feedback.get("corrections", []), "usage": json.loads(usage_json) if usage_json else None})
        summary = self.summary(run_id, cases.values())
        return {"run_id": run_id, "started_at": run[2], "model": run[0], "fixture": run[1],
                "versions": dependency_versions(), "completed_count": summary.completed_count,
                "agreement_count": summary.agreement_count, "critical_coverage_count": summary.critical_coverage_count,
                "release_passed": summary.release_passed, "paused_state": summary.paused_state, "cases": evidence}


def dependency_versions() -> dict[str, str]:
    """Return public reproducibility metadata without reading credentials."""
    return {"prompt": PROMPT_VERSION, "schema": SCHEMA_VERSION, "sdk": openai.__version__, "auth": "oauth-pkce-v1"}
