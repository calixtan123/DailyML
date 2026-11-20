# DML-12 Task 1: Verified backup and stopped-app restore

## Git alignment

- Initial implementation commit before rebasing: `4835d1b9fb98d9313839ca676a478f002ee861fe`.
- DML-10 integration base: `f41211b61382fa8953bfff351879543f8523ec43`.
- Current rebased hardening commit: `86f6fb6378b9253b1baaf8670687894dada3fa82` (this report is amended after final checks).
- DML-10 adds history projections rather than new persistent tables; its history reads from the practice records that the backup manifest preserves.

## Design decisions

- Extended `Database`, the owner of the authoritative SQLite file, rather than adding a second backup service. SQLite's online backup API is the copy mechanism; plain file copying was rejected because it can omit a live WAL snapshot.
- Added a durable table manifest (`daily_ml_backup_tables`). `Practice` records every table installed in its schema transaction, so validation rejects a valid SQLite file whose installed learning table was later removed. A base installation records only its own core tables, which keeps pre-practice migration backups valid.
- Before a replacement, restore creates and verifies a retained rollback copy, checkpoints WAL with `wal_checkpoint(TRUNCATE)`, and changes the old database to `journal_mode=DELETE`. Direct sidecar deletion was rejected because a failed replacement could otherwise discard committed WAL data.
- Replacement remains same-filesystem `os.replace`. If final fsync/validation fails after replacement, restore atomically re-installs the retained pre-restore copy. If that recovery itself fails, the error does not claim the old database is current and names the retained recovery copy.
- SQLite backup retry is bounded by the existing three-second SQLite wait policy using the online-backup progress callback. A busy failure removes preparation artifacts and leaves the live database usable.

## Behaviours completed

- Verified backup preserves authoritative SQLite records and editable preferences.
- Validation rejects corrupt, newer-version, foreign-key-invalid, marker-truncated, and manifest-truncated backups.
- Restore supports spaces and literal URI-significant filename characters (`#`, `?`, `%`).
- Restore preserves a pre-restore database, is blocked by the existing running-app ownership lock, and ignores interrupted sibling preparation files on startup.
- A post-replacement durability failure rolls the live installation back to its original data.
- Settings download/API/frontend, CLI restore, migration backup reuse, and README guidance were implemented in the preceding committed slice.

## Files changed in this hardening slice

- `src/daily_ml/database.py`
- `src/daily_ml/practice.py`
- `tests/test_backup.py`

The earlier DML-12 commit also changed CLI/API/frontend/documentation files. No real learner or provider data was accessed.

## Tests added

- Reject missing backup schema marker.
- Reject an omitted registered practice table.
- Roll back the original database after final fsync failure following replacement.
- Restore backups whose literal paths contain SQLite URI-significant characters.
- Ensure a simulated bounded SQLite-busy backup leaves no preparation file and the installation usable.

## Verification evidence

Passed:

```sh
PYTHONPATH=src /tmp/dailyml-20261120-ticket-python/bin/python -m pytest tests/test_backup.py -q
# 12 passed

npm --prefix frontend test -- --run src/BackupSettings.test.tsx src/App.test.tsx
# 2 test files passed, 6 tests passed

npm --prefix frontend run typecheck
# exit 0
```

Focused surrounding Python verification was also run:

```sh
PYTHONPATH=src /tmp/dailyml-20261120-ticket-python/bin/python -m pytest tests/test_backup.py tests/test_practice.py tests/test_submissions.py tests/test_local_app.py tests/test_config.py -q
# 91 passed, 2 failed after rebasing on DML-10
```

The two failures reproduce outside the backup behaviour in this sandbox:

- `test_shell_assets_and_protected_connection_flow`: sign-in start returned HTTP 409 where the test expects 200.
- `test_launcher_binds_loopback_free_port_and_reports_port_conflict`: sandbox refuses `bind_loopback(0)`.

## Remaining risks

- The SQLite progress callback is a bounded retry mechanism, but the test simulates its busy error rather than holding a real cross-process SQLite lock for three seconds.
- The manifest only tracks tables after current application initializers run. Future persistent-table tickets must continue to invoke `record_backup_tables` within their schema transaction (or extend the fixture) so their tables become part of the durable backup contract.
- The frontend test emits jsdom's known “Not implemented: navigation to another Document” diagnostic during download simulation; the test assertions pass.
