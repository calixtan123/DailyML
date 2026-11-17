# DML-3 verification

Verified on 17 November 2026 against the branch for [DML-3](https://synapse21.atlassian.net/browse/DML-3). The untouched baseline `b4512da` passed 92 Python tests with `uv sync --locked && uv run --locked pytest -q`.

## Commands and results

| Command | Result |
| --- | --- |
| `env -u PYTHONPATH /private/tmp/dml3-tests.yTlONB/venv/bin/python -m pytest -q` | 151 passed in 16.49 seconds against a clean, non-editable wheel install. Temporary loopback ports supported simulated OAuth and launcher tests. |
| `env -u PYTHONPATH /private/tmp/dml3-tests.yTlONB/venv/bin/daily-ml validate-bank` | Ran from `/private/tmp` and exited 0. `Validated 12 questions across 6 sections.` |
| `npm --prefix frontend test -- --run` | 17 passed, including five browser-component tests for selection, empty states, retry and stale responses. |
| `npm --prefix frontend run typecheck` | Exit 0. |
| `npm --prefix frontend run build` | Exit 0. Generated the web assets used by the wheel. |
| `/opt/anaconda3/bin/python3 -m hatchling build -t wheel -d /private/tmp/dml3-r1-wheel` | Built `daily_ml_practice-0.1.0-py3-none-any.whl` using cached Hatchling 1.30.1 and its dependencies on `PYTHONPATH`. All 26 application source and resource files matched the wheel byte for byte. |
| `env -u PYTHONPATH /private/tmp/dml3-tests.yTlONB/venv/bin/python scripts/smoke_installed.py /private/tmp/dml3-r1-wheel/daily_ml_practice-0.1.0-py3-none-any.whl /private/tmp/dml3-tests.yTlONB/venv/bin/daily-ml` | `Wheel and installed launcher passed.` The script validated the bank from outside the checkout and checked the installed UI, session API, Host protection and clean shutdown. |
| `DML_E2E_EXECUTABLE=/private/tmp/dml3-tests.yTlONB/venv/bin/daily-ml npm --prefix frontend run test:e2e` | 1 passed in 2.8 seconds in Chromium on the final wheel. The test loaded all six sections, read the Deployment prompt and objective, and checked keyboard focus and widths of 1440, 1024, 768, 390 and 320 pixels. At 200% zoom it selected the Forests section, counted two question options, and verified the default prompt. It also verified the tab title on entry, in Settings and on return to Today, Settings reload and simulated sign-in cancellation. |

After the tab title fix, the frontend tests, type check and build passed again. The same Hatchling command built the final wheel with `-d /private/tmp/dml3-final-wheel`. The packaged web files matched the built assets, and the 24 backend source and resource files matched those in the earlier wheel that passed the Python suite.

## Coverage

- **Content and review.** The bank holds 3 Fundamentals, 2 Forests, 2 Other ensembles, 2 Deep learning, 1 Deployment and 2 MLOps questions. All 12 start at version 1 and use introductory general-purpose examples. The evidence log maps 16 inspected source records to reviewed dates, the learning objectives and the criterion IDs in the JSON. [Starter question source review](content-review.md)
- **Validation.** Tests cover question and curriculum IDs, required fields, section and concept links, difficulty, source dates and ID mappings, HTTPS URL syntax, positive finite weights, the eight-criterion limit, the exact `1e-6` tolerance boundary and the required starter coverage. An installed CLI check with an oversized JSON decimal exponent exited 1 and named `generalisation.json` in its error.
- **Persistence.** Tests cover identical reimports, rollback on malformed input and an immutable-version conflict, version updates, retirement, and SQLite backup and restore with both question history and saved settings. The replacement regression verifies whole-import rollback until the release includes an explicit retirement. Upgraded and fresh databases then expose identical active prompts, and the upgraded database retains prior versions. Strict imports also reject stale releases.
- **Browser and API.** The protected API reads real SQLite records and returns only the ID, version, title, prompt, learning objective and difficulty for each question. API tests check invalid and unavailable sections and the absence of assessment content from browser responses. The browser-component tests exercise retry after a failed section or question request.

## Verification limits

The original editable test environment could not import `daily_ml` in isolated OAuth subprocesses or run `daily-ml validate-bank`. A non-editable wheel install resolved both failures. The sandboxed run could not bind `127.0.0.1`, so the full suite and installed smoke checks used temporary loopback ports with local simulated provider traffic. No live ChatGPT account or grading request was used. The offline `uv build` could not use the default cache under the sandbox, so the final build invoked the cached, pinned Hatchling backend directly.

The frontend reused the pinned MagicPath shell documented in the repository, including system fonts, dark tokens and the sidebar layout for project `473429111537307648`, component `473429115328958464` and revision `473455629521022976`. A MagicPath connector quota blocked a fresh rendering of the pinned revision, so visual verification covered the installed app in Chromium. [Committed design reference](design.md)
