"""Exercise settings precedence, validation and conditional preference saves."""

from pathlib import Path

import pytest

from daily_ml.config import ConfigError, load_settings, save_user_settings
from daily_ml.database import PreferenceConflict


def test_packaged_defaults_resolve_from_home_outside_checkout(tmp_path, monkeypatch):
    """The database path and approved defaults do not depend on the working directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    settings = load_settings(environ={})
    assert settings.data_dir == tmp_path / "Library/Application Support/Daily ML Practice"
    assert settings.timezone == "Europe/London"
    assert settings.grade_thresholds == (0.25, 0.60, 0.85)
    assert settings.review_intervals == (1, 3, 7, 14, 30)
    assert settings.answer_limit == 8000
    assert settings.request_timeout == 60


def test_preferences_load_from_resolved_data_directory(tmp_path, monkeypatch):
    """Read preferences only after resolving the TOML and environment data directory."""
    installation = tmp_path / "install config.toml"
    installation.write_text('data_dir = "Local Data"\ntimezone = "Asia/Tokyo"\n')
    installed_data = tmp_path / "Local Data"
    alternate_data = tmp_path / "Alternate Data"
    initial = load_settings(environ={"DML_DATA_DIR": str(alternate_data)})
    save_user_settings(initial, {"timezone": "America/New_York"}, expected_version=0)
    monkeypatch.chdir(Path("/private/tmp") if Path("/private/tmp").exists() else tmp_path)

    toml = load_settings(installation, environ={})
    assert toml.data_dir == installed_data
    assert toml.timezone == "Asia/Tokyo"
    assert toml.sources["timezone"] == "installation"
    effective = load_settings(
        installation, environ={"DML_DATA_DIR": str(alternate_data), "DML_TIMEZONE": "UTC"}
    )
    assert effective.data_dir == alternate_data
    assert effective.preferences.version == 1
    assert effective.preferences.values == {"timezone": "America/New_York"}
    assert effective.timezone == "UTC"
    assert effective.sources["timezone"] == "environment"
    saved = load_settings(environ={"DML_DATA_DIR": str(alternate_data)})
    assert saved.timezone == "America/New_York"
    assert saved.sources["timezone"] == "preferences"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DML_TIMEZONE", "Invalid/Zone"),
        ("DML_GRADE_THRESHOLDS", "[0.6, 0.25, 0.85]"),
        ("DML_GRADE_THRESHOLDS", "[0.25, 0.60, 1.0]"),
        ("DML_REVIEW_INTERVALS", "[1, 3, 3, 14, 30]"),
        ("DML_REVIEW_INTERVALS", "[1, 3, 7, 14, 3652059]"),
        ("DML_ANSWER_LIMIT", "0"),
        ("DML_REQUEST_TIMEOUT", "inf"),
        ("DML_PORT", "65536"),
        ("DML_PORT", "0\nunknown = 1"),
        ("DML_DATA_DIR", "relative"),
    ],
)
def test_invalid_configuration_has_actionable_key(tmp_path, variable, value):
    """Report the invalid setting without exposing its value."""
    with pytest.raises(ConfigError, match=variable.lower().removeprefix("dml_")):
        load_settings(environ={"DML_DATA_DIR": str(tmp_path), variable: value})


def test_preference_conflict_preserves_first_saved_value(tmp_path):
    """Two tabs cannot overwrite each other's settings with a stale version."""
    settings = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    assert save_user_settings(settings, {"timezone": "UTC"}, expected_version=0).version == 1
    with pytest.raises(PreferenceConflict):
        save_user_settings(settings, {"timezone": "Asia/Tokyo"}, expected_version=0)
    with pytest.raises(ConfigError, match="timezone"):
        save_user_settings(settings, {"timezone": "Invalid/Zone"}, expected_version=1)
    latest = load_settings(environ={"DML_DATA_DIR": str(tmp_path)})
    assert latest.preferences.version == 1
    assert latest.timezone == "UTC"


def test_model_override_rejects_control_characters_without_echoing_value(tmp_path):
    """Prevent invalid provider IDs and control characters from reaching runtime metadata."""
    invalid = "would-appear-in-logs\nprivate-line"
    with pytest.raises(ConfigError, match="model_id") as error:
        load_settings(environ={"DML_DATA_DIR": str(tmp_path), "DML_MODEL_ID": invalid})
    assert "private-line" not in str(error.value)
