# AGENTS.md

## Project

`myftp` - a Flet (Python) LAN peer-to-peer file transfer app, plus a
Flet-less terminal UI for peers without Flet.

- The product spec is `plan.txt` and `reference.webp` (radar-style UI).
- `src/main.py` is a thin CLI dispatcher (lazy `flet` import). The product code
  lives in `src/app/core` (engine/protocol/config, **no Flet**), `src/app/ui`
  (the Flet GUI) and `src/app/tui` (curses + plain line mode).
- `src/p2p/` is the networking toolkit (version 1.1.0), independent of the app
  and of Flet; its API reference is `src/p2p/README.md` and it can be copied
  into other projects or installed from its own `pyproject.toml`.
- There is no CI, linter, formatter or typecheck config. Don't invent such
  commands.

## Commands (run from the repo root)

- Desktop app: `uv run flet run`; web: `uv run flet run --web`
- Terminal UI: `python src/main.py --text` (curses), `--text --plain` (line
  mode; automatic when stdout is piped)
- All tests: `uv run pytest` (or `.venv/bin/python -m pytest`)
- Single test: `uv run pytest tests/test_engine.py -k decline`
- Build: `flet build {apk,ipa,macos,linux,windows,web} -v`

`uv.lock` records the dev environment (`uv sync` refreshes it), but `uv` is
not always on PATH (it lives at `~/.local/bin/uv`). Deps come from
`pyproject.toml` (`[dependency-groups] dev` = flet-cli/desktop/web +
`flet[test]`); a `.venv` already exists in the repo, so
`.venv/bin/python -m pytest` is the fastest way to run the suite. Pytest
config sets `pythonpath = ["src"]` (so `import p2p` and `import main` work
uninstalled), `asyncio_mode = "auto"`, `testpaths = ["tests"]`.

`requires-python = ">=3.11"` is real: the code uses `StrEnum`, `match`, and
`asyncio.timeout`.

## Testing the GUI (important)

`flet run`-style flags cannot be forwarded to the app, so every CLI flag also
has an `MYFTP_*` env var - that is what the tests use.

The display name is the one piece of state the app persists: renaming it in
any front-end writes `~/.config/myftp/settings.json` (`MYFTP_SETTINGS` moves
the file, `--name`/`MYFTP_NAME` beat the saved value, and
`tests/conftest.py` points `HOME` at a temp dir so tests never see the real
one).

The `flet_app` fixture from the flet pytest plugin is **not** used: device mode
needs a Flutter toolchain (`flet build` provisioning downloads a Flutter SDK
and compiles a test host). Instead:

- `tests/test_ui_app.py` builds the real control tree against a `FakePage`
  (records `show_dialog`/`run_task`/`update`) and drives the app's public
  methods (`select_tab`, `ask_send`, `choose_and_send` - which has a
  `_choose_file` seam, `respond_offer`, `connect_manual`, `rename`) against
  real `TransferEngine`s over loopback.
- `tests/test_ui_web.py` starts `python src/main.py` with
  `FLET_FORCE_WEB_SERVER=true` + `FLET_SERVER_PORT=<free port>` and completes
  the real client handshake over a websocket: a binary frame
  `b"\x00" + msgpack.packb([1, {"session_id": "", "page_name": "", "page": {}}])`
  (`MessageAction.REGISTER_CLIENT`) makes the server run `main(page)` and send
  control patches back. Assert on the raw patch bytes (e.g. `b"Recent activity"`)
  and on a `SIGINT` clean exit. A second test drives a whole interaction over
  that protocol: patches are msgpack trees where every control carries `_i`
  (id), `_c` (class) and `key`, so the client finds the button by key, sends a
  `CONTROL_EVENT` click (action 3), types into the dialog's text field with
  `UPDATE_CONTROL_PROPS` (action 4) and asserts the repainted name patch plus
  the settings file it wrote.

## `src/p2p/` - stdlib-only toolkit (1.1.0)

Decoupled from Flet and this app's data model; keep app/UI imports out of it.
"faxer" references in its docstrings are leftovers from the source project.

- Added in 1.1: `connect()` async context manager -> `Connection`
  (`send`/`receive`/`send_many`/async-iteration), `request()`,
  `PeerServer.peers`/`broadcast()`, `PeerDiscovery.announce()`/`update_meta()`,
  `send(..., timeout=None)`, `iter_messages()`, `__version__`, `py.typed`.
- Wiring gotchas: `send()` opens a fresh TCP connection per call; replies on a
  live connection go via `Peer.send()`. Discovery is UDP broadcast on 9101,
  data is TCP on 9100. Two instances on one machine can discover each other
  (SO_REUSEPORT), but the tests skip discovery and connect directly with the
  port from `PeerServer.start(port=0)`. Payload cap is 20 MB per frame, so
  files are chunked (`chunk_size` = 512 KB).

## App protocol (`src/app/core/protocol.py`)

`offer` -> `accept`/`decline` -> `file` -> `chunk` -> `done` -> `saved` ack,
plus a receiver-side `cancel`. One transfer at a time (a second offer is
auto-declined with "busy"), filenames sanitised, collisions become
`name (1).ext`, data is written to `<name>.part` and renamed after the byte
count checks out. The engine emits frozen dataclass events
(`PeersChanged`, `IncomingOffer`, `TransferStarted`, `TransferReply`,
`TransferProgress`, `TransferFinished`) and never raises from `send_file`.

## Flet 1.x quirks (verified against 1.0.4)

- This project uses the Flet >= 1.x API (`ft.run(main)`, `ft.Event[Control]`).
  Most Flet material online is 0.x and does not translate.
- Flet >= 1.x auto-updates after event handlers; the template has no
  `page.update()` calls - but updates from a background task / engine callback
  must call `page.update()` explicitly (`MyFtpApp._refresh`).
- Flet 1.0.4 facts worth reusing: `page.navigation_bar`, `page.bgcolor`,
  `page.padding`, `page.window.*` are real page properties; dialogs go through
  `page.show_dialog(dialog)` / `page.pop_dialog()`; `FilePicker` is a service
  (`page.services.append(picker)`) with async `pick_files()` and absolute
  `FilePickerFile.path`; layout helpers are classes, not module functions:
  `ft.Border.all`, `ft.BorderRadius.all`, `ft.Padding.all`, `ft.Margin.only`.
- Implicit animations live on `LayoutControl` (`animate_rotation`,
  `animate_position`, `animate_scale`, `animate_opacity`, ...) and are chained
  through `on_animation_end` - that is how the radar sweeps and the halo
  pulses (`src/app/ui/app.py`). The animations cookbook is the reference.
  Two traps, learned the hard way: (1) an implicit animation only runs when a
  value changes on a control the client *already has*, and nothing on the
  radar ever changes by itself - the chain has to be kicked off once after
  mount, a beat later so the creation patch lands first (`_kick_radar`); and
  (2) animating two properties on one control raises two `on_animation_end`
  events per cycle, so drive a cycle from a single property (the halo animates
  only `scale`). Leaving the Nearby tab unmounts the radar and therefore kills
  the chain, which is why every tab entry kicks it again.
- Check the Flet MCP for API/enum questions (`get_api`, `get_enum`,
  `search_enum_members`, `find_icon`): faster and more accurate than guessing,
  but member lookups for methods can report false negatives (e.g.
  `page.show_dialog`), so verify with `python -c` against the installed venv
  when in doubt.
