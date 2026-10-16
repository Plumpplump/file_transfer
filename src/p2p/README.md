# p2p - a tiny peer-to-peer messaging toolkit

A small, dependency-free (standard-library-only) way for two Python programs to
talk to each other over TCP - on a LAN or on one machine. **Nothing in here
knows about files, images, Flet or any particular app**, so you can copy this
folder into any Python project and `import p2p`.

- **Messages** - a `kind`, optional JSON `meta`, raw `data` bytes.
- **`PeerServer`** - listen for connections; every frame calls your callback.
- **`send`** - fire and forget: dial, write one message or a whole stream, hang up.
- **`connect` / `Connection` / `request`** - sessions: send, await replies and
  stream in both directions over one connection.
- **`PeerDiscovery`** - find peers on the LAN automatically via UDP beacons.

## Using it in another project

**Option 1 - copy the folder** (the original spirit of the toolkit):

```bash
cp -r path/to/repo/src/p2p myproject/
```

**Option 2 - install it**:

```bash
uv add --path ./src/p2p        # or:  pip install ./src/p2p
```

The package ships a `py.typed` marker, so editors and type checkers see the
annotations.

## Concepts

A **message** is a `kind` (a short name), optional JSON `meta`, and raw `data`
bytes:

```python
from p2p import Message

Message(kind="chat", data=b"hello", meta={"lang": "en"})
```

Messages are wrapped in a small framed format on the wire (a `P2P1` magic,
a length-prefixed JSON header, then the payload). `message.py` documents it in
detail. You never have to build frames yourself.

## Receiving

```python
import asyncio
from p2p import Message, PeerServer

async def handle_message(peer, message):
    # `peer.host_port` is "host:port"; `peer.send(...)` replies to this client.
    print("from", peer.host_port, ":", message.kind, message.data)
    await peer.send(Message("ack", b"ok"))

async def main():
    server = PeerServer(host="0.0.0.0", port=9100, on_message=handle_message)
    port = await server.start()     # actually listening; returns the real port
    print("listening on", port)
    await server.broadcast(Message("status", b"online"))   # to every peer
    await asyncio.Event().wait()    # run forever

asyncio.run(main())
```

`port=0` asks the OS for any free port - great for tests, since `start()`
returns the number that was chosen.

Callbacks may be plain functions **or** `async def`; the server awaits them
either way. Available callbacks: `on_connect`, `on_message`, `on_disconnect`,
`on_error`. `server.peers` lists who is connected right now;
`server.broadcast(message)` writes one frame to all of them.

## Sending (fire and forget)

```python
from p2p import Message, send

# One message:
await send("192.168.1.20", 9100, Message("hello", b"hi"))

# Several messages over one connection:
await send("192.168.1.20", 9100, [Message("a", b"1"), Message("b", b"2")])

# Stream slowly (an async generator) - messages arrive gradually:
async def frames():
    for i in range(100):
        yield Message("line", str(i).encode())
        await asyncio.sleep(0.02)

await send("192.168.1.20", 9100, frames())
```

Every call opens a fresh connection, writes the messages in order, then closes.
The `timeout` (30 s by default) bounds the **whole** send; pass `timeout=None`
for long streams that may legitimately take minutes.

## Sessions: `connect`, `Connection`, `request`

When you need replies - or one connection for a long back-and-forth - use a
**session** instead of `send`:

```python
from p2p import Message, connect, request

async with connect("192.168.1.20", 9100) as conn:
    await conn.send(Message("ping"))
    reply = await conn.receive(timeout=5)    # Message | None
    print("peer said:", reply.kind if reply else "(nothing)")

    async for message in conn:               # read until the peer hangs up
        print("more:", message.kind)

# One-liner for one question / one answer:
reply = await request("192.168.1.20", 9100, Message("stats"), timeout=5)
```

- `conn.send_many(messages)` accepts the same shapes as `send` (single message,
  list, async generator) on an already-open connection.
- Writes wait for `drain`, so a slow receiver slows the sender automatically -
  memory stays flat while streaming.
- `receive()` returns `None` when the peer closed cleanly between frames; a
  timeout raises `P2PConnectionError`.

## Finding peers on the LAN (discovery)

`PeerDiscovery` lets peers find each other without typing IP addresses. Each
instance broadcasts a tiny JSON *beacon* to `255.255.255.255` every couple of
seconds and listens for everyone else's:

```python
from p2p import PeerDiscovery, PeerInfo

def on_peer(info: PeerInfo):
    print("found", info.name, "at", info.address)   # e.g. FAX-B at 192.168.1.9:9100

discovery = PeerDiscovery(
    service="myapp",       # only beacons with this string are accepted
    port=9100,             # the TCP port peers should connect to
    name="MY-MACHINE",
    on_peer=on_peer,
)
await discovery.start()
discovery.announce()       # optional: send a beacon right now, don't wait
...
discovery.update_meta({"busy": True})   # extra fields ride along in beacons
print([p.address for p in discovery.peers])   # current peers
await discovery.stop()
```

- `on_peer(info)` fires when a peer is first seen; `on_lost(info)` when it has
  been silent for `ttl` seconds (default 6). The list self-heals as machines
  come and go.
- Both callbacks may be `def` or `async def`.
- The sender's IP comes from the datagram itself, so it cannot be spoofed by
  the payload.
- Discovery uses UDP port `9101` by default (separate from the TCP data port).
  With `SO_REUSEPORT` (macOS/Linux) **even several instances on one machine**
  discover each other, because the OS delivers every broadcast beacon to all
  sockets bound to that port.

## Errors

All failures derive from `p2p.errors.P2PError`:

| exception            | meaning                                        |
| -------------------- | ---------------------------------------------- |
| `P2PConnectionError` | could not connect, dropped, or timed out       |
| `P2PProtocolError`   | the other side is not speaking our frame format |
| `P2PPayloadTooLarge` | a frame exceeded the safety limit              |

```python
from p2p.errors import P2PError
try:
    await send(host, port, messages)
except P2PError as exc:
    print("network problem:", exc)
```

## Safety limits

- Header ≤ 64 KB, payload ≤ 20 MB per message (configurable on the server).
- Connect and send timeouts (30 s by default; pass `timeout=`/`connect_timeout=`).

These stop a buggy or hostile peer from exhausting memory or hanging your app.

## Changes in 1.1

- `connection.py`: `connect()`, `Connection` (send/receive/iterate/close) and
  `request()` - sessions without dial-back workarounds.
- `PeerServer.peers` and `PeerServer.broadcast(...)`.
- `send(..., timeout=None)` for big streams (the 30 s default no longer caps
  them silently).
- `PeerDiscovery.announce()` and `update_meta()`.
- `SO_REUSEPORT` on the discovery socket: **same-machine discovery now works**
  where the OS supports it.
- `iter_messages(reader)`, `py.typed`, standalone packaging
  (`src/p2p/pyproject.toml`) and a test suite (`tests/test_p2p.py`).
