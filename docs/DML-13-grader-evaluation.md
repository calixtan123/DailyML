# DML-13 fixed grader evaluation

`daily-ml evaluate-grader --model MODEL --confirm-reviewed-fixtures` is the supported live invocation. Review `src/daily_ml/resources/grader_evaluation_cases.json` independently before supplying the acknowledgement flag. The fixture is immutable package data (`fixed-v1`) and binds every answer to a reviewed starter-bank question version and `rubric-v1`.

One explicit invocation reports and makes at most 18 model submissions (possibly fewer when cases are already terminal). OAuth token refreshes and model-catalog reads are separate traffic and are not model submissions. Linked retries and cumulative counts can exceed 18 only over later explicit resumptions. The command uses the normal OAuth-backed provider and `GradingJobs`; an attempt is only counted after the same durable pre-send reservation used by practice. Access and allowance errors are saved as partial evaluation state. A stream interruption is saved as `outcome_unknown` and cannot be automatically replayed, because the provider may already have consumed a request.

Practice and evaluation build the provider input from the same versioned question snapshot and exact answer text. The question ID and case ID are retained in local report metadata. The provider receives the same question version label that practice sends, such as `v1`.

The normal command resumes proven-unsent access/allowance pauses. A confirmed failure remains paused by default. After reviewing the saved evidence, an operator may explicitly request a ledger-linked successor with:

```sh
daily-ml evaluate-grader --model MODEL --confirm-reviewed-fixtures --recover-confirmed-failures
```

An ambiguous `outcome_unknown` request remains non-replayable even with that option.

The report records case identifier, category, terminal state, local grade, expected-range agreement, detected critical misconception IDs, corrections, clarification explanations, safe token usage, model, prompt/schema/fixture versions, and date. It intentionally excludes fixed learner-answer text and credentials. The saved `versions` object includes the authentication flow label and the installed versions of Daily ML Practice (`app`), OpenAI (`sdk`), PyJWT (`pyjwt`), and HTTPX (`httpx`). Reports retain the versions captured when their run started. A package version change creates a separate run with its own evidence.

The command writes this JSON report to the local data directory after normal completion and after a graceful interrupt. An interrupted report has `interrupted: true` and the command exits nonzero. Retain the printed path to review persisted progress before a later explicit resume. A report or summary rejects a requested suite whose persisted fingerprint differs, so saved evidence cannot be reinterpreted as the canonical fixture. The release gate is complete only with all 18 canonical cases in their exact reviewed order and content, 16 or more agreements, and all predefined critical misconceptions detected. Inspect semantic required corrections using the `present` or `missing` evidence status. Clarification evidence comes from the model's structured `explanation`. This regression sample measures agreement on its 18 reviewed cases.
