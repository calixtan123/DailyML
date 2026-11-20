# Daily ML Practice

Daily ML Practice runs on your Mac and opens **Today** with two saved recommendations. Use **More questions** for extra practice or **Browse questions** to choose a topic or specific question. The 12 starter questions work offline without a ChatGPT sign-in.

Open **Settings** to connect your ChatGPT account, save local preferences and test grading with a fixed question about data leakage.

Use **Save draft** to keep your work in SQLite. Switching questions also saves any edits. If a save fails, the editor keeps your text and offers retry and copy controls. If another tab saves first, Today shows both versions and asks which to keep. Drafts stay available under **Resume practice** across days and keep their original question even if the bank is updated.

Open the hint or stored explanation when you need help. Using either before submitting marks the answer as **Assisted practice**. You still receive calendar credit and can receive a correctness assessment. The concept returns for review tomorrow after the assessment is accepted. Reading the explanation after submission preserves the answer’s original classification.

Choose **I don’t know** to open the stored explanation, then acknowledge it after reading to finish the item. This records a **Self-reported gap**, earns calendar credit on the day you acknowledge it, and schedules review for tomorrow. Hints, stored explanations, and this completion path work offline and require no grading request. The SQLite backup includes help use and gap completions.

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

## Test grading with your ChatGPT plan

After signing in, choose a **Grading model** from your account's list. Read the **Reference question** and **Reference answer** in Settings. **Run connection test** sends this fixed example, its marking guide and sources to OpenAI and can use your ChatGPT plan or credits. Opening Settings or selecting a model does not send a grading request.

Settings shows the model's feedback and a local grade from 0 to 3, or its request for clarification. **Model attempts in the last 24 hours** counts this installation's outbound and uncertain attempts. If a model rejects structured output, **Retry in JSON mode** authorizes another attempt. The test leaves your practice history untouched. Live account verification is pending. [Verification record](docs/DML-2-verification.md)

## Clarify or revise a practice answer

When feedback asks for clarification, edit the clarified answer and send it from the saved practice item. After assessment, **Revise answer** lets you submit one assisted revision and compare both answers and their feedback. You can save an edit locally before sending it. Clarification and revision preserve the original answer and completion credit. The eventual original assessment sets the next review date. A revision leaves that schedule in place.

## Dispute an assessment

Use **Dispute assessment** on a saved assessed answer to retain its exact evaluated version, feedback, and your objection. An open dispute removes only that original answer's accepted assessment from review scheduling; other accepted evidence and any earlier **Review sooner** request remain. If this leaves no accepted assessment, the concept shows **Awaiting accepted assessment** and stays available for practice. The original completion remains in the calendar.

**Request reassessment** sends that same frozen answer, marking guide, references, support classification, and role through the normal grading and usage path. It sends the objection only for that explicit reassessment. Compare the two assessments, then accept either one or leave the dispute open. Accepting a reassessment selects it for the original answer's review replay; an assisted-revision dispute never changes the original answer's review schedule. Reassessment failures and uncertain results remain saved and can be explicitly requested again. SQLite backup and restore retain open disputes, comparison feedback, and selected resolutions.

## History

Open **History** to find saved practice by section or practice date. Select an item to inspect its original question, marking guide, sources, answers and feedback, including any clarification, assisted revision or dispute. History shows when the item was completed or assessed and the concept's current review date when available.

**Resume practice** opens the same saved item and its draft. **Recover saved answer** opens the saved request so you can review the available grading action. History and drafts are available before signing in. Requesting grading or reassessment requires a connected account.

## Settings and local data

The default directory is `~/Library/Application Support/Daily ML Practice/`. It contains the SQLite database for preferences, imported question versions, saved recommendations, question snapshots and drafts, plus rotating operational logs, verified `backups/`, and a protected `credentials/` directory. Signing out retains SQLite data and the saved account registration and installation identity.

### Backup and restore

In **Settings**, choose **Back up learning data** to download a verified SQLite copy of the authoritative learning database. It includes editable preferences, imported question versions, saved practice records, answer snapshots, help use and self-reported gap completions. SQLite creates the copy while the app is running, then Daily ML checks database integrity and record relationships before offering the download. A retained copy is also stored under `backups/` in the data directory.

Backups deliberately exclude the protected `credentials/` directory, including ChatGPT access and refresh tokens. Keep downloaded backups somewhere private: learning answers and preferences are personal data.

To restore a backup, first stop every Daily ML Practice launcher using that data directory (press Ctrl+C in its terminal). Then run the stopped-app command; quote any path containing spaces:

```sh
daily-ml restore "/path/to/daily-ml-learning-backup.sqlite3"
daily-ml restore "/path with spaces/daily-ml-learning-backup.sqlite3" --config "/path with spaces/config.toml"
```

Restore validates the selected SQLite file in a temporary sibling file, creates and verifies a `daily-ml-pre-restore-*.sqlite3` copy of the current database, then atomically replaces the live database. Before replacement it settles any SQLite write-ahead log; on a final durability or validation failure it atomically rolls the verified pre-restore copy back. If that rollback itself cannot complete, the command names the retained pre-restore copy and tells you to restore it before reopening the app. For validation, permissions, space, or preparation failures before replacement, the existing live database remains in place. Keep the printed pre-restore copy until you have checked the restored records. If the command says the app is already running, return to its browser tab or stop that launcher before retrying. If a machine interruption leaves a `*.restore-preparing` file in the data directory, do not open it as the live database; restart Daily ML, which continues to use the complete `daily-ml.sqlite3` file.

On the first upgrade to self-reported gaps, the app verifies a private backup in `backups/` before migrating existing practice credits. The migration itself is transactional: if it cannot complete, SQLite rolls it back and the pre-upgrade backup remains available. Check available space and permissions in the data directory before retrying.

Configuration loads packaged defaults, saved SQLite preferences, installation TOML, then `DML_` environment overrides. The launcher resolves the data directory before it opens SQLite. Settings shows whether the timezone is controlled by an override.

Copy [config.example.toml](config.example.toml) to the default directory as `config.toml`, or pass `daily-ml run --config "/path with spaces/config.toml"`. `DML_CONFIG` also selects the TOML. Paths in the TOML can be relative to that file. Use absolute or home-relative paths in environment overrides.

- `DML_DATA_DIR` changes the data directory, and `DML_PORT` changes the port. Port `0` asks the OS for a free port.
- `DML_TIMEZONE` takes an IANA name such as `Europe/London`.
- `DML_GRADE_THRESHOLDS`, `DML_REQUEST_TIMEOUT` (60 seconds by default), and `DML_MODEL_ID` configure grading. `DML_REVIEW_INTERVALS` and `DML_ANSWER_LIMIT` configure learning preferences. Numeric values and arrays use TOML syntax.

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
