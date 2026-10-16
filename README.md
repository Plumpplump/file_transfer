# myftp

Peer-to-peer file transfer on your LAN: scan for nearby machines, pick one,
offer a file, and the other side decides whether to accept it. Accepted files
are saved to the **receiver's current working directory**.

Two front-ends, one core:

| front-end | how to start it | needs Flet? |
| --- | --- | --- |
| Flet GUI (radar, cards, dialogs) | `uv run flet run` (desktop) or `uv run flet run --web` | yes |
| Terminal UI (full-screen curses) | `python src/main.py --text` | no |
| Line mode (pipes, scripts) | `python src/main.py --text --plain` | no |

The GUI follows `reference.webp`: a navy header, white rounded cards and a radar
that sweeps while nearby devices orbit you. The radar, the pulsing halo and the
device chips are pure Flet implicit animations (`ft.Animation`,
`animate_rotation` / `animate_scale` / `animate_position` chained through
`on_animation_end`) — see `src/app/ui/app.py`.

## Running it

```bash
# GUI, desktop window
uv run flet run

# GUI in a browser
uv run flet run --web

# Terminal UI on a machine without Flet
python src/main.py --text        # full-screen (curses)
python src/main.py --text --plain   # line mode; chosen automatically when piped
```

`flet run` cannot forward app flags, so every option also reads an `MYFTP_*`
environment variable:

| flag | env var | meaning |
| --- | --- | --- |
| `--name NAME` | `MYFTP_NAME` | how other peers see you (default: the saved name, or hostname) |
| `--port N` | `MYFTP_PORT` | TCP port for incoming transfers (default 9100, `0` = any free) |
| `--discovery-port N` | `MYFTP_DISCOVERY_PORT` | UDP port used for LAN scan/announce (default 9101) |
| `--no-discovery` | `MYFTP_DISCOVERY=0` | skip the LAN scan, pair with `--connect` |
| `--connect HOST:PORT` | `MYFTP_CONNECT=a:1,b:2` | add a peer manually (repeatable) |
| `--save-dir DIR` | `MYFTP_SAVE_DIR` | where accepted files land (default: the working directory) |
| `--text`, `--plain` | `MYFTP_TEXT=1` | pick a terminal front-end |

The display name can be changed while the app runs: the pencil in the home
header, the "Change name" button under it, or "You appear as …" under the
radar. It is remembered in `~/.config/myftp/settings.json` (point
`MYFTP_SETTINGS` somewhere else to move it); `--name`/`MYFTP_NAME` still wins
over the saved value.

Line mode commands: `list`, `send N PATH`, `connect HOST:PORT`,
`name NEW NAME`, `a`/`accept`, `d`/`decline`, `help`, `q`.

## How a transfer works

Discovery is a UDP broadcast on 9101 (`p2p.PeerDiscovery`). Transfers use one
TCP connection: the sender offers, the receiver answers, then the file streams
in ≤512 KB frames and is written to `<name>.part` next to the final path. The
`.part` file is renamed only after the byte count matches, so a cancelled or
half-finished transfer never leaves a plausible-looking file behind. Names are
sanitised and collisions become `photo (1).jpg`.

```
sender                                 receiver
  │  offer(name, size)  ─────────────▶  │  prompt: accept / decline
  │  ◀─────────────  accept | decline   │
  │  file(name, size)  ───────────────▶ │  open <name>.part
  │  chunk(data) …     ───────────────▶ │  append, report progress
  │  done()            ───────────────▶ │  verify size, rename, fsync
  │  ◀─────────────  saved | cancel     │
```

Either side can drop the connection at any point — the other side reports
"Cancelled" or "Connection lost" and deletes its partial file.

## Layout

```
src/
  main.py            CLI dispatch; the only place Flet is imported (lazily)
  app/
    core/            config, wire protocol, transfer engine - no Flet, stdlib+p2p only
    ui/              Flet GUI (theme.py = palette/geometry, app.py = the app)
    tui/             curses UI (curses_ui.py) and line mode (plain.py)
  p2p/               copyable, stdlib-only networking toolkit (see its README)
tests/               pytest suite, including a real Flet web-server smoke test
```

`src/p2p` is deliberately independent of this app and of Flet: framed TCP
messages, a peer server, LAN discovery, done as a small library you can copy
into another project (the same folder is a standalone pip package).

## Tests

```bash
uv run pytest          # or: python -m pytest
```

The suite covers the p2p toolkit, config/protocol/engine, the line UI over
real sockets, the curses UI on a pty, the GUI's control tree against real
engines over loopback, and a smoke test that boots the real Flet web server
and completes the client registration handshake. Flet's `flet_app` fixture is
intentionally not used: it needs a Flutter toolchain.

## Building packages

`flet build {apk,ipa,macos,linux,windows,web} -v` packages the GUI; see the
Flet [publishing guides](https://flet.dev/docs/publish/). Terminal mode only
needs Python 3.11+ and this repository.
