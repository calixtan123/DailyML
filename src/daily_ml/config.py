"""Resolve installation paths and load validated settings in precedence order."""

import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database, Preferences, validate_editable_preferences

EDITABLE = {"timezone", "model_id"}
TEXT_KEYS = {"data_dir", "timezone", "model_id"}
MODEL_ID_PATTERN = r"^[A-Za-z0-9._:/-]+$"
MODEL_ID_MAX_LENGTH = 256
OVERRIDE_SOURCES = ("installation", "environment")
MAX_REVIEW_INTERVAL_DAYS = (date.max - date.min).days


class ConfigError(ValueError):
    """Configuration contains an invalid or unsupported setting."""


@dataclass(frozen=True)
class Settings:
    """Effective settings together with preferences and override provenance."""

    data_dir: Path
    timezone: str
    review_intervals: tuple[int, ...]
    grade_thresholds: tuple[float, ...]
    answer_limit: int
    request_timeout: float
    port: int
    model_id: str | None
    preferences: Preferences
    sources: dict[str, str]
    default_timezone: str

    def effective_timezone(self, preferences: Preferences) -> tuple[str, str]:
        """Resolve the learning timezone using current editable preferences.

        Parameters
        ----------
        preferences : Preferences
            Snapshot from the caller's read or write transaction.

        Returns
        -------
        tuple of str
            Effective timezone and its configuration source.
        """
        source = self.sources["timezone"]
        if source in OVERRIDE_SOURCES:
            return self.timezone, source
        timezone = preferences.values.get("timezone")
        return (str(timezone), "preferences") if timezone else (self.default_timezone, "default")

    def effective_model(self, preferences: Preferences) -> tuple[str | None, str]:
        """Resolve the grading model using the caller's latest saved preferences.

        Parameters
        ----------
        preferences : Preferences
            Snapshot from the caller's read or write transaction.

        Returns
        -------
        tuple
            The selected model ID, or None, and its configuration source.
        """
        source = self.sources["model_id"]
        if source in OVERRIDE_SOURCES:
            return self.model_id, source
        model = preferences.values.get("model_id")
        return (str(model), "preferences") if model else (None, "default")


@dataclass(frozen=True)
class Installation:
    """Resolved data directory and settings layers preceding database access."""

    data_dir: Path
    defaults: dict[str, object]
    overrides: dict[str, object]
    environment: dict[str, object]


def _error(key: str) -> ConfigError:
    """Report a key without echoing an untrusted setting value."""
    return ConfigError(f"Invalid {key}. Check your Daily ML configuration.")


def _read_toml(path: Path) -> dict[str, object]:
    """Read a TOML file while keeping parse input out of errors."""
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError("Could not read Daily ML configuration. Check the TOML file.") from None


def _resolve_path(value: object, *, key: str, base: Path | None = None) -> Path:
    """Resolve a home-relative, absolute, or explicitly based path."""
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise _error(key)
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            if base is None:
                raise _error(key)
            path = base / path
        return path.resolve()
    except (OSError, RuntimeError):
        raise _error(key) from None


def _validate(values: dict[str, object], allowed: set[str]) -> None:
    """Validate one settings layer independently of higher-priority overrides."""
    if allowed == EDITABLE:
        try:
            validate_editable_preferences(values)
        except ValueError as error:
            if str(error) == "unsupported preference key":
                raise ConfigError("Unsupported Daily ML configuration key. See config.example.toml.") from None
            raise _error(str(error)) from None
        return
    if values.keys() - allowed:
        raise ConfigError("Unsupported Daily ML configuration key. See config.example.toml.")
    if "timezone" in values:
        zone = values["timezone"]
        try:
            if not isinstance(zone, str) or not 0 < len(zone) <= 128 or "\0" in zone:
                raise ValueError
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise _error("timezone") from None
    if "model_id" in values:
        model = values["model_id"]
        if (not isinstance(model, str) or len(model) > MODEL_ID_MAX_LENGTH
                or not re.fullmatch(MODEL_ID_PATTERN, model)):
            raise _error("model_id")
    for key, minimum, maximum in (("answer_limit", 1, None), ("port", 0, 65535)):
        if key in values:
            value = values[key]
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                raise _error(key)
    if "request_timeout" in values:
        timeout = values["request_timeout"]
        if type(timeout) not in (int, float) or not 0 < timeout < math.inf:
            raise _error("request_timeout")
    if "grade_thresholds" in values:
        thresholds = values["grade_thresholds"]
        if (
            not isinstance(thresholds, list)
            or len(thresholds) != 3
            or any(type(value) not in (int, float) for value in thresholds)
            or not 0 < thresholds[0] < thresholds[1] < thresholds[2] < 1
        ):
            raise _error("grade_thresholds")
    if "review_intervals" in values:
        intervals = values["review_intervals"]
        if (
            not isinstance(intervals, list)
            or len(intervals) != 5
            or any(type(value) is not int or not 0 < value <= MAX_REVIEW_INTERVAL_DAYS for value in intervals)
            or sorted(set(intervals)) != intervals
        ):
            raise _error("review_intervals")


