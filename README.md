# Daily ML Practice

Daily ML Practice runs on your Mac and opens the question browser. Choose a section and question to read its prompt and learning objective. The 12 starter questions work offline without a ChatGPT sign-in.

## Install and run

Building the wheel requires [uv](https://docs.astral.sh/uv/), Python 3.12 or later, and Node 24 (or Node 22.12+). From the checkout, build the frontend and then the wheel.

```sh
npm --prefix frontend ci
npm --prefix frontend run build
uv build --wheel
uv export --quiet --locked --no-dev --no-emit-project --no-hashes --output-file dist/runtime-constraints.txt
uv tool install --python 3.12 --constraints dist/runtime-constraints.txt dist/daily_ml_practice-0.1.0-py3-none-any.whl
```

Run the installed command from any directory. Your browser opens on the printed `127.0.0.1` address. Use Ctrl+C in the terminal to stop. Use `daily-ml run --no-browser` to open the address yourself. Node is only needed for building and frontend development.

```sh
daily-ml run
```

To connect your account, open **Settings**, select **Continue with ChatGPT**, complete consent in the new browser tab, then return to Settings. The installation keeps one registered ChatGPT account and workspace. Reconnection uses that registration. Cancel or retry from Settings as needed. **Sign out** removes local tokens and attempts to revoke the renewable session. Use the **Disconnect in ChatGPT** link to remove the connection in ChatGPT. [ChatGPT plan, credit and connection controls](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)

Offline tests verify the OAuth protocol with simulated OpenAI replies. A sign-in with the learner's real account and workspace still needs manual verification. No live account or plan allowance was used for testing.

## Settings and local data

The default directory is `~/Library/Application Support/Daily ML Practice/`. It contains the SQLite database for preferences and imported question versions, rotating operational logs, and a protected `credentials/` directory. Keep credentials out of SQLite backups. Signing out retains SQLite data and the saved account registration and installation identity.

Configuration loads packaged defaults, saved SQLite preferences, installation TOML, then `DML_` environment overrides. The launcher resolves the data directory before it opens SQLite. Settings shows whether the timezone is controlled by an override.

Copy [config.example.toml](config.example.toml) to the default directory as `config.toml`, or pass `daily-ml run --config "/path with spaces/config.toml"`. `DML_CONFIG` also selects the TOML. Paths in the TOML can be relative to that file. Use absolute or home-relative paths in environment overrides.

- `DML_DATA_DIR` changes the data directory, and `DML_PORT` changes the port. Port `0` asks the OS for a free port.
- `DML_TIMEZONE` takes an IANA name such as `Europe/London`.
- `DML_REVIEW_INTERVALS`, `DML_GRADE_THRESHOLDS`, `DML_ANSWER_LIMIT`, `DML_REQUEST_TIMEOUT`, and `DML_MODEL_ID` configure future learning features. Numeric values and arrays use TOML syntax.

If the launcher reports missing assets, install the built wheel or run the frontend build before rebuilding. For a port conflict, stop the process using that port or set `DML_PORT=0`. If Daily ML is already running against your data directory, return to its browser tab or stop the first launcher.

## Question bank

The launcher imports the packaged question bank into SQLite in one transaction. Each question has a stable ID. A content update or retirement adds a new version so saved versions remain available for future practice history.

To retire a starter question in a release, keep its ID and include a higher version with `retired: true`. Include an active replacement for the same concept to retain the required starter coverage. The launcher rejects the whole update if it would leave the installed active questions different from the release. Existing versions stay available for history.

Run `daily-ml validate-bank` from any directory to check the installed bank. This command uses the local package resources and needs no browser assets, SQLite database, account or network. For content authoring, add `--bank-dir /path/to/bank` to check a directory containing `curriculum.json` and `questions/*.json`. The command reports record-specific errors and exits with a nonzero status for invalid content. [Starter question source review](docs/content-review.md)

## Offline checks

```sh
uv sync --locked --python 3.12
uv run pytest -q
uv run daily-ml validate-bank
npm --prefix frontend ci
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend exec -- playwright install chromium
npm --prefix frontend run test:e2e
```

Playwright launches the local app in a temporary directory and intercepts the OpenAI authorization page. Set `DML_E2E_EXECUTABLE` to an absolute installed command path to test the built wheel. The CI workflow installs the wheel with locked dependencies and runs both the installed HTTP smoke and the browser smoke.
