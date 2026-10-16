# p2p Toolkit Core

`src/p2p/` - standalone, stdlib-only TCP messaging toolkit for LAN use; designed to be copy-pasted into other projects. Public API reference: `src/p2p/README.md`.

## Wire format (`message.py`)
- Frame: `P2P1` magic (4 B) + header length (4 B big-endian) + JSON header `{kind, meta, len}` + payload.
- Header <= 64 KB; payload <= 20 MB per message (`DEFAULT_MAX_PAYLOAD_BYTES`) - larger files must be chunked into multiple messages.
- `PeerServer` payload limit configurable via `max_payload_bytes`.

## Transport / session
- `send(host, port, messages)` is one-shot: a fresh TCP connection per call, closed after writing. There is no reply channel; replies go the other way via `Peer.send()` on the receiver's live connection.
- `PeerServer` binds `0.0.0.0:9100` by default; `start(port=0)` lets the OS pick and returns the bound port (tests rely on this).
- Callbacks (`on_message`, `on_connect`, `on_disconnect`, `on_error`) may be sync or async.
- A malformed frame is fatal only for that connection (`on_error`); the server keeps serving other peers.

## Discovery (`discovery.py`)
- UDP broadcast beacons on port 9101 (`SO_BROADCAST`); `service` string filters apps; sender IP comes from the datagram, not the payload.
- Defaults: beacon every 2 s, TTL 6 s; `on_peer`/`on_lost` fire on first sight / expiry.
- Two instances on the same machine cannot discover each other (shared UDP port) - for local tests, connect directly via `host:port` and skip discovery.

## Errors
All derive from `P2PError`: `P2PConnectionError`, `P2PProtocolError`, `P2PPayloadTooLarge`.
