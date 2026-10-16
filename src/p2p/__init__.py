"""``p2p`` - a tiny, reusable peer-to-peer messaging toolkit.

This package lets two Python programs (on the same LAN, or even the same
machine) exchange labelled messages over TCP. It has **no** third-party
dependencies - the only imports are the Python standard library. That is
deliberate: copy the ``src/p2p`` folder into any other project and
``import p2p`` works there unchanged. (It can also be pip/uv-installed -
see ``pyproject.toml`` in this folder.)

Public API
----------
:class:`Message`
    A ``kind`` string, optional ``meta`` dict, and raw ``data`` bytes.
:class:`PeerServer`
    Listens for connections and calls ``on_message(peer, message)``.
:func:`send`
    Dials a peer and writes one message, or a whole stream, then hangs up.
:func:`connect` / :class:`Connection`
    Opens a *session*: send messages, wait for replies, iterate over whatever
    the peer sends (see :mod:`p2p.connection`).
:func:`request`
    Send one message, await the first reply, hang up.
:class:`PeerDiscovery`
    Finds peers on the LAN via UDP broadcast beacons.

Quick start
-----------
Receiver::

    from p2p import PeerServer

    async def handle(peer, message):
        print("from", peer.host_port, "->", message.kind, message.data)

    server = PeerServer(port=9100, on_message=handle)
    await server.start()

Sender, fire and forget::

    from p2p import Message, send

    await send("192.168.1.20", 9100, Message("hello", b"hi"))

Sender, ask and wait::

    from p2p import Message, request

    reply = await request("192.168.1.20", 9100, Message("ping"))
"""

from .client import send
from .connection import Connection, connect, request
from .discovery import (
    DEFAULT_DISCOVERY_PORT,
    DISCOVERY_MAGIC,
    PeerDiscovery,
    PeerInfo,
)
from .errors import (
    P2PConnectionError,
    P2PError,
    P2PPayloadTooLarge,
    P2PProtocolError,
)
from .message import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    MAGIC,
    Message,
    encode,
    iter_messages,
    read_message,
)
from .server import Peer, PeerServer

__version__ = "1.1.0"

__all__ = [
    # core
    "Message",
    "PeerServer",
    "Peer",
    "send",
    "connect",
    "Connection",
    "request",
    "encode",
    "read_message",
    "iter_messages",
    # discovery
    "PeerDiscovery",
    "PeerInfo",
    # constants
    "MAGIC",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_DISCOVERY_PORT",
    "DISCOVERY_MAGIC",
    "__version__",
    # exceptions
    "P2PError",
    "P2PConnectionError",
    "P2PProtocolError",
    "P2PPayloadTooLarge",
]
