# Suggested Commands

Run from the repo root.
- Desktop app: `uv run flet run`
- Web app: `uv run flet run --web`
- All tests: `uv run pytest` (or `flet test`)
- Single test: `uv run pytest tests/test_main.py::test_increment`
- Build: `flet build {apk,ipa,macos,linux,windows,web} -v`

Environment notes (Darwin):
- `uv` lives at `~/.local/bin/uv` and is not always on PATH in non-interactive agent shells; fall back to `~/.local/bin/uv ...` if plain `uv` fails.
- No `uv.lock`; the first `uv run` resolves dependencies into `.venv`.
