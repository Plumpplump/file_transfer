"""Full-screen terminal UI (curses): peer radar, transfers, accept/decline.

Keys::

    up/down, j/k   move in the peer list
    enter, s       send a file to the selected peer (asks for a path below)
    c              add a peer by address (asks for host:port)
    n              change the name peers see
    a / d          accept / decline the pending offer
    q              quit

Everything is drawn from engine events, so this file contains no networking
beyond calling the same :class:`~app.core.engine.TransferEngine` the GUI uses.
"""

from __future__ import annotations

import asyncio
import curses
import math
from pathlib import Path

from app.core import protocol
from app.core.config import AppConfig
from app.core.engine import (
    Event,
    IncomingOffer,
    PeersChanged,
    TransferEngine,
    TransferFinished,
    TransferProgress,
    TransferReply,
    TransferStarted,
)

FRAME_SECONDS = 1 / 15
LOG_KEEP = 5
SWEEP_DEGREES_PER_SECOND = 150

# color pair numbers (see _setup_colors)
C_ACCENT, C_GOOD, C_BAD, C_DIM = 1, 2, 3, 4


class CursesApp:
    """Holds all UI state; the engine mutates it via events."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.engine = TransferEngine(config, self._on_event)
        self.stdscr: curses.window | None = None
        self.peers: tuple = ()
        self.selected = 0
        self.offers: list[IncomingOffer] = []
        self.log: list[tuple[int, str]] = []  # (color pair, text)
        self.progress: dict[str, TransferProgress] = {}
        self.input_mode: str | None = None  # "send" | "connect" | "name"
        self.input_text = ""
        self.status = ""
        self.sweep = 0.0
        self.running = True

    # -- engine events -------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        match event:
            case PeersChanged(peers=peers):
                self.peers = peers
                self.selected = min(self.selected, max(0, len(peers) - 1))
                names = ", ".join(peer.name for peer in peers) or "(none)"
                self._log(C_DIM, f"peers: {names}")
            case IncomingOffer():
                self.offers.append(event)
                self._log(
                    C_ACCENT,
                    f'offer from {event.sender}: "{event.filename}" '
                    f"({protocol.human_size(event.size)})",
                )
            case TransferStarted():
                self.progress[event.tid] = TransferProgress(
                    tid=event.tid,
                    direction=event.direction,
                    done=0,
                    total=event.size,
                )
                self._log(
                    C_DIM,
                    f"{event.direction.value} {event.filename} "
                    f"({protocol.human_size(event.size)}) with {event.peer}",
                )
            case TransferReply(accepted=True):
                self._log(C_GOOD, "offer accepted - sending")
            case TransferReply(accepted=False, reason=reason):
                self._log(C_BAD, f"offer declined: {reason}")
            case TransferProgress():
                self.progress[event.tid] = event
            case TransferFinished(ok=True, message=message):
                self.progress.pop(event.tid, None)
                self._log(C_GOOD, message)
            case TransferFinished(ok=False, message=message):
                self.progress.pop(event.tid, None)
                self._log(C_BAD, message)

    def _log(self, color: int, text: str) -> None:
        self.log.append((color, text))
        del self.log[:-LOG_KEEP]

    # -- keys ----------------------------------------------------------------
    def _pump_keys(self) -> None:
        assert self.stdscr is not None
        while self.running:
            try:
                key = self.stdscr.get_wch()
            except curses.error:
                return  # nothing pending (nodelay mode)
            except KeyboardInterrupt:
                self.running = False
                return
            if key == -1:  # some curses builds return -1 instead of raising
                return
            self._handle_key(key)

    def _handle_key(self, key) -> None:
        if self.input_mode is not None:
            self._handle_input_key(key)
            return
        if isinstance(key, str):
            match key.lower():
                case "q":
                    self.running = False
                case "j":
                    self._move(1)
                case "k":
                    self._move(-1)
                case "a":
                    self._answer(True)
                case "d":
                    self._answer(False)
                case "s" | "\n" | "\r":
                    self._ask_send()
                case "c":
                    self._begin_input("connect", "peer address (host:port): ")
                case "n":
                    self._begin_input("name", f"your name [{self.engine.name}]: ")
        else:
            match key:
                case curses.KEY_UP:
                    self._move(-1)
                case curses.KEY_DOWN:
                    self._move(1)

    def _move(self, delta: int) -> None:
        if self.peers:
            self.selected = (self.selected + delta) % len(self.peers)

    def _ask_send(self) -> None:
        if not self.peers:
            self.status = "no peers yet - is the other side running?"
            return
        peer = self.peers[self.selected]
        self._begin_input("send", f"file to send to {peer.name}: ")

    def _begin_input(self, mode: str, prompt: str) -> None:
        self.input_mode = mode
        self.input_text = ""
        self.status = prompt

    def _handle_input_key(self, key) -> None:
        if not isinstance(key, str):
            return
        if key in ("\n", "\r"):
            text, mode = self.input_text, self.input_mode
            self.input_mode, self.input_text = None, ""
            self._submit_input(mode, text)
        elif key == "\x1b":  # esc cancels
            self.input_mode, self.input_text = None, ""
            self.status = "cancelled"
        elif key in ("\x7f", "\b"):
            self.input_text = self.input_text[:-1]
        elif key.isprintable():
            self.input_text += key

    def _submit_input(self, mode: str | None, text: str) -> None:
        text = text.strip()
        if mode == "send":
            path = Path(text).expanduser()
            if not path.is_file():
                self.status = f"not a file: {path}"
                return
            peer = self.peers[self.selected]
            self.status = f"offering {path.name} to {peer.name}"
            asyncio.get_running_loop().create_task(
                self.engine.send_file(peer.host, peer.port, path)
            )
        elif mode == "connect":
            host, _, port_text = text.partition(":")
            if not host or not port_text.isdigit():
                self.status = "expected host:port"
                return
            peer = self.engine.add_manual_peer(host, int(port_text))
            self.status = f"added {peer.address}"
        elif mode == "name":
            if not text:
                self.status = f"name unchanged: {self.engine.name}"
                return
            self.status = f"name changed to {self.engine.set_name(text)}"

    def _answer(self, accept: bool) -> None:
        if not self.offers:
            self.status = "no offer to answer"
            return
        offer = self.offers.pop(0)
        self.status = "accepted" if accept else "declined"
        asyncio.get_running_loop().create_task(
            self.engine.respond(offer.tid, accept=accept)
        )

    # -- drawing -------------------------------------------------------------
    def draw(self) -> None:
        stdscr = self.stdscr
        assert stdscr is not None
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 10 or width < 44:
            self._add(0, 0, "terminal too small for the curses UI - use --plain")
            stdscr.refresh()
            return

        self._draw_title(width)
        log_top = height - LOG_KEEP - 3
        self._draw_peers(2, 0, log_top - 2, width // 2 - 1)
        self._draw_transfers(2, width // 2 + 1, log_top - 2, width - 1)
        for index, (color, text) in enumerate(self.log):
            self._add(log_top + index, 1, text[: width - 2], _color(color))
        self._draw_status(height, width)
        stdscr.refresh()

    def _draw_title(self, width: int) -> None:
        where = "discovery off"
        if self.engine.discovery is not None:
            where = f"discovery :{self.config.discovery_port}"
        title = (
            f" myftp {self.engine.name} - port {self.engine.port} ({where}) "
            f"- {len(self.peers)} peer(s) "
        )
        self._add(0, 0, title.ljust(width - 1), curses.A_REVERSE)

    def _draw_peers(self, top: int, left: int, bottom: int, right: int) -> None:
        # Peer list on top, radar underneath - both inside the left column.
        mid = top + max(5, (bottom - top) // 2)
        self._add(top, left + 1, "NEARBY", curses.A_BOLD)
        if not self.peers:
            self._add(top + 2, left + 1, "scanning...", _color(C_DIM))
        for index, peer in enumerate(self.peers):
            row = top + 2 + index
            if row >= mid - 1:
                break
            marker = ">" if index == self.selected else " "
            label = f"{marker} {peer.name}  {peer.address}"
            attr = curses.A_REVERSE if index == self.selected else 0
            self._add(row, left + 1, label[: max(0, right - left - 2)], attr)
        # the pending offer, right above the radar
        if self.offers:
            offer = self.offers[0]
            row = max(top + 2, mid - 1)
            self._add(
                row,
                left + 1,
                f"[a]ccept [d]ecline {offer.filename}"[: max(0, right - left - 2)],
                _color(C_ACCENT) | curses.A_BOLD,
            )
        self._draw_radar(mid, left, bottom, right)

    def _draw_radar(self, top: int, left: int, bottom: int, right: int) -> None:
        center_y = (top + bottom) // 2
        center_x = (left + right) // 2
        radius_y = max(2, min((bottom - top) // 2 - 1, (right - left) // 4))
        radius_x = radius_y * 2
        for fraction in (1.0, 0.62, 0.3):
            for degrees in range(0, 360, 8):
                angle = math.radians(degrees)
                self._add(
                    center_y + int(radius_y * fraction * math.sin(angle)),
                    center_x + int(radius_x * fraction * math.cos(angle)),
                    ".",
                    _color(C_DIM),
                )
        # sweep line
        angle = math.radians(self.sweep)
        for step in range(1, radius_y + 1):
            self._add(
                center_y + int(step * math.sin(angle)),
                center_x + int(2 * step * math.cos(angle)),
                "*",
                _color(C_ACCENT),
            )
        # devices sit on the outer ring; their angle comes from the address so
        # a peer does not wander around between renders
        for peer in self.peers:
            angle = math.radians((hash(peer.address) % 360 + 360) % 360)
            self._add(
                center_y + int(0.85 * radius_y * math.sin(angle)),
                center_x + int(1.7 * radius_y * math.cos(angle)),
                (peer.name or "?")[0].upper(),
                curses.A_BOLD,
            )

    def _draw_transfers(self, top: int, left: int, bottom: int, right: int) -> None:
        self._add(top, left + 1, "TRANSFERS", curses.A_BOLD)
        width = max(10, right - left - 4)
        for index, progress in enumerate(list(self.progress.values())):
            row = top + 2 + index * 2
            if row + 1 >= bottom:
                break
            fraction = protocol.fraction(progress.done, progress.total)
            filled = int(fraction * width)
            bar = "#" * filled + "-" * (width - filled)
            self._add(
                row,
                left + 1,
                f"{progress.direction.value} {int(fraction * 100)}%",
                _color(C_ACCENT),
            )
            self._add(row + 1, left + 1, f"[{bar}]", _color(C_DIM))
        if not self.progress:
            self._add(top + 2, left + 1, "(idle)", _color(C_DIM))

    def _draw_status(self, height: int, width: int) -> None:
        prompt = self.status
        if self.input_mode is not None:
            prompt = f"{self.status}{self.input_text}_"
        self._add(height - 2, 1, prompt[: width - 2], _color(C_ACCENT))
        keys = " up/down select  enter send  c connect  n name  a/d offer  q quit "
        self._add(height - 1, 0, keys.ljust(width - 1), curses.A_REVERSE)

    def _add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert self.stdscr is not None
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass  # writing into the bottom-right corner is "allowed to fail"

    # -- run loop ------------------------------------------------------------
    async def run(self, stdscr: curses.window) -> None:
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        self._setup_colors()
        await self.engine.start()
        if self.engine.discovery_error:
            self._log(C_BAD, self.engine.discovery_error)
        try:
            while self.running:
                self._pump_keys()
                self.draw()
                self.sweep = (
                    self.sweep + SWEEP_DEGREES_PER_SECOND * FRAME_SECONDS
                ) % 360
                await asyncio.sleep(FRAME_SECONDS)
        finally:
            await self.engine.stop()

    def _setup_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:  # pragma: no cover - ancient terminals
            background = curses.COLOR_BLACK
        curses.init_pair(C_ACCENT, curses.COLOR_CYAN, background)
        curses.init_pair(C_GOOD, curses.COLOR_GREEN, background)
        curses.init_pair(C_BAD, curses.COLOR_RED, background)
        curses.init_pair(C_DIM, curses.COLOR_BLUE, background)


def _color(pair: int) -> int:
    return curses.color_pair(pair) if curses.has_colors() else 0


def run_curses(config: AppConfig) -> int:
    """Entry point for ``--text`` on a terminal (blocking, returns a code)."""
    app = CursesApp(config)
    try:
        curses.wrapper(lambda stdscr: asyncio.run(app.run(stdscr)))
    except KeyboardInterrupt:
        pass
    except curses.error as exc:  # e.g. TERM=dumb
        print(f"terminal UI unavailable ({exc}); try --text --plain")
        return 1
    return 0
