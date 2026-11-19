# DML-2 grading connection verification

## Offline checks on 17 November 2026

| Check | Recorded outcome |
| --- | --- |
| `uv run pytest -q` | 137 passed in 20.32 seconds, exit 0. Covers grade validation, streaming, usage, dispatch authorization, concurrent claims, SQLite lock recovery, backup/restore and saved references. |
| `npm --prefix frontend test -- --run` | 15 passed, exit 0. Covers the Settings grading action, result display, saved source links and explicit retry in JSON mode. |
| `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` | Both exited 0. |
| Playwright Settings smoke | Passed. Exercises local Settings and an intercepted sign-in. The browser smoke uses a temporary data directory. |

Before the review fixes, `.venv/bin/pytest -q tests/test_grading_jobs.py -k 'dispatch_requires or temporary_write or saved_reference'` failed four cases covering dispatch authorization, locks at claim and finalization, and the saved reference. `npm --prefix frontend test -- --run src/GradingSettings.test.tsx` failed two of three cases covering the retry preview and saved source links. After the fixes, `.venv/bin/pytest -q tests/test_grading_jobs.py tests/test_provider.py` passed all 19 cases and the same focused frontend command passed all three. Both focused commands and all four final suite, typecheck and build commands exited 0 after the fixes.

The installed development environment used Python 3.12.4, OpenAI Python SDK 2.54.0, HTTPX 0.28.1 and PyJWT 2.15.1. Authentication uses the app's ChatGPT OAuth implementation shipped in Daily ML Practice 0.1.0. The test uses reference content `connection-leakage-v1`, prompt `rubric-v1` and feedback schema `feedback-v1`. The first outbound attempt requests strict JSON schema. If the provider rejects that capability, the learner can initiate another attempt. The retry omits the rejected format parameter, includes the required JSON schema in its instructions and validates returned feedback locally.

The whole-branch review identified two issues in the offline implementation. An explicit retry now omits the rejected `text.format` field, asks for JSON in the instructions and validates the response against the same local schema. SQLite reservation now runs on a worker thread. Tests cover the event loop continuing under contention, an expired deadline and cancellation before dispatch on either side of a reservation commit.

Independent task review and a fresh whole-branch review both accepted offline requirement compliance and code quality after scoped re-reviews of the fixes. No Critical or Important implementation finding remains. Live acceptance is a separate required gate and remains pending below.

Before these fixes, `.venv/bin/pytest -q tests/test_grading_jobs.py tests/test_provider.py -k 'explicit_json or reservation_contention or silently_fallback or prompted_json'` exited 1 with five failures and one pass. After the fixes, the same command passed all six selected cases. With the cancellation regressions included, `.venv/bin/pytest -q tests/test_grading_jobs.py tests/test_provider.py` passed all 25 tests in 2.93 seconds. Both green runs exited 0. The frontend and Playwright results above are from the previous verification rounds. The final fixes affected Python only.

## Integration with DML-3 on 17 November 2026

Resolved PR #2's conflicts against main at `c6383a9`, which includes the DML-3 question browser. The app opens Today for offline browsing. Settings includes the grading connection test and resets its model controls when the account changes or signs out. Both the grading and question-bank APIs and their regression tests remain available.

| Command | Recorded outcome |
| --- | --- |
| `.venv/bin/pytest -q` | 196 passed in 21.96 seconds, exit 0, with temporary loopback binding permitted for the offline OAuth fixtures. |
| `npm --prefix frontend test -- --run` | 25 passed in four files, exit 0, including app navigation, signed-out grading controls, account changes, sign-out and recovery of an unacknowledged submission. |
| `npm --prefix frontend run typecheck` | Exit 0. |
| `npm --prefix frontend run build` | Exit 0. |
| `npm --prefix frontend run test:e2e` | 1 Playwright test passed, exit 0, using temporary local data and an offline OAuth stand-in. Covers Today browsing, Settings navigation, responsive layout, sign-in controls and saved timezone. |
| `git diff --check` | Exit 0. |

