"""The listening half of the toolkit: :class:`PeerServer`.

A ``PeerServer`` accepts TCP connections and, for each one, reads framed
:class:`~p2p.message.Message` objects in a loop. Every message is handed to the
``on_message`` callback you supply. That callback can be a normal function *or*
an ``async def`` - the server awaits it either way, so handlers are free to do
their own ``await`` work (like updating a UI).

The server knows nothing about *what* the messages mean. That is the caller's
job, which is exactly what makes this reusable across projects.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable

from .errors import P2PError, P2PProtocolError
from .message import DEFAULT_MAX_PAYLOAD_BYTES, Message, encode, read_message

logger = logging.getLogger("p2p")


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, otherwise return it unchanged.

    This tiny helper is what lets callbacks be either ``def`` or ``async def``.
    ``inspect.isawaitable`` is True for coroutines, so we only suspend when we
    actually need to.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class Peer:
    """The remote end of one live connection.

    Passed to your callbacks so you can see *who* sent a message and reply to
    them without keeping your own registry of sockets.
    """

    __slots__ = ("reader", "writer", "address")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        # ``peername`` is the (host, port) of the other machine.
        self.address = writer.get_extra_info("peername")

    @property
    def host_port(self) -> str:
        """A readable ``host:port`` label, handy for status messages."""
        if isinstance(self.address, tuple) and len(self.address) >= 2:
            return f"{self.address[0]}:{self.address[1]}"
        return str(self.address)

    async def send(self, message: Message) -> None:
        """Send one message back to this peer and wait for it to drain."""
        await self.send_bytes(encode(message))

    async def send_bytes(self, payload: bytes) -> None:
        """Send a *pre-encoded* frame; :meth:`send` is the friendly version.

        Exposed so :meth:`PeerServer.broadcast` can encode one frame and write
        it to every peer without re-encoding per peer.
        """
        self.writer.write(payload)
        await self.writer.drain()

    def close(self) -> None:
        """Ask the OS to close the socket (safe to call more than once)."""
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


class PeerServer:
    """An asyncio TCP server that receives :class:`Message` objects.

    Typical use::

        server = PeerServer(port=9100, on_message=my_handler)
        port = await server.start()      # actually listening now
        ...
        await server.stop()

    Callback signatures (all optional):

    * ``on_connect(peer)``   - a new peer connected
    * ``on_message(peer, message)`` - a frame arrived
    * ``on_disconnect(peer)``- the peer went away
    * ``on_error(peer, exc)``- a malformed frame or handler error
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 0,
        *,
        on_message: Callable[[Peer, Message], Any] | None = None,
        on_connect: Callable[[Peer], Any] | None = None,
        on_disconnect: Callable[[Peer], Any] | None = None,
        on_error: Callable[[Peer, Exception], Any] | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        # "0.0.0.0" means "every network interface on this machine", so peers on
        # the local network (not just this computer) can reach us.
        self.host = host
        self.port = port
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_error = on_error
        self.max_payload_bytes = max_payload_bytes

        self._server: asyncio.AbstractServer | None = None
        self._peers: set[Peer] = set()

    @property
    def is_running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int | None:
        """The port we are actually listening on (useful after ``port=0``)."""
        # ``asyncio.Server.sockets`` is a list of bound sockets; Pyright's stub
        # for the ABC does not declare it, so read it defensively.
        sockets = getattr(self._server, "sockets", None)
        if sockets:
            return sockets[0].getsockname()[1]
        return None

    @property
    def peers(self) -> list[Peer]:
        """A snapshot of the peers connected right now."""
        return list(self._peers)

    async def start(self, *, port: int | None = None) -> int:
        """Begin listening and return the bound port number.

        Pass ``port=0`` to let the operating system pick a free port - very
        convenient for tests, which then read the real port from the return
        value.
        """
        if self._server is not None:
            raise RuntimeError("server is already running")
        bind_port = self.port if port is None else port
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, bind_port
        )
        self.port = self.bound_port or bind_port
        return self.port

    async def stop(self) -> None:
        """Stop listening and drop every open connection."""
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        await server.wait_closed()
        # Closing the writers makes each connection's read loop exit cleanly.
        for peer in list(self._peers):
            peer.close()
        self._peers.clear()

    async def broadcast(self, message: Message) -> None:
        """Send ``message`` to every currently connected peer.

        The frame is encoded once and written to all peers concurrently, so a
        slow peer delays the others only by its own ``drain``. Peers whose
        socket fails mid-write are ignored here - their read loop notices and
        cleans up - so one dead peer never blocks the rest.
        """
        if not self._peers:
            return
        payload = encode(message)
        await asyncio.gather(
            *(peer.send_bytes(payload) for peer in list(self._peers)),
            return_exceptions=True,
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Run the read loop for a single peer (one asyncio Task per peer)."""
        peer = Peer(reader, writer)
        self._peers.add(peer)
        try:
            if self.on_connect is not None:
                await _maybe_await(self.on_connect(peer))

            while True:
                try:
                    message = await read_message(
                        reader, max_payload_bytes=self.max_payload_bytes
                    )
                except P2PError as exc:
                    # A bad frame is fatal for this connection only; the server
                    # keeps running and other peers are unaffected.
                    await self._report_error(peer, exc)
                    break

                if message is None:
                    break  # clean end of stream

                if self.on_message is not None:
                    try:
                        await _maybe_await(self.on_message(peer, message))
                    except Exception as exc:  # noqa: BLE001
                        # A bug in the handler should not kill the whole server.
                        await self._report_error(peer, exc)
                        break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the peer vanished; not worth reporting as an error
        finally:
            self._peers.discard(peer)
            peer.close()
            if self.on_disconnect is not None:
                try:
                    await _maybe_await(self.on_disconnect(peer))
                except Exception:  # noqa: BLE001
                    logger.exception("on_disconnect callback failed")

    async def _report_error(self, peer: Peer, exc: Exception) -> None:
        if self.on_error is not None:
            await _maybe_await(self.on_error(peer, exc))
        else:
            logger.warning("peer %s: %s", peer.host_port, exc)


__all__ = ["Peer", "PeerServer", "P2PProtocolError"]
