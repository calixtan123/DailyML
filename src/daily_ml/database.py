"""SQLite infrastructure and the versioned user preferences record."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

SQLITE_WAIT_SECONDS = 3


class PreferenceConflict(RuntimeError):
    """Another writer saved a newer preference version."""


@dataclass(frozen=True)
class Preferences:
    """A snapshot of the editable preferences and their version."""

    version: int
    values: dict[str, object]


class Database:
    """Open short-lived connections to a single installation's database."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "daily-ml.sqlite3"
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
