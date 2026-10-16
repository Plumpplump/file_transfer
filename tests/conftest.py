"""Shared fixtures.

The app remembers small user choices (so far only the display name) in a
settings file under the home directory. Point ``HOME`` at the test's own temp
directory so no test can read or write the real ``~/.config/myftp/`` - and so
the name tests always start from a clean slate. Subprocesses spawned by the
tests inherit the variable, which is why this covers them too.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
