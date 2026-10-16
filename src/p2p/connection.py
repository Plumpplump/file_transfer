"""Session-oriented half of the toolkit: :class:`Connection`, :func:`connect`.

:func:`p2p.send` is fire-and-forget - it opens a connection, writes every
message and closes the socket without ever reading a reply. That is perfect for
one-way pushes, but plenty of conversations need the other half:

* ask a question and *wait for the answer* on the same connection
* keep one connection open for a long, streamed transfer
* iterate over whatever the peer sends until it hangs up

This module adds exactly that, and nothing project-specific.

Example - a request/response over one connection::

    from p2p import Message, connect

    async with connect("192.168.1.20", 9100) as conn:
        await conn.send(Message("ping"))
        reply = await conn.receive(timeout=5)       # Message | None
        print("peer said:", reply.kind if reply else "(nothing)")

Example - the one-liner for "one question, one answer"::

    from p2p import Message, request

    reply = await request("192.168.1.20", 9100, Message("ping"), timeout=5)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from .errors import P2PConnectionError
from .message import DEFAULT_MAX_PAYLOAD_BYTES, Message, encode, read_message

#: How long connecting / a receive may take before it is called a failure.
DEFAULT_TIMEOUT = 30.0


class Connection:
    """One open TCP session with a peer: send messages, read replies.

    Create one with :func:`connect` - never build it by hand. The connection is
    *full duplex*: the peer may send while you are sending. Writes wait for
    ``drain``, so streaming a big transfer with :meth:`send_many` applies
    back-pressure instead of ballooning memory.
    """

    __slots__ = ("reader", "writer", "max_payload_bytes")

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.max_payload_bytes = max_payload_bytes

    # -- introspection -------------------------------------------------------
    @property
    def address(self) -> Any:
        """The peer address as reported by the OS: usually ``(host, port)``."""
        return self.writer.get_extra_info("peername")

    @property
    def host_port(self) -> str:
        """A readable ``host:port`` label, handy for status messages."""
        address = self.address
        if isinstance(address, tuple) and len(address) >= 2:
            return f"{address[0]}:{address[1]}"
        return str(address)

    @property
    def is_closing(self) -> bool:
        """True once :meth:`close` has been called (or the socket died)."""
        return self.writer.is_closing()

    # -- sending -------------------------------------------------------------
    async def send(self, message: Message) -> None:
        """Send one message and wait until the OS accepted the bytes.

        Waiting for ``drain`` is what creates *back-pressure*: a fast sender
        slows down automatically when the receiver stops reading.
        """
        if self.writer.is_closing():
            raise P2PConnectionError("connection is already closed")
        try:
            self.writer.write(encode(message))
            await self.writer.drain()
        except OSError as exc:
            raise P2PConnectionError(
                f"connection to {self.host_port} broke: {exc}"
            ) from exc

    async def send_many(
        self,
        messages: Message | Iterable[Message] | AsyncIterable[Message],
        *,
        timeout: float | None = None,
    ) -> None:
        """Send a whole stream of messages over this connection.

        ``messages`` accepts the same shapes as :func:`p2p.send`: one
        :class:`Message`, a plain iterable, or an async generator - the latter
        lets a caller stream a transfer gradually. ``timeout`` bounds the
        *whole* send; the default ``None`` means "no limit", which is what long
        transfers want.
        """

        async def pump() -> None:
            if isinstance(messages, Message):
                await self.send(messages)
                return
            if hasattr(messages, "__aiter__"):
                async for message in messages:  # type: ignore[union-attr]
                    await self.send(message)
            else:
                for message in messages:  # type: ignore[union-attr]
                    await self.send(message)

        if timeout is None:
            await pump()
            return
        try:
            await asyncio.wait_for(pump(), timeout)
        except asyncio.TimeoutError as exc:
            raise P2PConnectionError(
                f"sending to {self.host_port} timed out"
            ) from exc

    # -- receiving -----------------------------------------------------------
    async def receive(self, *, timeout: float | None = None) -> Message | None:
        """Read the next message.

        Returns ``None`` when the peer cleanly closed its side between frames -
        that is the normal end of a conversation, not an error. Raises
        :class:`~p2p.errors.P2PConnectionError` when no message arrives within
        ``timeout`` seconds (``None`` waits forever).
        """
        try:
            if timeout is None:
                return await read_message(
                    self.reader, max_payload_bytes=self.max_payload_bytes
                )
            return await asyncio.wait_for(
                read_message(self.reader, max_payload_bytes=self.max_payload_bytes),
                timeout,
            )
        except asyncio.TimeoutError as exc:
            raise P2PConnectionError(
                f"no message from {self.host_port} within {timeout}s"
            ) from exc
        except OSError as exc:
            raise P2PConnectionError(
                f"connection to {self.host_port} broke: {exc}"
            ) from exc

    async def __aiter__(self) -> AsyncIterator[Message]:
        """Iterate over incoming messages until the peer stops talking.

        ``async for message in conn`` reads frames until a clean end of stream;
        errors still raise :class:`~p2p.errors.P2PConnectionError`.
        """
        while (message := await self.receive()) is not None:
            yield message

    # -- lifecycle -----------------------------------------------------------
    async def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self.writer.is_closing():
            return
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass


@asynccontextmanager
async def connect(
    host: str,
    port: int,
    *,
    connect_timeout: float | None = DEFAULT_TIMEOUT,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> AsyncIterator[Connection]:
    """Open a session to ``host:port`` and close it when the block exits.

    ``connect_timeout`` is how long the OS may take to establish the
    connection; the default 30 s surfaces as
    :class:`~p2p.errors.P2PConnectionError` instead of hanging forever. Pass
    ``None`` to wait indefinitely.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        # Normalise every "could not connect" reason into one clear exception.
        raise P2PConnectionError(f"could not connect to {host}:{port}: {exc}") from exc

    connection = Connection(reader, writer, max_payload_bytes=max_payload_bytes)
    try:
        yield connection
    finally:
        await connection.close()


async def request(
    host: str,
    port: int,
    message: Message,
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
    connect_timeout: float | None = DEFAULT_TIMEOUT,
) -> Message | None:
    """Send one message and return the peer's first reply.

    The whole conversation happens over a single connection, closed right after
    the reply arrives. Returns ``None`` if the peer closed without replying;
    raises :class:`~p2p.errors.P2PConnectionError` on connect failure or when
    no reply arrives within ``timeout``.
    """
    async with connect(host, port, connect_timeout=connect_timeout) as conn:
        await conn.send(message)
        return await conn.receive(timeout=timeout)


__all__ = ["Connection", "connect", "request", "DEFAULT_TIMEOUT"]
