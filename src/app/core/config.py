"""Configuration: defaults, a settings file, CLI flags and ``MYFTP_*`` env vars.

Nothing in ``app.core`` may import Flet - that is what lets the terminal UI and
the test suite run on machines without it. This module is standard library only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

#: Discovery service name - peers only befriend beacons carrying this string.
SERVICE = "myftp"
DEFAULT_TCP_PORT = 9100  # where peers connect to send us files
DEFAULT_DISCOVERY_PORT = 9101  # the UDP port beacons are broadcast on
DEFAULT_CHUNK_SIZE = 512 * 1024  # per-frame payload (the p2p cap is 20 MB)
DEFAULT_OFFER_TIMEOUT = 120.0  # patience while the human decides to accept
DEFAULT_ACK_TIMEOUT = 15.0  # waiting for the receiver's "saved" confirmation
DEFAULT_CONNECT_TIMEOUT = 10.0
ENV_PREFIX = "MYFTP_"
NAME_LIMIT = 24  # a display name has to fit on a radar chip

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


class Mode(StrEnum):
    """Which front-end to start."""

    GUI = "gui"  # a Flet window / browser tab
    CURSES = "curses"  # full-screen terminal UI
    PLAIN = "plain"  # line-oriented terminal UI (scripts, pipes, tests)


def _default_name() -> str:
    """The machine's short hostname, upper-cased: ``DAVIDS-MACBOOK``."""
    return socket.gethostname().split(".")[0].upper()


def default_save_dir() -> Path:
    """The receiver's working directory - even under ``flet run``.

    Flet launches the app process with ``cwd`` set to a private storage dir
    and the *invocation* directory prepended to ``PYTHONPATH``. Prefer the
    latter when Flet's marker variable is present, so accepted files land
    where the user actually launched the app, not inside ``.flet/storage``.
    """
    if os.environ.get("FLET_APP_STORAGE_DATA"):
        first = os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
        if first and Path(first).is_dir():
            return Path(first)
    return Path.cwd()


