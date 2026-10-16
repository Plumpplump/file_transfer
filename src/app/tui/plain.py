"""Line-oriented terminal front-end.

Works through pipes, scripts and tests - and on machines without Flet. One
line per event, one command per line of input::

    list                  list nearby peers
    send N PATH           offer PATH to peer number N
    connect HOST:PORT     add a peer manually
    name NEW NAME         change the name peers see
    a | accept            accept the pending offer
    d | decline           decline it
    help                  show the command list
    q | quit              exit

Run it with ``python src/main.py --text --plain`` (or just ``--text`` when
stdout is not a terminal).
"""

from __future__ import annotations

import asyncio
import sys
import threading
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

HELP = """commands:
  list                  list nearby peers
  send N PATH           offer PATH to peer number N
  connect HOST:PORT     add a peer manually
  name NEW NAME         change the name peers see
  a | accept            accept the pending offer
  d | decline           decline it
  help                  show this text
  q | quit              exit"""


class PlainUI:
    """Prints engine events; turns stdin lines into engine calls."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.engine = TransferEngine(config, self._on_event)
        self._offers: list[IncomingOffer] = []
        self._known_peers: set[tuple[str, int]] = set()
        self._tasks: set[asyncio.Task] = set()
        self._progress_marks: dict[str, int] = {}
        self._stdin: asyncio.Queue[str | None] = asyncio.Queue()

    # -- engine events -------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        match event:
            case PeersChanged(peers=peers):
                keys = {peer.key for peer in peers}
                for peer in peers:
                    if peer.key not in self._known_peers:
                        print(f"[peer] + {peer.name} {peer.address}")
                for key in self._known_peers - keys:
                    print(f"[peer] - {key[0]}:{key[1]}")
                self._known_peers = keys
            case IncomingOffer():
                self._offers.append(event)
                print(
                    f'[offer] {event.sender} wants to send "{event.filename}" '
                    f"({protocol.human_size(event.size)}) - press 'a' to accept "
                    f"or 'd' to decline"
                )
            case TransferStarted():
                print(
                    f"[start] {event.direction.value} "
                    f'"{event.filename}" ({protocol.human_size(event.size)}) '
                    f"with {event.peer}"
                )
            case TransferReply(accepted=True):
                print("[reply] accepted - sending")
            case TransferReply(accepted=False, reason=reason):
                print(f"[reply] declined: {reason}")
            case TransferProgress(direction=direction, done=done, total=total):
                percent = int(protocol.fraction(done, total) * 100)
                mark = percent // 10
                if self._progress_marks.get(event.tid) != mark:
                    self._progress_marks[event.tid] = mark
                    print(
                        f"[progress] {direction.value} {percent}% "
                        f"({protocol.human_size(done)} of {protocol.human_size(total)})"
                    )
            case TransferFinished(ok=True, message=message):
                print(f"[finished] ok - {message}")
            case TransferFinished(ok=False, message=message):
                print(f"[finished] failed - {message}")

    # -- start-up ------------------------------------------------------------
    def _banner(self) -> None:
        where = "discovery off"
        if self.engine.discovery is not None:
            where = f"discovery UDP {self.config.discovery_port}"
        print(
            f"myftp {self.engine.name} listening on TCP {self.engine.port} ({where})"
        )
        if self.engine.discovery_error:
            print(f"[warn] {self.engine.discovery_error}")
        print(HELP)

    async def run(self) -> None:
        await self.engine.start()
        self._banner()
        self._start_stdin_reader()
        try:
            while True:
                line = await self._stdin.get()
                if line is None:  # stdout side closed (pipe/file input ended)
                    return
                if not await self._handle(line.strip()):
                    return
        finally:
            await self.engine.stop()

    # -- commands ------------------------------------------------------------
    async def _handle(self, line: str) -> bool:
        """Run one command line; return False when the user wants to quit."""
        if not line:
            return True
        command, _, rest = line.partition(" ")
        command, rest = command.lower(), rest.strip()
        match command:
            case "q" | "quit" | "exit":
                return False
            case "help" | "?":
                print(HELP)
            case "list":
                self._cmd_list()
            case "connect":
                self._cmd_connect(rest)
            case "name":
                self._cmd_name(rest)
            case "send":
                self._cmd_send(rest)
            case "a" | "accept":
                await self._answer(True)
            case "d" | "decline":
                await self._answer(False)
            case _:
                print(f"unknown command {command!r} - press 'help'")
        return True

    def _cmd_list(self) -> None:
        peers = self.engine.peers
        if not peers:
            print("no peers yet (waiting for discovery, or use 'connect HOST:PORT')")
        for number, peer in enumerate(peers, start=1):
            print(f"{number}) {peer.name} {peer.address}")

    def _cmd_connect(self, rest: str) -> None:
        host, _, port_text = rest.partition(":")
        if not host or not port_text.isdigit():
            print("usage: connect HOST:PORT")
            return
        peer = self.engine.add_manual_peer(host, int(port_text))
        print(f"added {peer.address}")

    def _cmd_name(self, rest: str) -> None:
        if not rest:
            print(f"your name is {self.engine.name!r} - usage: name NEW NAME")
            return
        print(f"name changed to {self.engine.set_name(rest)!r}")

    def _cmd_send(self, rest: str) -> None:
        parts = rest.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            print("usage: send <peer-number> <path>")
            return
        peers = self.engine.peers
        number = int(parts[0])
        if not 1 <= number <= len(peers):
            print(f"no peer {number} - press 'list'")
            return
        path = Path(parts[1]).expanduser()
        if not path.is_file():
            print(f"not a file: {path}")
            return
        peer = peers[number - 1]
        print(
            f"offering {path.name} ({protocol.human_size(path.stat().st_size)}) "
            f"to {peer.name} ({peer.address})"
        )
        self._spawn(self.engine.send_file(peer.host, peer.port, path))

    async def _answer(self, accept: bool) -> None:
        if not self._offers:
            print("no offer to answer")
            return
        offer = self._offers.pop(0)
        print("accepted" if accept else "declined")
        await self.engine.respond(offer.tid, accept=accept)

    # -- plumbing ------------------------------------------------------------
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _start_stdin_reader(self) -> None:
        """Feed stdin lines into an async queue from a daemon thread.

        A daemon thread (rather than ``asyncio.to_thread``) matters: it can
        stay blocked on ``readline`` at exit without hanging the process.
        """
        loop = asyncio.get_running_loop()
        queue = self._stdin

        def _read() -> None:
            while True:
                line = sys.stdin.readline()
                loop.call_soon_threadsafe(queue.put_nowait, line or None)
                if not line:  # EOF
                    return

        threading.Thread(target=_read, name="stdin-reader", daemon=True).start()


def run_plain(config: AppConfig) -> int:
    """Entry point for ``--text --plain`` (blocking, returns an exit code)."""
    try:
        asyncio.run(PlainUI(config).run())
    except KeyboardInterrupt:
        pass
    return 0
