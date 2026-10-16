"""Tests for CLI / environment configuration resolution."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from app.core.config import (
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_TCP_PORT,
    NAME_LIMIT,
    Mode,
    clean_name,
    load_settings,
    resolve_config,
    save_setting,
    settings_path,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Make sure the developer's own environment cannot leak into the tests."""
    for key in list(os.environ):
        if key.startswith("MYFTP_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)


class FakeStdout:
    def __init__(self, isatty: bool) -> None:
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def test_defaults() -> None:
    config = resolve_config([])
    assert config.mode is Mode.GUI
    assert config.port == DEFAULT_TCP_PORT
    assert config.discovery_port == DEFAULT_DISCOVERY_PORT
    assert config.discovery_enabled
    assert config.name and config.name == config.name.upper()
    assert config.save_dir == Path.cwd()
    assert config.manual_peers == ()


def test_text_mode_on_a_terminal_is_curses(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", FakeStdout(isatty=True))
    assert resolve_config(["--text"]).mode is Mode.CURSES


def test_text_mode_off_a_terminal_is_plain(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", FakeStdout(isatty=False))
    assert resolve_config(["--text"]).mode is Mode.PLAIN


def test_plain_flag_forces_plain_even_on_a_terminal(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdout", FakeStdout(isatty=True))
    assert resolve_config(["--text", "--plain"]).mode is Mode.PLAIN


def test_env_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("MYFTP_PORT", "9999")
    monkeypatch.setenv("MYFTP_NAME", "peer-name")
    monkeypatch.setenv("MYFTP_DISCOVERY_PORT", "9888")
    monkeypatch.setenv("MYFTP_DISCOVERY", "0")
    monkeypatch.setenv("MYFTP_SAVE_DIR", "~/downloads")
    monkeypatch.setenv("MYFTP_CONNECT", "10.0.0.5:9100, junk, 10.0.0.6:9101")
    config = resolve_config([])
    assert config.port == 9999
    assert config.name == "peer-name"
    assert config.discovery_port == 9888
    assert not config.discovery_enabled
    assert config.save_dir == Path("~/downloads").expanduser()
    assert config.manual_peers == (("10.0.0.5", 9100), ("10.0.0.6", 9101))


def test_cli_beats_environment(monkeypatch) -> None:
    monkeypatch.setenv("MYFTP_PORT", "9999")
    config = resolve_config(["--port", "1234", "--name", "chosen"])
    assert config.port == 1234
    assert config.name == "chosen"


def test_connect_flag_and_no_discovery() -> None:
    config = resolve_config(
        ["--connect", "192.168.1.9:9100", "--connect", "10.0.0.1:9101", "--no-discovery"]
    )
    assert config.manual_peers == (("192.168.1.9", 9100), ("10.0.0.1", 9101))
    assert not config.discovery_enabled


def test_port_zero_is_allowed() -> None:
    assert resolve_config(["--port", "0"]).port == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--port", "70000"],
        ["--port", "abc"],
        ["--connect", "no-port-here"],
        ["--connect", "host:99999"],
    ],
)
def test_bad_values_exit_with_a_usage_error(argv) -> None:
    with pytest.raises(SystemExit):
        resolve_config(argv)


def test_unknown_flags_are_ignored() -> None:
    # `flet run` and pytest may append their own arguments; ours must not choke.
    config = resolve_config(["--totally-unknown", "value"])
    assert config.mode is Mode.GUI


def test_save_dir_defaults_to_cwd(monkeypatch) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert resolve_config([]).save_dir == Path.cwd()


def test_save_dir_prefers_the_flet_invocation_dir(monkeypatch, tmp_path) -> None:
    """`flet run` changes the app's cwd; files must still land where the user was."""
    invocation = tmp_path / "workdir"
    invocation.mkdir()
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path / "storage"))
    monkeypatch.setenv("PYTHONPATH", f"{invocation}{os.pathsep}/elsewhere")
    assert resolve_config([]).save_dir == invocation


def test_explicit_save_dir_beats_the_flet_dance(monkeypatch, tmp_path) -> None:
    invocation = tmp_path / "workdir"
    invocation.mkdir()
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path / "storage"))
    monkeypatch.setenv("PYTHONPATH", str(invocation))
    chosen = tmp_path / "chosen"
    assert resolve_config(["--save-dir", str(chosen)]).save_dir == chosen


# --------------------------------------------------------------------------
# the settings file: where the edited display name is remembered
# --------------------------------------------------------------------------
def test_the_default_settings_file_lives_under_the_home_dir() -> None:
    # HOME is the test's own temp dir (see conftest): nothing real is touched.
    assert settings_path() == Path.home() / ".config" / "myftp" / "settings.json"
    assert not settings_path().exists()


def test_a_saved_name_wins_over_the_hostname(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MYFTP_SETTINGS", str(tmp_path / "state" / "settings.json"))
    save_setting("name", "  Ada   Lovelace ")  # stored as typed, tidied when used
    assert resolve_config([]).name == "Ada Lovelace"

    # an explicit name still beats a saved one
    monkeypatch.setenv("MYFTP_NAME", "from-env")
    assert resolve_config([]).name == "from-env"
    assert resolve_config(["--name", "from-cli"]).name == "from-cli"


def test_settings_are_written_and_read_back(monkeypatch, tmp_path) -> None:
    first = tmp_path / "one.json"
    monkeypatch.setenv("MYFTP_SETTINGS", str(first))
    save_setting("name", "PERSISTED")
    assert load_settings() == {"name": "PERSISTED"}
    assert json.loads(first.read_text("utf-8")) == {"name": "PERSISTED"}
    assert not list(tmp_path.glob("*.tmp"))  # staged, then moved, nothing left

    monkeypatch.setenv("MYFTP_SETTINGS", str(tmp_path / "two.json"))
    assert load_settings() == {}
    save_setting("name", "OTHER")
    assert load_settings() == {"name": "OTHER"}
    assert json.loads(first.read_text("utf-8")) == {"name": "PERSISTED"}


def test_broken_settings_are_ignored(monkeypatch, tmp_path) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("MYFTP_SETTINGS", str(path))

    path.write_text("{not json", "utf-8")
    assert load_settings() == {}
    assert resolve_config([]).name == socket.gethostname().split(".")[0].upper()

    path.write_text('["not", "a", "mapping"]', "utf-8")
    assert load_settings() == {}

    # only string values are meaningful; the rest is skipped, not fatal
    path.write_text('{"name": 42, "note": "keep"}', "utf-8")
    assert load_settings() == {"note": "keep"}


def test_names_are_tidied_to_fit_a_chip() -> None:
    assert clean_name("  Ada   Lovelace ", "fallback") == "Ada Lovelace"
    assert clean_name("   ", "fallback") == "fallback"
    assert clean_name("x" * 80, "fallback") == "x" * NAME_LIMIT
