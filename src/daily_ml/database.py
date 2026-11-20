"""SQLite infrastructure and the versioned user preferences record."""

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkstemp
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SQLITE_WAIT_SECONDS = 3
BACKUP_SCHEMA_VERSION = 1

# Columns read by the current Practice service. A manifest records which
# durable tables exist; these checks prevent an older/core-only manifest from
# admitting a truncated database that still names a current practice table.
_REQUIRED_TABLE_COLUMNS = {
    "practice_items": {
        "id", "local_date", "timezone", "created_at", "question_id", "question_version",
        "section_id", "section_title", "snapshot_json", "draft_text", "draft_version",
        "last_practised_date",
    },
}


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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS daily_ml_backup_schema (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version > 0)
                )
            """)
            connection.execute(
                "INSERT OR IGNORE INTO daily_ml_backup_schema VALUES (?, ?)",
                (1, BACKUP_SCHEMA_VERSION),
            )
            connection.execute("""
                CREATE TABLE IF NOT EXISTS daily_ml_backup_tables (
                    table_name TEXT PRIMARY KEY
                )
            """)
            connection.executemany(
                "INSERT OR IGNORE INTO daily_ml_backup_tables VALUES (?)",
                (("user_preferences",), ("daily_ml_backup_schema",), ("daily_ml_backup_tables",)),
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
        required = {"user_preferences", "daily_ml_backup_schema", "daily_ml_backup_tables"}
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not required <= tables:
            raise BackupError("The selected file is not a valid Daily ML backup.")
        try:
            row = connection.execute(
                "SELECT version, settings_json FROM user_preferences WHERE singleton = 1"
            ).fetchone()
            if row is None or not isinstance(row[0], int) or row[0] < 0 or not isinstance(json.loads(row[1]), dict):
                raise ValueError
            marker = connection.execute(
                "SELECT schema_version FROM daily_ml_backup_schema WHERE singleton = 1"
            ).fetchone()
            if marker != (BACKUP_SCHEMA_VERSION,):
                raise ValueError
            recorded_tables = {
                row[0] for row in connection.execute("SELECT table_name FROM daily_ml_backup_tables")
            }
            if not required <= recorded_tables or not recorded_tables <= tables:
                raise ValueError
            for table, required_columns in _REQUIRED_TABLE_COLUMNS.items():
                if table in tables:
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if not required_columns <= columns:
                        raise ValueError
            preferences = json.loads(row[1])
            timezone = preferences.get("timezone")
            if timezone is not None:
                if not isinstance(timezone, str) or not 0 < len(timezone) <= 128 or "\0" in timezone:
                    raise ValueError
                ZoneInfo(timezone)
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError, ZoneInfoNotFoundError):
            raise BackupError("The selected file is not a valid Daily ML backup.") from None

    @staticmethod
    def _online_backup(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
        """Copy a SQLite database while bounding busy/locked retry time."""
        deadline = time.monotonic() + SQLITE_WAIT_SECONDS

        def check_deadline(_: int, __: int, ___: int) -> None:
            if time.monotonic() >= deadline:
                raise BackupError(
                    "SQLite remained busy while creating a backup. Close the other Daily ML process and retry."
                )

        source.backup(target, pages=64, progress=check_deadline, sleep=0.05)

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
        temporary: Path | None = None
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(destination.parent, 0o700)
            self._fsync(destination.parent)
            descriptor, temporary_name = mkstemp(
                prefix=f".{destination.name}.", suffix=".preparing", dir=destination.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            with self.connect() as source, closing(sqlite3.connect(temporary)) as target:
                self._online_backup(source, target)
                target.execute("PRAGMA journal_mode = DELETE")
                self._validate_connection(target)
            os.chmod(temporary, 0o600)
            self._fsync(temporary)
            os.replace(temporary, destination)
            self._fsync(destination)
            return destination
        except (OSError, sqlite3.Error, BackupError) as error:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(error, BackupError):
                raise
            raise BackupError("Could not create a verified backup. Check disk space and permissions.") from None

    def create_backup(self, *, prefix: str = "daily-ml-backup") -> Path:
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
        return self._backup_to(self.backup_dir / f"{prefix}-{timestamp}-{uuid4().hex}.sqlite3")

    @staticmethod
    def record_backup_tables(connection: sqlite3.Connection) -> None:
        """Record the durable tables installed in this database transaction.

        Parameters
        ----------
        connection : sqlite3.Connection
            Active schema-initialization transaction. The recorded manifest lets
            backup validation reject a syntactically valid but truncated export.
        """
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        connection.executemany(
            "INSERT OR IGNORE INTO daily_ml_backup_tables VALUES (?)", tables
        )

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
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
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
        pre_restore: Path | None = None
        replaced = False
        try:
            with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(staging)) as target:
                self._online_backup(source, target)
                target.execute("PRAGMA journal_mode = DELETE")
                self._validate_connection(target)
            os.chmod(staging, 0o600)
            self._fsync(staging)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            pre_restore = self.backup_dir / f"daily-ml-pre-restore-{timestamp}-{uuid4().hex}.sqlite3"
            self._backup_to(pre_restore)
            self._settle_live_sidecars()
            os.replace(staging, self.path)
            replaced = True
            self._fsync(self.path)
            self.validate_backup(self.path)
            return pre_restore
        except (OSError, sqlite3.Error, BackupError) as error:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
            if replaced and pre_restore is not None:
                if self._restore_pre_replace_state(pre_restore):
                    raise BackupError(
                        "Restore could not complete after replacing the database; the verified pre-restore "
                        "copy was atomically rolled back and remains in backups."
                    ) from None
                raise BackupError(
                    "Restore could not complete after replacing the database. The verified pre-restore copy "
                    f"is retained at {pre_restore}; restore it manually before reopening Daily ML."
                ) from None
            if isinstance(error, BackupError):
                raise
            raise BackupError(
                "Could not complete restore. The verified pre-restore copy is retained in backups; "
                "check disk space and permissions before retrying."
            ) from None

    def _settle_live_sidecars(self) -> None:
        """Checkpoint WAL data before an atomic live-file replacement."""
        with closing(sqlite3.connect(self.path, timeout=SQLITE_WAIT_SECONDS)) as connection:
            connection.execute("PRAGMA busy_timeout = 3000")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise BackupError("SQLite is busy; close the running app before restoring a backup.")
            journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
            if journal_mode != ("delete",):
                raise BackupError("SQLite could not safely settle its journal before restore.")

    def _restore_pre_replace_state(self, pre_restore: Path) -> bool:
        """Try to atomically return the verified pre-restore data to the live path."""
        rollback = self.path.with_name(f".{self.path.name}.{uuid4().hex}.restore-rollback")
        try:
            os.link(pre_restore, rollback)
            self._fsync(rollback)
            os.replace(rollback, self.path)
            self._fsync(self.path)
            self.validate_backup(self.path)
            return True
        except (OSError, sqlite3.Error, BackupError):
            return False
        finally:
            try:
                rollback.unlink(missing_ok=True)
            except OSError:
                pass

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
