"""Exercise verified local SQLite backup and stopped-app restore behavior."""

import sqlite3
from pathlib import Path

import pytest

from daily_ml.database import BackupError, Database
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


def _config(root: Path, data_dir: Path) -> Path:
    """Write a minimal installation configuration for CLI tests."""
    config = root / "installation.toml"
    config.write_text(f'data_dir = "{data_dir}"\n', encoding="utf-8")
    return config