def settings_path() -> Path:
    """Where the app remembers small user choices (currently: the name).

    ``MYFTP_SETTINGS`` moves the file somewhere else, which tests and portable
    installs use to keep the real one - and the user's home - untouched.
    """
    override = os.environ.get(ENV_PREFIX + "SETTINGS", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "myftp" / "settings.json"


def load_settings() -> dict[str, str]:
    """Read the settings file; a missing or corrupt one simply means defaults."""
    try:
        raw = json.loads(settings_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def save_setting(key: str, value: str) -> None:
    """Remember one user choice; a read-only home is not worth crashing over."""
    settings = load_settings()
    settings[key] = value
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(path.name + ".tmp")
        staged.write_text(json.dumps(settings, indent=2) + "\n", "utf-8")
        os.replace(staged, path)  # atomic: readers never see half a file
    except OSError as exc:  # pragma: no cover - only on odd file systems
        logger.warning("could not save settings to %s: %s", path, exc)


def clean_name(text: str, fallback: str) -> str:
    """Tidy a user-typed display name: single spaces, no stray ends, not too long."""
    clean = " ".join(text.split())
    if len(clean) > NAME_LIMIT:
        clean = clean[:NAME_LIMIT].rstrip()
    return clean or fallback


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Everything the app needs to run, resolved from CLI flags and env vars.

    The dataclass is frozen: one config, shared by every part of a running app,
    is a value - nothing edits it after startup.
    """

    mode: Mode = Mode.GUI
    name: str = field(default_factory=_default_name)
    port: int = DEFAULT_TCP_PORT
    discovery_port: int = DEFAULT_DISCOVERY_PORT
    discovery_enabled: bool = True
    save_dir: Path = field(default_factory=default_save_dir)
    manual_peers: tuple[tuple[str, int], ...] = ()
    chunk_size: int = DEFAULT_CHUNK_SIZE
    offer_timeout: float = DEFAULT_OFFER_TIMEOUT
    ack_timeout: float = DEFAULT_ACK_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT


# --------------------------------------------------------------------------
# CLI parsing
# --------------------------------------------------------------------------


def _host_port(text: str) -> tuple[str, int]:
    """argparse type for ``HOST:PORT`` values (``--connect``)."""
    host, sep, port_text = text.rpartition(":")
    if not sep or not host:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {text!r}")
    try:
        port = int(port_text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port is not a number: {text!r}") from None
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f"port out of range: {text!r}")
    return host, port


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 0 <= port < 65536:
        raise argparse.ArgumentTypeError(f"port out of range: {text!r}")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myftp",
        description=(
            "LAN peer-to-peer file transfer. Default is the Flet GUI; "
            "--text starts a terminal UI (no Flet needed)."
        ),
        epilog=(
            "Every flag can also be set through an MYFTP_* environment "
            "variable (MYFTP_PORT, MYFTP_NAME, MYFTP_CONNECT, ...), which is "
            "handy because `flet run` cannot pass app flags."
        ),
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="run a terminal UI instead of the Flet GUI",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="line-oriented TUI: no full-screen drawing (implies --text; "
        "selected automatically when stdout is not a terminal)",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        default=None,
        help="display name shown to other peers (default: the name last "
        "saved in the UI, or the hostname)",
    )
    parser.add_argument(
        "--port",
        type=_port,
        metavar="N",
        default=None,
        help=f"TCP port to listen on, 0 = any free one (default: {DEFAULT_TCP_PORT})",
    )
    parser.add_argument(
        "--discovery-port",
        type=_port,
        metavar="N",
        default=None,
        help=f"UDP port used to find peers (default: {DEFAULT_DISCOVERY_PORT})",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="do not broadcast/scan on the LAN (pair with --connect)",
    )
    parser.add_argument(
        "--connect",
        type=_host_port,
        metavar="HOST:PORT",
        action="append",
        default=None,
        help="add a peer manually; repeatable, e.g. --connect 192.168.1.9:9100",
    )
    parser.add_argument(
        "--save-dir",
        metavar="DIR",
        default=None,
        help="folder accepted files are saved to (default: the working directory)",
    )
    return parser


def resolve_config(argv: Sequence[str] | None = None) -> AppConfig:
    """Merge CLI flags, environment variables and defaults (CLI wins)."""
    args, _unknown = build_parser().parse_known_args(argv)
    env = os.environ

    def env_int(key: str) -> int | None:
        raw = env.get(ENV_PREFIX + key)
        if raw is None or not raw.strip():
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    text_mode = bool(args.text or args.plain) or _truthy(env, "TEXT")
    if args.plain or (text_mode and not sys.stdout.isatty()):
        mode = Mode.PLAIN
    elif text_mode:
        mode = Mode.CURSES
    else:
        mode = Mode.GUI

    port = args.port
    if port is None:
        port = env_int("PORT")
    if port is None:
        port = DEFAULT_TCP_PORT

    discovery_port = args.discovery_port
    if discovery_port is None:
        discovery_port = env_int("DISCOVERY_PORT")
    if discovery_port is None:
        discovery_port = DEFAULT_DISCOVERY_PORT

    discovery_enabled = not args.no_discovery and not _falsy(env, "DISCOVERY")

    explicit_name = args.name or env.get(ENV_PREFIX + "NAME", "").strip()
    name = clean_name(explicit_name or load_settings().get("name", ""), _default_name())

    save_raw = args.save_dir or env.get(ENV_PREFIX + "SAVE_DIR", "").strip()
    save_dir = Path(save_raw).expanduser() if save_raw else default_save_dir()

    manual_peers = tuple(args.connect or ()) + tuple(
        _env_manual_peers(env.get(ENV_PREFIX + "CONNECT", ""))
    )

    return AppConfig(
        mode=mode,
        name=name,
        port=port,
        discovery_port=discovery_port,
        discovery_enabled=discovery_enabled,
        save_dir=save_dir,
        manual_peers=manual_peers,
    )


def _truthy(env: os._Environ[str], key: str) -> bool:
    return env.get(ENV_PREFIX + key, "").strip().lower() in _TRUTHY


def _falsy(env: os._Environ[str], key: str) -> bool:
    return env.get(ENV_PREFIX + key, "").strip().lower() in _FALSY


def _env_manual_peers(raw: str) -> list[tuple[str, int]]:
    peers: list[tuple[str, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            peers.append(_host_port(chunk))
        except argparse.ArgumentTypeError:
            continue  # ignore junk in the environment
    return peers
