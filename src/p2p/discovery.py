"""LAN peer discovery by UDP broadcast.

Every instance periodically shouts a tiny "I am here" datagram to the local
network's broadcast address, and also listens for everyone else's shouts. There
is no central server and no configuration: peers find each other automatically.

How the pieces fit together:

* ``PeerDiscovery`` owns one UDP socket bound to a shared **discovery port**.
* A background task broadcasts a small JSON *beacon* every few seconds
  (``_beacon_loop`` -> ``_send_beacon``).
* Incoming beacons arrive in ``_on_datagram``; each sender is recorded as a
  :class:`PeerInfo`, keyed by ``host:port``.
* Another background task drops peers that have gone quiet for ``ttl`` seconds
  (``_expire_loop``), so the list self-heals when a machine leaves.

Nothing here knows about faxes - ``service`` is just a string used to ignore
beacons from other kinds of app. That keeps the toolkit reusable.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

# A marker in every beacon so random UDP noise on the port is ignored.
DISCOVERY_MAGIC = "P2PD1"

# The UDP port everyone agrees to shout on and listen to. It is separate from the
# TCP data port (9100 by default) so the two never collide.
DEFAULT_DISCOVERY_PORT = 9101

# How often to broadcast, and how long a silent peer is remembered.
DEFAULT_INTERVAL = 2.0
DEFAULT_TTL = 6.0

# The limited-broadcast address reaches every host on the local subnet.
DEFAULT_BROADCAST = "255.255.255.255"


@dataclass
class PeerInfo:
    """One discovered peer on the network."""

    host: str
    port: int
    name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    last_seen: float = 0.0

    @property
    def address(self) -> str:
        """The ``host:port`` string the user would type into "Send to"."""
        return f"{self.host}:{self.port}"

    @property
    def key(self) -> tuple[str, int]:
        return (self.host, self.port)


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Bridge asyncio's datagram callbacks to the ``PeerDiscovery`` object."""

    def __init__(self, discovery: "PeerDiscovery") -> None:
        self.discovery = discovery

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.discovery._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover - rare
        # A send/recv error on UDP (e.g. "port unreachable") is not fatal.
        pass


