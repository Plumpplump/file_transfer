"""Boot the GUI under a *real* Flet server and talk to it over a websocket.

Flet's ``flet_app`` fixture would need a Flutter test host - a full
``flet build`` pipeline (Flutter SDK, native compile, ...) - which is far too
heavy to require for a test run. These tests take the light path instead: they
start the real app process in Flet's head-less web-server mode
(``FLET_FORCE_WEB_SERVER``) and connect a raw websocket client that speaks the
same protocol the Flutter client speaks - message framing, control ids and
all.

Seeing the app's patches arrive proves that ``ft.run(main)`` is wired up, that
the whole control tree serialises, and that the engine really starts - all
without a browser or a Flutter toolchain. The second test goes further and
drives a whole user interaction (click "Change name", type, save) through the
protocol, which is the closest thing to a real click these tests can get.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

msgpack = pytest.importorskip("msgpack")
pytest.importorskip("websockets.asyncio.client")

from websockets.asyncio.client import connect as ws_connect  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "src" / "main.py"

#: Frames are ``[0x00][msgpack body]`` in both directions.
WIRE = b"\x00"
#: ``flet.messaging.protocol.MessageAction`` members we speak.
REGISTER_CLIENT = 1
CONTROL_EVENT = 3
UPDATE_CONTROL_PROPS = 4
BOOT_TIMEOUT = 30.0
PATCH_TIMEOUT = 20.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_server(host: str, port: int, timeout: float = BOOT_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection(host, port)
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.2)
        else:
            writer.close()
            await writer.wait_closed()
            return


def app_env(tmp_path: Path, port: int) -> dict[str, str]:
    env = {
        **os.environ,
        "FLET_FORCE_WEB_SERVER": "true",
        "FLET_SERVER_PORT": str(port),
        "MYFTP_NAME": "WEBBOX",
        "MYFTP_PORT": "0",  # any free data port: tests must not collide
        "MYFTP_DISCOVERY": "0",  # no LAN chatter from a test run
        "MYFTP_SAVE_DIR": str(tmp_path / "inbox"),
        "FLET_LOG_LEVEL": "warning",
    }
    env.pop("FLET_APP_STORAGE_DATA", None)
    return env


class WebApp:
    """One app running under a real server, plus its process bookkeeping."""

    def __init__(self, port: int, proc: asyncio.subprocess.Process) -> None:
        self.port = port
        self.proc = proc
        self.output = b""

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def stop(self) -> None:
        self.proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.proc.wait(), 20)
        except TimeoutError:  # pragma: no cover - only if shutdown hangs
            self.proc.kill()
            await self.proc.wait()
        if self.proc.stdout is not None:
            self.output = await self.proc.stdout.read()

    def log(self) -> str:
        return self.output.decode(errors="replace")


@contextlib.asynccontextmanager
async def running_app(tmp_path: Path) -> AsyncIterator[WebApp]:
    port = free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(MAIN),
        cwd=str(REPO_ROOT),
        env=app_env(tmp_path, port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    app = WebApp(port, proc)
    try:
        await wait_for_server("127.0.0.1", port)
        yield app
    finally:
        await app.stop()


def decode(frame: bytes) -> Any:
    """Unpack one wire frame, tolerating anything that is not msgpack."""
    for candidate in (frame[1:], frame):
        try:
            return msgpack.unpackb(candidate, raw=False, strict_map_key=False)
        except Exception:  # pragma: no cover - only for non-patch frames
            continue
    return None


def walk(node: Any) -> Iterator[Any]:
    """Every value inside a decoded patch, depth first."""
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from walk(value)


class FletClient:
    """The slice of the Flutter client these tests need."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.frames: list[bytes] = []

    async def register(self) -> None:
        await self.ws.send(
            WIRE
            + msgpack.packb(
                [REGISTER_CLIENT, {"session_id": "", "page_name": "", "page": {}}]
            )
        )

    async def next_frame(
        self,
        predicate: Callable[[bytes], bool],
        *,
        timeout: float = PATCH_TIMEOUT,
        note: str = "",
    ) -> bytes:
        """Read frames until one satisfies ``predicate``; returns it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"no frame matched {note or predicate!r}; "
                    f"got {[f[:80] for f in self.frames]!r}"
                )
            try:
                frame = await asyncio.wait_for(self.ws.recv(), remaining)
            except TimeoutError:
                continue
            self.frames.append(frame)
            if predicate(frame):
                return frame

    def control_id(self, key: str) -> int:
        """The id of the control carrying ``key`` in everything seen so far."""
        for frame in self.frames:
            for node in walk(decode(frame)):
                if isinstance(node, dict) and node.get("key") == key:
                    return int(node["_i"])
        raise AssertionError(f"no control with key {key!r} in the patches")

    async def click(self, control_id: int) -> None:
        await self._send(
            CONTROL_EVENT, {"target": control_id, "name": "click", "data": None}
        )

    async def type_into(self, control_id: int, value: str) -> None:
        """What a client sends when a text field's value changes."""
        await self._send(
            UPDATE_CONTROL_PROPS, {"id": control_id, "props": {"value": value}}
        )

    async def _send(self, action: int, body: dict[str, Any]) -> None:
        await self.ws.send(WIRE + msgpack.packb([action, body]))


async def test_gui_boots_under_the_real_flet_server(tmp_path: Path) -> None:
    frames: list[bytes] = []
    async with running_app(tmp_path) as app:
        async with ws_connect(
            app.ws_url, open_timeout=BOOT_TIMEOUT, max_size=None
        ) as ws:
            client = FletClient(ws)
            await client.register()
            # read patches until the engine has started and repainted the header
            await client.next_frame(lambda frame: b"WEBBOX" in frame)
            frames = client.frames

    assert frames, (
        "the Flet server never sent a patch to the client:\n" + app.log()
    )
    blob = b"".join(frames)
    assert b"myftp" in blob, blob[:500]
    assert b"Recent activity" in blob  # the home screen really built
    assert b"Open the radar" in blob  # ... down to the buttons
    assert b"WEBBOX" in blob  # the engine started and the header was updated
    assert app.proc.returncode == 0, app.log()


async def test_renaming_round_trips_through_a_real_client(tmp_path: Path) -> None:
    """Click "Change name", type a name, save - the real protocol, twice over."""
    async with running_app(tmp_path) as app:
        async with ws_connect(
            app.ws_url, open_timeout=BOOT_TIMEOUT, max_size=None
        ) as ws:
            client = FletClient(ws)
            await client.register()
            await client.next_frame(lambda frame: b"WEBBOX" in frame)

            # the button really is in the tree, with a handler behind it
            await client.click(client.control_id("home-name-edit"))
            await client.next_frame(
                lambda frame: b"Your name" in frame, note="the rename dialog"
            )

            field = client.control_id("name-field")
            await client.type_into(field, "Ada Lovelace")
            await client.click(client.control_id("name-save"))
            await client.next_frame(
                lambda frame: b"Ada Lovelace" in frame, note="the repainted name"
            )

    # everything that is on screen was repainted: the header and the home strip
    # (the radar caption lives on the nearby screen, which is not mounted yet)
    blob = b"".join(client.frames)
    assert "Ada Lovelace · port".encode() in blob
    assert b"You appear to others as Ada Lovelace" in blob

    # ... and the choice was remembered for the next launch
    settings = Path(os.environ["HOME"]) / ".config" / "myftp" / "settings.json"
    assert "Ada Lovelace" in settings.read_text("utf-8")
    assert app.proc.returncode == 0, app.log()
