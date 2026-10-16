"""Tests for the ``p2p`` toolkit.

The toolkit is standard library only, so these tests need no Flet and no
network beyond loopback UDP/TCP. Run them with ``uv run pytest``.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct

import pytest

import p2p
from p2p import (
    MAGIC,
    Message,
    P2PConnectionError,
    P2PPayloadTooLarge,
    P2PProtocolError,
    PeerDiscovery,
    PeerServer,
    connect,
    encode,
    iter_messages,
    read_message,
    request,
    send,
)
from p2p.connection import Connection

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def reader_from(*frames: bytes) -> asyncio.StreamReader:
    """A StreamReader pre-loaded with ``frames`` and cleanly closed."""
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(frame)
    reader.feed_eof()
    return reader


async def wait_for(predicate, *, timeout: float = 3.0) -> None:
    """Poll until ``predicate()`` is true; fail the test if it never is."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition was not met in time"
        await asyncio.sleep(0.02)


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Collector:
    """Records everything a ``PeerServer`` receives, for assertions."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.events: list[str] = []
        self.errors: list[Exception] = []

    async def on_message(self, peer, message: Message) -> None:
        self.messages.append(message)

    async def on_connect(self, peer) -> None:
        self.events.append("connect")

    async def on_disconnect(self, peer) -> None:
        self.events.append("disconnect")

    async def on_error(self, peer, exc: Exception) -> None:
        self.errors.append(exc)


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------


async def test_message_roundtrip() -> None:
    original = Message(kind="chat", data=b"hello", meta={"n": 1, "who": "a"})
    reader = reader_from(encode(original))
    message = await read_message(reader)
    assert message == original


async def test_read_message_returns_none_on_clean_eof() -> None:
    assert await read_message(reader_from()) is None


async def test_read_message_rejects_bad_magic() -> None:
    with pytest.raises(P2PProtocolError):
        await read_message(reader_from(b"XXXXjunkjunk"))


async def test_read_message_rejects_truncated_frame() -> None:
    frame = encode(Message("a", b"payload"))
    with pytest.raises(P2PProtocolError):
        await read_message(reader_from(frame[: len(frame) - 3]))


async def test_read_message_rejects_oversized_payload() -> None:
    header = json.dumps({"kind": "big", "meta": {}, "len": 5000}).encode()
    frame = MAGIC + struct.pack("!I", len(header)) + header
    with pytest.raises(P2PPayloadTooLarge):
        await read_message(reader_from(frame), max_payload_bytes=100)


def test_encode_validation() -> None:
    with pytest.raises(P2PProtocolError):
        encode(Message(""))
    with pytest.raises(P2PPayloadTooLarge):
        encode(Message("big", b"x" * (p2p.DEFAULT_MAX_PAYLOAD_BYTES + 1)))


async def test_iter_messages_yields_until_eof() -> None:
    reader = reader_from(
        encode(Message("a")), encode(Message("b", b"2")), encode(Message("c"))
    )
    kinds = [message.kind async for message in iter_messages(reader)]
    assert kinds == ["a", "b", "c"]


# --------------------------------------------------------------------------
# PeerServer + send
# --------------------------------------------------------------------------


async def test_server_receives_and_replies() -> None:
    collector = Collector()

    async def on_message(peer, message: Message) -> None:
        await collector.on_message(peer, message)
        await peer.send(Message("pong", b"ok"))

    server = PeerServer(port=0, on_message=on_message)
    port = await server.start()
    try:
        async with connect("127.0.0.1", port) as conn:
            await conn.send(Message("ping", b"data", {"x": 1}))
            assert await conn.receive(timeout=2) == Message("pong", b"ok")
    finally:
        await server.stop()

    assert [m.kind for m in collector.messages] == ["ping"]
    assert collector.messages[0].data == b"data"


async def test_send_accepts_message_list_and_async_generator() -> None:
    collector = Collector()
    server = PeerServer(port=0, on_message=collector.on_message)
    port = await server.start()
    try:
        await send("127.0.0.1", port, Message("one", b"1"))
        await send("127.0.0.1", port, [Message("two", b"2"), Message("three", b"3")])

        async def frames():
            for i in range(2):
                yield Message("gen", str(i).encode())

        await send("127.0.0.1", port, frames())
        await wait_for(lambda: len(collector.messages) == 5)
    finally:
        await server.stop()

    assert [(m.kind, m.data) for m in collector.messages] == [
        ("one", b"1"),
        ("two", b"2"),
        ("three", b"3"),
        ("gen", b"0"),
        ("gen", b"1"),
    ]


async def test_send_times_out_when_the_peer_stalls(monkeypatch) -> None:
    server = PeerServer(port=0)
    port = await server.start()

    async def never_drains(self, message):
        await asyncio.sleep(30)  # simulate a transfer stuck on a dead peer

    # Patch the innermost write so the real timeout logic in `send_many` runs.
    monkeypatch.setattr(Connection, "send", never_drains)
    try:
        with pytest.raises(P2PConnectionError, match="timed out"):
            await send("127.0.0.1", port, Message("stuck"), timeout=0.05)
    finally:
        await server.stop()


async def test_send_timeout_none_means_unlimited(monkeypatch) -> None:
    server = PeerServer(port=0)
    port = await server.start()

    async def mildly_slow(self, message):
        await asyncio.sleep(0.1)

    monkeypatch.setattr(Connection, "send", mildly_slow)
    try:
        await send("127.0.0.1", port, Message("slow but fine"), timeout=None)
    finally:
        await server.stop()


async def test_connect_failure_is_normalised() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Nothing is listening on `port` any more.
    with pytest.raises(P2PConnectionError, match="could not connect"):
        async with connect("127.0.0.1", port, connect_timeout=1):
            pass


async def test_connection_receive_returns_none_when_peer_closes() -> None:
    async def on_connect(peer) -> None:
        peer.close()

    server = PeerServer(port=0, on_connect=on_connect)
    port = await server.start()
    try:
        async with connect("127.0.0.1", port) as conn:
            assert await conn.receive(timeout=2) is None
    finally:
        await server.stop()


async def test_connection_send_after_close_raises() -> None:
    server = PeerServer(port=0)
    port = await server.start()
    try:
        async with connect("127.0.0.1", port) as conn:
            await conn.close()
            assert conn.is_closing
            with pytest.raises(P2PConnectionError, match="closed"):
                await conn.send(Message("late"))
    finally:
        await server.stop()


async def test_connection_async_iteration() -> None:
    async def on_message(peer, message: Message) -> None:
        for i in range(3):
            await peer.send(Message("tick", str(i).encode()))
        peer.close()

    server = PeerServer(port=0, on_message=on_message)
    port = await server.start()
    try:
        async with connect("127.0.0.1", port) as conn:
            await conn.send(Message("go"))
            got = [message.data async for message in conn]
        assert got == [b"0", b"1", b"2"]
    finally:
        await server.stop()


# --------------------------------------------------------------------------
# request / broadcast
# --------------------------------------------------------------------------


async def test_request_gets_first_reply() -> None:
    async def on_message(peer, message: Message) -> None:
        await peer.send(Message("reply", b"pong"))

    server = PeerServer(port=0, on_message=on_message)
    port = await server.start()
    try:
        assert await request("127.0.0.1", port, Message("ping"), timeout=2) == Message(
            "reply", b"pong"
        )
    finally:
        await server.stop()


async def test_request_times_out_on_silent_peer() -> None:
    server = PeerServer(port=0)  # never replies
    port = await server.start()
    try:
        with pytest.raises(P2PConnectionError, match="no message"):
            await request("127.0.0.1", port, Message("ping"), timeout=0.1)
    finally:
        await server.stop()


async def test_server_broadcast_reaches_every_peer() -> None:
    server = PeerServer(port=0)
    port = await server.start()

    async def client() -> Message | None:
        async with connect("127.0.0.1", port) as conn:
            return await conn.receive(timeout=2)

    try:
        tasks = [asyncio.create_task(client()), asyncio.create_task(client())]
        await wait_for(lambda: len(server.peers) == 2)
        await server.broadcast(Message("news", b"hello everyone"))
        results = await asyncio.gather(*tasks)
        assert results == [Message("news", b"hello everyone")] * 2
    finally:
        await server.stop()


async def test_server_lifecycle_guards() -> None:
    server = PeerServer(port=0)
    port = await server.start()
    assert port > 0
    with pytest.raises(RuntimeError):
        await server.start()
    await server.stop()
    assert not server.is_running


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def beacon_payload(instance_id: str, *, service: str, port: int = 9100, **extra) -> bytes:
    payload = {
        "magic": "P2PD1",
        "id": instance_id,
        "service": service,
        "name": "TEST-PEER",
        "port": port,
        "meta": {"role": "sender"},
    }
    payload.update(extra)
    return json.dumps(payload).encode()


async def test_discovery_receives_handcrafted_beacon() -> None:
    discovery_port = free_udp_port()
    discovery = PeerDiscovery(
        "p2p-tests-beacon", port=1234, name="ME", discovery_port=discovery_port
    )
    await discovery.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(
                beacon_payload("other-id", service="p2p-tests-beacon"),
                ("127.0.0.1", discovery_port),
            )
        await wait_for(lambda: len(discovery.peers) == 1)
        info = discovery.peers[0]
        assert (info.host, info.port) == ("127.0.0.1", 9100)
        assert info.name == "TEST-PEER"
        assert info.meta == {"role": "sender"}
    finally:
        await discovery.stop()


async def test_discovery_ignores_foreign_and_own_beacons() -> None:
    discovery_port = free_udp_port()
    discovery = PeerDiscovery(
        "p2p-tests-filter", port=1234, discovery_port=discovery_port
    )
    await discovery.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(
                beacon_payload("foreign", service="some-other-app"),
                ("127.0.0.1", discovery_port),
            )
            sender.sendto(
                beacon_payload(discovery.instance_id, service="p2p-tests-filter"),
                ("127.0.0.1", discovery_port),
            )
        await asyncio.sleep(0.2)
        assert discovery.peers == []
    finally:
        await discovery.stop()


async def test_two_instances_discover_each_other() -> None:
    """Two instances on one machine - the case that used to fail on macOS."""
    discovery_port = free_udp_port()
    lost: list[str] = []
    a = PeerDiscovery(
        "p2p-tests-pair", port=9100, name="AAA", discovery_port=discovery_port,
        interval=30, ttl=1.0,
    )
    b = PeerDiscovery(
        "p2p-tests-pair", port=9101, name="BBB", discovery_port=discovery_port,
        interval=30, ttl=1.0, on_lost=lambda info: lost.append(info.name),
    )
    await a.start()
    try:
        await b.start()
    except OSError:
        await a.stop()
        pytest.skip("platform cannot bind two discovery sockets to one port")

    try:
        # `announce()` sends immediately instead of waiting `interval` seconds.
        a.update_name("AAA-V2")
        a.update_meta({"busy": True})
        a.announce()
        await wait_for(lambda: len(b.peers) == 1)
        seen = b.peers[0]
        assert seen.name == "AAA-V2"
        assert seen.meta == {"busy": True}
        # The beacon source address is the machine's LAN IP (broadcast does not
        # travel over loopback), whatever that is on this network.
        assert socket.inet_aton(seen.host)  # a valid IPv4 address
        assert seen.port == 9100

        # Going silent makes the peer expire after the TTL.
        await a.stop()
        await wait_for(lambda: lost != [], timeout=5.0)
        assert lost == ["AAA-V2"]
    finally:
        await a.stop()
        await b.stop()


async def test_malformed_datagrams_are_ignored() -> None:
    discovery_port = free_udp_port()
    discovery = PeerDiscovery(
        "p2p-tests-junk", port=1, discovery_port=discovery_port
    )
    await discovery.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"not json at all", ("127.0.0.1", discovery_port))
            sender.sendto(b'{"magic": "NOPE"}', ("127.0.0.1", discovery_port))
            sender.sendto(
                beacon_payload("x", service="p2p-tests-junk", port=0),
                ("127.0.0.1", discovery_port),
            )  # invalid TCP port
        await asyncio.sleep(0.2)
        assert discovery.peers == []
    finally:
        await discovery.stop()
