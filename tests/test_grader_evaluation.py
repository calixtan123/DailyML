"""Exercise the offline, resumable fixed grader evaluation."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from importlib import metadata
from types import SimpleNamespace

import pytest

from daily_ml import cli
from daily_ml.bank import import_bank, validate_bank
from daily_ml.config import load_settings
from daily_ml.database import BackupError, Database
from daily_ml.grader_evaluation import EvaluationRunner, _context, load_fixed_cases
from daily_ml.grading import Assessment, CriterionAssessment, FeedbackEnvelope
from daily_ml.practice import Practice
from daily_ml.provider import ProviderFailure, ProviderResult


class OfflineProvider:
    """Produce reviewed fixture feedback without an external provider."""

    async def grade(self, context, _model_id, *, mode, before_send):
        """Return an assessment after recording the production send reservation."""
        before_send()
        criteria = [
            CriterionAssessment(criterion_id=item.id, status="met", justification="Reviewed evidence.")
            for item in context.criteria
        ]
        return ProviderResult(
            FeedbackEnvelope(feedback=Assessment(
                status="assessed", summary="Reviewed fixture outcome.", criteria=criteria,
                critical_misconception_ids=[], corrections=[], model_answer="A reviewed answer.",
                reasoning=["The answer covers the rubric."], example="A minimal example.",
                source_ids=[next(iter(context.sources))], follow_up=[],
            )),
            response_id="offline-result", usage={"total_tokens": 7},
        )


class AccessUnavailableProvider:
    """Model the confirmed unavailable-access boundary without network traffic."""

    async def grade(self, _context, _model_id, *, mode, before_send):
        """Stop before admission so the case remains resumable and uncharged."""
        raise ProviderFailure("access", confirmed_unsent=True)


class OutcomeUnknownProvider:
    """Model a post-admission interruption which must never be automatically retried."""

    async def grade(self, _context, _model_id, *, mode, before_send):
        """Reserve the production attempt, then lose its outcome."""
        before_send()
        raise ProviderFailure("outcome_unknown")


class AllowanceAfterSendProvider:
    """Model a provider allowance response after durable outbound admission."""

    async def grade(self, _context, _model_id, *, mode, before_send):
        """Record the outbound attempt before returning a resumable allowance error."""
        before_send()
        raise ProviderFailure("allowance")


class RecoverableFailureProvider:
    """Model a confirmed schema rejection after a durable outbound reservation."""

    async def grade(self, _context, _model_id, *, mode, before_send):
        """Reserve the production attempt before returning a recoverable failure."""
        before_send()
        raise ProviderFailure("schema_unsupported")


class FirstCaseThenAccessProvider:
    """Complete one case before a proven-unsent access pause."""

    def __init__(self):
        """Track the fixed-case order within one process."""
        self.calls = 0
        self.success = OfflineProvider()

    async def grade(self, context, model_id, *, mode, before_send):
        """Return one terminal result and then pause before admission."""
        self.calls += 1
        if self.calls == 1:
            return await self.success.grade(context, model_id, mode=mode, before_send=before_send)
        raise ProviderFailure("access", confirmed_unsent=True)


def test_fixed_cases_are_versioned_reviewed_and_cover_the_release_categories():
    """The packaged evaluation set is immutable, starter-backed and sufficiently diverse."""
    cases = load_fixed_cases()
    categories = {category for case in cases for category in case.categories}

    assert len(cases) >= 18
    assert {"fully_correct", "mostly_correct", "substantial_gap", "critical_misconception",
            "concise_paraphrased", "valid_alternative", "needs_clarification", "grading_manipulation"} <= categories
    assert all(case.question_version == 1 and case.rubric_version for case in cases)
    assert all(case.required_corrections for case in cases if case.expected_critical_ids)


@pytest.mark.parametrize("case", load_fixed_cases(), ids=lambda case: case.id)
def test_fixed_case_serializes_the_same_grading_context_as_practice(tmp_path, case):
    """Submitting the same reviewed question and answer produces identical model input."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    import_bank(database, validate_bank())
    practice = Practice(database, answer_limit=settings.answer_limit)
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    item = practice.add_item("UTC", question_id=case.question_id)

    practice.submit(
        item.item_id, case.answer, expected_version=0,
        resolve_settings=lambda _db: ("UTC", "offline-model"),
    )
    asyncio.run(runner.run("offline-model", cases=(case,)))

    with database.connect() as db:
        contexts = dict(db.execute("SELECT kind, context_json FROM grading_requests"))
    assert contexts["evaluation"] == contexts["practice"]


