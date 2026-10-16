"""Head-less GUI tests: the real control tree, a stand-in page, real engines.

Flet's own ``flet_app`` fixture needs a Flutter test host (a full ``flet
build`` pipeline: Flutter SDK, native compile, ...), which is far too heavy to
require for a test run. These tests take the light path instead: they build
``app.ui.app`` against a small stand-in for ``ft.Page`` (recording dialogs,
services and tasks), then drive the app's public methods - exactly what the
buttons and rows call - against **real** ``TransferEngine`` instances talking
over loopback.

What is *not* covered here (real rendering, real tap routing) is covered by
``test_ui_web.py``, which boots the app under a real Flet server.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

import flet as ft
import pytest
from plain_peer import spawn_plain_peer

from app.core.config import AppConfig, Mode, load_settings
from app.core.engine import (
    Direction,
    Event,
    IncomingOffer,
    TransferEngine,
    TransferFinished,
    TransferProgress,
    TransferStarted,
)
from app.ui import theme
from app.ui.app import PULSE_MAX, PULSE_MIN, MyFtpApp, build_app


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
class FakePage:
    """The slice of ``ft.Page`` the GUI touches - plus the bits tests assert on."""

    def __init__(self) -> None:
        self.controls: list[ft.Control] = []
        self.services: list[Any] = []
        self.dialogs: list[ft.AlertDialog] = []
        self.window = ft.Window()
        self.navigation_bar: ft.NavigationBar | None = None
        self.title = ""
        self.theme: ft.Theme | None = None
        self.theme_mode: ft.ThemeMode | None = None
        self.bgcolor: str | None = None
        self.padding: int | None = None
        self.on_disconnect: Callable[..., Any] | None = None
        self.updates = 0
        self.tasks: list[asyncio.Future[Any]] = []

    # the page surface the app uses -------------------------------------
    def add(self, *controls: ft.Control) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        self.updates += 1

    def run_task(self, handler: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        future = asyncio.ensure_future(handler(*args, **kwargs))
        self.tasks.append(future)
        return future

    def show_dialog(self, dialog: ft.AlertDialog) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self) -> ft.AlertDialog | None:
        return self.dialogs.pop() if self.dialogs else None


class HeadlessPeer:
    """A peer with no UI at all: it accepts every offer it is given."""

    def __init__(self, config: AppConfig) -> None:
        self.engine = TransferEngine(config, self._on_event)

    def _on_event(self, event: Event) -> None:
        if isinstance(event, IncomingOffer):
            asyncio.ensure_future(self.engine.respond(event.tid, accept=True))


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    """A quiet config: no discovery, random ports, patient-enough timeouts."""
    base = AppConfig(
        mode=Mode.GUI,
        name="TESTBOX",
        port=0,
        discovery_enabled=False,
        save_dir=tmp_path / "inbox",
        offer_timeout=10.0,
        ack_timeout=10.0,
        connect_timeout=10.0,
    )
    return replace(base, **overrides)


def walk(control: Any) -> Iterator[Any]:
    """Every control reachable from ``control`` (containers, rows, dialogs)."""
    yield control
    for child in getattr(control, "controls", None) or ():
        yield from walk(child)
    for attribute in ("content", "title"):
        child = getattr(control, attribute, None)
        if isinstance(child, ft.Control):
            yield from walk(child)
    for action in getattr(control, "actions", None) or ():
        yield from walk(action)


def roots(page: FakePage) -> list[Any]:
    return [*page.controls, *page.dialogs]


def collect_texts(*controls: Any) -> list[str]:
    texts: list[str] = []
    for control in controls:
        for node in walk(control):
            if isinstance(node, ft.Text) and node.value:
                texts.append(node.value)
    return texts


def texts(page: FakePage) -> list[str]:
    return collect_texts(*roots(page))


def find(page: FakePage, key: str) -> Any:
    for root in roots(page):
        for node in walk(root):
            if getattr(node, "key", None) == key:
                return node
    return None


async def start_app(tmp_path: Path, **overrides: Any) -> tuple[MyFtpApp, FakePage]:
    """Build the GUI on a fake page and wait for its engine to be up."""
    page = FakePage()
    app = build_app(make_config(tmp_path, **overrides), page)
    assert page.tasks, "build_app should have scheduled MyFtpApp.start()"
    await asyncio.wait_for(page.tasks[-1], 10)
    return app, page


async def stop_app(app: MyFtpApp, page: FakePage) -> None:
    await app.engine.stop()
    for task in page.tasks:
        task.cancel()
    await asyncio.gather(*page.tasks, return_exceptions=True)


async def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out waiting for the app to react"
        await asyncio.sleep(0.02)


# --------------------------------------------------------------------------
# the tree itself
# --------------------------------------------------------------------------
async def test_initial_screen_is_the_home_screen(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        assert page.title == "myftp"
        assert page.navigation_bar is not None
        labels = [destination.label for destination in page.navigation_bar.destinations]
        assert labels == ["Home", "Nearby"]
        assert page.navigation_bar.selected_index == 0

        shown = texts(page)
        assert "myftp" in shown
        assert "Send files" in shown and "Receive" in shown
        assert "Recent activity" in shown
        assert any(f"port {app.engine.port}" in text for text in shown)
        assert str(app.config.save_dir) in " ".join(shown)

        # the radar is built but only mounted on the nearby screen
        assert find(page, "radar") is None
        app.select_tab(1)
        assert find(page, "radar") is not None
        assert page.navigation_bar.selected_index == 1
        shown = texts(page)
        assert "Nearby devices" in shown
        assert "No devices yet" in shown
    finally:
        await stop_app(app, page)


async def test_manual_peer_lands_on_the_radar_and_in_the_list(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        app.select_tab(1)
        assert app.connect_manual("not-an-address") is False
        assert app.connect_manual("192.168.1.9:9100") is True

        assert [peer.address for peer in app.peers] == ["192.168.1.9:9100"]
        assert find(page, "peer-192.168.1.9:9100") is not None
        assert "1 device in range" in texts(page)

        half = theme.RADAR_SIZE / 2
        chip = find(page, "radar-192.168.1.9:9100")
        assert chip is not None, "the device should have a chip on the radar"
        assert chip.left == pytest.approx(half - theme.RADAR_CHIP / 2, abs=1e-6)
        assert chip.top < half  # a single device always sits at the top
        assert chip.tooltip.startswith("192.168.1.9")
    finally:
        await stop_app(app, page)


async def test_the_name_can_be_edited_from_the_header(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        edit = find(page, "name-edit")
        assert edit is not None, "home needs a way into the rename dialog"
        assert edit.tooltip == "Change your name"

        # ... and it is not the only way in: the home strip names the device
        strip = find(page, "home-name-edit")
        assert strip is not None and strip.content.value == "Change name"
        app.select_tab(1)
        assert find(page, "nearby-name-edit").content.value == "You appear as TESTBOX"
        app.select_tab(0)

        app.ask_rename()
        assert page.dialogs, "the dialog should be open"
        assert app._name_field.value == "TESTBOX"  # prefilled with the current one

        app._name_field.value = "  Ada  Lovelace "
        app._submit_name()
        assert not page.dialogs  # saving closes the dialog
        assert app.engine.name == "Ada Lovelace"
        assert app._header_sub.value.startswith("Ada Lovelace · port ")
        assert app._header_avatar.content.value == "AL"
        assert app._home_identity.value == "You appear to others as Ada Lovelace"
        assert app._radar_you.content.value == "You appear as Ada Lovelace"
        assert load_settings()["name"] == "Ada Lovelace"
        assert any("Ada Lovelace" in text for text in texts(page))

        # an empty name is refused: the dialog stays, nothing changes
        app.ask_rename()
        app._name_field.value = "   "
        app._submit_name()
        assert page.dialogs, "the dialog should stay open"
        assert app._name_error is not None and app._name_error.visible
        assert app.engine.name == "Ada Lovelace"

        # ... and so does an outright empty rename outside the dialog
        app._close_dialog()
        assert app.rename("") == "Ada Lovelace"
        assert app._header_sub.value.startswith("Ada Lovelace · port ")
    finally:
        await stop_app(app, page)


async def test_navigation_change_routes_to_a_screen(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        await_to_radar = ft.ControlEvent(name="change", data="1", control=None)
        app._on_nav_change(await_to_radar)
        assert page.navigation_bar.selected_index == 1
        assert find(page, "radar") is not None

        back_home = ft.ControlEvent(name="change", data="0", control=None)
        app._on_nav_change(back_home)
        assert page.navigation_bar.selected_index == 0
        assert find(page, "radar") is None
    finally:
        await stop_app(app, page)


async def test_entering_the_radar_starts_the_animations(tmp_path: Path) -> None:
    """The sweep and the halo only move if their chain is started after mount."""
    app, page = await start_app(tmp_path)
    try:
        assert app._sweep.rotate.angle == 0.0
        app.select_tab(1)

        # the nudge lands a beat after the swap, once the client has the radar
        await wait_for(lambda: app._sweep.rotate.angle > 0.0)
        assert app._sweep.rotate.angle == pytest.approx(math.pi)
        assert app._pulse.scale == PULSE_MAX

        # every finished leg queues the next one (the cookbook's chaining)
        app._on_sweep_end(
            ft.ControlEvent(name="animation_end", data="rotation", control=app._sweep)
        )
        assert app._sweep.rotate.angle == pytest.approx(2 * math.pi)
        app._on_pulse_end(
            ft.ControlEvent(name="animation_end", data="scale", control=app._pulse)
        )
        assert app._pulse.scale == PULSE_MIN

        # leaving the tab tears the radar down, so coming back starts it again
        app.select_tab(0)
        app.select_tab(1)
        before = app._sweep.rotate.angle
        await wait_for(lambda: app._sweep.rotate.angle > before)
    finally:
        await stop_app(app, page)


# --------------------------------------------------------------------------
# transfers, end to end (GUI <-> plain peer over loopback)
# --------------------------------------------------------------------------
async def test_incoming_offer_becomes_a_dialog_and_a_saved_file(
    tmp_path: Path,
) -> None:
    app, page = await start_app(tmp_path)
    sender = HeadlessPeer(make_config(tmp_path, name="SENDER"))
    await sender.engine.start()
    try:
        source = tmp_path / "notes.txt"
        payload = b"hello from the other side\n" * 256
        source.write_bytes(payload)

        send_task = asyncio.ensure_future(
            sender.engine.send_file("127.0.0.1", app.engine.port, source)
        )
        await wait_for(lambda: bool(page.dialogs))

        dialog = page.dialogs[-1]
        shown = collect_texts(dialog)
        assert "Incoming file" in shown
        assert "notes.txt" in shown
        assert "SENDER" in shown
        assert str(app.config.save_dir) in " ".join(shown)

        await app.respond_offer(True)
        assert page.dialogs == []  # the prompt is gone

        await asyncio.wait_for(send_task, 10)
        await wait_for(lambda: bool(app.activity))
        saved = app.config.save_dir / "notes.txt"
        assert saved.read_bytes() == payload

        assert app.activity[0].ok is True
        assert app.activity[0].title == "Received notes.txt"
        assert app.activity[0].detail == f"Saved to {saved}"
        assert "Saved notes.txt" in app._banner_title.value
        assert str(saved) in app._banner_detail.value
        assert app.transfer is None
    finally:
        await sender.engine.stop()
        await stop_app(app, page)


async def test_declining_an_offer_leaves_no_file(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    sender = HeadlessPeer(make_config(tmp_path, name="SENDER"))
    await sender.engine.start()
    try:
        source = tmp_path / "pushy.bin"
        source.write_bytes(b"x" * 4096)
        send_task = asyncio.ensure_future(
            sender.engine.send_file("127.0.0.1", app.engine.port, source)
        )
        await wait_for(lambda: bool(page.dialogs))

        await app.respond_offer(False)
        await asyncio.wait_for(send_task, 10)

        assert not (app.config.save_dir / "pushy.bin").exists()
        assert any(not item.ok for item in app.activity)
    finally:
        await sender.engine.stop()
        await stop_app(app, page)


async def test_sending_from_the_gui_uses_the_picker_and_reports_delivery(
    tmp_path: Path,
) -> None:
    receiver = HeadlessPeer(
        make_config(tmp_path, name="RECEIVER", save_dir=tmp_path / "receiver")
    )
    await receiver.engine.start()
    app, page = await start_app(
        tmp_path, manual_peers=(("127.0.0.1", receiver.engine.port),)
    )
    try:
        source = tmp_path / "report.md"
        payload = b"# report\n" * 512
        source.write_bytes(payload)

        picked: list[bool] = []

        async def fake_pick() -> str:
            picked.append(True)
            return str(source)

        app._choose_file = fake_pick  # type: ignore[method-assign]

        peer = app.peers[0]
        assert peer.port == receiver.engine.port
        app.pick_peer(peer)
        assert "Send to 127.0.0.1" in collect_texts(page.dialogs[-1])

        await app.choose_and_send(peer)
        delivered = receiver.engine.config.save_dir / "report.md"
        await wait_for(delivered.exists)

        assert picked == [True]
        assert delivered.read_bytes() == payload
        await wait_for(lambda: bool(app.activity))
        assert app.activity[0].ok is True
        assert app.activity[0].title == "Sent report.md"
        assert app._banner_title.value.startswith("Delivered to 127.0.0.1")
        assert page.dialogs == []
    finally:
        await receiver.engine.stop()
        await stop_app(app, page)


# --------------------------------------------------------------------------
# a real terminal peer talking to the GUI
# --------------------------------------------------------------------------
async def test_a_terminal_peer_sends_to_the_gui_app(tmp_path: Path) -> None:
    """The product story end to end: a peer without Flet offers us a file."""
    app, page = await start_app(tmp_path)
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    source = plain_dir / "photo.jpg"
    payload = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 2048  # ~512 KB
    source.write_bytes(payload)

    peer = await spawn_plain_peer("PLAINPEER", tmp_path, plain_dir)
    try:
        await peer.send(f"connect 127.0.0.1:{app.engine.port}")
        await peer.read_until(lambda line: line.startswith("added "))

        await peer.send(f"send 1 {source}")
        await wait_for(lambda: bool(page.dialogs))
        shown = collect_texts(page.dialogs[-1])
        assert "Incoming file" in shown
        assert "PLAINPEER" in shown
        assert "photo.jpg" in shown

        await app.respond_offer(True)
        await peer.read_until(
            lambda line: line.startswith("[finished] ok"), timeout=20
        )

        saved = app.config.save_dir / "photo.jpg"
        await wait_for(saved.exists)
        assert saved.read_bytes() == payload
        assert not list(app.config.save_dir.glob("*.part"))
        await wait_for(lambda: bool(app.activity))
        assert app.activity[0].title == "Received photo.jpg"
        assert app.activity[0].ok is True
    finally:
        await peer.quit()
        await stop_app(app, page)


# --------------------------------------------------------------------------
# the banner
# --------------------------------------------------------------------------
async def test_banner_follows_engine_events(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        app.on_event(
            TransferStarted(
                tid="t1",
                direction=Direction.RECEIVE,
                filename="movie.mp4",
                size=1000,
                peer="SAM",
            )
        )
        assert app._banner.visible is True
        assert app._banner_title.value == "Receiving movie.mp4"
        assert app._banner_button.content.value == "Cancel"

        app.on_event(
            TransferProgress(
                tid="t1", direction=Direction.RECEIVE, done=500, total=1000
            )
        )
        assert "50%" in app._banner_detail.value
        assert app._banner_bar.value == pytest.approx(0.5)

        app.on_event(
            TransferFinished(
                tid="t1",
                direction=Direction.RECEIVE,
                ok=False,
                message="Connection lost",
            )
        )
        assert app._banner_title.value == "Connection lost"
        assert app._banner_button.content.value == "Dismiss"
        assert app.activity[0].title == "Received movie.mp4"
        assert app.activity[0].detail == "Connection lost"

        # a dismiss tap hides the strip
        app._on_banner_button(None)
        assert app._banner.visible is False
    finally:
        await stop_app(app, page)


# --------------------------------------------------------------------------
# dialogs that do not need the network
# --------------------------------------------------------------------------
async def test_receive_info_and_manual_dialogs_open(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        app.show_receive_info()
        assert str(app.config.save_dir) in " ".join(collect_texts(page.dialogs[-1]))
        app._close_dialog()
        assert page.dialogs == []

        app.ask_manual_peer()
        assert "Connect by address" in collect_texts(page.dialogs[-1])

        # a bad address paints an error inside the open dialog
        app._manual_field.value = "nonsense"
        app._submit_manual()
        assert app._manual_error.visible is True
        assert "HOST:PORT" in app._manual_error.value

        # a good one registers the peer, closes the dialog and shows the radar
        app._manual_field = ft.TextField(value="10.0.0.5:9200")
        app._submit_manual()
        assert page.dialogs == []
        assert page.navigation_bar.selected_index == 1
        assert [peer.address for peer in app.peers] == ["10.0.0.5:9200"]
    finally:
        await stop_app(app, page)


async def test_banner_cancel_asks_the_engine(tmp_path: Path) -> None:
    app, page = await start_app(tmp_path)
    try:
        app.on_event(
            TransferStarted(
                tid="t-cancel",
                direction=Direction.SEND,
                filename="huge.iso",
                size=10_000,
                peer="SAM",
            )
        )
        before = len(page.tasks)
        app._on_banner_button(None)  # the running transfer's Cancel button
        await asyncio.sleep(0)
        assert len(page.tasks) > before  # engine.cancel() was scheduled
        assert app._banner_button.content.value == "Cancel"
    finally:
        await stop_app(app, page)


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def test_initials() -> None:
    assert theme.initials("DAVIDS-MACBOOK") == "DM"
    assert theme.initials("laptop") == "LA"
    assert theme.initials("") == "?"
    assert theme.initials("pi.local") == "PL"


def test_radar_geometry_spreads_devices_evenly() -> None:
    assert theme.device_angle(0, 4) == pytest.approx(-math.pi / 2)
    angles = {round(theme.device_angle(index, 4), 6) for index in range(4)}
    assert len(angles) == 4

    for index in range(4):
        dx, dy = theme.device_offset(index, 4, radius=10)
        assert math.hypot(dx, dy) == pytest.approx(10)

    dx, dy = theme.device_offset(0, 4, radius=10)
    assert dx == pytest.approx(0, abs=1e-9)
    assert dy == pytest.approx(-10)
