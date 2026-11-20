"""Exercise the offline, resumable fixed grader evaluation."""

import asyncio

import pytest

from daily_ml.config import load_settings
from daily_ml.database import Database
from daily_ml.grader_evaluation import EvaluationRunner, load_fixed_cases
from daily_ml.grading import Assessment, CriterionAssessment, FeedbackEnvelope
from daily_ml.provider import ProviderResult
from daily_ml.provider import ProviderFailure
from daily_ml import cli


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


def test_fixed_cases_are_versioned_reviewed_and_cover_the_release_categories():
    """The packaged evaluation set is immutable, starter-backed and sufficiently diverse."""
    cases = load_fixed_cases()
    categories = {category for case in cases for category in case.categories}

    assert len(cases) >= 18
    assert {"fully_correct", "mostly_correct", "substantial_gap", "critical_misconception",
            "concise_paraphrased", "valid_alternative", "needs_clarification", "grading_manipulation"} <= categories
    assert all(case.question_version == 1 and case.rubric_version for case in cases)
    assert all(case.required_corrections for case in cases if case.expected_critical_ids)


def test_runner_resumes_terminal_cases_and_records_usage_from_the_production_ledger(tmp_path):
    """A second invocation reads saved terminal cases without another model reservation."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OfflineProvider(), settings)
    cases = load_fixed_cases()[:2]

    first = asyncio.run(runner.run("offline-model", cases=cases))
    second = asyncio.run(runner.run("offline-model", cases=cases))

    assert first.completed_count == second.completed_count == 2
    assert first.usage_count == second.usage_count == 2
    assert second.resumed_count == 2


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


def test_runner_does_not_replay_an_unknown_outcome_on_later_invocation(tmp_path):
    """A potentially completed provider request blocks resumption instead of being duplicated."""
    database = Database(tmp_path)
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    runner = EvaluationRunner(database, OutcomeUnknownProvider(), settings)
    cases = load_fixed_cases()[:2]

    first = asyncio.run(runner.run("offline-model", cases=cases))
    runner.jobs.provider = OfflineProvider()
    second = asyncio.run(runner.run("offline-model", cases=cases))

    assert first.paused_state == second.paused_state == "outcome_unknown"
    assert first.usage_count == second.usage_count == 1


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
