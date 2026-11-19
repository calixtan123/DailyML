"""Start the installed launcher with a controlled clock and offline grading."""

import asyncio
import os
from datetime import datetime
from functools import partial
from pathlib import Path

from daily_ml import app, cli
from daily_ml.database import Database
from daily_ml.grading import validate_feedback
from daily_ml.practice import Practice
from daily_ml.provider import ProviderResult


class OfflineProvider:
    """Hold transport until the browser test releases it, then return validated feedback."""

    def __init__(self, _connection, *, timeout):
        self.timeout = timeout

    async def grade(self, context, _model_id, *, mode, before_send):
        """Reserve one send and return a rubric-matched result without using the network."""
        before_send()
        gate = Path(os.environ["DML_E2E_GRADING_GATE"])
        async with asyncio.timeout(self.timeout):
            while not gate.exists():
                await asyncio.sleep(0.025)
        feedback = validate_feedback({
            "feedback": {
                "status": "assessed",
                "summary": "You explained the central idea.",
                "criteria": [
                    {"criterion_id": criterion.id, "status": "met", "justification": "Explains the criterion."}
                    for criterion in context.criteria
                ],
                "critical_misconception_ids": [],
                "corrections": ["Give a concrete example."],
                "model_answer": context.reference,
                "reasoning": ["Apply the principle to a new dataset."],
                "example": "Evaluate a model on held-out examples.",
                "source_ids": [next(iter(context.sources))],
                "follow_up": [],
            },
        }, context)
        return ProviderResult(feedback, "offline-response", {"output_tokens": 120})


def main():
    """Configure the disposable test app and launch it through its normal entry point."""
    clock_file = Path(os.environ["DML_E2E_CLOCK"])
    app.Practice = partial(Practice, clock=lambda: datetime.fromisoformat(clock_file.read_text()))
    app.Provider = OfflineProvider
    database = Database(Path(os.environ["DML_DATA_DIR"]))
    preferences = database.get_preferences()
    database.save_preferences({**preferences.values, "model_id": "offline-test"}, expected_version=preferences.version)
    return cli.main(["run", "--no-browser"])


if __name__ == "__main__":
    raise SystemExit(main())
