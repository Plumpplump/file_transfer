"""The `--text` promise: the core (and the plain TUI) must import without Flet.

A peer machine is allowed to have nothing but this repository and Python. The
GUI imports ``flet`` lazily in ``main.py`` only; these tests pin that down by
importing the core modules in a fresh interpreter and checking that ``flet``
never entered ``sys.modules``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

CODE = (
    "import sys\n"
    "import app.core.config, app.core.protocol, app.core.engine\n"
    "import app.tui.plain\n"
    "assert 'flet' not in sys.modules, 'something in app.core/app.tui imported flet!'\n"
)


def test_core_and_plain_tui_import_without_flet() -> None:
    result = subprocess.run(
        [sys.executable, "-c", CODE],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cli_help_works_without_flet() -> None:
    result = subprocess.run(
        [sys.executable, str(SRC / "main.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--text" in result.stdout and "--save-dir" in result.stdout
