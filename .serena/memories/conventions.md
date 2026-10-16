# Conventions

- p2p modules: "why"-oriented docstrings, `from __future__ import annotations`, dataclasses for value types, explicit `__all__` per module, callbacks accepted as both `def` and `async def` (awaited via `inspect.isawaitable`).
- `src/p2p/__init__.py` re-exports the public API; keep `src/p2p/README.md` in sync with it.
- p2p public names: `Message`, `Peer`, `PeerServer`, `send`, `PeerDiscovery`; all errors derive from `P2PError`.
- App/UI code follows the stock template style: small closures typed as `ft.Event[...]`.
- No linter/formatter config - match surrounding style.