def test_report_retains_installed_package_versions_from_run_creation(tmp_path, monkeypatch):
    """An environment change preserves prior evidence and starts a new evaluation run."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    cases = load_fixed_cases()[:1]
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    originals = {
        "app": metadata.version("daily-ml-practice"),
        "sdk": metadata.version("openai"),
        "pyjwt": metadata.version("PyJWT"),
        "httpx": metadata.version("httpx"),
    }
    first = asyncio.run(runner.run("offline-model", cases=cases))
    installed_version = metadata.version
    monkeypatch.setattr(
        metadata, "version", lambda name: "99.0.0" if name.lower() == "pyjwt" else installed_version(name),
    )

    reopened = EvaluationRunner(Database(tmp_path), OfflineProvider(), settings)
    second = asyncio.run(reopened.run("offline-model", cases=cases))

    retained = reopened.report(first.run_id, cases=cases)["versions"]
    assert {key: retained.get(key) for key in originals} == originals
    assert retained["prompt"] == cases[0].rubric_version
    assert retained["auth"] == "oauth-pkce-v1"
    assert second.run_id != first.run_id
    assert reopened.report(second.run_id, cases=cases)["versions"]["pyjwt"] == "99.0.0"


def test_runner_resumes_terminal_cases_and_records_usage_from_the_production_ledger(tmp_path):
    """A second invocation reads saved terminal cases without another model reservation."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    cases = load_fixed_cases()[:2]
    run_id = runner._run_id("offline-model", cases)

    first = asyncio.run(runner.run("offline-model", cases=cases))
    second = asyncio.run(runner.run("offline-model", cases=cases))

    assert first.completed_count == second.completed_count == 2
    assert first.usage_count == second.usage_count == 2
    assert second.resumed_count == 2


def test_runner_keeps_altered_or_subset_case_suites_out_of_a_canonical_run(tmp_path):
    """Different selected case content or coverage cannot reuse and corrupt a full fixed-set run."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    canonical = load_fixed_cases()
    altered = (canonical[0].model_copy(update={"answer": "A materially different fixed answer."}), *canonical[1:])

    full = asyncio.run(runner.run("offline-model", cases=canonical))
    subset = asyncio.run(runner.run("offline-model", cases=canonical[:2]))
    changed = asyncio.run(runner.run("offline-model", cases=altered))

    assert len({full.run_id, subset.run_id, changed.run_id}) == 3
    assert runner.summary(subset.run_id, canonical[:2]).release_passed is False
    assert runner.summary(changed.run_id, altered).release_passed is False


def test_report_and_summary_reject_a_requested_suite_that_differs_from_the_saved_run(tmp_path):
    """A caller cannot interpret altered persisted evidence as the reviewed fixed suite."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    canonical = load_fixed_cases()
    altered = (canonical[0].model_copy(update={"answer": "A materially different fixed answer."}), *canonical[1:])
    run_id = runner._run_id("offline-model", altered)
    with database.connect() as db:
        for case in altered:
            request_id = runner.jobs.enqueue_evaluation(_context(case), "offline-model", connection=db)
            db.execute("INSERT INTO grader_evaluation_cases VALUES (?,?,?)", (run_id, case.id, request_id))
            db.execute("INSERT INTO grader_evaluation_attempts VALUES (?,?,?)", (run_id, case.id, request_id))
            db.execute("""UPDATE grading_requests SET state=?, grade=?, feedback_json=? WHERE id=?""",
                       (case.expected_states[0], case.expected_grades[0] if case.expected_grades else None,
                        json.dumps({"critical_misconception_ids": list(case.expected_critical_ids)}), request_id))

    with pytest.raises(ValueError, match="Requested evaluation suite does not match the persisted run"):
        runner.summary(run_id, canonical)
    with pytest.raises(ValueError, match="Requested evaluation suite does not match the persisted run"):
        runner.report(run_id, cases=canonical)


