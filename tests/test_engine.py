"""Loopback tests for the transfer engine (no Flet, real TCP on 127.0.0.1)."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from p2p import connect

from app.core import protocol
from app.core.config import AppConfig, Mode, load_settings
from app.core.engine import (
    IncomingOffer,
    PeersChanged,
    TransferFinished,
    TransferProgress,
    TransferReply,
    TransferStarted,
    TransferEngine,
)


class Events:
    """Collects engine events; little helpers keep the tests readable."""

    def __init__(self) -> None:
        self.items: list = []

    def __call__(self, event) -> None:
        self.items.append(event)

    def of(self, cls):
        return [event for event in self.items if isinstance(event, cls)]

    async def wait(self, predicate, *, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate(self.items):
            assert loop.time() < deadline, f"event never arrived; got {self.items!r}"
            await asyncio.sleep(0.01)


def make_config(save_dir: Path, name: str, **overrides) -> AppConfig:
    defaults = dict(
        mode=Mode.PLAIN,
        name=name,
        port=0,
        discovery_enabled=False,
        save_dir=save_dir,
        chunk_size=8 * 1024,
        offer_timeout=3.0,
        ack_timeout=3.0,
        connect_timeout=3.0,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


@pytest.fixture
async def pair(tmp_path: Path):
    """A ready sender engine and receiver engine, on loopback ports of their own."""
    (tmp_path / "sender").mkdir()
    (tmp_path / "receiver").mkdir()
    sender_events, receiver_events = Events(), Events()
    sender = TransferEngine(make_config(tmp_path / "sender", "SENDER"), sender_events)
    receiver = TransferEngine(
        make_config(tmp_path / "receiver", "RECEIVER"), receiver_events
    )
    await sender.start()
    await receiver.start()
    try:
        yield sender, sender_events, receiver, receiver_events
    finally:
        await sender.stop()
        await receiver.stop()


async def auto_accept(receiver: TransferEngine, events: Events) -> str:
    """Wait for the next offer and accept it; returns the tid."""
    await events.wait(lambda items: any(isinstance(e, IncomingOffer) for e in items))
    tid = events.of(IncomingOffer)[0].tid
    await receiver.respond(tid, accept=True)
    return tid


async def test_roundtrip_delivers_the_file(pair, tmp_path: Path) -> None:
    sender, sender_events, receiver, receiver_events = pair
    payload = bytes(range(256)) * 500  # 128 KB -> several 8 KB chunks
    source = tmp_path / "sender" / "holiday.jpg"
    source.write_bytes(payload)

    send_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, source)
    )
    tid = await auto_accept(receiver, receiver_events)
    await send_task
    await receiver_events.wait(
        lambda items: any(isinstance(e, TransferFinished) for e in items)
    )

    saved = tmp_path / "receiver" / "holiday.jpg"
    assert saved.read_bytes() == payload
    assert not list((tmp_path / "receiver").glob("*.part"))

    # -- receiver side
    offer = receiver_events.of(IncomingOffer)[0]
    assert (offer.filename, offer.size, offer.sender) == (
        "holiday.jpg",
        len(payload),
        "SENDER",
    )
    assert receiver_events.of(TransferStarted)[0].direction.value == "receive"
    finished = receiver_events.of(TransferFinished)[0]
    assert finished.ok and finished.path == saved

    # -- sender side
    assert sender_events.of(TransferReply) == [TransferReply(tid=tid, accepted=True)]
    sender_finished = sender_events.of(TransferFinished)[0]
    assert sender_finished.ok and sender_finished.message == "Delivered"
    progress = sender_events.of(TransferProgress)
    assert progress and progress[-1].done == len(payload)
    assert all(p.direction.value == "send" for p in progress)


async def test_decline_leaves_no_file(pair, tmp_path: Path) -> None:
    sender, sender_events, receiver, receiver_events = pair
    source = tmp_path / "sender" / "unwanted.bin"
    source.write_bytes(b"nothing personal")

    send_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, source)
    )
    await receiver_events.wait(
        lambda items: any(isinstance(e, IncomingOffer) for e in items)
    )
    tid = receiver_events.of(IncomingOffer)[0].tid
    await receiver.respond(tid, accept=False, reason="Not now")
    await send_task
    await receiver_events.wait(
        lambda items: any(isinstance(e, TransferFinished) for e in items)
    )

    assert list((tmp_path / "receiver").iterdir()) == []
    assert sender_events.of(TransferReply) == [
        TransferReply(tid=tid, accepted=False, reason="Not now")
    ]
    assert not sender_events.of(TransferFinished)[0].ok
    assert receiver_events.of(TransferFinished)[0].message == "Not now"


async def test_missing_source_file_reports_finished_event(pair, tmp_path: Path) -> None:
    sender, sender_events, _receiver, _receiver_events = pair
    await sender.send_file("127.0.0.1", 1, tmp_path / "does-not-exist.bin")
    finished = sender_events.of(TransferFinished)[0]
    assert not finished.ok and "Cannot read" in finished.message


async def test_second_offer_while_busy_is_declined(pair, tmp_path: Path) -> None:
    sender, _sender_events, receiver, receiver_events = pair
    first = tmp_path / "sender" / "first.bin"
    first.write_bytes(b"a" * 100)
    second = tmp_path / "sender" / "second.bin"
    second.write_bytes(b"b" * 100)

    first_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, first)
    )
    await receiver_events.wait(
        lambda items: any(isinstance(e, IncomingOffer) for e in items)
    )

    other_events = Events()
    other_sender = TransferEngine(make_config(tmp_path / "sender", "OTHER"), other_events)
    await other_sender.start()
    try:
        await other_sender.send_file("127.0.0.1", receiver.port, second)
    finally:
        await other_sender.stop()

    busy_finished = other_events.of(TransferFinished)[0]
    assert not busy_finished.ok and "busy" in busy_finished.message

    await auto_accept(receiver, receiver_events)
    await first_task  # the first transfer still completes normally


async def test_sender_cancel_while_waiting_for_the_answer(pair, tmp_path: Path) -> None:
    sender, sender_events, receiver, receiver_events = pair
    source = tmp_path / "sender" / "slow.bin"
    source.write_bytes(b"x" * 1000)

    send_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, source)
    )
    await receiver_events.wait(
        lambda items: any(isinstance(e, IncomingOffer) for e in items)
    )
    tid = receiver_events.of(IncomingOffer)[0].tid

    await sender.cancel(tid)
    await send_task
    await receiver_events.wait(
        lambda items: any(isinstance(e, TransferFinished) for e in items)
    )

    assert sender_events.of(TransferFinished)[0].message == "Cancelled"
    assert not receiver_events.of(TransferFinished)[0].ok


async def test_receiver_cancel_mid_stream(pair, tmp_path: Path, monkeypatch) -> None:
    sender, sender_events, receiver, receiver_events = pair
    source = tmp_path / "sender" / "big.bin"
    source.write_bytes(b"z" * (1024 * 1024))

    send_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, source)
    )
    await receiver_events.wait(
        lambda items: any(isinstance(e, IncomingOffer) for e in items)
    )
    tid = receiver_events.of(IncomingOffer)[0].tid

    # Pull the plug after a few chunks: deterministic mid-stream cancellation.
    original = TransferEngine._handle_chunk
    seen = {"chunks": 0}

    async def cancel_after_three(self, peer, message):
        await original(self, peer, message)
        seen["chunks"] += 1
        if seen["chunks"] == 3:
            asyncio.get_running_loop().create_task(self.cancel(tid))

    monkeypatch.setattr(TransferEngine, "_handle_chunk", cancel_after_three)
    await receiver.respond(tid, accept=True)
    await send_task
    await receiver_events.wait(
        lambda items: any(isinstance(e, TransferFinished) for e in items)
    )

    receiver_finished = receiver_events.of(TransferFinished)[0]
    assert not receiver_finished.ok
    assert list((tmp_path / "receiver").iterdir()) == []  # no .part, no file
    assert not sender_events.of(TransferFinished)[0].ok


async def test_incomplete_transfer_is_discarded(pair, tmp_path: Path) -> None:
    _sender, _sender_events, receiver, receiver_events = pair
    tid = "raw-incomplete"
    accept_task = asyncio.create_task(auto_accept(receiver, receiver_events))
    async with connect("127.0.0.1", receiver.port) as conn:
        await conn.send(protocol.offer(tid, "data.bin", 100, "LIAR"))
        reply = await conn.receive(timeout=3)
        assert reply.kind == protocol.KIND_ACCEPT
        await conn.send(protocol.file_header(tid, "data.bin", 100))
        await conn.send(protocol.chunk(tid, b"x" * 40))  # only 40 of 100 bytes
        await conn.send(protocol.done(tid))
    await accept_task
    await receiver_events.wait(
        lambda items: any(isinstance(e, TransferFinished) for e in items)
    )

    finished = receiver_events.of(TransferFinished)[0]
    assert not finished.ok and "Incomplete" in finished.message
    assert list((tmp_path / "receiver").iterdir()) == []


async def test_overflowing_sender_is_dropped(pair, tmp_path: Path) -> None:
    _sender, _sender_events, receiver, receiver_events = pair
    tid = "raw-overflow"
    accept_task = asyncio.create_task(auto_accept(receiver, receiver_events))
    async with connect("127.0.0.1", receiver.port) as conn:
        await conn.send(protocol.offer(tid, "data.bin", 10, "LIAR"))
        assert (await conn.receive(timeout=3)).kind == protocol.KIND_ACCEPT
        await conn.send(protocol.file_header(tid, "data.bin", 10))
        await conn.send(protocol.chunk(tid, b"x" * 8))
        await conn.send(protocol.chunk(tid, b"x" * 8))  # 16 > 10 announced
    await accept_task
    await receiver_events.wait(
        lambda items: any(isinstance(e, TransferFinished) for e in items)
    )

    finished = receiver_events.of(TransferFinished)[0]
    assert not finished.ok and "more data" in finished.message
    assert list((tmp_path / "receiver").iterdir()) == []


async def test_hostile_filename_is_sanitized_and_collisions_avoided(
    pair, tmp_path: Path
) -> None:
    _sender, _sender_events, receiver, receiver_events = pair
    (tmp_path / "receiver" / "photo.jpg").write_bytes(b"the original")

    tid = "raw-evil"
    accept_task = asyncio.create_task(auto_accept(receiver, receiver_events))
    async with connect("127.0.0.1", receiver.port) as conn:
        await conn.send(protocol.offer(tid, "../../photo.jpg", 4, "EVIL"))
        assert (await conn.receive(timeout=3)).kind == protocol.KIND_ACCEPT
        await conn.send(protocol.file_header(tid, "../../photo.jpg", 4))
        await conn.send(protocol.chunk(tid, b"new!"))
        await conn.send(protocol.done(tid))
        assert (await conn.receive(timeout=3)).kind == protocol.KIND_SAVED
    await accept_task

    assert (tmp_path / "receiver" / "photo.jpg").read_bytes() == b"the original"
    assert (tmp_path / "receiver" / "photo (1).jpg").read_bytes() == b"new!"
    # Nothing escaped the save folder.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["receiver", "sender"]


async def test_unknown_tid_data_is_ignored(pair) -> None:
    _sender, _sender_events, receiver, receiver_events = pair
    async with connect("127.0.0.1", receiver.port) as conn:
        await conn.send(protocol.chunk("never-offered", b"junk"))
        await conn.send(protocol.done("never-offered"))
    await asyncio.sleep(0.1)
    assert receiver.server is not None and receiver.server.is_running
    assert receiver_events.of(TransferFinished) == []


async def test_manual_peer_is_announced(pair) -> None:
    sender, sender_events, _receiver, _receiver_events = pair
    sender.add_manual_peer("10.1.2.3", 9100, "FOO")
    assert [peer.name for peer in sender.peers] == ["FOO"]
    last = sender_events.of(PeersChanged)[-1]
    assert [peer.name for peer in last.peers] == ["FOO"]
    assert last.peers[0].meta == {"manual": True}


async def test_busy_tcp_port_falls_back_to_a_random_one(tmp_path: Path) -> None:
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]

        engine = TransferEngine(make_config(tmp_path, "FALLBACK", port=busy_port), Events())
        await engine.start()
        try:
            assert engine.port != busy_port
            assert engine.port > 0
        finally:
            await engine.stop()


async def test_stop_aborts_a_pending_offer(pair, tmp_path: Path) -> None:
    sender, sender_events, receiver, receiver_events = pair
    source = tmp_path / "sender" / "interrupted.bin"
    source.write_bytes(b"x" * 100)

    send_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, source)
    )
    await receiver_events.wait(
        lambda items: any(isinstance(e, IncomingOffer) for e in items)
    )

    await receiver.stop()
    await send_task  # sender notices the receiver went away

    assert not sender_events.of(TransferFinished)[0].ok
    receiver_finished = receiver_events.of(TransferFinished)
    assert receiver_finished and not receiver_finished[0].ok


# --------------------------------------------------------------------------
# the display name can change while the app is running
# --------------------------------------------------------------------------
class FakeDiscovery:
    """Stands in for the UDP announcer: records what the engine asks of it."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.announcements = 0

    def update_name(self, name: str) -> None:
        self.names.append(name)

    def announce(self) -> None:
        self.announcements += 1

    async def stop(self) -> None:  # the fixture tears the engine down
        pass


