"""Exceptions raised by the :mod:`p2p` toolkit.

Keeping them in one small module means other projects that copy the ``p2p``
folder get a single, predictable place to catch network problems::

    from p2p.errors import P2PError

    try:
        await send(host, port, messages)
    except P2PError as exc:
        print("network problem:", exc)
"""

from __future__ import annotations


class P2PError(Exception):
    """Base class for every problem this package reports.

    Catching ``P2PError`` lets a caller handle *any* p2p failure (bad address,
    a peer that refused the connection, a malformed frame, a timeout) without
    knowing the individual subclasses.
    """


class P2PConnectionError(P2PError):
    """The connection could not be opened, dropped, or timed out.

    This is the "normal network" kind of failure - the cable is unplugged, the
    other machine is not listening on that port, a firewall blocked us, etc.
    """


class P2PProtocolError(P2PError):
    """The bytes on the wire did not follow the framing format.

    ``message.py`` raises this when the magic header is wrong, when JSON is
    corrupt, or when the stream ends halfway through a frame. It usually means
    "the thing you connected to is not a p2p peer".
    """


class P2PPayloadTooLarge(P2PProtocolError):
    """A frame declared more bytes than the agreed safety limit.

    The limit protects us from a malicious or buggy peer that would otherwise
    make us allocate gigabytes of memory in one read.
    """