def test_report_marks_missing_required_critical_evidence_as_an_overall_mismatch(tmp_path):
    """Numeric agreement cannot hide a missing required critical misconception ID."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    case = next(case for case in load_fixed_cases() if case.expected_critical_ids)
    summary = asyncio.run(runner.run("offline-model", cases=(case,)))
    with database.connect() as db:
        db.execute("""UPDATE grading_requests SET grade=?, feedback_json=?
                      WHERE id=(SELECT request_id FROM grader_evaluation_cases WHERE run_id=? AND case_id=?)""",
                   (case.expected_grades[0], json.dumps({"critical_misconception_ids": [], "corrections": []}),
                    summary.run_id, case.id))

    evidence = runner.report(summary.run_id, cases=(case,))["cases"][0]

    assert evidence["matches_expected"] is False
    assert evidence["missing_critical_ids"] == list(case.expected_critical_ids)
    assert evidence["correction_evidence"] == "missing"


def test_report_uses_clarification_explanation_as_evidence(tmp_path):
    """Clarification evidence comes from the production Clarification explanation field."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    case = next(case for case in load_fixed_cases() if "needs_clarification" in case.expected_states)
    run_id = runner._run_id("offline-model", (case,))
    explanation = "The option letters have no supplied referents."
    with database.connect() as db:
        request_id = runner.jobs.enqueue_evaluation(_context(case), "offline-model", connection=db)
        db.execute("INSERT INTO grader_evaluation_cases VALUES (?,?,?)", (run_id, case.id, request_id))
        db.execute("INSERT INTO grader_evaluation_attempts VALUES (?,?,?)", (run_id, case.id, request_id))
        db.execute("UPDATE grading_requests SET state=?, feedback_json=? WHERE id=?",
                   ("needs_clarification", json.dumps({"explanation": explanation}), request_id))

    evidence = runner.report(run_id, cases=(case,))["cases"][0]

    assert evidence["clarification_explanation"] == explanation
    assert evidence["clarification_evidence"] == "present"


def test_release_gate_passes_only_for_complete_canonical_expected_evidence(tmp_path):
    """The release gate accepts complete canonical evidence and rejects a single critical mismatch."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    cases = load_fixed_cases()
    run_id = runner._run_id("offline-model", cases)
    with database.connect() as db:
        for case in cases:
            request_id = runner.jobs.enqueue_evaluation(_context(case), "offline-model", connection=db)
            db.execute("INSERT INTO grader_evaluation_cases VALUES (?,?,?)", (run_id, case.id, request_id))
            db.execute("INSERT INTO grader_evaluation_attempts VALUES (?,?,?)", (run_id, case.id, request_id))
            db.execute("""UPDATE grading_requests SET state=?, grade=?, feedback_json=? WHERE id=?""",
                       (case.expected_states[0], case.expected_grades[0] if case.expected_grades else None,
                        json.dumps({"critical_misconception_ids": list(case.expected_critical_ids)}), request_id))

    assert runner.summary(run_id, cases).release_passed is True
    critical_case = next(case for case in cases if case.expected_critical_ids)
    with database.connect() as db:
        db.execute("""UPDATE grading_requests SET feedback_json='{}' WHERE id=(
                      SELECT request_id FROM grader_evaluation_cases WHERE run_id=? AND case_id=?)""",
                   (run_id, critical_case.id))
    assert runner.summary(run_id, cases).release_passed is False


def test_runner_keeps_partial_access_failure_visible_without_sending_later_cases(tmp_path):
    """An access pause records the first case and does not repeat or charge it on rerun."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, AccessUnavailableProvider(), settings)

    cases = load_fixed_cases()[:2]
    summary = asyncio.run(runner.run("offline-model", cases=cases))
    report = runner.report(summary.run_id, cases=cases)

    assert summary.completed_count == summary.usage_count == 0
    assert summary.paused_state == "awaiting_access"
    assert report["paused_state"] == "awaiting_access"
    assert len(report["cases"]) == 2
    assert report["cases"][1]["state"] == "not_started"
    assert "answer" not in str(report)

    runner.jobs.provider = OfflineProvider()
    resumed = asyncio.run(runner.run("offline-model", cases=cases))
    assert resumed.completed_count == resumed.usage_count == 2


