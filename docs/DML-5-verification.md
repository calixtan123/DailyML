# DML-5 submission and grading verification

## Saved behavior

Submitting an answer commits its original text, completion instant and learning timezone, calendar credit, and a grading request in one SQLite transaction. The submission uses the saved question snapshot. A unique item key protects the original, and a unique question/date key protects calendar credit. A second tab can read the first committed submission. An explicit retry or clarification creates a linked request while preserving the original and its credit.

The Today calendar shows actual counts for the week containing the server's learning date. It provides date and status text for assistive technology and leaves future and pre-first-use dates unmarked. Grading runs after the submission response is ready. Reopening feedback reads SQLite. The visible question polls only while its request can advance automatically.

The existing grading ledger records request state, provider usage, and validated feedback with its versioned context. Restart recovery distinguishes queued work from sends with an unknown outcome. A confirmed transient 429 with a usable Retry-After of at most five seconds can trigger one bounded retry. Explicit later sends are available for interrupted or recoverable requests, with a possible-prior-usage message when the outcome is unknown. SQLite backup coverage checks that the original, calendar credit, and a linked clarification request survive restore.

## Offline verification on 19 November 2026

| Command | Recorded result |
| --- | --- |
| `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_submissions.py --tb=short` | 12 passed in 1.99 s, exit 0. Includes concurrent action keys, date and credit deduplication, London DST, access recovery, request counts, the 429 bound, restart and SQLite backup. |
| `npm --prefix frontend test -- --run` | 35 passed in 1.97 s, exit 0. Includes immediate credit, navigation while waiting, stored feedback, and calendar counts above two. |
| `npm --prefix frontend run typecheck` | Exit 0. |
| `npm --prefix frontend run build` | Exit 0. |
| `/private/tmp/dml5-verify.sXg9zmtc/venv/bin/python -m pytest -q --tb=short` | 221 passed in 20.35 s, exit 0, using the freshly built and installed wheel. |
| `/private/tmp/dml5-verify.sXg9zmtc/venv/bin/python scripts/smoke_installed.py /private/tmp/dml5-verify.sXg9zmtc/*.whl /private/tmp/dml5-verify.sXg9zmtc/venv/bin/daily-ml` | Wheel and installed launcher passed, exit 0. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml5-verify.sXg9zmtc/venv/bin/daily-ml npm --prefix frontend run test:e2e -- --output=/private/tmp/dml5-verify.sXg9zmtc/browser-results` | Two Chromium journeys passed in 4.7 s, exit 0. Covers an overnight two-tab submission, one credit and one model attempt, other practice while grading, refresh and stored feedback, polling cleanup, 320–1440 px overflow checks, and the existing drafts/Settings journey. |

The first sandboxed runs could not open Chromium's macOS Mach port or bind temporary loopback ports. The complete runs passed with approved loopback access. An editable install omitted the package from Python imports locally. The verification environment used the locked dependencies and installed the built wheel into a disposable virtual environment. Provider responses and clocks were controlled locally. Live ChatGPT-plan grading remains pending under the DML-2 real-account acceptance check.
