# DML-4 verification

DML-4 saves daily recommendations, additional practice items and answer drafts in the local SQLite database. Recommendations preserve the version of the question, rubric and sources presented to the learner. Conditional draft versions protect edits made in multiple tabs. An unfinished draft can be resumed after the learning date changes.

## Initial verification

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

The editable-environment run had 201 passing tests and seven OAuth subprocess import failures. Diagnosis confirmed macOS repeatedly sets `UF_HIDDEN` on the editable `.pth` file. Python ignores hidden `.pth` files, so isolated subprocesses could not import `daily_ml`. Reinstalling the editable package did not resolve the flag. Building the initial wheel with `UV_CACHE_DIR=/private/tmp/dml4-uv-cache uv build --wheel --offline --out-dir /private/tmp/dml4-python-proof` and installing it in a disposable environment allowed the complete suite to pass.

## Review fix verification

Choosing an exact question reopens its saved overnight draft even when today's pair contains a new item for that question. Back and Forward wait for the draft save when leaving Today for Settings. If the save fails, the browser returns to Today with the edit visible and the history action can be retried.

| Check | Result |
| --- | --- |
| `/private/tmp/dml4-history-verify.o2fM5aQ5/venv/bin/python -m pytest -q --tb=short` | 209 passed in 19.73 s against the fresh installed wheel. The new API test saved an older draft after midnight, reopened it and verified that the next day's pair and question snapshots were preserved. |
| `npm --prefix frontend test -- --run` | 33 tests passed, including opening the overnight draft and retrying Back and Forward after a failed save. |
| `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` | Both passed. |
| `/private/tmp/dml4-history-verify.o2fM5aQ5/venv/bin/python scripts/smoke_installed.py /private/tmp/dml4-history-verify.o2fM5aQ5/*.whl /private/tmp/dml4-history-verify.o2fM5aQ5/venv/bin/daily-ml` | Wheel and installed launcher passed. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml4-history-verify.o2fM5aQ5/venv/bin/daily-ml npm --prefix frontend run test:e2e -- --output=/private/tmp/dml4-history-verify.o2fM5aQ5/browser-results` | One Chromium journey passed in 3.6 s. It verified failed Back recovery, the retry and the answer after reloading. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml4-history-verify.o2fM5aQ5/venv/bin/daily-ml npm --prefix frontend run test:e2e -- --config=/private/tmp/dml4-history-verify.o2fM5aQ5/playwright.config.ts --output=/private/tmp/dml4-history-verify.o2fM5aQ5/forward-results` | One companion Chromium journey passed in 3.6 s. The temporary spec verified failed Forward recovery, the retry and the answer after reloading. |

Installed checks used temporary local data and simulated sign-in. The sandbox blocked temporary loopback ports, and the full runs passed with approved loopback access. The responsive journey checked overflow at 200% CSS zoom. Native Chrome visual and keyboard verification at 200% browser zoom remains pending because macOS ScreenCaptureKit returned `SCStreamErrorDomain -3812` (invalid parameter) when opening Chrome for inspection.

## Browser zoom follow-up

A browser-level zoom check passed against the same installed wheel. The temporary Playwright Chromium profile used `chrome.tabs.setZoom(2)` and confirmed a 2.0 zoom factor. The layout viewport reflowed from 1440 to 720 CSS pixels and the scroll width stayed at 720 CSS pixels. Keyboard input reached and activated the skip link, moved focus to the compact session selector, and activated More questions. The selector then contained three questions, focus moved to the new question heading, and the answer editor stayed available. The viewport capture showed a visible focus ring and a readable session panel. The headless runner could not confirm keyboard selection inside Chromium’s native select popup.

Command (exit 0)

```sh
node /private/tmp/dml-native-zoom-efj_v_i8/verify.cjs /private/tmp/dml4-history-verify.o2fM5aQ5/venv/bin/daily-ml
```

## Whole-branch review follow-up

When a section's questions are already in the session, the section action resumes an unfinished draft first. It offers another editable item if none has a draft and explains when all questions in the section are complete. Regression tests cover resuming the unfinished alternative and showing the shortage while retaining the daily goal of two.

| Command | Result |
| --- | --- |
| `/private/tmp/dml45-final.69bO01/venv/bin/python -m pytest -q --tb=short` | 225 passed in 21.64 s, exit 0, against the freshly built and installed wheel. |
| `npm --prefix frontend test -- --run` | 44 passed across five files, exit 0. |
| `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` | Both exited 0. |
| `/private/tmp/dml45-final.69bO01/venv/bin/python scripts/smoke_installed.py /private/tmp/dml45-final.69bO01/*.whl /private/tmp/dml45-final.69bO01/venv/bin/daily-ml` | Wheel and installed launcher passed, exit 0. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml45-final.69bO01/venv/bin/daily-ml npm --prefix frontend run test:e2e -- --output=/private/tmp/dml45-final.69bO01/browser-results` | Two offline Chromium journeys passed in 5.1 s, exit 0, including the installed-app draft and Settings journey. |

The browser journeys used temporary local data and simulated grading. The responsive screenshots and 200% browser zoom evidence above are from the earlier review round.