def test_live_cli_requires_explicit_fixture_review_before_opening_local_data(tmp_path, monkeypatch):
    """The potentially chargeable command refuses without the review acknowledgement."""
    monkeypatch.setenv("DML_DATA_DIR", str(tmp_path))

    with pytest.raises(SystemExit) as error:
        cli.main(["evaluate-grader", "--model", "offline-model"])

    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_live_cli_reports_missing_package_metadata_before_queuing_cases(tmp_path, monkeypatch, capsys):
    """A broken installation cannot start an unversioned evaluation."""
    def missing_version(name):
        """Simulate absent distribution metadata despite an importable source tree."""
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setenv("DML_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(metadata, "version", missing_version)
    monkeypatch.setattr(cli, "Connection", lambda _path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "Provider", lambda *_args, **_kwargs: OfflineProvider())

    with pytest.raises(SystemExit) as error:
        cli.main(["evaluate-grader", "--model", "offline-model", "--confirm-reviewed-fixtures"])

    assert error.value.code == 1
    assert "Could not read installed package versions. Reinstall Daily ML Practice" in capsys.readouterr().err
    with Database(tmp_path).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM grading_requests").fetchone()[0] == 0


def test_live_cli_announces_the_bounded_send_scope_and_separate_catalog_traffic(tmp_path, monkeypatch, capsys):
    """The chargeable command explains its per-invocation send boundary before evaluation."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})

    class FakeConnection:
        """Avoid opening OAuth state while testing command messaging."""

        def __init__(self, _data_dir):
            """Accept the production constructor shape."""

        def close(self):
            """Match the production resource boundary."""

    class FakeRunner:
        """Avoid live model work while retaining CLI orchestration."""

        def __init__(self, *_args):
            self.jobs = SimpleNamespace(recover_interrupted=lambda: None)

        def _run_id(self, *_args):
            return "offline"

        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(run_id="offline", completed_count=0, agreement_count=0,
                                   critical_coverage_count=0, release_passed=False, paused_state=None)

        def report(self, *_args, **_kwargs):
            return {"report": "offline"}

    monkeypatch.setattr(cli, "resolve_installation", lambda _config: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(cli, "Connection", FakeConnection)
    monkeypatch.setattr(cli, "EvaluationRunner", FakeRunner)

    assert cli.main(["evaluate-grader", "--model", "offline-model", "--confirm-reviewed-fixtures"]) == 0

    assert "at most 18 model sends in one explicit invocation (possibly fewer)" in capsys.readouterr().out


def test_live_cli_writes_persisted_progress_report_after_interrupt(tmp_path, monkeypatch):
    """An interruption returns a durable progress report instead of silently losing evidence."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})

    class FakeConnection:
        """Avoid OAuth I/O in CLI orchestration coverage."""

        def __init__(self, _data_dir):
            """Accept the production constructor shape."""

        def close(self):
            """Match the production resource boundary."""

    class FakeRunner:
        """Expose saved progress after the simulated cancellation."""

        def __init__(self, *_args):
            self.jobs = SimpleNamespace(recover_interrupted=lambda: None)

        def _run_id(self, *_args):
            return "interrupted"

        async def run(self, *_args, **_kwargs):
            raise KeyboardInterrupt

        def summary(self, *_args, **_kwargs):
            return SimpleNamespace(run_id="interrupted", completed_count=1, agreement_count=1,
                                   critical_coverage_count=0, release_passed=False,
                                   paused_state="outcome_unknown")

        def report(self, *_args, **_kwargs):
            return {"paused_state": "outcome_unknown", "completed_count": 1}

    monkeypatch.setattr(cli, "resolve_installation", lambda _config: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda **_kwargs: settings)
    monkeypatch.setattr(cli, "Connection", FakeConnection)
    monkeypatch.setattr(cli, "EvaluationRunner", FakeRunner)

    assert cli.main(["evaluate-grader", "--model", "offline-model", "--confirm-reviewed-fixtures"]) == 1
    report = json.loads((tmp_path / "grader-evaluation-interrupted.json").read_text())
    assert report["paused_state"] == "outcome_unknown"
    assert report["interrupted"] is True


def test_runner_does_not_replay_an_unknown_outcome_on_later_invocation(tmp_path):
    """A potentially completed provider request blocks resumption instead of being duplicated."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OutcomeUnknownProvider(), settings)
    cases = load_fixed_cases()[:2]

    first = asyncio.run(runner.run("offline-model", cases=cases))
    runner.jobs.provider = OfflineProvider()
    second = asyncio.run(runner.run("offline-model", cases=cases))
    explicit_recovery = asyncio.run(runner.run("offline-model", cases=cases, recover_confirmed_failures=True))

    assert first.paused_state == second.paused_state == explicit_recovery.paused_state == "outcome_unknown"
    assert first.usage_count == second.usage_count == explicit_recovery.usage_count == 1


def test_runner_resumes_post_send_allowance_with_a_linked_attempt_and_counts_both_sends(tmp_path):
    """A post-admission allowance pause preserves the first send before its successor completes."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, AllowanceAfterSendProvider(), settings)
    cases = load_fixed_cases()[:1]

    paused = asyncio.run(runner.run("offline-model", cases=cases))
    runner.jobs.provider = OfflineProvider()
    resumed = asyncio.run(runner.run("offline-model", cases=cases))
    report = runner.report(resumed.run_id, cases=cases)

    assert paused.paused_state == "awaiting_allowance"
    assert resumed.completed_count == 1
    assert resumed.usage_count == 2
    assert report["cases"][0]["attempt_count"] == 2


def test_runner_requires_explicit_recovery_after_confirmed_evaluation_failure(tmp_path):
    """A failed send pauses the set until an explicit linked recovery is requested."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, RecoverableFailureProvider(), settings)
    cases = load_fixed_cases()[:2]

    paused = asyncio.run(runner.run("offline-model", cases=cases))
    runner.jobs.provider = OfflineProvider()
    blocked = asyncio.run(runner.run("offline-model", cases=cases))
    recovered = asyncio.run(runner.run("offline-model", cases=cases, recover_confirmed_failures=True))

    assert paused.paused_state == blocked.paused_state == "failed_recoverable"
    assert blocked.completed_count == 0
    assert recovered.completed_count == 2
    assert recovered.usage_count == 3


def test_concurrent_direct_resumes_create_one_linked_recovery_attempt(tmp_path):
    """Two public runners resuming one failure share a successor instead of racing its lineage."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    case = load_fixed_cases()[:1]
    failed_runner = EvaluationRunner(database, RecoverableFailureProvider(), settings)
    paused = asyncio.run(failed_runner.run("offline-model", cases=case))
    runners = [EvaluationRunner(database, OfflineProvider(), settings) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, runner.run("offline-model", cases=case,
                                                        recover_confirmed_failures=True)) for runner in runners]
        summaries = [future.result(timeout=10) for future in futures]

    with database.connect() as db:
        successors = db.execute("SELECT COUNT(*) FROM grading_requests WHERE retry_of IS NOT NULL").fetchone()[0]

    assert paused.paused_state == "failed_recoverable"
    assert successors == 1
    assert all(summary.usage_count <= 2 for summary in summaries)
    final = asyncio.run(runners[0].run("offline-model", cases=case, recover_confirmed_failures=True))
    assert final.completed_count == 1
    assert final.usage_count == 2


