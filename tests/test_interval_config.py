"""Tests for sync interval configuration and CLI behavior."""

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from anilist_mal_sync.cli import _resolve_sync_interval, main
from anilist_mal_sync.config import Settings, SyncConfig


def test_sync_interval_defaults_to_six_hours():
    assert SyncConfig().interval == 360


@pytest.mark.parametrize("interval", [0, -1])
def test_sync_interval_must_be_positive(interval):
    with pytest.raises(ValidationError):
        SyncConfig(interval=interval)


def test_settings_load_sync_interval_from_yaml(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  interval: 120\n", encoding="utf-8")
    monkeypatch.setattr(Settings, "_get_config_path", lambda self: config_path)
    monkeypatch.setattr(Settings, "_set_env_vars", lambda self: None)

    settings = Settings()

    assert settings.sync_interval == 120


def test_cli_interval_uses_config_when_not_provided():
    assert _resolve_sync_interval(None, 120) == 120


def test_cli_interval_override_takes_precedence():
    assert _resolve_sync_interval(30, 120) == 30


def test_cli_rejects_nonpositive_interval():
    result = CliRunner().invoke(main, ["run", "--interval", "0"])

    assert result.exit_code == 2
    assert "Invalid value for '--interval'" in result.output
