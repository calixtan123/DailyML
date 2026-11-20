"""Exercise verified local SQLite backup and stopped-app restore behavior."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from daily_ml.bank import import_bank, validate_bank
from daily_ml.database import BackupError, Database
from daily_ml.practice import Practice
from daily_ml import cli


def test_verified_backup_round_trips_preferences_and_learning_records(tmp_path):
    """A backup is a valid standalone SQLite copy of the authoritative data."""
    live = Database(tmp_path / "data with spaces")
    live.save_preferences({"timezone": "UTC"}, expected_version=0)
    with live.connect() as connection:
        connection.execute("CREATE TABLE learning_fixture (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        connection.execute("INSERT INTO learning_fixture VALUES (1, 'saved answer')")

    backup = live.create_backup()

    assert backup.is_file()
    Database.validate_backup(backup)
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT settings_json FROM user_preferences").fetchone() == ('{"timezone": "UTC"}',)
        assert connection.execute("SELECT note FROM learning_fixture").fetchone() == ("saved answer",)


def test_backup_round_trips_populated_current_practice_records(tmp_path):
    """A supported-schema backup reopens current practice records after restore."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    practice = Practice(source, answer_limit=8_000)
    item = practice.today("Europe/London").selections[0].item
    practice.submit(
        item.id, "A saved answer included in the backup.", expected_version=0,
        resolve_settings=lambda _connection: ("Europe/London", None),
    )
    with source.connect() as connection:
        original_payload = connection.execute(
            "SELECT question_id, question_version, snapshot_json FROM practice_items WHERE id = ?", (item.id,)
        ).fetchone()

    restored = Database(tmp_path / "restored")
    restored.restore_backup(source.create_backup())

    reopened = Practice(restored, answer_limit=8_000)
    assert reopened.submission(item.id)["request"]["state"] == "awaiting_access"
    assert restored.get_preferences().values == {}
    with restored.connect() as connection:
        assert connection.execute(
            "SELECT question_id, question_version, snapshot_json FROM practice_items WHERE id = ?", (item.id,)
        ).fetchone() == original_payload


def test_validate_backup_rejects_corrupt_or_newer_database(tmp_path):
    """A damaged or unsupported backup cannot reach the restore path."""
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(corrupt)

    newer = Database(tmp_path / "source")
    backup = newer.create_backup()
    with sqlite3.connect(backup) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(BackupError, match="newer"):
        Database.validate_backup(backup)


def test_validate_backup_rejects_a_truncated_schema_with_valid_preferences(tmp_path):
    """A SQLite file with an intact preferences table is not enough to restore."""
    source = Database(tmp_path / "source")
    backup = source.create_backup()
    with sqlite3.connect(backup) as connection:
        connection.execute("DROP TABLE daily_ml_backup_schema")

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(backup)


def test_validate_backup_rejects_a_missing_registered_learning_table(tmp_path):
    """A backup cannot silently omit a practice table installed by this app."""
    source = Database(tmp_path / "source")
    Practice(source, answer_limit=8_000)
    backup = source.create_backup()
    with sqlite3.connect(backup) as connection:
        connection.execute("DROP TABLE practice_disputes")

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(backup)


def test_validate_backup_rejects_missing_required_practice_column_and_invalid_timezone(tmp_path):
    """A restore candidate must remain usable by Practice and Settings after startup."""
    source = Database(tmp_path / "source")
    Practice(source, answer_limit=8_000)
    backup = source.create_backup()
    with sqlite3.connect(backup) as connection:
        connection.execute("ALTER TABLE practice_items DROP COLUMN last_practised_date")
    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(backup)

    valid = source.create_backup()
    with sqlite3.connect(valid) as connection:
        connection.execute("UPDATE user_preferences SET settings_json = ?", ('{"timezone": "bogus timezone"}',))
    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(valid)


