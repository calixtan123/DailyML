"""Verify local scoring and rubric-aware validation independently of inference."""

from copy import deepcopy

import pytest

from daily_ml.config import load_settings
from daily_ml.grading import GradingContext, calculate_grade, validate_feedback


def example_feedback():
    """Provide a complete reviewed-shape assessment."""
    return {"feedback": {"status": "assessed", "summary": "Good distinction.",
            "criteria": [{"criterion_id": "fit", "status": "met", "justification": "Uses training statistics."},
                         {"criterion_id": "apply", "status": "not_met", "justification": "Missing application."}],
            "critical_misconception_ids": [], "corrections": ["Apply the saved transform."],
            "model_answer": "Fit on training, then transform held-out data.",
            "reasoning": ["Isolate held-out data."], "example": "Use a training-set mean.",
            "source_ids": ["scikit-learn"], "follow_up": []}}


@pytest.mark.parametrize(("weight", "grade"), [(24, 0), (25, 1), (59, 1), (60, 2), (84, 2), (85, 3)])
def test_normalized_boundaries_and_critical_cap(tmp_path, weight, grade):
    """Weights normalize before comparison with exact decimal boundaries."""
    ctx = GradingContext.connection_test()
    ctx.criteria[0].weight = weight
    ctx.criteria[1].weight = 100 - weight
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    raw = example_feedback()
    feedback = validate_feedback(raw, ctx)
    result = calculate_grade(feedback, ctx, settings)
    assert type(result) is int and result == grade
    raw["feedback"]["critical_misconception_ids"] = ["fit-on-all"]
    assert calculate_grade(validate_feedback(raw, ctx), ctx, settings) == min(grade, 1)
    raw["feedback"]["criteria"][1]["status"] = "partial"
    raw["feedback"]["criteria"][0]["status"] = "partial"
    raw["feedback"]["critical_misconception_ids"] = []
    assert calculate_grade(validate_feedback(raw, ctx), ctx, settings) == 1


@pytest.mark.parametrize(("field", "value"), [
    ("summary", None), ("model_answer", " "), ("reasoning", []), ("example", "a" * 9000),
    ("corrections", ["gap"] * 50), ("source_ids", ["unknown"]),
    ("critical_misconception_ids", ["unknown"]), ("grade", 3),
    ("criteria", []), ("criteria", [{"criterion_id": "unknown", "status": "all", "justification": None}]),
])
def test_invalid_assessments_raise_safe_error(field, value):
    """Invalid fields cannot leave validated feedback or echo input."""
    raw = example_feedback()
    raw["feedback"][field] = value
    with pytest.raises(ValueError, match="Invalid grading feedback"):
        validate_feedback(raw, GradingContext.connection_test())


def test_exact_coverage_required():
    """A duplicated known criterion cannot replace a missing one."""
    raw = example_feedback()
    raw["feedback"]["criteria"][1] = deepcopy(raw["feedback"]["criteria"][0])
    with pytest.raises(ValueError):
        validate_feedback(raw, GradingContext.connection_test())


def test_clarification_needs_text_and_has_no_grade(tmp_path):
    """Clarification carries an actionable explanation without grading."""
    ctx = GradingContext.connection_test()
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    raw = {"feedback": {"status": "needs_clarification", "explanation": "Which split supplied the mean?"}}
    assert calculate_grade(validate_feedback(raw, ctx), ctx, settings) is None
    del raw["feedback"]["explanation"]
    with pytest.raises(ValueError):
        validate_feedback(raw, ctx)
