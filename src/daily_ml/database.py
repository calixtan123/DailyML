"""SQLite infrastructure and the versioned user preferences record."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkstemp
from uuid import uuid4

SQLITE_WAIT_SECONDS = 3
BACKUP_SCHEMA_VERSION = 1


class PreferenceConflict(RuntimeError):
    """Another writer saved a newer preference version."""


class BackupError(RuntimeError):
    """A backup cannot be created, verified, or safely restored."""


@dataclass(frozen=True)
class Preferences:
    """A snapshot of the editable preferences and their version."""

    version: int
    values: dict[str, object]


class Database:
    """Open short-lived connections to a single installation's database."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "daily-ml.sqlite3"
        self.backup_dir = data_dir / "backups"
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL CHECK (version >= 0),
                    settings_json TEXT NOT NULL
                )
            """)
            connection.execute(
                "INSERT OR IGNORE INTO user_preferences VALUES (?, ?, ?)", (1, 0, "{}")
            )
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.execute(f"PRAGMA user_version = {BACKUP_SCHEMA_VERSION}")

    @staticmethod
    def _validate_connection(connection: sqlite3.Connection) -> None:
        """Raise BackupError unless a connection is a compatible, complete database."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > BACKUP_SCHEMA_VERSION:
            raise BackupError(
                "This backup was made by a newer Daily ML Practice version. "
                "Update Daily ML Practice before restoring it."
            )
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("The selected file is not a valid Daily ML backup (integrity check failed).")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("The selected backup has invalid record relationships and was not restored.")
        preferences = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'user_preferences'"
        ).fetchone()
        if preferences is None:
            raise BackupError("The selected file is not a valid Daily ML backup.")
        try:
            row = connection.execute(
                "SELECT version, settings_json FROM user_preferences WHERE singleton = 1"
            ).fetchone()
            if row is None or not isinstance(row[0], int) or row[0] < 0 or not isinstance(json.loads(row[1]), dict):
                raise ValueError
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            raise BackupError("The selected file is not a valid Daily ML backup.") from None

    @staticmethod
    def _fsync(path: Path) -> None:
        """Flush a regular file and its containing directory to durable storage."""
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _backup_to(self, destination: Path) -> Path:
        """Make, validate, and durably publish an online SQLite backup."""
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        descriptor, temporary_name = mkstemp(
            prefix=f".{destination.name}.", suffix=".preparing", dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self.connect() as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                self._validate_connection(target)
            os.chmod(temporary, 0o600)
            self._fsync(temporary)
            os.replace(temporary, destination)
            self._fsync(destination)
            return destination
        except (OSError, sqlite3.Error, BackupError) as error:
            temporary.unlink(missing_ok=True)
            if isinstance(error, BackupError):
                raise
            raise BackupError("Could not create a verified backup. Check disk space and permissions.") from None

    def create_backup(self) -> Path:
        """Create a verified, private online backup of all learning data.

        Returns
        -------
        pathlib.Path
            Durable SQLite backup inside the app-owned ``backups`` directory.

        Raises
        ------
        BackupError
            If SQLite cannot create or verify the backup.
        """
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self._backup_to(self.backup_dir / f"daily-ml-backup-{timestamp}-{uuid4().hex}.sqlite3")

    @classmethod
    def validate_backup(cls, path: Path) -> None:
        """Validate a backup without changing the active database.

        Parameters
        ----------
        path : pathlib.Path
            Candidate SQLite backup selected for restore.

        Raises
        ------
        BackupError
            If the candidate is missing, corrupt, incomplete, or incompatible.
        """
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
                cls._validate_connection(connection)
        except (OSError, sqlite3.Error, BackupError) as error:
            if isinstance(error, BackupError):
                raise
            raise BackupError("The selected file is not a valid Daily ML backup.") from None

    def restore_backup(self, backup: Path) -> Path:
        """Validate and atomically replace this stopped installation's database.

        Parameters
        ----------
        backup : pathlib.Path
            Candidate SQLite backup selected by the stopped-app CLI.

        Returns
        -------
        pathlib.Path
            The verified pre-restore backup retained for manual rollback.

        Raises
        ------
        BackupError
            If validation, pre-restore protection, replacement, or final validation fails.
        """
        self.validate_backup(backup)
        staging = self.path.with_name(f".{self.path.name}.{uuid4().hex}.restore-preparing")
        try:
            with closing(sqlite3.connect(f"file:{backup}?mode=ro", uri=True)) as source, closing(sqlite3.connect(staging)) as target:
                source.backup(target)
                self._validate_connection(target)
            os.chmod(staging, 0o600)
            self._fsync(staging)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            pre_restore = self.backup_dir / f"daily-ml-pre-restore-{timestamp}-{uuid4().hex}.sqlite3"
            self._backup_to(pre_restore)
            os.replace(staging, self.path)
            self._fsync(self.path)
            self.validate_backup(self.path)
            return pre_restore
        except (OSError, sqlite3.Error, BackupError) as error:
            staging.unlink(missing_ok=True)
            if isinstance(error, BackupError):
                raise
            raise BackupError(
                "Could not restore the backup. Your existing database was kept; check disk space and permissions."
            ) from None

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a connection, committing on success and rolling back on error.

        Parameters
        ----------
        immediate : bool
            Reserve the write transaction before read-then-write operations.

        Yields
        ------
        sqlite3.Connection
            A connection with foreign-key enforcement and a bounded busy wait.
        """
        with closing(sqlite3.connect(self.path, timeout=SQLITE_WAIT_SECONDS)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                if immediate:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection

    def get_preferences(self) -> Preferences:
        """Read the authoritative preference snapshot.

        Returns
        -------
        Preferences
            Current preference values and version.
        """
        with self.connect() as connection:
            return self.read_preferences(connection)

    @staticmethod
    def read_preferences(connection: sqlite3.Connection) -> Preferences:
        """Read saved settings as part of an existing transaction.

        Parameters
        ----------
        connection : sqlite3.Connection
            Active transaction used by the caller.

        Returns
        -------
        Preferences
            Preference snapshot from the same transaction.
        """
        version, settings_json = connection.execute(
            "SELECT version, settings_json FROM user_preferences WHERE singleton = ?", (1,)
        ).fetchone()
        return Preferences(version, json.loads(settings_json))

    def save_preferences(
        self, values: dict[str, object], *, expected_version: int
    ) -> Preferences:
        """Replace the preference snapshot if its version still matches.

        Parameters
        ----------
        values : dict
            Validated settings to persist.
        expected_version : int
            Version last seen by the caller.

        Returns
        -------
        Preferences
            Committed snapshot.

        Raises
        ------
        PreferenceConflict
            If another writer updated the record.
        """
        with self.connect() as connection:
            result = connection.execute(
                """UPDATE user_preferences SET settings_json = ?, version = version + 1
                   WHERE singleton = ? AND version = ?""",
                (json.dumps(values), 1, expected_version),
            )
            if result.rowcount != 1:
                raise PreferenceConflict("Settings changed. Reload them before saving.")
        return Preferences(expected_version + 1, values)

    def initialize_grading(self) -> None:
        """Create the additive request ledger without modifying existing preferences.

        Raises
        ------
        sqlite3.Error
            If any table cannot be created, the transaction rolls back.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS grading_requests (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, model_id TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL, finished_at REAL,
                    retry_of TEXT UNIQUE REFERENCES grading_requests(id),
                    schema_mode TEXT NOT NULL, prompt_version TEXT NOT NULL, schema_version TEXT NOT NULL,
                    context_json TEXT NOT NULL, feedback_json TEXT, grade INTEGER CHECK(grade BETWEEN 0 AND 3),
                    error TEXT, response_id TEXT, usage_json TEXT, latency_ms INTEGER
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS grading_action_keys (
                    action_key TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES grading_requests(id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS model_attempts (
                    request_id TEXT PRIMARY KEY REFERENCES grading_requests(id), sent_at REAL NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS model_attempts_by_time ON model_attempts(sent_at)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS model_rate_limit_retries (
                    request_id TEXT PRIMARY KEY REFERENCES model_attempts(request_id), sent_at REAL NOT NULL
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS model_rate_limit_retries_by_time ON model_rate_limit_retries(sent_at)
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS grading_context_immutable
                BEFORE UPDATE OF context_json, kind, retry_of ON grading_requests
                BEGIN
                    SELECT RAISE(ABORT, 'The grading request context and lineage are immutable');
                END
            """)