@pytest.mark.parametrize("manifested", [True, False], ids=["registered", "unregistered"])
def test_evaluation_history_survives_sqlite_backup_and_restored_resume(tmp_path, manifested):
    """A restored database keeps case lineage and safely resumes a proven-unsent pause."""
    live = Database(tmp_path / "live")
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path / "live")})
    cases = load_fixed_cases()[:2]
    partial_runner = EvaluationRunner(live, AllowanceAfterSendProvider(), settings)

    asyncio.run(partial_runner.run("offline-model", cases=cases))
    partial_runner.jobs.provider = FirstCaseThenAccessProvider()
    partial = asyncio.run(partial_runner.run("offline-model", cases=cases))
    backup = live.create_backup()
    with closing(sqlite3.connect(backup)) as connection, connection:
        tables = {row[0] for row in connection.execute("SELECT table_name FROM daily_ml_backup_tables")}
        assert {"grader_evaluation_runs", "grader_evaluation_cases", "grader_evaluation_attempts"} <= tables
        if not manifested:
            connection.execute("DELETE FROM daily_ml_backup_tables WHERE table_name LIKE 'grader_evaluation_%'")
    restored = Database(tmp_path / "restored")
    restored.restore_backup(backup)

    restored_runner = EvaluationRunner(restored, OfflineProvider(), settings)
    resumed = asyncio.run(restored_runner.run("offline-model", cases=cases))
    report = restored_runner.report(resumed.run_id, cases=cases)

    assert partial.paused_state == "awaiting_access"
    assert partial.usage_count == 2
    assert resumed.run_id == partial.run_id
    assert resumed.completed_count == 2
    assert resumed.usage_count == 3
    assert [case["attempt_count"] for case in report["cases"]] == [2, 1]


