"""Validate rubric-grounded feedback and compute grades locally from criterion weights."""

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .config import Settings

PROMPT_VERSION = "rubric-v1"
SCHEMA_VERSION = "feedback-v1"
# Local response limits keep one assessment readable and bounded in storage.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Scores = {"met": Decimal(1), "partial": Decimal("0.5"), "not_met": Decimal(0)}


class _StrictModel(BaseModel):
    """Forbid unknown fields and coercion of provider values."""

    model_config = ConfigDict(extra="forbid", strict=True)


class Criterion(BaseModel):
    """One approved rubric criterion and its relative importance."""

    id: Identifier
    description: Text
    weight: Decimal = Field(gt=0, allow_inf_nan=False)


class GradingContext(BaseModel):
    """The versioned content needed to judge a single answer."""

    version: Identifier
    question: Text
    answer: str = Field(min_length=1)
    reference: Text
    criteria: list[Criterion] = Field(min_length=1, max_length=8)
    critical_misconceptions: dict[str, str]
    sources: dict[str, str]

    @classmethod
    def connection_test(cls) -> "GradingContext":
        """Load the packaged reference test independent of practice data.

        Returns
        -------
        GradingContext
            A fresh validated snapshot of the fixed question and reference answer.
        """
        path = Path(__file__).with_name("resources") / "connection_question.json"
        return cls.model_validate_json(path.read_bytes())


class CriterionAssessment(_StrictModel):
    """One answer-grounded judgement."""

    criterion_id: Identifier
    status: Literal["met", "partial", "not_met"]
    justification: Text


class Assessment(_StrictModel):
    """Concise assessment and optional expanded teaching content."""

    status: Literal["assessed"]
    summary: Text
    criteria: list[CriterionAssessment] = Field(min_length=1, max_length=8)
    critical_misconception_ids: list[Identifier] = Field(max_length=8)
    corrections: list[Text] = Field(max_length=8)
    model_answer: Text
    reasoning: list[Text] = Field(min_length=1, max_length=8)
    example: Text
    source_ids: list[Identifier] = Field(min_length=1, max_length=8)
    follow_up: list[Text] = Field(max_length=1)


class Clarification(_StrictModel):
    """A request for clarification ahead of any grade."""

    status: Literal["needs_clarification"]
    explanation: Text


class FeedbackEnvelope(_StrictModel):
    """Object-root structured-output schema shared by both provider modes."""

    feedback: Assessment | Clarification


def validate_feedback(payload: object, context: GradingContext) -> FeedbackEnvelope:
    """Check feedback structure and references against the saved question.

    Parameters
    ----------
    payload : object
        Decoded provider JSON.
    context : GradingContext
        Authoritative rubric, sources and critical misconceptions.

    Returns
    -------
    FeedbackEnvelope
        Complete feedback suitable for a local grade.

    Raises
    ------
    ValueError
        When structure, coverage or identifiers are invalid.
    """
    try:
        envelope = FeedbackEnvelope.model_validate(payload)
    except ValidationError:
        raise ValueError("Invalid grading feedback.") from None
    result = envelope.feedback
    if isinstance(result, Assessment):
        ids = [item.criterion_id for item in result.criteria]
        if (len(ids) != len(set(ids)) or set(ids) != {item.id for item in context.criteria}
                or not set(result.source_ids) <= context.sources.keys()
                or not set(result.critical_misconception_ids) <= context.critical_misconceptions.keys()):
            raise ValueError("Invalid grading feedback.")
    return envelope


def calculate_grade(feedback: FeedbackEnvelope, context: GradingContext, settings: Settings) -> int | None:
    """Compute normalized criterion coverage with exact decimal thresholds.

    Parameters
    ----------
    feedback : FeedbackEnvelope
        Locally validated feedback for this rubric.
    context : GradingContext
        Weighted rubric used in the model request.
    settings : Settings
        Effective grading thresholds in ascending order.

    Returns
    -------
    int or None
        Grade in [0, 3], or no grade when clarification is needed.
    """
    result = feedback.feedback
    if isinstance(result, Clarification):
        return None
    weights = {item.id: Decimal(str(item.weight)) for item in context.criteria}
    score = sum(weights[item.criterion_id] * Scores[item.status] for item in result.criteria)
    coverage = score / sum(weights.values())
    grade = sum(coverage >= Decimal(str(boundary)) for boundary in settings.grade_thresholds)
    return min(grade, 1) if result.critical_misconception_ids else grade