def test_validate_backup_rejects_records_outside_the_supported_application_contract(tmp_path):
    """A restorable backup includes usable current-schema records, not merely valid SQLite."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    practice = Practice(source, answer_limit=8_000)
    practice.today("Europe/London")

    missing_column = source.create_backup()
    with sqlite3.connect(missing_column) as connection:
        connection.execute("ALTER TABLE grading_requests DROP COLUMN latency_ms")
    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(missing_column)

    invalid_preferences = source.create_backup()
    with sqlite3.connect(invalid_preferences) as connection:
        connection.execute("UPDATE user_preferences SET settings_json = ?", ('{"timezone": null}',))
    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid_preferences)

    invalid_snapshot = source.create_backup()
    with sqlite3.connect(invalid_snapshot) as connection:
        connection.execute("DROP TRIGGER practice_snapshot_immutable")
        connection.execute("UPDATE practice_items SET snapshot_json = ?", ("{not json}",))
    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid_snapshot)


def test_validate_backup_rejects_saved_bank_record_that_is_not_a_question(tmp_path):
    """A saved immutable bank version must remain a complete question record."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    invalid = source.create_backup()
    with sqlite3.connect(invalid) as connection:
        connection.execute("UPDATE bank_question_versions SET record_json = ?", ("{not json}",))

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid)


def test_validate_backup_rejects_saved_packaged_version_with_changed_content(tmp_path):
    """A packaged question version cannot silently differ from its saved immutable record."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    invalid = source.create_backup()
    with sqlite3.connect(invalid) as connection:
        question_id, version, record_json = connection.execute(
            "SELECT question_id, version, record_json FROM bank_question_versions LIMIT 1"
        ).fetchone()
        changed = json.loads(record_json)
        changed["title"] = "A different but structurally valid title"
        connection.execute(
            "UPDATE bank_question_versions SET record_json = ? WHERE question_id = ? AND version = ?",
            (json.dumps(changed, separators=(",", ":")), question_id, version),
        )

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid)


def test_restore_preserves_a_valid_historical_bank_version_absent_from_package(tmp_path):
    """Older valid saved versions stay restorable when the current bundle no longer ships them."""
    source = Database(tmp_path / "source")
    bank = validate_bank()
    import_bank(source, bank)
    historical = bank.questions[0].model_copy(update={"id": "retired-historical-question", "retired": True})
    with source.connect() as connection:
        connection.execute(
            """INSERT INTO bank_question_versions
               (question_id, version, section_id, concept_id, retired, record_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                historical.id,
                historical.version,
                historical.section_id,
                historical.concept_id,
                historical.retired,
                historical.model_dump_json(),
            ),
        )
    backup = source.create_backup()
    Database.validate_backup(backup)

    restored = Database(tmp_path / "restored")
    restored.restore_backup(backup)
    assert import_bank(restored, bank).inserted == 0


def test_validate_backup_rejects_practice_snapshot_for_a_different_question(tmp_path):
    """A practice item snapshot must identify the same immutable question version as its row."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    Practice(source, answer_limit=8_000).today("Europe/London")
    invalid = source.create_backup()
    with sqlite3.connect(invalid) as connection:
        connection.execute("DROP TRIGGER practice_snapshot_immutable")
        first, second = connection.execute(
            "SELECT id, snapshot_json FROM practice_items ORDER BY id LIMIT 2"
        ).fetchall()
        connection.execute("UPDATE practice_items SET snapshot_json = ? WHERE id = ?", (second[1], first[0]))

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid)


def test_validate_backup_rejects_practice_credits_without_submission_conflict_key(tmp_path):
    """A restored credit ledger retains the unique key used by normal submission writes."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    practice = Practice(source, answer_limit=8_000)
    item = practice.today("Europe/London").selections[0].item
    practice.submit(
        item.id, "A saved answer.", expected_version=0,
        resolve_settings=lambda _connection: ("Europe/London", None),
    )
    invalid = source.create_backup()
    with sqlite3.connect(invalid) as connection:
        connection.execute("DROP INDEX practice_credits_by_date")
        connection.execute("ALTER TABLE practice_credits RENAME TO old_practice_credits")
        connection.execute(
            """CREATE TABLE practice_credits (
                question_id TEXT NOT NULL,
                local_date TEXT NOT NULL REFERENCES practice_days(local_date),
                item_id TEXT NOT NULL UNIQUE REFERENCES practice_items(id),
                FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id)
            )"""
        )
        connection.execute("INSERT INTO practice_credits SELECT * FROM old_practice_credits")
        connection.execute("DROP TABLE old_practice_credits")

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid)