def bundled_defaults() -> dict[str, object]:
    """Read the defaults shipped with the installed Python package.

    Returns
    -------
    dict
        Default settings before installation and preference overrides.
    """
    return _read_toml(Path(__file__).with_name("defaults.toml"))


def resolve_installation(
    config_path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Installation:
    """Resolve the installation before opening its preferences database.

    Parameters
    ----------
    config_path : Path or None
        Explicit installation TOML, otherwise use DML_CONFIG or the default.
    environ : Mapping or None
        Environment overrides. Defaults to the process environment.

    Returns
    -------
    Installation
        Validated settings layers and the absolute data directory.

    Raises
    ------
    ConfigError
        If an installation or environment setting is invalid.
    """
    environ = os.environ if environ is None else environ
    defaults = bundled_defaults()
    allowed = set(defaults) | EDITABLE
    configured_path = environ.get("DML_CONFIG")
    explicit_path = config_path is not None or configured_path is not None
    if config_path is not None:
        path = _resolve_path(str(config_path), key="config", base=Path.cwd())
    elif configured_path is not None:
        path = _resolve_path(configured_path, key="config")
    else:
        path = Path.home() / "Library/Application Support/Daily ML Practice/config.toml"
    try:
        overrides = _read_toml(path)
    except FileNotFoundError:
        if explicit_path:
            raise ConfigError("Installation TOML is missing. Check DML_CONFIG or --config.") from None
        overrides = {}

    environment: dict[str, object] = {}
    for key in allowed:
        value = environ.get(f"DML_{key.upper()}")
        if value is not None:
            try:
                if key in TEXT_KEYS:
                    environment[key] = value
                else:
                    parsed = tomllib.loads(f"value = {value}")
                    if parsed.keys() != {"value"}:
                        raise _error(key)
                    environment[key] = parsed["value"]
            except tomllib.TOMLDecodeError:
                raise _error(key) from None

    _validate(overrides, allowed)
    _validate(environment, allowed)
    data_dir = _resolve_path(defaults["data_dir"], key="data_dir")
    if "data_dir" in overrides:
        data_dir = _resolve_path(overrides["data_dir"], key="data_dir", base=path.parent)
    if "data_dir" in environment:
        data_dir = _resolve_path(environment["data_dir"], key="data_dir")
    return Installation(data_dir, defaults, overrides, environment)


def load_settings(
    config_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    installation: Installation | None = None,
) -> Settings:
    """Load defaults, preferences, installation settings and environment.

    Parameters
    ----------
    config_path : Path or None
        Optional explicit installation TOML.
    environ : Mapping or None
        Optional environment mapping for overrides.
    installation : Installation or None
        Previously resolved installation, used by the launcher after taking ownership.

    Returns
    -------
    Settings
        Effective settings and their provenance.

    Raises
    ------
    ConfigError
        If any layer contains an invalid setting.
    """
    resolved = installation or resolve_installation(config_path, environ=environ)
    try:
        preferences = Database(resolved.data_dir).get_preferences()
    except json.JSONDecodeError:
        raise ConfigError("Saved preferences are unreadable. Check your local database.") from None
    if not isinstance(preferences.values, dict):
        raise ConfigError("Saved preferences are unreadable. Check your local database.")
    _validate(preferences.values, EDITABLE)
    merged: dict = {"model_id": None}
    sources = {"model_id": "default"}
    for source, values in (
        ("default", resolved.defaults),
        ("preferences", preferences.values),
        ("installation", resolved.overrides),
        ("environment", resolved.environment),
    ):
        merged.update(values)
        sources.update(dict.fromkeys(values, source))
    merged.update(
        data_dir=resolved.data_dir,
        grade_thresholds=tuple(merged["grade_thresholds"]),
        review_intervals=tuple(merged["review_intervals"]),
    )
    return Settings(
        **merged, preferences=preferences, sources=sources, default_timezone=resolved.defaults["timezone"]
    )


def save_user_settings(
    settings: Settings, values: dict[str, object], *, expected_version: int
) -> Preferences:
    """Validate and conditionally save editable preferences.

    Parameters
    ----------
    settings : Settings
        Settings for the installation receiving the change.
    values : dict
        Complete editable preference snapshot.
    expected_version : int
        Version the caller originally read.

    Returns
    -------
    Preferences
        Committed preference snapshot.

    Raises
    ------
    ConfigError
        If the edited values are invalid.
    PreferenceConflict
        If the supplied version is stale.
    """
    _validate(values, EDITABLE)
    return Database(settings.data_dir).save_preferences(values, expected_version=expected_version)
