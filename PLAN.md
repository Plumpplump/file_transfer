# myftp — implementation plan

A Flet (Python) LAN peer-to-peer file transfer app. The product spec lives in
`plan.txt`; the UI reference is `reference.webp`: a home screen with action
cards and a "Nearby Devices" **radar scanner**.

Flow: start → scan the LAN → pick a peer → choose a file → the peer confirms
(accept/decline) → the file is saved into the receiver's working directory.

## Architecture

```
src/main.py            CLI entry point. Parses flags, then dispatches:
                       Flet GUI (flet imported lazily!) or terminal UI.
src/app/core/          Pure asyncio + p2p. NEVER imports Flet.
    config.py            defaults, CLI flags, MYFTP_* env vars
    protocol.py          message kinds, sanitised filenames, size formatting
    engine.py            TransferEngine: server + discovery + send/receive
                         state machines, exposed as typed events
src/app/ui/            Flet front-end (theme, home, radar, dialogs)
src/app/tui/           Terminal front-ends on the same engine:
    plain.py             line-oriented (scripts, pipes, tests)
    curses_ui.py         full-screen radar + device list
src/p2p/               stdlib-only networking toolkit (independently reusable)
```

Everything interesting lives in `app.core`; the GUI and both terminal UIs are
thin adapters over the same engine and wire protocol. `app.core` importing Flet
is an error, guarded by a test. Python target: **3.11+** (StrEnum, TaskGroup,
`asyncio.timeout`).

## p2p 1.1 — reuse improvements (additive, backward compatible)

- `connect()` (async CM) → `Connection`: `send`, `receive(timeout)`,
  `send_many`, `async for`, `close` — request/response and long-lived
  streaming sessions without dial-back hacks.
- `request(host, port, message, timeout)` — one question, one answer.
- `send(..., timeout=None)` — the 30 s default cap no longer kills big streams.
- `PeerServer.peers` + `PeerServer.broadcast(message)` — server-initiated push.
- `PeerDiscovery.announce()` (instant beacon), `update_meta()`.
- **Fixed:** `SO_REUSEPORT` on the discovery socket → several instances on the
  *same machine* now discover each other (broadcast reaches every socket).
- `iter_messages(reader)`, `__version__`, `py.typed`, standalone
  `src/p2p/pyproject.toml` (`uv add --path` / `pip install`) — copying the
  folder still works.
- Own test suite in `tests/test_p2p.py`.

## Wire protocol (over one p2p connection per transfer)

| kind      | direction              | payload                                   |
| --------- | ---------------------- | ----------------------------------------- |
| `offer`   | sender → receiver      | `tid, filename, size, name`               |
| `accept` / `decline` | receiver → sender | `tid` (+ optional reason)                 |
| `file`    | sender → receiver      | `tid, filename, size` (data phase begins) |
| `chunk`   | sender → receiver      | `data` ≤ 512 KB (20 MB p2p frame cap)     |
| `done`    | sender → receiver      | `tid` — receiver verifies byte count      |
| `saved`   | receiver → sender      | `tid` — file renamed into place (ack)     |

- Connection close = cancel: receiver deletes the `.part` file, sender reports
  an interrupted transfer. No extra message needed.
- One transfer at a time; concurrent offers get an automatic `decline`.
- Receiver safety: `safe_filename()` (no paths/control chars), collision-safe
  `name (1).ext`, `.part` → final rename only after size verification.

## Terminal UI (peers without Flet)

```
python src/main.py --text          # curses UI when stdout is a TTY
python src/main.py --text --plain  # line mode (also auto when piped)
uv run flet run                    # GUI, unchanged
```

Flags: `--name`, `--port`, `--discovery-port`, `--no-discovery`,
`--connect HOST:PORT` (repeatable), `--save-dir`, `--plain`. The same values
are readable from `MYFTP_*` environment variables (GUI launches via
`flet run` can't take app flags). Flet is imported only in GUI mode.

Plain-mode commands: `list`, `send <n> <path>`, `connect <host:port>`,
`a`/`d` when an offer is pending, `quit` — printable transcript, so the e2e
test drives two `--plain` subprocesses over loopback.

## GUI

- Home: header (name, address, status), Send/Receive cards, recent activity.
- Nearby: animated radar — rotating sweep (`animate_rotation` chained through
  `on_animation_end`), pulsing rings, device avatars with `animate_position`;
  device list with Send buttons; "add by address" dialog.
- Incoming offer → `AlertDialog` (accept/decline); transfers show progress +
  cancel; on completion, "Saved to `<absolute path>`".
- Every control/animation checked against the Flet MCP before use (plan.txt #7).

## Status

Implemented end to end (M0-M6 below); the suite is green with
`uv run pytest` (p2p, core, engine, both terminal UIs, the GUI's control tree
against real engines, and a real Flet web-server smoke test).

## Milestones

- **M0** skeleton: package split, CLI dispatch, config, core-without-Flet guard. ✔
- **M1** p2p 1.1 + tests. ✔
- **M2** protocol + engine + loopback tests (accept/decline/timeout/cancel/
  collision/sanitise/size mismatch). ✔
- **M3** plain TUI (first full e2e over loopback). ✔
- **M4** Flet GUI (transfers, then radar + home polish). ✔
- **M5** curses TUI. ✔
- **M6** docs (`AGENTS.md`, root `README.md`), full `uv run pytest` green. ✔

## Decisions

- Accepted files go to the receiver's cwd (spec), overridable with `--save-dir`;
  the absolute path is always shown after saving.
- `saved` ack: sender reports "Delivered" (or "Sent" if the ack times out).
- Manual `--connect` exists mainly for tests and odd networks; discovery now
  works same-machine.
- Save-target naming never overwrites an existing file.
