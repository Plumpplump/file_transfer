# Project Core

`myftp`: Flet (Python) desktop/web app for LAN peer-to-peer file transfer.

## Source map
- `src/main.py` - Flet app entrypoint (`[tool.flet.app] path = "src"` in `pyproject.toml`). Currently the stock Flet counter demo, not the product UI.
- `src/p2p/` - working LAN discovery + TCP messaging toolkit. Protocol, ports and integration constraints: `mem:p2p/core`.
- `tests/` - pytest suite; only the Flet template UI test (`tests/test_main.py`). No p2p tests yet.
- `plan.txt` - product spec (radar scan -> pick peer -> offer file -> receiver confirms -> saved to receiver cwd); `reference.webp` is the UI mock. Don't invent behavior beyond these.
- `AGENTS.md` - repo instructions for coding agents.

## Invariants
- `src/p2p/` must stay stdlib-only (no Flet/Pillow/app imports) - it is meant to be copied into other projects unchanged.
- Python >= 3.10. Dev tooling: uv; no lockfile. Commands: `mem:suggested_commands`; toolchain details: `mem:tech_stack`.
- No CI, linter, formatter or typechecker configured.
