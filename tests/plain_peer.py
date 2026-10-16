"""Spawn and drive a real ``--text --plain`` peer over pipes.

Shared by the terminal end-to-end tests and the GUI cross test: both need a
process that behaves exactly like a machine without Flet running the app the
way a user would (flags on the command line, commands typed on stdin).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "src" / "main.py"
_PORT_RE = re.compile(r"TCP (\d+)")


class PlainPeer:
    """One running ``--plain`` process, spoken to over stdin/stdout."""

    def __init__(self, name: str, proc: asyncio.subprocess.Process) -> None:
        self.name = name
        self.proc = proc
        self.lines: list[str] = []
        self.port = 0

    async def read_until(
        self, predicate: Callable[[str], bool], *, timeout: float = 15.0
    ) -> str:
        while True:
            try:
                raw = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
            except asyncio.TimeoutError:
                raise AssertionError(
                    f"{self.name}: timed out; output so far: {self.lines!r}"
                ) from None
            if not raw:
                raise AssertionError(
                    f"{self.name}: exited; output so far: {self.lines!r}"
                )
            line = raw.decode().rstrip()
            self.lines.append(line)
            if not self.port and (found := _PORT_RE.search(line)):
                self.port = int(found.group(1))
            if predicate(line):
                return line

    async def send(self, command: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((command + "\n").encode())
        await self.proc.stdin.drain()

    async def quit(self) -> None:
        try:
            await self.send("quit")
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except (asyncio.TimeoutError, BrokenPipeError):
            self.proc.kill()
            await self.proc.wait()


async def spawn_plain_peer(
    name: str,
    cwd: Path,
    save_dir: Path,
    *,
    extra_args: Iterable[str] = (),
) -> PlainPeer:
    """Start ``python src/main.py --text --plain`` and wait until it listens."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.pop("FLET_APP_STORAGE_DATA", None)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(MAIN),
        "--text",
        "--plain",
        "--no-discovery",
        "--name",
        name,
        "--port",
        "0",
        "--save-dir",
        str(save_dir),
        *extra_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=str(cwd),
    )
    peer = PlainPeer(name, proc)
    await peer.read_until(lambda line: "listening on TCP" in line)
    assert peer.port > 0
    return peer
