# Daily ML Practice

Daily ML Practice runs on your Mac and opens Settings in your browser. Connect your ChatGPT account, save local preferences and test grading with a fixed question about data leakage.

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

Select **Continue with ChatGPT**, complete consent in the new browser tab, then return to Settings. The installation keeps one registered ChatGPT account and workspace. Reconnection uses that registration. Cancel or retry from Settings as needed. **Sign out** removes local tokens and attempts to revoke the renewable session. Use the **Disconnect in ChatGPT** link to remove the connection in ChatGPT. [ChatGPT plan, credit and connection controls](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)

## Test grading with your ChatGPT plan

After signing in, choose a **Grading model** from your account's list. Read the **Reference question** and **Reference answer** in Settings. **Run connection test** sends this fixed example, its marking guide and sources to OpenAI and can use your ChatGPT plan or credits. Opening Settings or selecting a model does not send a grading request.

Settings shows the model's feedback and a local grade from 0 to 3, or its request for clarification. **Model attempts in the last 24 hours** counts this installation's outbound and uncertain attempts. If a model rejects structured output, **Retry in JSON mode** authorizes another attempt. The test leaves your practice history untouched. Live account verification is pending. [Verification record](docs/DML-2-verification.md)

## Settings and local data

The default directory is `~/Library/Application Support/Daily ML Practice/`. It contains the SQLite preferences database, rotating operational logs, and a protected `credentials/` directory. Keep credentials out of SQLite backups. Signing out retains SQLite data and the saved account registration and installation identity.

Configuration loads packaged defaults, saved SQLite preferences, installation TOML, then `DML_` environment overrides. The launcher resolves the data directory before it opens SQLite. Settings shows whether the timezone is controlled by an override.

Copy [config.example.toml](config.example.toml) to the default directory as `config.toml`, or pass `daily-ml run --config "/path with spaces/config.toml"`. `DML_CONFIG` also selects the TOML. Paths in the TOML can be relative to that file. Use absolute or home-relative paths in environment overrides.

- `DML_DATA_DIR` changes the data directory, and `DML_PORT` changes the port. Port `0` asks the OS for a free port.
- `DML_TIMEZONE` takes an IANA name such as `Europe/London`.
- `DML_GRADE_THRESHOLDS`, `DML_REQUEST_TIMEOUT` (60 seconds by default), and `DML_MODEL_ID` configure grading. `DML_REVIEW_INTERVALS` and `DML_ANSWER_LIMIT` configure learning preferences. Numeric values and arrays use TOML syntax.

If the launcher reports missing assets, install the built wheel or run the frontend build before rebuilding. For a port conflict, stop the process using that port or set `DML_PORT=0`. If Daily ML is already running against your data directory, return to its browser tab or stop the first launcher.

## Offline checks

```sh
uv sync --locked --python 3.12
uv run pytest -q
npm --prefix frontend ci
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend exec -- playwright install chromium
npm --prefix frontend run test:e2e
```

Playwright launches the local app in a temporary directory and intercepts the OpenAI authorization page. Set `DML_E2E_EXECUTABLE` to an absolute installed command path to test the built wheel. The CI workflow installs the wheel with locked dependencies and runs both the installed HTTP smoke and the browser smoke.
