"""myftp application package.

Layout:

* ``app.core`` - configuration, wire protocol and the transfer engine. Pure
  asyncio + p2p, **never imports Flet** (a test enforces this).
* ``app.ui`` - the Flet GUI.
* ``app.tui`` - terminal front-ends (full-screen curses and plain line mode).
"""
