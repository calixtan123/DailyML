# DML-1 implementation plan

## Authority

Jira DML-1: https://synapse21.atlassian.net/browse/DML-1
Approved specification and design are in the ML Learning workspace. Only Task 1 is authorized for this ticket.

## Global constraints

- Practice is self-paced and untimed. The daily goal is two distinct completed questions and further questions are available on demand.
- Save an original submission before grading and credit its completion date immediately. Pending or disputed assessment retains that credit. A self-reported gap counts after its explanation is viewed. Drafts, retries, clarifications, revisions, and reassessments add no credit.
- Preserve editable unfinished answers across days. Recommendation dates and completion dates are separate saved facts. Resumed and focused practice contribute to the actual completion day's goal.
- Select one random due review when available and one varied question from another section when possible. Without a due review, use varied selection twice. Save recommendations before displaying them.
- Use brief general-purpose examples. The learner explicitly excluded WiFi/Bluetooth and workplace-specific crowd analytics themes.
- Validate version-controlled JSON content with stable IDs, immutable historical snapshots, at most eight positive rubric criteria, and weights summing to one within 1e-6.
- Stages are 0–4 and independent-success intervals are [1, 3, 7, 14, 30] days. Grade 2 resets to stage 0 and returns in three days. Grade 0/1, viewed self-reported gaps, and pre-answer help reset to stage 0 and return tomorrow.
- Criterion values are met=1, partial=0.5, and not_met=0. Coverage boundaries are 0.25, 0.60, and 0.85. A rubric-defined critical misconception caps the calculated grade at 1.
- Default answer capacity is 8,000 Unicode code points and the model-request deadline is 60 seconds. The author proposed these defaults and the learner approved the complete design in Q20.
- ChatGPT plan controls govern model availability. Record each outbound model attempt and display the app's rolling 24-hour count. Local counts include connection tests, retries, clarification, revision, reassessment, and live evaluation.
- Use the documented plan-authorized connection, actual account models, store=false, stream=true, and array input. Accept feedback only after response.completed and complete local validation. Recheck preview restrictions during implementation.
- Bind to 127.0.0.1 with one backend owner per data directory and one server worker. Protect Host, Origin, and mutations with per-instance same-origin request protection.
- Default data directory is ~/Library/Application Support/Daily ML Practice/. Resolve paths programmatically, including paths containing spaces.
- Configuration precedence is packaged defaults, saved SQLite preferences, installation TOML overrides, then environment overrides. Resolve the data-directory location from installation configuration before loading its SQLite preferences. Editable preferences and learning data share one authoritative backup file.
- Default timezone is Europe/London. Record UTC timestamps and immutable local dates/timezones for recommendations and completions. Changes to the configured zone affect later completion events.
- Keep credentials separate from learning data and backups, with directory mode 0700, file mode 0600, atomic replacement, and a cross-process refresh/sign-out lock.
- Use Python docstrings, specific recoverable exceptions, redacted rotating logs, parameterised SQL, foreign keys, bounded lock waits, and short transactions.
- Each new Python function/class receives happy-path and edge/error tests. Explain Arrange / Act / Assert when tests are first introduced. Default tests use temporary data, mocked providers, controlled clocks, and injected randomness.
- Preserve concurrent edits and stop temporary processes started for verification. Record real-account, clean-Mac, editorial-source, and backup/restore evidence for release.


### Task 1 — Runnable local app with protected ChatGPT sign-in

**Files**

Create the launcher, config, database, initial preferences schema, app, connection, credentials and minimal React Settings files. Add README, example TOML, locks and secret exclusions. Add tests/test_config.py, test_local_app.py, test_connection.py, and test_credentials.py.

**Interfaces**

Produce load_settings, save_user_settings, create_app and Database from the shared contracts. connection.start_sign_in() returns a safe SignInStart. get_status() returns ConnectionStatus. get_access_token() remains Python-only. sign_out() removes credentials. Expose POST /api/connection/sign-in, GET /api/connection, and POST /api/connection/sign-out.

- [ ] Write failing tests for configuration precedence, invalid zones/thresholds, preference version conflicts, path resolution outside the project, loopback binding, exclusive instance ownership, port conflicts, and rejected Host/Origin/CSRF.
- [ ] Write mocked OAuth tests covering state, nonce, signature, issuer, audience, expiry, identity/client mismatch, missing plan scope, cancellation and callback timeout. Assert token-file mode 0600, directory mode 0700, one successful concurrent refresh rotation, and sign-out preventing a blocked refresh from recreating credentials.
- [ ] Run uv run pytest tests/test_config.py tests/test_local_app.py tests/test_connection.py tests/test_credentials.py -q and confirm failures identify missing behaviour.
- [ ] Implement documented dynamic registration, stable installation host ID, retained client ID, temporary loopback callback and maintained OIDC/JWT validation. Validate replacement credentials before replacing a working connection. Serialize refresh and sign-out under the same cross-process lock.
- [ ] Implement the launcher, minimal connection controls and compiled-asset serving. Define npm typecheck, build, test and test:e2e scripts. The launcher opens a free loopback port, supports run --no-browser, and stops cleanly.
- [ ] Repeat targeted pytest, npm --prefix frontend run typecheck, and npm --prefix frontend run build. Launch the installed command from outside the repository and stop it. Record the actual results and commit this slice.
