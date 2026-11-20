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

# Columns required by each durable table in the currently supported schema.
# The manifest records installed tables, so unknown additive tables from a
# future compatible application release remain permitted. This contract only
# rejects a backup that claims a known table but removed a field current code
# reads or writes.
_REQUIRED_TABLE_COLUMNS = {
    "bank_question_versions": {"question_id", "version", "section_id", "concept_id", "retired", "record_json"},
    "bank_sections": {"id", "domain_id", "title", "position"},
    "concept_review_state": {"concept_id", "stage", "evidence_due_date"},
    "grading_action_keys": {"action_key", "request_id"},
    "grading_requests": {
        "id", "kind", "model_id", "state", "created_at", "finished_at", "retry_of", "schema_mode",
        "prompt_version", "schema_version", "context_json", "feedback_json", "grade", "error",
        "response_id", "usage_json", "latency_ms",
    },
    "model_attempts": {"request_id", "sent_at"},
    "model_rate_limit_retries": {"request_id", "sent_at"},
    "practice_clarification_drafts": {"request_id", "text", "version"},
    "practice_credits": {"question_id", "local_date", "item_id"},
    "practice_days": {"local_date", "timezone", "created_at"},
    "practice_dispute_reassessment_actions": {"action_key", "dispute_id", "request_id"},
    "practice_dispute_reassessments": {"request_id", "dispute_id"},
    "practice_disputes": {
        "id", "item_id", "target_request_id", "target_version_id", "role", "objection", "opened_at",
        "resolved_at", "selected_request_id",
    },
    "practice_gap_actions": {"action_key", "item_id"},
    "practice_gap_completions": {"item_id", "completed_at", "completed_date", "completed_timezone"},
    "practice_grading_versions": {"request_id", "version_id"},
    "practice_help_events": {"item_id", "kind", "viewed_at"},
    "practice_items": {
        "id", "local_date", "timezone", "created_at", "question_id", "question_version",
        "section_id", "section_title", "snapshot_json", "draft_text", "draft_version",
        "last_practised_date",
    },
    "practice_revision_drafts": {"request_id", "text", "version"},
    "practice_revisions": {"item_id", "initial_request_id", "current_request_id"},
    "practice_selections": {"local_date", "slot", "kind", "item_id", "question_id"},
    "practice_submission_support": {"item_id", "supported"},
    "practice_submission_versions": {
        "id", "item_id", "ordinal", "role", "kind", "parent_version_id", "text", "supported", "created_at",
    },
    "practice_submissions": {
        "item_id", "original_text", "completed_at", "completed_date", "completed_timezone", "original_request_id",
        "current_request_id",
    },
    "review_evidence": {
        "id", "original_item_id", "grading_request_id", "concept_id", "submitted_at", "submitted_date", "grade",
        "supported", "accepted_date",
    },
    "review_evidence_selection": {"original_item_id", "evidence_id"},
    "review_sooner_requests": {"idempotency_key", "concept_id", "requested_at", "due_date"},
}

_EDITABLE_PREFERENCE_KEYS = {"timezone", "model_id"}

# Keys and relationships used by current application writes. These supplement
# the durable column contract above: SQLite accepts a table rebuilt without a
# unique key or foreign key, but normal ``ON CONFLICT`` writes may then fail.
_REQUIRED_UNIQUE_KEYS = {
    "bank_sections": (frozenset({"id"}),),
    "bank_question_versions": (frozenset({"question_id", "version"}),),
    "concept_review_state": (frozenset({"concept_id"}),),
    "review_evidence_selection": (frozenset({"original_item_id"}),),
    "model_attempts": (frozenset({"request_id"}),),
    "model_rate_limit_retries": (frozenset({"request_id"}),),
    "practice_clarification_drafts": (frozenset({"request_id"}),),
    "practice_revision_drafts": (frozenset({"request_id"}),),
    "practice_days": (frozenset({"local_date"}),),
    "practice_items": (frozenset({"id"}), frozenset({"id", "question_id"})),
    "practice_selections": (
        frozenset({"local_date", "slot"}),
        frozenset({"local_date", "question_id"}),
    ),
    "practice_credits": (
        frozenset({"question_id", "local_date"}),
        frozenset({"item_id"}),
    ),
}