@pytest.mark.parametrize("manifested", [True, False], ids=["registered", "unregistered"])
@pytest.mark.parametrize("damage", ["missing_attempts", "missing_fingerprint", "missing_key"])
def test_restore_rejects_incomplete_evaluation_backups(tmp_path, manifested, damage):
    """Fail before replacing saved preferences if evaluation evidence would be lost."""
    source = Database(tmp_path / "source")
    settings = load_settings(environ={"DML_DATA_DIR": str(source.path.parent)})
    cases = load_fixed_cases()[:1]
    runner = EvaluationRunner(source, AllowanceAfterSendProvider(), settings)
    asyncio.run(runner.run("offline-model", cases=cases))
    runner.jobs.provider = OfflineProvider()
    completed = asyncio.run(runner.run("offline-model", cases=cases))
    assert completed.completed_count == 1 and completed.usage_count == 2
    backup = source.create_backup()
    with closing(sqlite3.connect(backup)) as connection, connection:
        if not manifested:
            connection.execute("DELETE FROM daily_ml_backup_tables WHERE table_name LIKE 'grader_evaluation_%'")
        if damage == "missing_attempts":
            connection.execute("DROP TABLE grader_evaluation_attempts")
        elif damage == "missing_fingerprint":
            connection.execute("ALTER TABLE grader_evaluation_runs DROP COLUMN suite_fingerprint")
        else:
            schema = connection.execute("""SELECT sql FROM sqlite_schema
                                           WHERE name='grader_evaluation_cases'""").fetchone()[0]
            rows = connection.execute("SELECT * FROM grader_evaluation_cases").fetchall()
            connection.execute("DROP TABLE grader_evaluation_cases")
            connection.execute(schema.replace("NOT NULL UNIQUE REFERENCES", "NOT NULL REFERENCES"))
            connection.executemany("INSERT INTO grader_evaluation_cases VALUES (?, ?, ?)", rows)

    target = Database(tmp_path / "target")
    target.save_preferences({"timezone": "Asia/Tokyo"}, expected_version=0)
    with pytest.raises(BackupError):
        target.restore_backup(backup)
    assert target.get_preferences().values == {"timezone": "Asia/Tokyo"}


