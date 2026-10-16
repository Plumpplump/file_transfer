"""``main()`` dispatch: each resolved mode must reach its own front-end.

The heavy lifting of *parsing* flags lives in ``app.core.config`` (see
``test_config.py``); what is checked here is the wiring: curses, plain and GUI
each get exactly one entry point, and the GUI is the only branch that touches
Flet at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import main as main_module
from app.core.config import AppConfig, Mode


@pytest.fixture
def patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Force ``main()`` to see a config of our choosing."""

    def patch(mode: Mode) -> AppConfig:
        config = AppConfig(mode=mode, save_dir=tmp_path)
        monkeypatch.setattr(main_module, "resolve_config", lambda argv=None: config)
        return config

    return patch


def test_text_mode_runs_the_curses_ui(
    monkeypatch: pytest.MonkeyPatch, patch_config
) -> None:
    config = patch_config(Mode.CURSES)
    seen: list[AppConfig] = []

    def run_curses(received: AppConfig) -> int:
        seen.append(received)
        return 7

    fake = types.ModuleType("app.tui.curses_ui")
    fake.run_curses = run_curses
    monkeypatch.setitem(sys.modules, "app.tui.curses_ui", fake)

    assert main_module.main(["--text"]) == 7
    assert seen == [config]


def test_plain_mode_runs_the_line_ui(
    monkeypatch: pytest.MonkeyPatch, patch_config
) -> None:
    config = patch_config(Mode.PLAIN)
    seen: list[AppConfig] = []

    def run_plain(received: AppConfig) -> int:
        seen.append(received)
        return 0

    fake = types.ModuleType("app.tui.plain")
    fake.run_plain = run_plain
    monkeypatch.setitem(sys.modules, "app.tui.plain", fake)

    assert main_module.main(["--plain"]) == 0
    assert seen == [config]


def test_gui_mode_hands_a_page_builder_to_flet(
    monkeypatch: pytest.MonkeyPatch, patch_config
) -> None:
    config = patch_config(Mode.GUI)
    targets: list[object] = []
    built: list[tuple[AppConfig, object]] = []

    fake_flet = types.ModuleType("flet")

    def run(target: object, **kwargs: object) -> None:
        targets.append(target)

    fake_flet.run = run
    monkeypatch.setitem(sys.modules, "flet", fake_flet)

    fake_ui = types.ModuleType("app.ui.app")

    def build_app(received: AppConfig, page: object) -> str:
        built.append((received, page))
        return "the app"

    fake_ui.build_app = build_app
    monkeypatch.setitem(sys.modules, "app.ui.app", fake_ui)

    # the GUI path must not pull the terminal front-ends in
    monkeypatch.delitem(sys.modules, "app.tui.curses_ui", raising=False)
    monkeypatch.delitem(sys.modules, "app.tui.plain", raising=False)

    assert main_module.main([]) == 0
    assert len(targets) == 1

    # Flet calls the callable with the page; that is what builds our GUI.
    targets[0]("PAGE")  # type: ignore[operator]
    assert built == [(config, "PAGE")]

    assert "app.tui.curses_ui" not in sys.modules
    assert "app.tui.plain" not in sys.modules