def test_validate_backup_rejects_partial_credit_conflict_key(tmp_path):
    """A partial index cannot satisfy the unconditional credit upsert conflict target."""
    source = Database(tmp_path / "source")
    import_bank(source, validate_bank())
    Practice(source, answer_limit=8_000).today("Europe/London")
    invalid = source.create_backup()
    with sqlite3.connect(invalid) as connection:
        connection.execute("DROP INDEX practice_credits_by_date")
        connection.execute("ALTER TABLE practice_credits RENAME TO old_practice_credits")
        connection.execute(
            """CREATE TABLE practice_credits (
                question_id TEXT NOT NULL,
                local_date TEXT NOT NULL REFERENCES practice_days(local_date),
                item_id TEXT NOT NULL UNIQUE REFERENCES practice_items(id),
                FOREIGN KEY (item_id, question_id) REFERENCES practice_items(id, question_id)
            )"""
        )
        connection.execute("INSERT INTO practice_credits SELECT * FROM old_practice_credits")
        connection.execute("DROP TABLE old_practice_credits")
        connection.execute(
            """CREATE UNIQUE INDEX practice_credits_partial_question_date
               ON practice_credits(question_id, local_date) WHERE local_date IS NOT NULL"""
        )

    with pytest.raises(BackupError, match="valid Daily ML backup"):
        Database.validate_backup(invalid)


def test_restore_keeps_pre_restore_copy_and_replaces_database_atomically(tmp_path):
    """Stopped-app restore preserves rollback material before replacing live data."""
    data_dir = tmp_path / "Live Data"
    live = Database(data_dir)
    live.save_preferences({"timezone": "Europe/London"}, expected_version=0)
    source = Database(tmp_path / "source")
    source.save_preferences({"timezone": "UTC"}, expected_version=0)
    backup = source.create_backup()

    pre_restore = live.restore_backup(backup)

    assert pre_restore.is_file()
    assert Database(data_dir).get_preferences().values == {"timezone": "UTC"}
    with sqlite3.connect(pre_restore) as connection:
        assert connection.execute("SELECT settings_json FROM user_preferences").fetchone() == ('{"timezone": "Europe/London"}',)


def test_restore_rolls_back_when_final_durability_check_fails(tmp_path, monkeypatch):
    """A failure after replacement restores the pre-restore learning database."""
    live = Database(tmp_path / "live")
    live.save_preferences({"timezone": "Europe/London"}, expected_version=0)
    source = Database(tmp_path / "source")
    source.save_preferences({"timezone": "UTC"}, expected_version=0)
    backup = source.create_backup()
    original_fsync = Database._fsync
    failed_once = False

    def fail_once_after_replace(path):
        nonlocal failed_once
        if path == live.path and not failed_once:
            failed_once = True
            raise OSError("durability failure")
        original_fsync(path)

    monkeypatch.setattr(Database, "_fsync", staticmethod(fail_once_after_replace))

    with pytest.raises(BackupError, match="rolled back"):
        live.restore_backup(backup)

    assert Database(live.path.parent).get_preferences().values == {"timezone": "Europe/London"}
    [pre_restore] = live.backup_dir.glob("daily-ml-pre-restore-*.sqlite3")
    assert live.path.stat().st_ino != pre_restore.stat().st_ino
    restored = Database(live.path.parent)
    restored.save_preferences({"timezone": "UTC"}, expected_version=1)
    with sqlite3.connect(pre_restore) as connection:
        assert connection.execute("SELECT settings_json FROM user_preferences").fetchone() == (
            '{"timezone": "Europe/London"}',
        )


