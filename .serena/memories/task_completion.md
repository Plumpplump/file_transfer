# Task Completion

- Run the test suite: `uv run pytest` (fallback: `~/.local/bin/uv run pytest`).
- New UI behavior should come with a test in `tests/` driven by the `flet_app` fixture (`flet.testing`).
- p2p has no tests yet; add stdlib-only tests under `tests/` when changing it.
- No linter, formatter or typechecker is configured - do not invent commands.
