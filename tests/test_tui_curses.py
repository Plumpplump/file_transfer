"""Tests for the full-screen curses UI.

Two layers:

* the state machine (events -> status lines, keys -> engine calls, drawing)
  driven through a fake screen, so it runs everywhere; and
* one pty smoke test that runs the *real* process on a pseudo terminal: it
  has to draw and it has to exit cleanly when the user presses ``q``.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import struct
import sys
import termios
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config import AppConfig, Mode
from app.core.engine import (
    Direction,
    IncomingOffer,
    PeersChanged,
    TransferFinished,
    TransferProgress,
    TransferStarted,
)
from app.tui import curses_ui
from app.tui.curses_ui import CursesApp
from p2p import PeerInfo

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="curses is POSIX-only"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "src" / "main.py"


def make_config(tmp_path: Path, **overrides) -> AppConfig:
    base = AppConfig(
        mode=Mode.CURSES,
        name="CURSESBOX",
        port=0,
        discovery_enabled=False,
        save_dir=tmp_path / "inbox",
    )
    return replace(base, **overrides)


class FakeScreen:
    """Just enough of a curses window to render into a grid of characters."""

    def __init__(self, height: int = 24, width: int = 90) -> None:
        self.height = height
        self.width = width
        self.grid = [[" "] * width for _ in range(height)]

    def erase(self) -> None:
        self.grid = [[" "] * self.width for _ in range(self.height)]

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        for offset, char in enumerate(text):
            column = x + offset
            if 0 <= y < self.height and 0 <= column < self.width:
                self.grid[y][column] = char

    def refresh(self) -> None:
        pass

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.grid]


@pytest.fixture(autouse=True)
def no_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Colour pairs need a real terminal; the fake screen does not have one."""
    monkeypatch.setattr(curses_ui, "_color", lambda pair: 0)


@pytest.fixture
def peers() -> tuple[PeerInfo, ...]:
    return (
        PeerInfo(host="10.0.0.4", port=9100, name="BOB", meta={}),
        PeerInfo(host="10.0.0.7", port=9100, name="ALICE", meta={}),
    )


# --------------------------------------------------------------------------
# state machine, keys and drawing - no terminal needed
# --------------------------------------------------------------------------
async def test_events_fill_the_status_log(
    tmp_path: Path, peers: tuple[PeerInfo, ...]
) -> None:
    ui = CursesApp(make_config(tmp_path))
    ui._on_event(PeersChanged(peers=peers))
    assert ui.peers == peers
    assert "BOB" in ui.log[-1][1]

    ui._on_event(
        IncomingOffer(
            tid="t1", filename="photo.jpg", size=2048, sender="BOB", address="10.0.0.4"
        )
    )
    ui._on_event(
        TransferStarted(
            tid="t1",
            direction=Direction.RECEIVE,
            filename="photo.jpg",
            size=2048,
            peer="BOB",
        )
    )
    assert ui.offers and ui.progress["t1"].done == 0

    ui._on_event(
        TransferProgress(tid="t1", direction=Direction.RECEIVE, done=1024, total=2048)
    )
    assert ui.progress["t1"].done == 1024

    ui._on_event(
        TransferFinished(
            tid="t1", direction=Direction.RECEIVE, ok=True, message="Saved to /tmp/x"
        )
    )
    assert "t1" not in ui.progress
    assert ui.log[-1] == (curses_ui.C_GOOD, "Saved to /tmp/x")

    # the log is a rolling window, not an ever-growing list
    for index in range(20):
        ui._log(curses_ui.C_DIM, f"line {index}")
    assert len(ui.log) == curses_ui.LOG_KEEP


async def test_keys_move_selection_and_quit(
    tmp_path: Path, peers: tuple[PeerInfo, ...]
) -> None:
    ui = CursesApp(make_config(tmp_path))
    ui._on_event(PeersChanged(peers=peers))

    ui._handle_key("j")
    assert ui.selected == 1
    ui._handle_key("j")
    assert ui.selected == 0  # wraps around
    ui._handle_key("k")
    assert ui.selected == 1
    ui._handle_key(curses_ui.curses.KEY_UP)
    assert ui.selected == 0
    ui._handle_key(curses_ui.curses.KEY_DOWN)
    assert ui.selected == 1

    ui._handle_key("q")
    assert ui.running is False


async def test_typing_an_address_registers_a_peer(tmp_path: Path) -> None:
    ui = CursesApp(make_config(tmp_path))

    ui._handle_key("c")
    assert ui.input_mode == "connect"
    for char in "10.0.0.7:9100":
        ui._handle_key(char)
    assert ui.input_text == "10.0.0.7:9100"
    ui._handle_key("\x7f")  # backspace
    assert ui.input_text == "10.0.0.7:910"
    ui._handle_key("0")
    ui._handle_key("\n")

    assert ui.input_mode is None
    assert [peer.address for peer in ui.engine.peers] == ["10.0.0.7:9100"]
    assert ui.status == "added 10.0.0.7:9100"

    # a malformed address is refused, and esc cancels the prompt
    ui._handle_key("c")
    for char in "nonsense":
        ui._handle_key(char)
    ui._handle_key("\r")
    assert ui.status == "expected host:port"
    ui._handle_key("c")
    ui._handle_key("\x1b")
    assert ui.input_mode is None and ui.status == "cancelled"