_REQUIRED_FOREIGN_KEYS = {
    "bank_question_versions": (
        (frozenset({("section_id", "id")}), "bank_sections"),
    ),
    "practice_items": (
        (frozenset({("local_date", "local_date")}), "practice_days"),
        (frozenset({("question_id", "question_id"), ("question_version", "version")}), "bank_question_versions"),
    ),
    "practice_selections": (
        (frozenset({("local_date", "local_date")}), "practice_days"),
        (frozenset({("item_id", "id"), ("question_id", "question_id")}), "practice_items"),
    ),
}

# ``practice_credits`` had one supported earlier layout. Startup migrates it
# transactionally, so a backup remains restorable if it has either identity.
_PRACTICE_CREDITS_FOREIGN_KEY_LAYOUTS = (
    frozenset({
        ("practice_days", frozenset({("local_date", "local_date")})),
        ("practice_items", frozenset({("item_id", "id")})),
        ("practice_items", frozenset({("item_id", "id"), ("question_id", "question_id")})),
    }),
    frozenset({
        ("practice_days", frozenset({("local_date", "local_date")})),
        ("practice_submissions", frozenset({("item_id", "item_id")})),
        ("practice_items", frozenset({("item_id", "id"), ("question_id", "question_id")})),
    }),
)


class PreferenceConflict(RuntimeError):
    """Another writer saved a newer preference version."""


class BackupError(RuntimeError):
    """A backup cannot be created, verified, or safely restored."""


def _unique_keys(connection: sqlite3.Connection, table: str) -> set[frozenset[str]]:
    """Return primary and unique column sets enforced by one SQLite table."""
    keys: set[frozenset[str]] = set()
    primary_key = {
        row[1] for row in connection.execute(f"PRAGMA table_info('{table}')") if row[5] > 0
    }
    if primary_key:
        keys.add(frozenset(primary_key))
    for _, index_name, unique, _, partial in connection.execute(f"PRAGMA index_list('{table}')"):
        if unique and not partial:
            quoted_index_name = index_name.replace("'", "''")
            columns = {
                row[2] for row in connection.execute(f"PRAGMA index_info('{quoted_index_name}')")
            }
            keys.add(frozenset(columns))
    return keys


def _foreign_keys(connection: sqlite3.Connection, table: str) -> set[tuple[str, frozenset[tuple[str, str]]]]:
    """Return enforced foreign-key column mappings grouped by referenced table."""
    grouped: dict[tuple[int, str], set[tuple[str, str]]] = {}
    for key_id, _, referenced_table, source, target, *_ in connection.execute(
        f"PRAGMA foreign_key_list('{table}')"
    ):
        grouped.setdefault((key_id, referenced_table), set()).add((source, target))
    return {(referenced_table, frozenset(columns)) for (_, referenced_table), columns in grouped.items()}


def _validate_practice_snapshots(connection: sqlite3.Connection) -> None:
    """Compare each saved prompt with the bank version from which it was copied."""
    from .bank import parse_question_snapshot

    # Practice tables can be initialized before the first bank import.
    if connection.execute("SELECT 1 FROM practice_items LIMIT 1").fetchone() is None:
        return
    rows = connection.execute(
        """SELECT item.section_id, item.snapshot_json, bank.section_id, bank.record_json
           FROM practice_items AS item
           LEFT JOIN bank_question_versions AS bank
             ON item.question_id = bank.question_id AND item.question_version = bank.version"""
    )
    for section_id, snapshot, bank_section_id, record in rows:
        if section_id != bank_section_id or record is None:
            raise ValueError("practice item does not reference a matching bank version")
        if snapshot != record and parse_question_snapshot(snapshot) != parse_question_snapshot(record):
            raise ValueError("practice snapshot differs from its immutable bank version")


def _validate_table_contracts(connection: sqlite3.Connection, recorded_tables: set[str]) -> None:
    """Require SQL keys and relationships relied on by supported durable layouts."""
    for table, required_keys in _REQUIRED_UNIQUE_KEYS.items():
        if table in recorded_tables and not set(required_keys) <= _unique_keys(connection, table):
            raise ValueError("required unique key is missing")
    for table, requirements in _REQUIRED_FOREIGN_KEYS.items():
        if table in recorded_tables:
            foreign_keys = _foreign_keys(connection, table)
            if any((referenced_table, columns) not in foreign_keys for columns, referenced_table in requirements):
                raise ValueError("required foreign key is missing")
    if "practice_credits" in recorded_tables:
        foreign_keys = frozenset(_foreign_keys(connection, "practice_credits"))
        if foreign_keys not in _PRACTICE_CREDITS_FOREIGN_KEY_LAYOUTS:
            raise ValueError("unsupported practice credit relationship layout")