async def test_renaming_reaches_beacons_offers_and_disk(
    pair, tmp_path: Path
) -> None:
    sender, _sender_events, receiver, receiver_events = pair
    assert sender.name == "SENDER"

    discovery = FakeDiscovery()
    sender.discovery = discovery

    # names are tidied: single spaces, no stray ends
    assert sender.set_name("  Ada   Lovelace  ") == "Ada Lovelace"
    assert sender.name == "Ada Lovelace"
    assert discovery.names == ["Ada Lovelace"]  # the beacon carries it ...
    assert discovery.announcements == 1  # ... and goes out right away
    assert load_settings()["name"] == "Ada Lovelace"  # remembered for next time

    # empty input keeps the name that is in use - and stays quiet about it
    assert sender.set_name("   ") == "Ada Lovelace"
    assert sender.name == "Ada Lovelace"
    assert discovery.names == ["Ada Lovelace"]
    assert discovery.announcements == 1

    # the new name travels with the next offer
    source = tmp_path / "sender" / "note.txt"
    source.write_bytes(b"hello")
    send_task = asyncio.create_task(
        sender.send_file("127.0.0.1", receiver.port, source)
    )
    await receiver_events.wait(
        lambda items: any(isinstance(e, IncomingOffer) for e in items)
    )
    offer = receiver_events.of(IncomingOffer)[0]
    assert offer.sender == "Ada Lovelace"
    await receiver.respond(offer.tid, accept=False)
    await send_task

    # an engine without discovery renames just as happily
    assert receiver.set_name("Receiver") == "Receiver"
    assert receiver.name == "Receiver"
