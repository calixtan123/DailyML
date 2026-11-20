"""Exercise verified local SQLite backup and stopped-app restore behavior."""

import sqlite3
from pathlib import Path

import pytest

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
