"""Transfer engine: LAN discovery, a listening server, and the transfer state
machines for sending and receiving.

The engine is pure ``asyncio`` + ``p2p``: it never prints, never reads input
and never imports Flet. Front-ends subscribe by passing an ``on_event``
callback and drive it by calling methods in response to user input::

    engine -> UI   PeersChanged | IncomingOffer | TransferStarted
                   TransferReply | TransferProgress | TransferFinished
    UI -> engine   send_file() | respond() | cancel() | add_manual_peer()

Everything happens on the one asyncio loop, so an event callback may touch UI
state directly (and, in Flet, call ``update()``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from p2p import (
    Connection,
    Message,
    P2PConnectionError,
    Peer,
    PeerDiscovery,
    PeerInfo,
    PeerServer,
    connect,
)

from . import protocol
from .config import SERVICE, AppConfig, clean_name, save_setting

logger = logging.getLogger("myftp.engine")

# Progress events are throttled to keep every front-end cheap: at most one per
# megabyte, and at most ten per second, unless the transfer just finished.
_PROGRESS_MIN_BYTES = 1 << 20
_PROGRESS_MIN_INTERVAL = 0.1


class Direction(StrEnum):
    """Which way the bytes travel."""

    SEND = "send"
    RECEIVE = "receive"


# --------------------------------------------------------------------------
# events (engine -> UI)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeersChanged:
    """The peer list changed: somebody appeared, vanished or was added by hand."""

    peers: tuple[PeerInfo, ...]


@dataclass(frozen=True, slots=True)
class IncomingOffer:
    """A remote peer is asking "may I send you this file?" - answer with
    :meth:`TransferEngine.respond`."""

    tid: str
    filename: str
    size: int
    sender: str
    address: str


@dataclass(frozen=True, slots=True)
class TransferStarted:
    """A transfer became real (we offered / we accepted)."""

    tid: str
    direction: Direction
    filename: str
    size: int
    peer: str


@dataclass(frozen=True, slots=True)
class TransferReply:
    """The peer answered our offer."""

    tid: str
    accepted: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """Throttled progress report (see module docstring)."""

    tid: str
    direction: Direction
    done: int
    total: int


@dataclass(frozen=True, slots=True)
class TransferFinished:
    """Terminal state of a transfer. ``ok`` says whether it worked."""

    tid: str
    direction: Direction
    ok: bool
    message: str
    path: Path | None = None


#: Everything the engine can emit.
Event = (
    PeersChanged
    | IncomingOffer
    | TransferStarted
    | TransferReply
    | TransferProgress
    | TransferFinished
)


# --------------------------------------------------------------------------
# internal transfer bookkeeping
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Outgoing:
    tid: str
    host: str
    port: int
    path: Path
    size: int
    filename: str
    conn: Connection | None = None
    cancelled: bool = False
    sent: int = 0
    last_emit_bytes: int = 0
    last_emit_time: float = 0.0


@dataclass(slots=True)
class _Incoming:
    tid: str
    filename: str
    size: int
    sender: str
    peer: Peer
    accepted: bool = False
    cancelled: bool = False
    target: Path | None = None
    part: Path | None = None
    file: BinaryIO | None = None
    received: int = 0
    last_emit_bytes: int = 0
    last_emit_time: float = 0.0


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------


class TransferEngine:
    """Owns the network side of myftp; front-ends only translate events."""

    def __init__(
        self,
        config: AppConfig,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.config = config
        self._on_event = on_event
        self._name = config.name
        self.server: PeerServer | None = None
        self.discovery: PeerDiscovery | None = None
        self.discovery_error: str | None = None
        self.port: int = config.port
        self._outgoing: dict[str, _Outgoing] = {}
        self._incoming: dict[str, _Incoming] = {}
        self._manual: dict[tuple[str, int], PeerInfo] = {}

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Bind the listening socket and start announcing/scanning the LAN."""
        if self.server is not None:
            raise RuntimeError("engine is already started")

        self.server = PeerServer(
            port=self.config.port,
            on_message=self._on_message,
            on_disconnect=self._on_disconnect,
            on_error=self._on_error,
        )
        try:
            self.port = await self.server.start()
        except OSError as exc:
            logger.warning(
                "TCP port %s is unavailable (%s); using a random one",
                self.config.port,
                exc,
            )
            self.port = await self.server.start(port=0)

        if self.config.discovery_enabled:
            self.discovery = PeerDiscovery(
                SERVICE,
                port=self.port,
                name=self.config.name,
                discovery_port=self.config.discovery_port,
                on_peer=self._on_peers_changed,
                on_lost=self._on_peers_changed,
            )
            try:
                await self.discovery.start()
                self.discovery.announce()  # show up immediately, not in 2 s
            except OSError as exc:
                # A taken UDP port only costs us automatic discovery; manual
                # connections still work, so this is a warning and not fatal.
                self.discovery_error = f"LAN discovery is unavailable: {exc}"
                self.discovery = None
                logger.warning("discovery disabled: %s", exc)

        for host, port in self.config.manual_peers:
            self.add_manual_peer(host, port)

    async def stop(self) -> None:
        """Cancel everything in flight and close every socket."""
        for out in list(self._outgoing.values()):
            out.cancelled = True
            if out.conn is not None:
                await out.conn.close()
        for incoming in list(self._incoming.values()):
            await self._abort_incoming(incoming, "Cancelled")
        if self.discovery is not None:
            await self.discovery.stop()
            self.discovery = None
        if self.server is not None:
            await self.server.stop()
            self.server = None

    # -- peers ---------------------------------------------------------------
    @property
    def name(self) -> str:
        """The display name peers see (may differ from the one we started with)."""
        return self._name

    def set_name(self, name: str) -> str:
        """Change the display name used in beacons and offers, and remember it.

        Empty input keeps the current name. The new name goes out in the next
        beacon - announced right away, so peers do not have to wait - and into
        every offer we send from now on. Returns the cleaned name.
        """
        clean = clean_name(name, self._name)
        if clean == self._name:
            return clean  # nothing to tell the world (or the disk) about
        self._name = clean
        if self.discovery is not None:
            self.discovery.update_name(clean)
            self.discovery.announce()
        save_setting("name", clean)
        return clean

    @property
    def peers(self) -> tuple[PeerInfo, ...]:
        """Discovered peers plus manual ones (a discovered entry wins)."""
        merged: dict[tuple[str, int], PeerInfo] = {}
        if self.discovery is not None:
            for info in self.discovery.peers:
                merged[info.key] = info
        for key, info in self._manual.items():
            merged.setdefault(key, info)
        return tuple(
            sorted(merged.values(), key=lambda info: (info.name.lower(), info.host))
        )

    def add_manual_peer(self, host: str, port: int, name: str = "") -> PeerInfo:
        """Register a peer typed in by hand (discovery not required)."""
        info = PeerInfo(host=host, port=port, name=name or host, meta={"manual": True})
        self._manual[info.key] = info
        self._on_peers_changed()
        return info

    def _on_peers_changed(self, _info: PeerInfo | None = None) -> None:
        # Doubles as the discovery on_peer/on_lost callback (it passes a PeerInfo
        # we don't need - the engine always reports the whole list).
        self._emit(PeersChanged(peers=self.peers))

    # -- sending -------------------------------------------------------------
    async def send_file(self, host: str, port: int, path: str | Path) -> None:
        """Offer ``path`` to ``host:port`` and, if accepted, stream it there.

        This runs the whole send conversation; callers normally run it as a
        task (``asyncio.create_task`` / ``page.run_task``). Failures never
        raise: they arrive as ``TransferFinished(ok=False, ...)``.
        """
        path = Path(path)
        tid = uuid.uuid4().hex
        try:
            size = path.stat().st_size
        except OSError as exc:
            self._emit(
                TransferFinished(
                    tid=tid,
                    direction=Direction.SEND,
                    ok=False,
                    message=f"Cannot read {path}: {exc}",
                )
            )
            return

        out = _Outgoing(
            tid=tid,
            host=host,
            port=port,
            path=path,
            size=size,
            filename=path.name,
        )
        self._outgoing[tid] = out
        self._emit(
            TransferStarted(
                tid=tid,
                direction=Direction.SEND,
                filename=out.filename,
                size=size,
                peer=f"{host}:{port}",
            )
        )
        try:
            await self._run_send(out)
        except P2PConnectionError as exc:
            self._finish_send(out, "Cancelled" if out.cancelled else str(exc))
        except OSError as exc:
            # A disk error while reading the file, for example.
            failure = "Cancelled" if out.cancelled else f"Transfer failed: {exc}"
            self._finish_send(out, failure)
        finally:
            self._outgoing.pop(tid, None)

    async def _run_send(self, out: _Outgoing) -> None:
        async with connect(
            out.host, out.port, connect_timeout=self.config.connect_timeout
        ) as conn:
            out.conn = conn
            await conn.send(
                protocol.offer(out.tid, out.filename, out.size, self.name)
            )
            reply = await conn.receive(timeout=self.config.offer_timeout)

            if reply is None:
                message = (
                    "Cancelled"
                    if out.cancelled
                    else "Peer disconnected before answering"
                )
                self._finish_send(out, message)
                return
            if reply.kind == protocol.KIND_DECLINE:
                reason = str(reply.meta.get("reason") or "Declined")
                self._emit(
                    TransferReply(tid=out.tid, accepted=False, reason=reason)
                )
                self._finish_send(out, reason)
                return
            if reply.kind != protocol.KIND_ACCEPT:
                self._finish_send(out, f"Unexpected reply: {reply.kind!r}")
                return

            self._emit(TransferReply(tid=out.tid, accepted=True))
            await conn.send(protocol.file_header(out.tid, out.filename, out.size))

            with out.path.open("rb") as file:
                while not out.cancelled:
                    data = await asyncio.to_thread(file.read, self.config.chunk_size)
                    if not data:
                        break
                    await conn.send(protocol.chunk(out.tid, data))
                    out.sent += len(data)
                    self._note_progress(out)
            if out.cancelled:
                self._finish_send(out, "Cancelled")
                return

            await conn.send(protocol.done(out.tid))
            try:
                ack = await conn.receive(timeout=self.config.ack_timeout)
            except P2PConnectionError:
                ack = None  # no confirmation; the bytes still left our side
            if ack is not None and ack.kind == protocol.KIND_CANCEL:
                self._finish_send(
                    out, str(ack.meta.get("reason") or "Cancelled by the receiver")
                )
                return
            if ack is not None and ack.kind == protocol.KIND_SAVED:
                self._finish_send(out, "Delivered", ok=True)
            else:
                self._finish_send(out, "Sent (no delivery confirmation)", ok=True)

    def _note_progress(self, out: _Outgoing) -> None:
        now = time.monotonic()
        if (
            out.sent < out.size
            and out.sent - out.last_emit_bytes < _PROGRESS_MIN_BYTES
            and now - out.last_emit_time < _PROGRESS_MIN_INTERVAL
        ):
            return
        out.last_emit_bytes = out.sent
        out.last_emit_time = now
        self._emit(
            TransferProgress(
                tid=out.tid,
                direction=Direction.SEND,
                done=out.sent,
                total=out.size,
            )
        )

    def _finish_send(self, out: _Outgoing, message: str, *, ok: bool = False) -> None:
        self._emit(
            TransferFinished(
                tid=out.tid, direction=Direction.SEND, ok=ok, message=message
            )
        )

    # -- receiving -----------------------------------------------------------
    async def respond(self, tid: str, *, accept: bool, reason: str = "") -> None:
        """Answer a pending :class:`IncomingOffer` (accept or decline)."""
        incoming = self._incoming.get(tid)
        if incoming is None:
            return

        if not accept:
            try:
                await incoming.peer.send(protocol.decline(tid, reason))
            except P2PConnectionError:
                pass
            await self._abort_incoming(incoming, reason or "Declined")
            return

        try:
            self.config.save_dir.mkdir(parents=True, exist_ok=True)
            incoming.target = protocol.unique_path(
                self.config.save_dir, incoming.filename
            )
            incoming.part = incoming.target.with_name(incoming.target.name + ".part")
            incoming.file = await asyncio.to_thread(open, incoming.part, "wb")
            await incoming.peer.send(protocol.accept(tid))
        except (OSError, P2PConnectionError) as exc:
            self._incoming.pop(tid, None)
            await self._discard_incoming(incoming)
            self._emit(
                TransferFinished(
                    tid=tid,
                    direction=Direction.RECEIVE,
                    ok=False,
                    message=f"Could not accept: {exc}",
                )
            )
            return

        incoming.accepted = True
        self._emit(
            TransferStarted(
                tid=tid,
                direction=Direction.RECEIVE,
                filename=incoming.filename,
                size=incoming.size,
                peer=incoming.sender,
            )
        )

    async def cancel(self, tid: str, reason: str = "Cancelled") -> None:
        """Abort an in-flight transfer, whichever direction it goes."""
        out = self._outgoing.get(tid)
        if out is not None:
            out.cancelled = True
            if out.conn is not None:
                await out.conn.close()
            return

        incoming = self._incoming.get(tid)
        if incoming is not None:
            # Polite note first, then drop the connection; the sender may or may
            # not be reading yet - either way its side fails safely.
            try:
                await incoming.peer.send(protocol.cancel(tid, reason))
            except P2PConnectionError:
                pass
            await self._abort_incoming(incoming, reason)

    async def _on_message(self, peer: Peer, message: Message) -> None:
        if message.kind == protocol.KIND_OFFER:
            await self._handle_offer(peer, message)
        elif message.kind == protocol.KIND_FILE:
            self._handle_file_header(peer, message)
        elif message.kind == protocol.KIND_CHUNK:
            await self._handle_chunk(peer, message)
        elif message.kind == protocol.KIND_DONE:
            await self._handle_done(peer, message)
        else:
            # accept/decline/saved belong on the *sender's* outgoing connection;
            # anything else is simply not ours. Ignore, but leave a trace.
            logger.debug(
                "ignoring %r from %s (no handler)", message.kind, peer.host_port
            )

    async def _handle_offer(self, peer: Peer, message: Message) -> None:
        offer = protocol.Offer.parse(message)
        if offer is None:
            await self._send_quietly(
                peer, protocol.decline("", "malformed offer")
            )
            return
        if self._incoming:
            await self._send_quietly(
                peer, protocol.decline(offer.tid, "busy: another transfer is active")
            )
            return
        self._incoming[offer.tid] = _Incoming(
            tid=offer.tid,
            filename=offer.safe_name,
            size=offer.size,
            sender=offer.name or peer.host_port,
            peer=peer,
        )
        self._emit(
            IncomingOffer(
                tid=offer.tid,
                filename=offer.safe_name,
                size=offer.size,
                sender=offer.name or peer.host_port,
                address=peer.host_port,
            )
        )

    def _handle_file_header(self, peer: Peer, message: Message) -> None:
        incoming = self._incoming.get(str(message.meta.get("tid") or ""))
        if incoming is None or not incoming.accepted:
            peer.close()  # a data stream nobody agreed to

    async def _handle_chunk(self, peer: Peer, message: Message) -> None:
        incoming = self._incoming.get(str(message.meta.get("tid") or ""))
        if incoming is None or not incoming.accepted or incoming.cancelled:
            peer.close()
            return
        if incoming.received + len(message.data) > incoming.size:
            await self._abort_incoming(
                incoming,
                f"Sender sent more data than the announced {incoming.size} bytes",
            )
            return
        if not message.data:
            return
        if incoming.file is None:  # pragma: no cover - accept() opens it eagerly
            return
        await asyncio.to_thread(incoming.file.write, message.data)
        incoming.received += len(message.data)
        self._note_receive_progress(incoming)

    def _note_receive_progress(self, incoming: _Incoming) -> None:
        now = time.monotonic()
        if (
            incoming.received < incoming.size
            and incoming.received - incoming.last_emit_bytes < _PROGRESS_MIN_BYTES
            and now - incoming.last_emit_time < _PROGRESS_MIN_INTERVAL
        ):
            return
        incoming.last_emit_bytes = incoming.received
        incoming.last_emit_time = now
        self._emit(
            TransferProgress(
                tid=incoming.tid,
                direction=Direction.RECEIVE,
                done=incoming.received,
                total=incoming.size,
            )
        )

    async def _handle_done(self, peer: Peer, message: Message) -> None:
        incoming = self._incoming.get(str(message.meta.get("tid") or ""))
        if incoming is None or not incoming.accepted:
            peer.close()
            return
        # Claim the transfer first: exactly one of the paths below finalises it.
        self._incoming.pop(incoming.tid, None)

        if incoming.received != incoming.size:
            await self._discard_incoming(incoming)
            self._emit(
                TransferFinished(
                    tid=incoming.tid,
                    direction=Direction.RECEIVE,
                    ok=False,
                    message=(
                        f"Incomplete transfer: {incoming.received} of "
                        f"{incoming.size} bytes arrived"
                    ),
                )
            )
            return

        try:
            await self._close_incoming_file(incoming)
            assert incoming.part is not None and incoming.target is not None
            await asyncio.to_thread(os.replace, incoming.part, incoming.target)
        except OSError as exc:
            await self._discard_incoming(incoming)
            self._emit(
                TransferFinished(
                    tid=incoming.tid,
                    direction=Direction.RECEIVE,
                    ok=False,
                    message=f"Could not save the file: {exc}",
                )
            )
            return

        await self._send_quietly(peer, protocol.saved(incoming.tid))
        self._emit(
            TransferFinished(
                tid=incoming.tid,
                direction=Direction.RECEIVE,
                ok=True,
                message=f"Saved to {incoming.target}",
                path=incoming.target,
            )
        )

    async def _abort_incoming(
        self, incoming: _Incoming, message: str, *, close_peer: bool = True
    ) -> None:
        """Drop a receive: close the socket, delete the partial file, report."""
        if self._incoming.pop(incoming.tid, None) is None:
            return  # already finished by another path
        incoming.cancelled = True
        if close_peer:
            incoming.peer.close()
        await self._discard_incoming(incoming)
        self._emit(
            TransferFinished(
                tid=incoming.tid,
                direction=Direction.RECEIVE,
                ok=False,
                message=message,
            )
        )

    async def _discard_incoming(self, incoming: _Incoming) -> None:
        try:
            await self._close_incoming_file(incoming)
        except OSError:
            pass
        if incoming.part is not None:
            await asyncio.to_thread(_unlink_quietly, incoming.part)

    @staticmethod
    async def _close_incoming_file(incoming: _Incoming) -> None:
        file = incoming.file
        if file is None:
            return
        incoming.file = None

        def _flush_and_close() -> None:
            file.flush()
            os.fsync(file.fileno())
            file.close()

        await asyncio.to_thread(_flush_and_close)

    # -- server callbacks ----------------------------------------------------
    async def _on_disconnect(self, peer: Peer) -> None:
        for incoming in list(self._incoming.values()):
            if incoming.peer is peer:
                await self._abort_incoming(
                    incoming, "Connection lost", close_peer=False
                )

    async def _on_error(self, peer: Peer, exc: Exception) -> None:
        # The server isolates connection errors; just keep a trace (the abort
        # itself happens via the on_disconnect that follows).
        logger.warning("peer %s: %s", peer.host_port, exc)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    async def _send_quietly(peer: Peer, message: Message) -> None:
        try:
            await peer.send(message)
        except P2PConnectionError:
            pass

    def _emit(self, event: Event) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - a broken UI must not kill the engine
            logger.exception("on_event callback failed for %r", event)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
