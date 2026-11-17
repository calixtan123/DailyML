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

## Live acceptance on the learner's Mac

**Pending.** The recorded checks used simulated provider responses. The learner's actual account model list and a completed grading request still need verification.

1. Launch the app, select **Continue with ChatGPT** and complete consent for the intended account and workspace.
2. Open **Grading model** and choose an entry from the returned list. Record its identifier from the result and observe the starting 24-hour count.
3. Review the fixed question and answer, then choose **Run connection test** once. Wait for the terminal result. A new outbound attempt increases the local count. Reopening Settings shows the stored result without sending another model request.
4. Record the date, model identifier, terminal result, prompt version, schema mode, installed SDK and auth versions, and observed counts. If a provider restriction blocks completion, record the displayed recovery message and stage reached. Only the learner can authorize another attempt.

Keep credentials, authorization codes and account identifiers out of the evidence. DML-2 remains open until the live check succeeds.
