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
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, committing on success and rolling back on error.

        Yields
        ------
        sqlite3.Connection
            A connection with foreign-key enforcement and a bounded busy wait.
        """
        with closing(sqlite3.connect(self.path, timeout=SQLITE_WAIT_SECONDS)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
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