def test_restore_rolls_back_before_retrying_a_persistently_failing_fsync(tmp_path, monkeypatch):
    """A post-publication ENOSPC cannot prevent the atomic rollback rename."""
    live = Database(tmp_path / "live")
    live.save_preferences({"timezone": "Europe/London"}, expected_version=0)
    source = Database(tmp_path / "source")
    source.save_preferences({"timezone": "UTC"}, expected_version=0)
    published = False
    original_replace = os.replace
    original_fsync = Database._fsync

    def record_candidate_publication(source_path, destination_path):
        nonlocal published
        if Path(source_path).name.endswith(".restore-preparing") and Path(destination_path) == live.path:
            published = True
        original_replace(source_path, destination_path)

    def fail_after_publication(path):
        if published and (path == live.path or path.name.endswith(".restore-rollback")):
            raise OSError("no space left")
        original_fsync(path)

    monkeypatch.setattr(os, "replace", record_candidate_publication)
    monkeypatch.setattr(Database, "_fsync", staticmethod(fail_after_publication))

    with pytest.raises(BackupError):
        live.restore_backup(source.create_backup())

    assert Database(live.path.parent).get_preferences().values == {"timezone": "Europe/London"}
    assert list(live.path.parent.glob("*.restore-rollback")) == []
    assert list(live.backup_dir.glob("daily-ml-pre-restore-*.sqlite3"))


def test_restore_preserves_rollback_stage_when_rollback_rename_fails(tmp_path, monkeypatch):
    """A failed rollback rename leaves the original file at the recovery path reported to the user."""
    live = Database(tmp_path / "live")
    live.save_preferences({"timezone": "Europe/London"}, expected_version=0)
    source = Database(tmp_path / "source")
    source.save_preferences({"timezone": "UTC"}, expected_version=0)
    published = False
    rollback: Path | None = None
    original_replace = os.replace
    original_fsync = Database._fsync

    def fail_rollback_rename(source_path, destination_path):
        nonlocal published, rollback
        source = Path(source_path)
        destination = Path(destination_path)
        if source.name.endswith(".restore-preparing") and destination == live.path:
            published = True
        if source.name.endswith(".restore-rollback") and destination == live.path:
            rollback = source
            raise OSError("rollback rename blocked")
        original_replace(source_path, destination_path)

    def fail_final_live_fsync(path):
        if published and path == live.path:
            raise OSError("no space left")
        original_fsync(path)

    monkeypatch.setattr(os, "replace", fail_rollback_rename)
    monkeypatch.setattr(Database, "_fsync", staticmethod(fail_final_live_fsync))

    with pytest.raises(BackupError) as raised:
        live.restore_backup(source.create_backup())

    assert rollback is not None
    assert str(rollback) in str(raised.value)
    assert rollback.is_file()
    with sqlite3.connect(rollback) as connection:
        assert connection.execute("SELECT settings_json FROM user_preferences").fetchone() == (
            '{"timezone": "Europe/London"}',
        )
    assert list(live.backup_dir.glob("daily-ml-pre-restore-*.sqlite3"))


def test_restore_rejects_foreign_key_violation_without_changing_live_data(tmp_path):
    """Validation failure leaves the current authoritative database untouched."""
    live = Database(tmp_path / "live")
    live.save_preferences({"timezone": "UTC"}, expected_version=0)
    invalid = Database(tmp_path / "invalid")
    with invalid.connect() as connection:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("INSERT INTO child VALUES (99)")
    backup = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(invalid.path) as source, sqlite3.connect(backup) as target:
        source.backup(target)

    with pytest.raises(BackupError, match="relationships"):
        live.restore_backup(backup)

    assert live.get_preferences().values == {"timezone": "UTC"}


