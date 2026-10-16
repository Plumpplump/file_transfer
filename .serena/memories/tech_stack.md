# Tech Stack

- Python >= 3.10 (`pyproject.toml` requires-python).
- Flet >= 1.0.4 - GUI framework. Flet 1.x API only: `ft.run(main)`, `ft.Event[Control]` annotations; page updates happen automatically after event handlers (no explicit `page.update()`; most Flet examples online are 0.x).
- Dev dependencies (uv `[dependency-groups] dev`): `flet-cli`, `flet-desktop`, `flet-web`, `flet[test]` (pytest + pytest-asyncio + screenshot comparison deps).
- Pytest config in `pyproject.toml`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `pythonpath = ["src"]` (so `import p2p` works without installing).
- UI tests use the `flet_app` fixture from the flet pytest plugin (`flet.testing`) and drive a real app.
- Packaging: `[tool.flet]` in `pyproject.toml`; `src/assets/icon.png` is the single icon for all platforms (keep transparent/full-canvas).
