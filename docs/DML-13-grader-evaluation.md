# DML-13 fixed grader evaluation

`daily-ml evaluate-grader --model MODEL --confirm-reviewed-fixtures` is the only supported live invocation. Review `src/daily_ml/resources/grader_evaluation_cases.json` independently before supplying the acknowledgement flag. The fixture is immutable package data (`fixed-v1`) and binds every answer to a reviewed starter-bank question version and `rubric-v1`.

The command reports a maximum of 18 planned requests before sending one. It uses the normal OAuth-backed provider and `GradingJobs`; an attempt is only counted after the same durable pre-send reservation used by practice. Access and allowance errors are saved as partial evaluation state. A stream interruption is saved as `outcome_unknown` and cannot be automatically replayed, because the provider may already have consumed a request.

The report records case identifier, category, terminal state, local grade, expected-range agreement, detected critical misconception IDs, corrections, safe token usage, model, prompt/schema/fixture/SDK/auth versions, and date. It intentionally excludes fixed learner-answer text and credentials. The release gate is complete only with 18 terminal cases, 16 or more agreements, and all predefined critical misconceptions detected. This is a narrow regression sample, not a population-level quality estimate.
