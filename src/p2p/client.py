"""The dialling half of the toolkit: :func:`send`.

Open a connection to a peer, write one or more framed messages, close. That is
the whole job. Because ``messages`` may be a *generator*, this supports
streaming: you can yield a message, ``await asyncio.sleep(...)``, then yield the
next one, and the receiver sees them arrive gradually.

For conversations that need replies, use :mod:`p2p.connection` instead
(:func:`~p2p.connection.connect`, :func:`~p2p.connection.request`).

Example - send two messages::

    from p2p import Message, send
    await send("192.168.1.20", 9100, [Message("hello", b"1"), Message("bye", b"2")])

Example - stream a big transfer slowly::

    async def frames():
        for chunk in chunks:
            yield Message("chunk", chunk)
            await asyncio.sleep(0.01)
    await send("192.168.1.20", 9100, frames())
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable

from .connection import DEFAULT_TIMEOUT, connect
from .message import Message


async def send(
    host: str,
    port: int,
    messages: Message | Iterable[Message] | AsyncIterable[Message],
    *,
    timeout: float | None = DEFAULT_TIMEOUT,
    connect_timeout: float | None = DEFAULT_TIMEOUT,
) -> None:
    """Send ``messages`` to ``host:port`` over one connection.

    ``messages`` accepts a single :class:`Message`, a normal iterable (list,
    tuple, generator) or an *async* iterable (``async def`` generator). Every
    message is flushed before the next one is written, so ordering is preserved
    and the peer sees a smooth stream.

    ``timeout`` bounds the whole send. It defaults to 30 s so a dead peer
    cannot hang the caller, but pass ``timeout=None`` when streaming something
    big - a real transfer may legitimately take minutes.
    """
    async with connect(host, port, connect_timeout=connect_timeout) as conn:
        await conn.send_many(messages, timeout=timeout)


__all__ = ["send", "DEFAULT_TIMEOUT"]