class PeerDiscovery:
    """Announce this app on the LAN and keep a list of the peers found.

    Typical use::

        discovery = PeerDiscovery(
            service="faxer", port=9100, name="FAX-LAPTOP",
            on_peer=handle_new_peer, on_lost=handle_gone_peer,
        )
        await discovery.start()
        ...
        for peer in discovery.peers:
            print(peer.address)
        await discovery.stop()

    ``on_peer`` is called once when a peer is first seen; ``on_lost`` when it has
    been silent for ``ttl`` seconds. Both may be plain functions or ``async def``.
    """

    def __init__(
        self,
        service: str,
        *,
        port: int,
        name: str = "",
        meta: dict[str, Any] | None = None,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        broadcast: str = DEFAULT_BROADCAST,
        interval: float = DEFAULT_INTERVAL,
        ttl: float = DEFAULT_TTL,
        on_peer: Callable[[PeerInfo], Any] | None = None,
        on_lost: Callable[[PeerInfo], Any] | None = None,
    ) -> None:
        self.service = service
        self.port = port  # the TCP port peers should connect to
        self.name = name
        self.meta = dict(meta or {})
        self.discovery_port = discovery_port
        self.broadcast = broadcast
        self.interval = interval
        self.ttl = ttl
        self.on_peer = on_peer
        self.on_lost = on_lost

        # A per-instance id so we can ignore our own broadcast looped back to us.
        self.instance_id = uuid.uuid4().hex

        self._peers: dict[tuple[str, int], PeerInfo] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: list[asyncio.Task] = []

    # -- public API ----------------------------------------------------------
    @property
    def peers(self) -> list[PeerInfo]:
        """Currently known peers, sorted by name then address."""
        return sorted(self._peers.values(), key=lambda p: (p.name.lower(), p.host))

    @property
    def is_running(self) -> bool:
        return self._transport is not None

    @property
    def bound_port(self) -> int | None:
        """The UDP port we are actually listening on (known after ``start``)."""
        if self._transport is None:
            return None
        sockname = self._transport.get_extra_info("sockname")
        return sockname[1] if sockname else None

    async def start(self) -> int:
        """Bind the discovery socket and begin announcing. Returns the port."""
        if self._transport is not None:
            raise RuntimeError("discovery is already running")

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SO_REUSEADDR lets several apps/instances share the discovery port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT (where the OS has it) is what lets *two instances on the
        # same machine* share the discovery port: the OS then delivers every
        # broadcast beacon to each of them. Without it the second bind fails
        # with "address already in use" on macOS.
        reuseport = getattr(socket, "SO_REUSEPORT", None)
        if reuseport is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, reuseport, 1)
            except OSError:
                pass  # platform knows the constant but refused the option
        # SO_BROADCAST is required before sending to 255.255.255.255.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        try:
            sock.bind(("", self.discovery_port))
        except OSError:
            sock.close()
            raise

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(self), sock=sock
        )
        self._tasks = [
            asyncio.create_task(self._beacon_loop()),
            asyncio.create_task(self._expire_loop()),
        ]
        return self.discovery_port

    async def stop(self) -> None:
        """Stop announcing and listening; cancel background tasks."""
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._peers.clear()

    def update_port(self, port: int) -> None:
        """Change the TCP port we advertise (e.g. after the user edits settings)."""
        self.port = port

    def update_name(self, name: str) -> None:
        self.name = name

    def update_meta(self, meta: dict[str, Any] | None = None) -> None:
        """Replace the extra beacon fields (e.g. an app's "busy" flag).

        The new dict is broadcast from the next beacon onwards.
        """
        self.meta = dict(meta or {})

    def announce(self) -> None:
        """Broadcast a beacon right now instead of waiting for the next tick.

        Call this right after :meth:`start` so peers notice this instance
        immediately rather than up to ``interval`` seconds later.
        """
        self._send_beacon()

    # -- broadcasting --------------------------------------------------------
    async def _beacon_loop(self) -> None:
        try:
            while True:
                self._send_beacon()
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            return

    def _send_beacon(self) -> None:
        if self._transport is None:
            return
        payload = {
            "magic": DISCOVERY_MAGIC,
            "id": self.instance_id,
            "service": self.service,
            "name": self.name,
            "port": self.port,
            "meta": self.meta,
        }
        try:
            self._transport.sendto(
                json.dumps(payload).encode("utf-8"),
                (self.broadcast, self.discovery_port),
            )
        except (OSError, ValueError):
            # Broadcasting can fail if the interface is down; just retry later.
            pass

    # -- receiving -----------------------------------------------------------
    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return  # not ours / not JSON
        if payload.get("magic") != DISCOVERY_MAGIC:
            return
        if payload.get("service") != self.service:
            return  # a different app sharing the discovery port
        if payload.get("id") == self.instance_id:
            return  # our own broadcast came back to us

        port = int(payload.get("port", 0))
        if not 0 < port < 65536:
            return

        # The sender's IP is the datagram source address, so it cannot be forged
        # by the payload - safer than trusting a field inside the message.
        info = PeerInfo(
            host=addr[0],
            port=port,
            name=str(payload.get("name", "")),
            meta=payload.get("meta") or {},
            last_seen=time.monotonic(),
        )
        is_new = info.key not in self._peers
        self._peers[info.key] = info
        if is_new and self.on_peer is not None:
            _fire(self.on_peer, info)

    async def _expire_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                for key, info in list(self._peers.items()):
                    if now - info.last_seen > self.ttl:
                        del self._peers[key]
                        if self.on_lost is not None:
                            _fire(self.on_lost, info)
        except asyncio.CancelledError:
            return


def _fire(callback: Callable[[PeerInfo], Any], info: PeerInfo) -> None:
    """Call a peer callback, scheduling it if it is a coroutine function."""
    result = callback(info)
    if inspect.isawaitable(result):
        # We are on the event loop already; run async callbacks as tasks.
        asyncio.ensure_future(result)


__all__ = [
    "PeerDiscovery",
    "PeerInfo",
    "DISCOVERY_MAGIC",
    "DEFAULT_DISCOVERY_PORT",
]