def test_restore_cli_uses_configured_directory_with_spaces_and_reports_recovery_copy(tmp_path, capsys):
    """The stopped-app command restores the configured installation, not its current directory."""
    data_dir = tmp_path / "Data With Spaces"
    installation = tmp_path / "config with spaces.toml"
    installation.write_text(f'data_dir = "{data_dir}"\n', encoding="utf-8")
    live = Database(data_dir)
    source = Database(tmp_path / "source")
    source.save_preferences({"timezone": "UTC"}, expected_version=0)

    assert cli.main(["restore", str(source.create_backup()), "--config", str(installation)]) == 0

    assert Database(data_dir).get_preferences().values == {"timezone": "UTC"}
    assert "Pre-restore copy" in capsys.readouterr().out


def test_restore_accepts_backup_path_with_uri_special_characters(tmp_path):
    """Restore treats a literal backup filename, not a hand-built SQLite URI."""
    live = Database(tmp_path / "live")
    source = Database(tmp_path / "source")
    source.save_preferences({"timezone": "UTC"}, expected_version=0)
    backup = tmp_path / "backup #1? 100%.sqlite3"
    with sqlite3.connect(source.path) as source_connection, sqlite3.connect(backup) as target_connection:
        source_connection.backup(target_connection)

    live.restore_backup(backup)

    assert live.get_preferences().values == {"timezone": "UTC"}


def test_busy_online_backup_leaves_no_partial_file_and_keeps_app_usable(tmp_path, monkeypatch):
    """A bounded SQLite-busy failure cannot publish a partial backup or damage learning data."""
    live = Database(tmp_path / "live")
    live.save_preferences({"timezone": "UTC"}, expected_version=0)

    def busy_backup(source, target):
        raise BackupError("SQLite remained busy while creating a backup.")

    monkeypatch.setattr(Database, "_online_backup", staticmethod(busy_backup))

    with pytest.raises(BackupError, match="busy"):
        live.create_backup()

    assert not list(live.backup_dir.glob("*.preparing"))
    assert live.get_preferences().values == {"timezone": "UTC"}


def test_restore_preparation_failure_does_not_claim_a_missing_recovery_copy(tmp_path, monkeypatch):
    """A restore error before rollback publication reports that exact recoverable state."""
    live = Database(tmp_path / "live")
    source = Database(tmp_path / "source")
    backup = source.create_backup()

    def no_space(source_connection, target_connection):
        raise OSError("no space left")

    monkeypatch.setattr(Database, "_online_backup", staticmethod(no_space))

    with pytest.raises(BackupError, match="before replacing") as raised:
        live.restore_backup(backup)

    assert "pre-restore copy" not in str(raised.value)
    assert live.get_preferences().values == {}


def test_restore_cli_refuses_to_replace_data_owned_by_running_app(tmp_path, capsys):
    """The restore command cannot race the launcher that owns the installation."""
    data_dir = tmp_path / "live"
    source = Database(tmp_path / "source")
    backup = source.create_backup()

    with cli.own_data_directory(data_dir):
        with pytest.raises(SystemExit) as raised:
            cli.main(["restore", str(backup), "--config", str(_config(tmp_path, data_dir))])

    assert raised.value.code == 1
    assert "already running" in capsys.readouterr().err


def test_startup_ignores_an_interrupted_restore_preparation_file(tmp_path):
    """An incomplete sibling staging file never becomes the live database on restart."""
    live = Database(tmp_path / "live")
    live.save_preferences({"timezone": "UTC"}, expected_version=0)
    staging = live.path.with_name(f".{live.path.name}.interrupted.restore-preparing")
    staging.write_bytes(b"incomplete")

    restarted = Database(live.path.parent)

    assert restarted.get_preferences().values == {"timezone": "UTC"}


def _config(root: Path, data_dir: Path) -> Path:
    """Write a minimal installation configuration for CLI tests."""
    config = root / "installation.toml"
    config.write_text(f'data_dir = "{data_dir}"\n', encoding="utf-8")
    return config