@pytest.mark.parametrize("had_attempt_history", [True, False])
@pytest.mark.parametrize("manifested", [True, False], ids=["registered", "unregistered"])
def test_restore_preserves_records_from_pre_fingerprint_evaluator(tmp_path, had_attempt_history, manifested):
    """Upgrade both older layouts without discarding the current request for a saved case."""
    source = Database(tmp_path / "source")
    settings = load_settings(environ={"DML_DATA_DIR": str(source.path.parent)})
    cases = load_fixed_cases()[:1]
    runner = EvaluationRunner(source, OfflineProvider(), settings)
    if had_attempt_history:
        runner.jobs.provider = AllowanceAfterSendProvider()
        asyncio.run(runner.run("offline-model", cases=cases))
        runner.jobs.provider = OfflineProvider()
    result = asyncio.run(runner.run("offline-model", cases=cases))
    assert result.usage_count == (2 if had_attempt_history else 1)
    backup = source.create_backup()
    with closing(sqlite3.connect(backup)) as connection, connection:
        saved_case = connection.execute("SELECT * FROM grader_evaluation_cases").fetchone()
        saved_attempts = set(connection.execute("SELECT * FROM grader_evaluation_attempts"))
        connection.execute("DELETE FROM daily_ml_backup_tables WHERE table_name LIKE 'grader_evaluation_%'")
        connection.execute("ALTER TABLE grader_evaluation_runs DROP COLUMN suite_fingerprint")
        connection.execute("ALTER TABLE grader_evaluation_runs DROP COLUMN reproducibility_json")
        if not had_attempt_history:
            connection.execute("DROP TABLE grader_evaluation_attempts")
        if manifested:
            Database.record_backup_tables(connection)

    restored = Database(tmp_path / "restored")
    restored.restore_backup(backup)
    EvaluationRunner(restored, OfflineProvider(), settings)

    with restored.connect() as connection:
        assert connection.execute("SELECT id FROM grader_evaluation_runs").fetchall() == [(result.run_id,)]
        assert connection.execute("SELECT * FROM grader_evaluation_cases").fetchall() == [saved_case]
        assert set(connection.execute("SELECT * FROM grader_evaluation_attempts")) == saved_attempts
    Database.validate_backup(restored.create_backup())


def test_startup_recovers_an_unreserved_evaluation_claim_before_running_later_cases(tmp_path):
    """A crash after claim but before the send reservation returns the first fixed case to its queue."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    cases = load_fixed_cases()[:2]
    run_id = runner._run_id("offline-model", cases)
    request_id = runner.jobs.enqueue_evaluation(
        __import__("daily_ml.grader_evaluation", fromlist=["_context"])._context(cases[0]), "offline-model",
    )
    with database.connect() as db:
        db.execute("INSERT INTO grader_evaluation_cases VALUES (?,?,?)", (run_id, cases[0].id, request_id))
        db.execute("INSERT INTO grader_evaluation_attempts VALUES (?,?,?)", (run_id, cases[0].id, request_id))
    assert runner.jobs._claim(request_id)["id"] == request_id

    runner.jobs.recover_interrupted()
    summary = asyncio.run(runner.run("offline-model", cases=cases))

    assert summary.completed_count == 2
    assert summary.usage_count == 2
