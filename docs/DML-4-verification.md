# DML-4 verification

DML-4 saves daily recommendations, additional practice items and answer drafts in the local SQLite database. Recommendations preserve the version of the question, rubric and sources presented to the learner. Conditional draft versions protect edits made in multiple tabs. An unfinished draft can be resumed after the learning date changes.

| Check | Result |
| --- | --- |
| `npm --prefix frontend test -- --run` | 30 tests passed. |
| `npm --prefix frontend run typecheck` | Passed. |
| `npm --prefix frontend run build` | Passed. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml4-verification.TEVwYZjY/venv/bin/daily-ml npm --prefix frontend run test:e2e -- --output=/private/tmp/dml4-keyboard-focus-verify-20261119` | One test passed in 3.8 s in Chromium against the installed wheel. The journey verified a stable pair in two tabs, failed-save recovery, saved-version conflict recovery, refresh, focused practice, skip navigation, keyboard focus, narrow layouts and simulated sign-in. |
| `/private/tmp/dml4-verification.TEVwYZjY/venv/bin/python -m pytest -q` | 208 passed in 22.65 s against the freshly built and installed wheel. |

The first sandboxed Chromium launch was blocked by Mach port permissions. The approved rerun launched Chromium and used a temporary data directory. The browser test intercepts sign-in locally. It checks layout at 1440, 1024, 768, 390 and 320 CSS pixels, with an additional 200% CSS zoom check.

Installed-wheel screenshots were captured at widths of 1440, 1024, 768, 390 and 384 pixels under `/private/tmp/dml4-verification.TEVwYZjY/`. Desktop and phone captures were checked against the saved design reference. The desktop retained the 232 px session column and the phone kept the compact session selector and More questions action reachable. MagicPath returned Internal errors during the live reference check.

Python tests cover deterministic selection, concurrent database connections, immutable snapshots, draft version conflicts and SQLite backup and restore. DML-6 supplies real accepted-attempt due dates to the recommendation selector.

The editable-environment run had 201 passing tests and seven OAuth subprocess import failures. Diagnosis confirmed macOS repeatedly sets `UF_HIDDEN` on the editable `.pth` file; Python ignores hidden `.pth` files, so isolated subprocesses cannot import `daily_ml`. Reinstalling the editable package did not resolve the flag. Built the current wheel with `UV_CACHE_DIR=/private/tmp/dml4-uv-cache uv build --wheel --offline --out-dir /private/tmp/dml4-python-proof`, installed it into the disposable verification environment, and ran the complete suite there successfully. No application code was changed to accommodate this environment issue.