async def test_typing_a_name_renames_the_device(tmp_path: Path) -> None:
    ui = CursesApp(make_config(tmp_path))
    assert ui.engine.name == "CURSESBOX"

    ui._handle_key("n")
    assert ui.input_mode == "name"
    assert "CURSESBOX" in ui.status  # the prompt shows the current name
    for char in "Ada  Lovelace":
        ui._handle_key(char)
    ui._handle_key("\n")
    assert ui.input_mode is None
    assert ui.engine.name == "Ada Lovelace"  # doubled spaces are tidied
    assert "Ada Lovelace" in ui.status

    # enter with nothing typed leaves the name alone ...
    ui._handle_key("n")
    ui._handle_key("\r")
    assert ui.input_mode is None and "unchanged" in ui.status
    assert ui.engine.name == "Ada Lovelace"

    # ... and the title bar shows the name that is in use
    screen = FakeScreen()
    ui.stdscr = screen  # type: ignore[assignment]
    ui._draw_title(screen.width)
    assert "Ada Lovelace" in screen.lines()[0]


async def test_send_prompt_rejects_missing_files(
    tmp_path: Path, peers: tuple[PeerInfo, ...]
) -> None:
    ui = CursesApp(make_config(tmp_path))
    ui._handle_key("s")
    assert "no peers yet" in ui.status

    ui._on_event(PeersChanged(peers=peers))
    ui._handle_key("s")
    assert ui.input_mode == "send"
    assert "BOB" in ui.status  # the selected peer is part of the prompt
    ui._handle_key("\x1b")  # cancel the prompt

    ui._handle_key("j")  # move the cursor to the second peer
    ui._handle_key("s")
    assert "ALICE" in ui.status

    for char in "/definitely/missing.bin":
        ui._handle_key(char)
    ui._handle_key("\n")
    assert ui.status.startswith("not a file:")


async def test_answering_offers(
    tmp_path: Path, peers: tuple[PeerInfo, ...]
) -> None:
    ui = CursesApp(make_config(tmp_path))
    ui._handle_key("a")
    assert ui.status == "no offer to answer"

    ui._on_event(
        IncomingOffer(
            tid="t9", filename="notes.txt", size=10, sender="BOB", address="10.0.0.4"
        )
    )
    ui._handle_key("d")
    assert ui.status == "declined"
    assert ui.offers == []
    await asyncio.sleep(0)  # let the scheduled engine.respond() run


async def test_draws_a_frame(
    tmp_path: Path, peers: tuple[PeerInfo, ...]
) -> None:
    ui = CursesApp(make_config(tmp_path))
    screen = FakeScreen()
    ui.stdscr = screen  # type: ignore[assignment]

    ui.draw()
    lines = screen.lines()
    assert "myftp CURSESBOX" in lines[0]
    assert "port 0" in lines[0]
    assert any("NEARBY" in line for line in lines)
    assert any("TRANSFERS" in line for line in lines)

    ui._on_event(PeersChanged(peers=peers))
    ui._on_event(
        IncomingOffer(
            tid="t1", filename="photo.jpg", size=2048, sender="BOB", address="10.0.0.4"
        )
    )
    ui._on_event(
        TransferProgress(tid="t1", direction=Direction.RECEIVE, done=1024, total=2048)
    )
    ui.draw()
    lines = screen.lines()
    assert any("BOB" in line and "10.0.0.4:9100" in line for line in lines)
    assert any("[a]ccept [d]ecline photo.jpg" in line for line in lines)
    assert any("50%" in line for line in lines)


async def test_small_terminal_gets_a_hint(tmp_path: Path) -> None:
    ui = CursesApp(make_config(tmp_path))
    ui.stdscr = FakeScreen(height=6, width=30)  # type: ignore[assignment]
    ui.draw()
    assert "too small" in " ".join(ui.stdscr.lines())


async def test_radar_marks_devices(tmp_path: Path, peers: tuple[PeerInfo, ...]) -> None:
    ui = CursesApp(make_config(tmp_path))
    ui.stdscr = FakeScreen()  # type: ignore[assignment]
    ui._on_event(PeersChanged(peers=peers))
    ui.draw()

    # each peer contributes its initial somewhere on the radar
    drawn = "".join(ui.stdscr.lines())
    assert drawn.count("B") >= 1 and drawn.count("A") >= 1
    # the sweep line is drawn with stars
    assert "*" in drawn


# --------------------------------------------------------------------------
# the real thing, on a pseudo terminal
# --------------------------------------------------------------------------
async def test_curses_ui_starts_draws_and_quits(tmp_path: Path) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    os.set_blocking(master, False)
    env = {**os.environ, "TERM": "xterm-256color", "PYTHONUNBUFFERED": "1"}
    env.pop("FLET_APP_STORAGE_DATA", None)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(MAIN),
        "--text",
        "--no-discovery",
        "--name",
        "CURSES",
        "--port",
        "0",
        "--save-dir",
        str(tmp_path),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=str(REPO_ROOT),
        env=env,
    )
    os.close(slave)
    output = bytearray()

    async def drain() -> None:
        while True:
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                await asyncio.sleep(0.05)
                continue
            except OSError:
                return  # pty closed after the child exited
            if not data:
                return
            output.extend(data)

    reader = asyncio.create_task(drain())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while b"myftp" not in output:  # the title bar should be drawn
            assert proc.returncode is None, (
                f"curses UI exited early (code {proc.returncode}): {bytes(output)!r}"
            )
            assert loop.time() < deadline, f"UI never drew anything: {bytes(output)!r}"
            await asyncio.sleep(0.05)

        os.write(master, b"q")
        await asyncio.wait_for(proc.wait(), 10)
        assert proc.returncode == 0, f"unclean exit: {bytes(output)!r}"
    finally:
        reader.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        os.close(master)