The two new app integration tests failed before the grading panel was connected to Settings, then passed after the integration. The first full frontend run exposed an asynchronous sign-out assertion; it now waits for the controls to disappear. The initial Python run could not bind temporary loopback ports inside the default sandbox; permitting those local sockets allowed the complete offline suite to pass. These checks used temporary data and simulated provider responses.

The task review also identified a navigation recovery gap. App now retains an unacknowledged submission's complete body in memory, scoped to the verified account. Returning from Today to Settings can check that original action even if the refreshed model catalog no longer contains its model. Account changes and sign-out clear the retained action; an unavailable status hides grading controls until the account is verified again. Regression tests failed before each fix and passed afterward. This recovery covers navigation within the current App instance; a full browser reload loses the in-memory submission body.

The fresh whole-branch review also required recovery during a model-catalog outage. **Check previous test** now remains available to the verified account and resends the original model, request key and retry parent. A fresh submission still requires a valid model selection. The new outage regression failed before the fix and passed afterward. The final scoped re-review accepted both requirements and code quality with no remaining blocking findings. Live acceptance remains pending.

## Live acceptance on the learner's Mac

**Confirmed on 19 November 2026.** The learner confirmed successful sign-in, checks and the Settings connection test. The installed app saved the completed live assessment below. Earlier offline checks and pending statements above describe the pre-acceptance state.

1. Launch the app. From **Today**, open **Settings**, then select **Continue with ChatGPT** and complete consent for the intended account and workspace.
2. Open **Grading model** and choose an entry from the returned list. Record its identifier from the result and observe the starting 24-hour count.
3. Review the fixed question and answer, then choose **Run connection test** once. Wait for the terminal result. A new outbound attempt increases the local count. Reopening Settings shows the stored result without sending another model request.
4. Record the date, model identifier, terminal result, prompt version, schema mode, installed SDK and auth versions, and observed counts. If a provider restriction blocks completion, record the displayed recovery message and stage reached. Only the learner can authorize another attempt.

Keep credentials, authorization codes and account identifiers out of the evidence. The successful live check below clears this acceptance gate.


### Recorded live result, 19 November 2026

The learner initiated the Settings test through the installed launcher using their ChatGPT plan and confirmed it worked. A read-only inspection of the existing SQLite database found one connection-test request, `f467ae5e-f71a-4440-8035-04ebc0ac8848`, created at 15:53:32.094961 UTC and completed at 15:53:40.801436 UTC. It is `assessed`, with stored feedback, grade **3/3**, no error, and latency 8,701 ms. The model is `gpt-6.1-terra`; schema mode is `schema` (strict structured output), reference `connection-leakage-v1`, prompt `rubric-v1`, and schema `feedback-v1`.

Runtime versions from the installed environment: Python 3.12.4, Daily ML Practice 0.1.0 (including its ChatGPT OAuth implementation), OpenAI Python SDK 2.54.0, HTTPX 0.28.1 and PyJWT 2.15.1. The installed wheel contains the application code from reviewed commit `89822b0411fe51d33c1f529163182252149cf81c`.

Verification used `/private/tmp/dml-live-launch-20261119/venv/bin/python` with SQLite URI `mode=ro`: selected the latest assessed connection-test row, parsed `context_json` with `GradingContext.model_validate_json`, and ran `validate_feedback({"feedback": json.loads(row["feedback_json"])}, context)`. Result: **PASS**, both rubric criteria valid, assessed feedback and grade 3 present, no error. The ledger contains **one outbound model attempt**, **zero automatic retries**, and **zero practice requests**. The rolling 24-hour model-attempt count was **1** at inspection. This inspection sent no additional model request.

The earlier sign-in failure came from launching uninstalled source in a development environment: the isolated OAuth helper could not import the package. Building and installing the wheel with locked runtime dependencies restored the helper. The successful test used that installed launcher; no application source change was needed. The model catalog and sign-in are confirmed by the learner's Settings journey, while the completed feedback and attempt count were independently read from saved state.

This proves one real plan-backed connection test. It does not establish future allowance or a successful live practice submission. Practice submission, recovery and calendar behavior retain their independently reviewed offline verification under DML-5.
