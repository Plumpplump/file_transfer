"""Framing: turn a :class:`Message` into bytes, and read those bytes back.

``p2p`` never assumes the other side sends exactly one ``write()`` per message.
TCP is a *stream*: two writes can arrive glued together, and one write can be
split across two reads. So every message is wrapped in a small, self-describing
frame::

    +--------+----------------+-------------+-------------------------+
    | "P2P1" | header length  | JSON header | raw data bytes          |
    | 4 B    | 4 B big-endian | N bytes     | "len" bytes from header |
    +--------+----------------+-------------+-------------------------+

The JSON header is ``{"kind": "...", "meta": {...}, "len": <int>}``:

* ``kind`` - a short string naming what the message is (the receiver decides
  what it means; the toolkit itself does not care).
* ``meta`` - optional JSON metadata (numbers, strings, small dicts).
* ``len``  - how many raw bytes of payload follow the header.

Nothing here knows about faxes, images or Flet - it is deliberately generic so
the same code can carry any project's data.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .errors import P2PProtocolError, P2PPayloadTooLarge

# A fixed, recognisable marker at the start of every frame. If the first four
# bytes are not exactly this, the stream is not ours (or is out of sync).
MAGIC = b"P2P1"

# The header length is a 4-byte unsigned big-endian integer (``struct`` code
# "!I"). 4 bytes can describe headers up to ~4 GB, far more than we need.
_HEADER_LENGTH_SIZE = 4

# Sensible ceilings so a bad peer cannot exhaust our memory with one frame.
MAX_HEADER_BYTES = 64 * 1024  # a JSON header should never be this big
DEFAULT_MAX_PAYLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per message


@dataclass
class Message:
    """One framed message: a named kind, optional metadata and raw payload.

    Example::

        Message(kind="greeting", data=b"hi", meta={"lang": "en"})
    """

    kind: str
    data: bytes = b""
    # ``field(default_factory=dict)`` gives every instance its own dict, instead
    # of sharing one mutable default between all Messages (a classic Python trap).
    meta: dict[str, Any] = field(default_factory=dict)


def encode(message: Message) -> bytes:
    """Serialise ``message`` into the frame layout described above."""
    if not message.kind:
        raise P2PProtocolError("message kind must not be empty")

    data = message.data or b""
    if len(data) > DEFAULT_MAX_PAYLOAD_BYTES:
        raise P2PPayloadTooLarge(
            f"payload of {len(data)} bytes exceeds the "
            f"{DEFAULT_MAX_PAYLOAD_BYTES} byte limit"
        )

    # ``separators`` strips the spaces json.dumps adds by default, keeping the
    # header compact and its byte length predictable.
    header = json.dumps(
        {"kind": message.kind, "meta": message.meta or {}, "len": len(data)},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header) > MAX_HEADER_BYTES:
        raise P2PProtocolError(f"header of {len(header)} bytes is too large")

    return MAGIC + struct.pack("!I", len(header)) + header + data


async def read_message(
    reader: asyncio.StreamReader,
    *,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> Message | None:
    """Read exactly one frame from ``reader``.

    Returns ``None`` when the peer cleanly closed the connection *between*
    frames - that is the normal end of a conversation, not an error. Raises
    :class:`P2PProtocolError` if the stream ends in the middle of a frame or the
    bytes do not match the frame format.
    """
    magic = await _read_exactly(reader, len(MAGIC))
    if magic is None:
        return None  # clean EOF: nothing buffered, connection closed politely
    if magic != MAGIC:
        raise P2PProtocolError("stream does not start with the P2P1 marker")

    raw_length = await _read_exactly(reader, _HEADER_LENGTH_SIZE)
    if raw_length is None:
        raise P2PProtocolError("stream ended before the header length")
    (header_length,) = struct.unpack("!I", raw_length)
    if header_length > MAX_HEADER_BYTES:
        raise P2PProtocolError(f"header length {header_length} is out of range")

    raw_header = await _read_exactly(reader, header_length)
    if raw_header is None:
        raise P2PProtocolError("stream ended before the header finished")

    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise P2PProtocolError("header is not valid JSON") from exc

    kind = header.get("kind")
    if not kind:
        raise P2PProtocolError("header is missing 'kind'")

    data_length = int(header.get("len", 0))
    if data_length < 0 or data_length > max_payload_bytes:
        raise P2PPayloadTooLarge(
            f"peer announced {data_length} payload bytes "
            f"(limit is {max_payload_bytes})"
        )

    data = b""
    if data_length:
        data = await _read_exactly(reader, data_length)
        if data is None:
            raise P2PProtocolError("stream ended before the payload finished")

    return Message(kind=kind, data=data, meta=header.get("meta") or {})


async def iter_messages(
    reader: asyncio.StreamReader,
    *,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> AsyncIterator[Message]:
    """Yield messages from ``reader`` until the peer cleanly closes it.

    A thin async-iterator sugar over :func:`read_message` for custom read
    loops; protocol errors still raise
    :class:`~p2p.errors.P2PProtocolError`.
    """
    while True:
        message = await read_message(reader, max_payload_bytes=max_payload_bytes)
        if message is None:
            return
        yield message


async def _read_exactly(reader: asyncio.StreamReader, count: int) -> bytes | None:
    """Read exactly ``count`` bytes.

    Returns ``None`` if the stream is already at a clean end (zero bytes
    buffered). Any *partial* read is corruption, so it is raised as a protocol
    error rather than silently returning a short buffer.
    """
    if count == 0:
        return b""
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise P2PProtocolError("connection closed in the middle of a frame") from exc