def validate_editable_preferences(values: dict[str, object]) -> None:
    """Validate the complete persisted editable-preferences record.

    Parameters
    ----------
    values : dict[str, object]
        The JSON object persisted in ``user_preferences``.

    Raises
    ------
    ValueError
        If a preference is unsupported or has a value the settings layer cannot use.
    """
    if values.keys() - _EDITABLE_PREFERENCE_KEYS:
        raise ValueError("unsupported preference key")
    if "timezone" in values:
        timezone = values["timezone"]
        try:
            if not isinstance(timezone, str) or not 0 < len(timezone) <= 128 or "\0" in timezone:
                raise ValueError
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("timezone") from None
    if "model_id" in values:
        model_id = values["model_id"]
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 256:
            raise ValueError("model_id")


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
            # Startup rebuilds these views in its schema transactions. A table
            # or index in the same SQLite namespace would prevent that rebuild.
            if connection.execute("""
                SELECT 1 FROM sqlite_schema WHERE type IN ('table', 'index')
                AND name COLLATE NOCASE IN ('bank_active_questions', 'practice_evaluated_requests')
            """).fetchone():
                raise ValueError("application view name is occupied")
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
                if table in recorded_tables:
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if not required_columns <= columns:
                        raise ValueError
            _validate_table_contracts(connection, recorded_tables)
            preferences = json.loads(row[1])
            validate_editable_preferences(preferences)
            if "bank_question_versions" in recorded_tables:
                from .bank import validate_saved_question_versions

                validate_saved_question_versions(connection)
            if "practice_items" in recorded_tables:
                _validate_practice_snapshots(connection)
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
        rollback = self.path.with_name(f".{self.path.name}.{uuid4().hex}.restore-rollback")
        pre_restore: Path | None = None
        replaced = False
        retain_rollback = False
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
            os.link(self.path, rollback)
            self._fsync(rollback)
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
                rollback_state = self._restore_pre_replace_state(rollback)
                if rollback_state == "verified":
                    raise BackupError(
                        "Restore could not complete after replacing the database; the verified pre-restore "
                        "copy was atomically rolled back and remains in backups."
                    ) from None
                if rollback_state == "restored_unverified":
                    raise BackupError(
                        "Restore could not complete after replacing the database. The original database was "
                        "atomically put back, but its final durability check could not be confirmed; the verified "
                        f"pre-restore copy remains at {pre_restore}. Check disk space before reopening Daily ML."
                    ) from None
                retain_rollback = True
                raise BackupError(
                    "Restore could not complete after replacing the database. The original database remains "
                    f"staged at {rollback}; while Daily ML is stopped, replace {self.path} with that file manually. "
                    f"The verified pre-restore copy is also retained at {pre_restore}."
                ) from None
            if isinstance(error, BackupError):
                raise
            if pre_restore is None:
                raise BackupError(
                    "Could not prepare restore before replacing the live database. "
                    "The existing database was not changed; check disk space and permissions before retrying."
                ) from None
            raise BackupError(
                "Could not complete restore. The verified pre-restore copy is retained in backups; "
                "check disk space and permissions before retrying."
            ) from None
        finally:
            if not retain_rollback:
                try:
                    rollback.unlink(missing_ok=True)
                except OSError:
                    pass

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

    def _restore_pre_replace_state(self, rollback: Path) -> str:
        """Try to atomically return the verified pre-restore data to the live path.

        Returns
        -------
        str
            ``"verified"`` after durable rollback validation,
            ``"restored_unverified"`` after the rename succeeds but its final
            durability check fails, or ``"staged"`` when the original remains
            at ``rollback`` because the rename cannot complete.
        """
        try:
            os.replace(rollback, self.path)
        except OSError:
            return "staged"
        try:
            self._fsync(self.path)
            self.validate_backup(self.path)
            return "verified"
        except (OSError, sqlite3.Error, BackupError):
            return "restored_unverified"

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
