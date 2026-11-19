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

## Submission notice and calendar legend review follow-up

The answer editor shows which data OpenAI receives for grading and uses the same visible notice as the submit button's accessible description. The calendar explains all three count categories even when the current week contains only zero-credit days. Each date retains its accessible status.

| Command | Result |
| --- | --- |
| `npm --prefix frontend test -- --run src/Today.test.tsx -t 'shows what OpenAI receives\|shows a calendar legend' --reporter=dot` (before the fix) | Both regression tests failed on the missing text, exit 1. |
| `npm --prefix frontend test -- --run` (after the fix) | 37 passed across five files, exit 0. |
| `npm --prefix frontend run typecheck` | Exit 0. |
| `npm --prefix frontend run build` | Exit 0. |

## Whole-branch review follow-up

An original answer can be stored and credited against an imported question snapshot with long editorial text, even if the bank has since been updated. A clarification has its own versioned SQLite draft. The editor saves before leaving the question or Today, warns on unload when there are unsaved changes, and preserves text on failure or a concurrent edit. Sending uses the verified draft version and preserves the original answer, completion date and credit. Both session selectors show the latest known grading state for submitted items. Only the active request is polled.

| Command | Result |
| --- | --- |
| `/private/tmp/dml45-final.69bO01/venv/bin/python -m pytest -q --tb=short` | 225 passed in 21.64 s, exit 0, against the freshly built and installed wheel. Covers the saved snapshot, clarification backup and restore, stale saves and sends, and credit preservation. |
| `npm --prefix frontend test -- --run` | 44 passed across five files, exit 0. Covers clarification save, reload, navigation, failure, conflict, delayed status reads, send and grading labels. |
| `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` | Both exited 0. |
| `/private/tmp/dml45-final.69bO01/venv/bin/python scripts/smoke_installed.py /private/tmp/dml45-final.69bO01/*.whl /private/tmp/dml45-final.69bO01/venv/bin/daily-ml` | Wheel and installed launcher passed, exit 0. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml45-final.69bO01/venv/bin/daily-ml npm --prefix frontend run test:e2e -- --output=/private/tmp/dml45-final.69bO01/browser-results` | Two offline Chromium journeys passed in 5.1 s, exit 0, including two tabs submitting across London midnight and reopening saved feedback. |

Grading was simulated. Live ChatGPT-plan grading remains pending under DML-2. Clarification interactions were verified in frontend tests. The Chromium journeys covered installed submission and navigation behavior.

## Clarification draft race follow-up

When opening another question, Today keeps the clarification read-only until the question request finishes. A failed request restores editing to the saved draft. If a status refresh shows another tab sent the clarification while this tab has unsaved text, Today keeps the local text read-only and available to copy. The learner can then discard that edit to show the latest grading state.

| Command | Result |
| --- | --- |
| `npm --prefix frontend test -- --run src/Today.test.tsx -t 'keeps clarification read-only\|keeps unsaved text available' --reporter=dot` | Both tests reproduced the reported races before the fix, exit 1. Both passed afterward, exit 0. |
| `npm --prefix frontend test -- --run` | 46 passed across five files, exit 0. |
| `npm --prefix frontend run typecheck` | Exit 0. |
| `npm --prefix frontend run build` | Exit 0. |

The race tests ran the Today and Settings interface with controlled local API responses. Live grading remains pending under DML-2.
