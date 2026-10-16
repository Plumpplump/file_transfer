"""myftp entry point: parse the CLI, then dispatch to a front-end.

Flet is imported *lazily*, inside the GUI branch only. That is a promise worth
keeping: a peer with nothing but this repository and Python 3.11+ can run the
terminal UI::

    python src/main.py --text           # full-screen terminal UI
    python src/main.py --text --plain   # line-oriented terminal UI
    uv run flet run                     # the Flet GUI (default)

The dispatch table:

* ``--text`` with stdout on a terminal -> curses UI (``app.tui.curses_ui``)
* ``--text --plain``, or stdout piped  -> line mode (``app.tui.plain``)
* otherwise                            -> Flet GUI (``app.ui.app``)
"""

from __future__ import annotations

from app.core.config import Mode, resolve_config


def main(argv: list[str] | None = None) -> int:
    config = resolve_config(argv)
    match config.mode:
        case Mode.CURSES:
            from app.tui.curses_ui import run_curses

            return run_curses(config)
        case Mode.PLAIN:
            from app.tui.plain import run_plain

            return run_plain(config)
        case _:
            # The GUI is the only branch that needs Flet - keep the import here.
            import flet as ft

            from app.ui.app import build_app

            def _gui(page: ft.Page) -> None:
                build_app(config, page)

            # `flet run --web` works transparently: the CLI sets
            # FLET_FORCE_WEB_SERVER in our environment and ft.run honours it.
            ft.run(_gui)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
