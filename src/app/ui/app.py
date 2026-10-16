"""The Flet GUI: a radar of nearby devices and one transfer at a time.

The layout follows ``reference.webp``:

* a navy header with this machine's name, port and listening state,
* two quick-action cards ("send files" / "receive"),
* a radar screen where discovered peers orbit the local device, and
* one banner, pinned above the navigation bar, that shows the transfer in
  flight and the outcome of the last one.

All networking lives in :mod:`app.core.engine`. This module only translates
engine events into controls and user taps back into engine calls, so every
interesting flow can be exercised head-lessly by calling the public methods
(:meth:`MyFtpApp.select_tab`, :meth:`MyFtpApp.ask_send`,
:meth:`MyFtpApp.choose_and_send`, :meth:`MyFtpApp.respond_offer`,
:meth:`MyFtpApp.connect_manual`, :meth:`MyFtpApp.rename`) and inspecting the
control tree.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import flet as ft

from p2p import PeerInfo

from app.core import protocol
from app.core.config import NAME_LIMIT, AppConfig
from app.core.engine import (
    Direction,
    Event,
    IncomingOffer,
    PeersChanged,
    TransferEngine,
    TransferFinished,
    TransferProgress,
    TransferReply,
    TransferStarted,
)

from . import theme

logger = logging.getLogger("myftp.ui")

#: How many rows the "Recent activity" card keeps.
RECENT_LIMIT = 8
#: How long a finished transfer stays in the banner before hiding itself.
RESULT_BANNER_SECONDS = 6.0
#: How long to wait before nudging the radar, so the client has mounted it and
#: its implicit animations can actually run (see :meth:`MyFtpApp._kick_radar`).
RADAR_KICK_DELAY = 0.12
#: The halo breathes between these scales; the sweep turns by pi per leg.
PULSE_MIN, PULSE_MAX = 0.86, 1.18


@dataclass(slots=True)
class TransferView:
    """What the banner needs to know about the transfer on screen."""

    tid: str
    direction: Direction
    filename: str
    size: int
    peer: str
    done: int = 0


@dataclass(slots=True)
class Activity:
    """One line in "Recent activity"."""

    ok: bool
    title: str
    detail: str


class MyFtpApp:
    """The GUI: two screens (home, nearby) and a transfer banner."""

    def __init__(self, page: ft.Page, config: AppConfig) -> None:
        self.page = page
        self.config = config
        self.engine = TransferEngine(config, self.on_event)

        self.peers: tuple[PeerInfo, ...] = ()
        self.transfer: TransferView | None = None
        self.activity: list[Activity] = []

        self._alive = True
        self._offer: IncomingOffer | None = None
        self._dialog: ft.AlertDialog | None = None
        self._manual_field: ft.TextField | None = None
        self._manual_error: ft.Text | None = None
        self._name_field: ft.TextField | None = None
        self._name_error: ft.Text | None = None
        self._sweep_angle = 0.0
        self._hide_generation = 0
        self._picker = ft.FilePicker()

        self._build_controls()
        self._render_peers()
        self._render_activity()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_controls(self) -> None:
        self._nav = ft.NavigationBar(
            selected_index=0,
            on_change=self._on_nav_change,
            bgcolor=theme.SURFACE,
            indicator_color=theme.TEAL_SOFT,
            height=64,
            destinations=[
                ft.NavigationBarDestination(
                    icon=ft.Icons.HOME_OUTLINED,
                    selected_icon=ft.Icons.HOME,
                    label="Home",
                ),
                ft.NavigationBarDestination(
                    icon=ft.Icons.RADAR, selected_icon=ft.Icons.RADAR, label="Nearby"
                ),
            ],
        )
        self._build_banner()
        self._screens = [self._build_home(), self._build_nearby()]
        self._content = ft.Column(controls=[self._screens[0]], expand=True, spacing=0)
        self._root = ft.Column(
            controls=[self._content, self._banner], expand=True, spacing=0
        )

    # -- banner --------------------------------------------------------------
    def _build_banner(self) -> None:
        self._banner_icon = theme.avatar("", size=38, bgcolor=theme.TEAL_SOFT)
        self._banner_icon.content = ft.Icon(
            ft.Icons.UPLOAD_FILE, size=20, color=theme.TEAL
        )
        self._banner_title = ft.Text(
            "",
            size=14,
            weight=ft.FontWeight.W_600,
            color=theme.INK,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._banner_detail = ft.Text(
            "",
            size=12,
            color=theme.MUTED,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._banner_bar = ft.ProgressBar(
            value=0.0,
            color=theme.TEAL,
            bgcolor=theme.TEAL_SOFT,
            bar_height=6,
            border_radius=ft.BorderRadius.all(3),
        )
        self._banner_button = ft.TextButton(
            content=ft.Text("Cancel"),
            icon=ft.Icons.CLOSE,
            on_click=self._on_banner_button,
        )
        self._banner = theme.card(
            ft.Row(
                controls=[
                    self._banner_icon,
                    ft.Column(
                        controls=[
                            self._banner_title,
                            self._banner_detail,
                            self._banner_bar,
                        ],
                        spacing=4,
                        tight=True,
                        expand=True,
                    ),
                    self._banner_button,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=12,
            radius=18,
            key="banner",
        )
        self._banner.visible = False
        self._banner.margin = ft.Margin.only(left=12, right=12, bottom=10)
        self._banner.shadow = theme.shadow(8, 22, 0.16)

    # -- home screen ---------------------------------------------------------
    def _build_home(self) -> ft.Control:
        self._header_sub = ft.Text(
            "starting…",
            size=12,
            color=theme.alpha(0.72, theme.WHITE),
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        self._state_dot = ft.Container(
            width=7, height=7, shape=ft.BoxShape.CIRCLE, bgcolor=theme.MINT
        )
        self._state_text = ft.Text(
            "starting…", size=11, weight=ft.FontWeight.W_600, color=theme.WHITE
        )
        state = ft.Container(
            content=ft.Row(
                controls=[self._state_dot, self._state_text], spacing=6, tight=True
            ),
            padding=ft.Padding.symmetric(vertical=4, horizontal=10),
            border_radius=ft.BorderRadius.all(999),
            bgcolor=theme.alpha(0.16, theme.WHITE),
        )
        self._header_avatar = theme.avatar(
            theme.initials(self.engine.name),
            size=46,
            bgcolor=theme.alpha(0.18, theme.WHITE),
            color=theme.WHITE,
        )
        header = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                self._header_avatar,
                                ft.Column(
                                    controls=[
                                        ft.Text(
                                            "myftp",
                                            size=22,
                                            weight=ft.FontWeight.W_700,
                                            color=theme.WHITE,
                                        ),
                                        self._header_sub,
                                    ],
                                    spacing=2,
                                    tight=True,
                                    expand=True,
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        on_click=lambda _event: self.ask_rename(),
                        ink=True,
                        border_radius=ft.BorderRadius.all(14),
                        tooltip="Change your name",
                        expand=True,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.EDIT_OUTLINED,
                        icon_size=18,
                        icon_color=theme.alpha(0.85, theme.WHITE),
                        tooltip="Change your name",
                        on_click=lambda _event: self.ask_rename(),
                        key="name-edit",
                    ),
                    state,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.only(left=18, right=18, top=24, bottom=24),
            bgcolor=theme.NAVY,
            border_radius=ft.BorderRadius.only(bottom_left=26, bottom_right=26),
            shadow=theme.shadow(10, 26, 0.18),
            key="home-header",
        )

        send_card = theme.card(
            ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Icon(
                            ft.Icons.UPLOAD_FILE, size=22, color=theme.WHITE
                        ),
                        width=42,
                        height=42,
                        shape=ft.BoxShape.CIRCLE,
                        bgcolor=theme.alpha(0.18, theme.WHITE),
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text(
                        "Send files",
                        size=15,
                        weight=ft.FontWeight.W_700,
                        color=theme.WHITE,
                    ),
                    ft.Text(
                        "Pick a device, then a file",
                        size=11,
                        color=theme.alpha(0.8, theme.WHITE),
                    ),
                ],
                spacing=8,
                tight=True,
            ),
            bgcolor=theme.TEAL,
            on_click=lambda _event: self.select_tab(1),
            expand=True,
            key="quick-send",
        )
        receive_card = theme.card(
            ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Icon(
                            ft.Icons.FILE_DOWNLOAD, size=22, color=theme.NAVY
                        ),
                        width=42,
                        height=42,
                        shape=ft.BoxShape.CIRCLE,
                        bgcolor=theme.alpha(0.10, theme.NAVY),
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text(
                        "Receive",
                        size=15,
                        weight=ft.FontWeight.W_700,
                        color=theme.INK,
                    ),
                    ft.Text(
                        "Offers pop up here", size=11, color=theme.MUTED
                    ),
                ],
                spacing=8,
                tight=True,
            ),
            on_click=lambda _event: self.show_receive_info(),
            expand=True,
            key="quick-receive",
        )
        quick = ft.Row(controls=[send_card, receive_card], spacing=12)

        self._home_peers_summary = ft.Text("no devices yet", size=12, color=theme.MUTED)
        self._home_peers_preview = ft.Column(spacing=6, tight=True)
        nearby_card = theme.card(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text(
                                "Nearby devices",
                                size=14,
                                weight=ft.FontWeight.W_700,
                                color=theme.INK,
                            ),
                            self._home_peers_summary,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._home_peers_preview,
                    ft.TextButton(
                        content=ft.Text("Open the radar"),
                        icon=ft.Icons.RADAR,
                        on_click=lambda _event: self.select_tab(1),
                    ),
                ],
                spacing=10,
                tight=True,
            ),
            key="nearby-card",
        )

        self._home_activity = ft.Column(spacing=8, tight=True)
        activity_card = theme.card(
            ft.Column(
                controls=[
                    ft.Text(
                        "Recent activity",
                        size=14,
                        weight=ft.FontWeight.W_700,
                        color=theme.INK,
                    ),
                    self._home_activity,
                ],
                spacing=10,
                tight=True,
            ),
            key="activity-card",
        )
        footer = ft.Row(
            controls=[
                ft.Text(
                    f"Received files are saved to {self.config.save_dir}",
                    size=11,
                    color=theme.MUTED,
                )
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        )
        self._home_identity = ft.Text(
            f"You appear to others as {self.engine.name}",
            size=12,
            color=theme.MUTED,
        )
        identity_strip = ft.Container(
            content=ft.Row(
                controls=[
                    self._home_identity,
                    ft.TextButton(
                        content=ft.Text("Change name"),
                        icon=ft.Icons.EDIT_OUTLINED,
                        on_click=lambda _event: self.ask_rename(),
                        key="home-name-edit",
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.only(left=16, right=8, top=10),
        )
        body = ft.Container(
            content=ft.Column(
                controls=[quick, nearby_card, activity_card, footer],
                spacing=14,
                tight=True,
            ),
            padding=ft.Padding.symmetric(vertical=16, horizontal=16),
        )
        return ft.Column(
            controls=[header, identity_strip, body],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # -- nearby screen -------------------------------------------------------
    def _build_nearby(self) -> ft.Control:
        header = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Text(
                                "Nearby devices",
                                size=20,
                                weight=ft.FontWeight.W_700,
                                color=theme.WHITE,
                            ),
                            ft.Text(
                                "everything running myftp on your network",
                                size=12,
                                color=theme.alpha(0.72, theme.WHITE),
                            ),
                        ],
                        spacing=2,
                        tight=True,
                        expand=True,
                    ),
                    ft.TextButton(
                        content=ft.Text("Add device", color=theme.WHITE),
                        icon=ft.Icons.ADD_LINK,
                        icon_color=theme.WHITE,
                        on_click=lambda _event: self.ask_manual_peer(),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(vertical=20, horizontal=18),
            bgcolor=theme.NAVY,
            border_radius=ft.BorderRadius.only(bottom_left=26, bottom_right=26),
            shadow=theme.shadow(10, 26, 0.18),
            key="nearby-header",
        )

        self._radar = self._build_radar()
        self._radar_you = ft.TextButton(
            content=ft.Text(f"You appear as {self.engine.name}", size=12),
            icon=ft.Icons.EDIT_OUTLINED,
            on_click=lambda _event: self.ask_rename(),
            key="nearby-name-edit",
        )
        self._radar_caption = ft.Text(
            "Scanning the LAN…", size=11, color=theme.MUTED
        )
        radar_card = theme.card(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[self._radar], alignment=ft.MainAxisAlignment.CENTER
                    ),
                    self._radar_you,
                    self._radar_caption,
                ],
                spacing=6,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=14,
            key="radar-card",
        )

        self._device_list = ft.Column(spacing=6, tight=True)
        self._devices_empty = ft.Column(
            controls=[
                ft.Text(
                    "No devices yet",
                    size=14,
                    weight=ft.FontWeight.W_600,
                    color=theme.INK,
                ),
                ft.Text(
                    "Keep this screen open: anyone running myftp on your "
                    "network shows up on the radar automatically. You can also "
                    "add a device by address.",
                    size=12,
                    color=theme.MUTED,
                ),
                ft.FilledButton(
                    content=ft.Text("Add by address"),
                    icon=ft.Icons.ADD_LINK,
                    bgcolor=theme.TEAL,
                    color=theme.WHITE,
                    on_click=lambda _event: self.ask_manual_peer(),
                ),
            ],
            spacing=8,
            tight=True,
        )
        devices_card = theme.card(
            ft.Column(
                controls=[
                    ft.Text(
                        "Devices",
                        size=14,
                        weight=ft.FontWeight.W_700,
                        color=theme.INK,
                    ),
                    self._device_list,
                    self._devices_empty,
                    ft.Text(
                        f"Incoming files are saved to {self.config.save_dir}",
                        size=11,
                        color=theme.MUTED,
                    ),
                ],
                spacing=10,
                tight=True,
            ),
            key="devices-card",
        )

        body = ft.Container(
            content=ft.Column(
                controls=[radar_card, devices_card], spacing=14, tight=True
            ),
            padding=ft.Padding.symmetric(vertical=16, horizontal=16),
        )
        return ft.Column(
            controls=[header, body], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True
        )

    def _build_radar(self) -> ft.Stack:
        """The radar: rings, a sweeping wedge, a pulsing halo, device chips."""
        size = theme.RADAR_SIZE
        half = size / 2
        layers: list[ft.Control] = []
        for fraction in (1.0, 0.74, 0.48, 0.22):
            diameter = size * fraction
            layers.append(
                ft.Container(
                    width=diameter,
                    height=diameter,
                    shape=ft.BoxShape.CIRCLE,
                    border=ft.Border.all(1, theme.alpha(0.20, theme.TEAL)),
                    left=half - diameter / 2,
                    top=half - diameter / 2,
                )
            )
        layers.append(
            ft.Container(
                width=size,
                height=1,
                bgcolor=theme.alpha(0.10, theme.TEAL),
                left=0,
                top=half - 0.5,
            )
        )
        layers.append(
            ft.Container(
                width=1,
                height=size,
                bgcolor=theme.alpha(0.10, theme.TEAL),
                left=half - 0.5,
                top=0,
            )
        )

        # The halo and the sweep never stop: each finished animation queues the
        # next leg via on_animation_end (the animations cookbook's "chaining
        # animations" pattern). The chain has to be *started* once the control
        # is on the client though - see `_kick_radar` below.
        halo = half * 1.15
        self._pulse = ft.Container(
            width=halo,
            height=halo,
            shape=ft.BoxShape.CIRCLE,
            bgcolor=theme.alpha(0.12, theme.TEAL),
            left=half - halo / 2,
            top=half - halo / 2,
            scale=PULSE_MIN,
            opacity=0.9,
            # Only one property animates: that gives exactly one
            # "animation_end" event per breath, which is what keeps it simple.
            animate_scale=ft.Animation(2400, ft.AnimationCurve.EASE_IN_OUT),
            on_animation_end=self._on_pulse_end,
        )
        self._sweep = ft.Container(
            width=size,
            height=size,
            left=0,
            top=0,
            shape=ft.BoxShape.CIRCLE,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            gradient=ft.SweepGradient(
                colors=[theme.alpha(0.0, theme.TEAL), theme.alpha(0.42, theme.TEAL)],
                stops=[0.55, 1.0],
                center=ft.Alignment.CENTER,
            ),
            rotate=ft.Rotate(self._sweep_angle, alignment=ft.Alignment.CENTER),
            animate_rotation=ft.Animation(1400, ft.AnimationCurve.LINEAR),
            on_animation_end=self._on_sweep_end,
        )
        dot = ft.Container(
            width=10,
            height=10,
            shape=ft.BoxShape.CIRCLE,
            bgcolor=theme.NAVY,
            border=ft.Border.all(2, theme.WHITE),
            left=half - 5,
            top=half - 5,
        )

        self._radar_base: list[ft.Control] = [*layers, self._pulse, self._sweep, dot]
        return ft.Stack(
            controls=list(self._radar_base), width=size, height=size, key="radar"
        )

    # ------------------------------------------------------------------
    # painting (engine state -> controls)
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        """Push control changes to the client (events outside handlers need it)."""
        if not self._alive:
            return
        try:
            self.page.update()
        except Exception as exc:  # pragma: no cover - page torn down mid-update
            logger.debug("page update failed: %s", exc)

    def _render_peers(self) -> None:
        count = len(self.peers)
        half = theme.RADAR_SIZE / 2
        radius = half - theme.RADAR_CHIP / 2 - 6

        chips: list[ft.Control] = []
        for index, peer in enumerate(self.peers):
            dx, dy = theme.device_offset(index, count, radius)
            chips.append(
                ft.Container(
                    key=f"radar-{peer.host}:{peer.port}",
                    content=ft.Text(
                        theme.initials(peer.name),
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=theme.INK,
                    ),
                    width=theme.RADAR_CHIP,
                    height=theme.RADAR_CHIP,
                    shape=ft.BoxShape.CIRCLE,
                    bgcolor=theme.SURFACE,
                    border=ft.Border.all(2, theme.TEAL),
                    alignment=ft.Alignment.CENTER,
                    ink=True,
                    tooltip=f"{peer.name} · {peer.address}",
                    left=half + dx - theme.RADAR_CHIP / 2,
                    top=half + dy - theme.RADAR_CHIP / 2,
                    animate_position=ft.Animation(
                        700, ft.AnimationCurve.EASE_OUT_CUBIC
                    ),
                    on_click=functools.partial(self._on_peer_tap, peer),
                )
            )
        self._radar.controls = [*self._radar_base, *chips]

        rows = [self._device_row(peer) for peer in self.peers]
        self._device_list.controls = rows
        self._devices_empty.visible = not rows
        if count == 0:
            self._radar_caption.value = (
                "Scanning the LAN - devices appear as they announce themselves"
            )
            self._home_peers_summary.value = "no devices yet"
        else:
            self._radar_caption.value = (
                "1 device in range" if count == 1 else f"{count} devices in range"
            )
            self._home_peers_summary.value = (
                "1 device nearby" if count == 1 else f"{count} devices nearby"
            )

        preview: list[ft.Control] = []
        for peer in self.peers[:2]:
            preview.append(
                ft.Row(
                    controls=[
                        theme.avatar(
                            theme.initials(peer.name),
                            size=28,
                            bgcolor=theme.TEAL_SOFT,
                            color=theme.TEAL_DARK,
                        ),
                        ft.Text(
                            peer.name,
                            size=13,
                            weight=ft.FontWeight.W_600,
                            color=theme.INK,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True,
                        ),
                        ft.Text(peer.address, size=11, color=theme.MUTED),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
        self._home_peers_preview.controls = preview or [
            ft.Text("Scanning the network…", size=12, color=theme.MUTED)
        ]

    def _device_row(self, peer: PeerInfo) -> ft.Control:
        how = "added by hand" if peer.meta.get("manual") else "found on the LAN"
        return ft.Container(
            key=f"peer-{peer.host}:{peer.port}",
            content=ft.Row(
                controls=[
                    theme.avatar(
                        theme.initials(peer.name),
                        size=38,
                        bgcolor=theme.TEAL_SOFT,
                        color=theme.TEAL_DARK,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(
                                peer.name,
                                size=14,
                                weight=ft.FontWeight.W_600,
                                color=theme.INK,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                f"{peer.address} · {how}",
                                size=11,
                                color=theme.MUTED,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=1,
                        tight=True,
                        expand=True,
                    ),
                    ft.FilledButton(
                        content=ft.Text("Send"),
                        icon=ft.Icons.SEND,
                        bgcolor=theme.TEAL,
                        color=theme.WHITE,
                        height=38,
                        on_click=functools.partial(self._on_peer_tap, peer),
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(vertical=8, horizontal=10),
            border_radius=ft.BorderRadius.all(16),
            on_click=functools.partial(self._on_peer_tap, peer),
            ink=True,
        )

    def _render_activity(self) -> None:
        rows: list[ft.Control] = []
        for item in self.activity[:5]:
            color = theme.SUCCESS if item.ok else theme.DANGER
            icon = ft.Icons.CHECK_CIRCLE if item.ok else ft.Icons.ERROR_OUTLINE
            rows.append(
                ft.Row(
                    controls=[
                        ft.Icon(icon, size=18, color=color),
                        ft.Column(
                            controls=[
                                ft.Text(
                                    item.title,
                                    size=13,
                                    weight=ft.FontWeight.W_600,
                                    color=theme.INK,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    item.detail,
                                    size=11,
                                    color=theme.MUTED,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                            spacing=1,
                            tight=True,
                            expand=True,
                        ),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
        self._home_activity.controls = rows or [
            ft.Text(
                "Nothing yet - sent and received files show up here.",
                size=12,
                color=theme.MUTED,
            )
        ]

    def _render_banner(self) -> None:
        view = self.transfer
        if view is None:
            self._banner.visible = False
            return
        self._hide_generation += 1  # a live transfer beats a pending auto-hide
        sending = view.direction == Direction.SEND
        self._banner_icon.content = ft.Icon(
            ft.Icons.UPLOAD_FILE if sending else ft.Icons.DOWNLOAD,
            size=20,
            color=theme.TEAL,
        )
        self._banner_icon.bgcolor = theme.TEAL_SOFT
        self._banner_title.value = (
            f"{'Sending' if sending else 'Receiving'} {view.filename}"
        )
        percent = int(protocol.fraction(view.done, view.size) * 100)
        self._banner_detail.value = (
            f"{percent}% · {protocol.human_size(view.done)} of "
            f"{protocol.human_size(view.size)} · {'to' if sending else 'from'} "
            f"{view.peer}"
        )
        self._banner_bar.visible = True
        self._banner_bar.value = min(1.0, protocol.fraction(view.done, view.size))
        self._banner_button.content = ft.Text("Cancel")
        self._banner_button.icon = ft.Icons.CLOSE
        self._banner.visible = True

    def _show_result(self, ok: bool, title: str, detail: str = "") -> None:
        """Turn the banner into a result strip that hides itself after a while."""
        self.transfer = None
        self._hide_generation += 1
        generation = self._hide_generation
        color = theme.SUCCESS if ok else theme.DANGER
        self._banner_icon.content = ft.Icon(
            ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR_OUTLINE,
            size=20,
            color=color,
        )
        self._banner_icon.bgcolor = theme.alpha(0.12, color)
        self._banner_title.value = title
        self._banner_detail.value = detail
        self._banner_bar.visible = False
        self._banner_button.content = ft.Text("Dismiss")
        self._banner_button.icon = ft.Icons.CLOSE
        self._banner.visible = True
        self._refresh()
        self.page.run_task(self._auto_hide, generation)

    async def _auto_hide(self, generation: int) -> None:
        await asyncio.sleep(RESULT_BANNER_SECONDS)
        if (
            self._alive
            and generation == self._hide_generation
            and self.transfer is None
        ):
            self._hide_banner()

    def _hide_banner(self) -> None:
        self._banner.visible = False
        self._refresh()

    # ------------------------------------------------------------------
    # engine events
    # ------------------------------------------------------------------
    def on_event(self, event: Event) -> None:
        match event:
            case PeersChanged(peers=peers):
                self.peers = tuple(peers)
                self._render_peers()
            case IncomingOffer():
                self._show_offer_dialog(event)
            case TransferStarted():
                self.transfer = TransferView(
                    tid=event.tid,
                    direction=event.direction,
                    filename=event.filename,
                    size=event.size,
                    peer=event.peer,
                )
                self._render_banner()
            case TransferProgress(tid=tid, done=done):
                if self.transfer is not None and self.transfer.tid == tid:
                    self.transfer.done = done
                    self._render_banner()
            case TransferReply(accepted=True):
                if self.transfer is not None:
                    self._banner_detail.value = "Accepted - sending…"
            case TransferReply(accepted=False, reason=reason):
                if self.transfer is not None:
                    self._banner_detail.value = f"Declined: {reason}"
            case TransferFinished():
                self._finish(event)
        self._refresh()

    def _finish(self, event: TransferFinished) -> None:
        tracked = self.transfer
        view = tracked if tracked is not None and tracked.tid == event.tid else None
        name = view.filename if view is not None else ""
        sending = event.direction == Direction.SEND

        if event.ok and sending:
            title = f"Delivered to {view.peer}" if view is not None else "Delivered"
            detail = name
        elif event.ok:
            title = f"Saved {name}".strip()
            detail = str(event.path) if event.path is not None else ""
        else:
            title = event.message
            detail = f"{name} · {view.peer}".strip(" ·") if view is not None else ""

        verb = "Sent" if sending else "Received"
        self.activity.insert(
            0,
            Activity(
                ok=event.ok,
                title=f"{verb} {name}".strip(),
                detail=event.message,
            ),
        )
        del self.activity[RECENT_LIMIT:]
        self._render_activity()
        self._show_result(event.ok, title, detail)

    # ------------------------------------------------------------------
    # user actions
    # ------------------------------------------------------------------
    def select_tab(self, index: int) -> None:
        """Show screen ``0`` (home) or ``1`` (nearby) and sync the nav bar."""
        index = max(0, min(index, len(self._screens) - 1))
        entering_radar = index == 1 and self._nav.selected_index != 1
        self._nav.selected_index = index
        self._content.controls = [self._screens[index]]
        self._refresh()
        if entering_radar:
            # Leaving the nearby screen tears its animations down with it, so
            # every visit starts the chain again.
            self.page.run_task(self._kick_radar)

    def pick_peer(self, peer: PeerInfo) -> None:
        """A device was chosen on the radar or in the list: offer to send."""
        self.ask_send(peer)

    async def choose_and_send(self, peer: PeerInfo) -> None:
        """Pick a local file and offer it - the second half of the send flow."""
        self._close_dialog()
        try:
            path = await self._choose_file()
        except Exception as exc:  # the picker is a platform service; it can fail
            logger.warning("file picker failed: %s", exc)
            self._show_result(False, "Could not open the file picker", str(exc))
            return
        if not path:
            return
        logger.info("offering %s to %s", path, peer.address)
        self.page.run_task(self.engine.send_file, peer.host, peer.port, path)

    async def _choose_file(self) -> str | None:
        """The platform file dialog; tests substitute this seam."""
        files = await self._picker.pick_files(dialog_title="Choose a file to send")
        if not files:
            return None
        return files[0].path

    async def respond_offer(self, accept: bool) -> None:
        """Answer the pending incoming offer (the dialog's buttons call this)."""
        offer = self._offer
        self._offer = None
        self._close_dialog()
        if offer is None:
            return
        await self.engine.respond(offer.tid, accept=accept)

    def connect_manual(self, text: str) -> bool:
        """Parse ``HOST:PORT``, register the peer and jump to the radar."""
        host, separator, port_text = text.strip().rpartition(":")
        port = int(port_text) if port_text.isdigit() else 0
        if not separator or not host or not 0 < port < 65536:
            self._show_manual_error("Expected HOST:PORT, for example 192.168.1.20:9100")
            return False
        self.engine.add_manual_peer(host, port)
        self._manual_field = None
        self._manual_error = None
        self._close_dialog()
        self.select_tab(1)
        return True

    def rename(self, value: str) -> str:
        """Use ``value`` as the display name and repaint where it shows.

        The engine advertises it in the next beacon and in every offer from
        now on, and remembers it for the next launch. Empty input keeps the
        current name. Returns the name that is in use now.
        """
        name = self.engine.set_name(value)
        self._name_field = None
        self._name_error = None
        self._paint_name()
        self._close_dialog()
        self._refresh()
        return name

    def _paint_name(self) -> None:
        """Show the current name in every spot that carries it."""
        name = self.engine.name
        self._header_avatar.content.value = theme.initials(name)
        self._header_sub.value = f"{name} · port {self.engine.port}"
        self._home_identity.value = f"You appear to others as {name}"
        self._radar_you.content.value = f"You appear as {name}"

    # -- dialogs -------------------------------------------------------------
    def _show_offer_dialog(self, offer: IncomingOffer) -> None:
        self._offer = offer
        file_card = theme.card(
            ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Icon(
                            ft.Icons.FILE_DOWNLOAD, size=20, color=theme.TEAL
                        ),
                        width=38,
                        height=38,
                        shape=ft.BoxShape.CIRCLE,
                        bgcolor=theme.TEAL_SOFT,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(
                                offer.filename,
                                size=14,
                                weight=ft.FontWeight.W_600,
                                color=theme.INK,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                f"{protocol.human_size(offer.size)} · saved to "
                                f"{self.config.save_dir}",
                                size=11,
                                color=theme.MUTED,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                        expand=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=12,
            radius=16,
            bgcolor=theme.BG,
            ink=False,
        )
        content = ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        theme.avatar(theme.initials(offer.sender)),
                        ft.Column(
                            controls=[
                                ft.Text(
                                    offer.sender,
                                    size=15,
                                    weight=ft.FontWeight.W_600,
                                    color=theme.INK,
                                ),
                                ft.Text(offer.address, size=12, color=theme.MUTED),
                            ],
                            spacing=1,
                            tight=True,
                            expand=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                file_card,
            ],
            spacing=14,
            tight=True,
            width=330,
        )
        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Incoming file", size=18, weight=ft.FontWeight.W_700),
            content=content,
            actions=[
                ft.TextButton(
                    content=ft.Text("Decline"),
                    on_click=lambda _event: self.page.run_task(
                        self.respond_offer, False
                    ),
                ),
                ft.FilledButton(
                    content=ft.Text("Accept"),
                    icon=ft.Icons.DOWNLOAD,
                    bgcolor=theme.TEAL,
                    color=theme.WHITE,
                    on_click=lambda _event: self.page.run_task(
                        self.respond_offer, True
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: self._dismiss_offer(),
        )
        self.page.show_dialog(self._dialog)

    def _dismiss_offer(self) -> None:
        # The framework already took the dialog down; just answer "declined".
        self._dialog = None
        self.page.run_task(self.respond_offer, False)

    def ask_send(self, peer: PeerInfo) -> None:
        """Show the "send to <peer>" prompt (radar chips and rows call this)."""
        self._close_dialog()
        how = "added by hand" if peer.meta.get("manual") else "found on your LAN"
        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Send to {peer.name}", size=18, weight=ft.FontWeight.W_700),
            content=ft.Column(
                controls=[
                    ft.Text(f"{peer.address} · {how}", size=12, color=theme.MUTED),
                    ft.Text(
                        "Pick a file on this computer. The other side gets a "
                        "prompt and decides whether to accept it.",
                        size=13,
                        color=theme.INK,
                    ),
                ],
                spacing=10,
                tight=True,
                width=320,
            ),
            actions=[
                ft.TextButton(
                    content=ft.Text("Cancel"),
                    on_click=lambda _event: self._close_dialog(),
                ),
                ft.FilledButton(
                    content=ft.Text("Choose a file"),
                    icon=ft.Icons.FOLDER_OPEN,
                    bgcolor=theme.TEAL,
                    color=theme.WHITE,
                    on_click=lambda _event: self.page.run_task(
                        self.choose_and_send, peer
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: self._forget_dialog(),
        )
        self.page.show_dialog(self._dialog)

    def ask_manual_peer(self) -> None:
        """Ask for a ``HOST:PORT`` when discovery cannot find the peer."""
        self._manual_field = ft.TextField(
            label="Peer address",
            hint_text="192.168.1.20:9100",
            autofocus=True,
            border_radius=ft.BorderRadius.all(12),
            on_submit=lambda _event: self._submit_manual(),
        )
        self._manual_error = ft.Text("", size=12, color=theme.DANGER, visible=False)
        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                "Connect by address", size=18, weight=ft.FontWeight.W_700
            ),
            content=ft.Column(
                controls=[
                    ft.Text(
                        "Devices on your LAN are found automatically - use this "
                        "when discovery is blocked.",
                        size=12,
                        color=theme.MUTED,
                    ),
                    self._manual_field,
                    self._manual_error,
                ],
                spacing=10,
                tight=True,
                width=320,
            ),
            actions=[
                ft.TextButton(
                    content=ft.Text("Cancel"),
                    on_click=lambda _event: self._close_dialog(),
                ),
                ft.FilledButton(
                    content=ft.Text("Connect"),
                    icon=ft.Icons.ADD_LINK,
                    bgcolor=theme.TEAL,
                    color=theme.WHITE,
                    on_click=lambda _event: self._submit_manual(),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: self._forget_dialog(),
        )
        self.page.show_dialog(self._dialog)

    def ask_rename(self) -> None:
        """Ask for a display name (the pencil in the home header calls this)."""
        self._name_field = ft.TextField(
            label="Your name",
            value=self.engine.name,
            autofocus=True,
            max_length=NAME_LIMIT,
            border_radius=ft.BorderRadius.all(12),
            on_submit=lambda _event: self._submit_name(),
            key="name-field",
        )
        self._name_error = ft.Text("", size=12, color=theme.DANGER, visible=False)
        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Your name", size=18, weight=ft.FontWeight.W_700),
            content=ft.Column(
                controls=[
                    ft.Text(
                        "This is the name other devices see on the radar and "
                        "in the offers you send.",
                        size=12,
                        color=theme.MUTED,
                    ),
                    self._name_field,
                    self._name_error,
                ],
                spacing=10,
                tight=True,
                width=320,
            ),
            actions=[
                ft.TextButton(
                    content=ft.Text("Cancel"),
                    on_click=lambda _event: self._close_dialog(),
                ),
                ft.FilledButton(
                    content=ft.Text("Save"),
                    icon=ft.Icons.CHECK,
                    bgcolor=theme.TEAL,
                    color=theme.WHITE,
                    on_click=lambda _event: self._submit_name(),
                    key="name-save",
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: self._forget_dialog(),
        )
        self.page.show_dialog(self._dialog)

    def show_receive_info(self) -> None:
        self._dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Receiving files", size=18, weight=ft.FontWeight.W_700),
            content=ft.Column(
                controls=[
                    ft.Text(
                        "Whenever somebody offers you a file a prompt appears - "
                        "nothing is written to disk until you accept it.",
                        size=13,
                        color=theme.INK,
                    ),
                    ft.Text(
                        f"Accepted files land in {self.config.save_dir}",
                        size=12,
                        color=theme.MUTED,
                    ),
                ],
                spacing=10,
                tight=True,
                width=320,
            ),
            actions=[
                ft.FilledButton(
                    content=ft.Text("Got it"),
                    bgcolor=theme.TEAL,
                    color=theme.WHITE,
                    on_click=lambda _event: self._close_dialog(),
                )
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: self._forget_dialog(),
        )
        self.page.show_dialog(self._dialog)

    # -- plumbing ------------------------------------------------------------
    def _submit_manual(self) -> None:
        value = self._manual_field.value if self._manual_field is not None else ""
        self.connect_manual(value or "")

    def _submit_name(self) -> None:
        value = self._name_field.value if self._name_field is not None else ""
        if not value.strip():
            self._show_name_error("Please type a name")
            return
        self.rename(value)

    def _show_manual_error(self, message: str) -> None:
        if self._manual_error is None:
            return
        self._manual_error.value = message
        self._manual_error.visible = True
        self._refresh()

    def _show_name_error(self, message: str) -> None:
        if self._name_error is None:
            return
        self._name_error.value = message
        self._name_error.visible = True
        self._refresh()

    def _close_dialog(self) -> None:
        self._dialog = None
        try:
            self.page.pop_dialog()
        except Exception as exc:  # pragma: no cover - stack already empty
            logger.debug("pop_dialog failed: %s", exc)

    def _forget_dialog(self) -> None:
        self._dialog = None

    def _on_peer_tap(self, peer: PeerInfo, _event: ft.ControlEvent) -> None:
        self.pick_peer(peer)

    def _on_nav_change(self, event: ft.ControlEvent) -> None:
        index = getattr(event.control, "selected_index", None)
        if index is None:
            try:
                index = int(event.data)
            except (TypeError, ValueError):
                index = self._nav.selected_index
        self.select_tab(int(index or 0))

    def _on_banner_button(self, _event: ft.ControlEvent) -> None:
        if self.transfer is not None:
            self.page.run_task(self.engine.cancel, self.transfer.tid)
            return
        self._hide_banner()

    async def _kick_radar(self) -> None:
        """Start the radar's animations, once, after its controls are mounted.

        Implicit animations only run when a value changes on a control the
        client *already has* - and nothing on the radar ever changes by itself.
        Without this nudge `on_animation_end` never fires and the sweep just
        sits there. Runs a beat after the screen swap, because the patch that
        creates the controls has to land first.
        """
        await asyncio.sleep(RADAR_KICK_DELAY)
        self._turn_sweep()
        self._breathe_halo()
        self._refresh()

    def _turn_sweep(self) -> None:
        """Queue the sweep's next half-turn (never modded: the angle only grows)."""
        self._sweep_angle += math.pi
        self._sweep.rotate.angle = self._sweep_angle

    def _breathe_halo(self) -> None:
        """Queue the halo's next leg - exactly one property animates, so one
        finished animation means one event."""
        self._pulse.scale = PULSE_MIN if self._pulse.scale > 1.0 else PULSE_MAX

    def _on_sweep_end(self, _event: ft.ControlEvent) -> None:
        # Chaining: every finished half-turn queues the next one (the
        # animations cookbook's "chaining animations" pattern).
        self._turn_sweep()
        self._refresh()

    def _on_pulse_end(self, _event: ft.ControlEvent) -> None:
        self._breathe_halo()
        self._refresh()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def mount(self) -> None:
        """Attach the app to its page: call once, from ``ft.run``."""
        page = self.page
        page.title = "myftp"
        page.theme = ft.Theme(color_scheme_seed=theme.TEAL)
        page.theme_mode = ft.ThemeMode.LIGHT
        page.bgcolor = theme.BG
        page.padding = 0
        page.window.width = 430
        page.window.height = 780
        page.window.min_width = 360
        page.window.min_height = 560
        page.navigation_bar = self._nav
        page.services.append(self._picker)
        page.on_disconnect = self._on_disconnect
        page.add(self._root)
        page.run_task(self.start)

    async def start(self) -> None:
        """Start the engine, then paint the header with the real address."""
        await self.engine.start()
        self._paint_name()
        if self.engine.discovery is not None:
            self._state_dot.bgcolor = theme.MINT
            self._state_text.value = f"radar on UDP {self.config.discovery_port}"
        else:
            self._state_dot.bgcolor = theme.AMBER
            self._state_text.value = "manual peers only"
        self._render_peers()
        self._refresh()
        if self.engine.discovery_error:
            self._show_result(
                False, "LAN discovery is off", self.engine.discovery_error
            )

    def _on_disconnect(self, _event: ft.ControlEvent | None = None) -> None:
        """The window/tab went away: nothing is left to paint, stop the engine."""
        self._alive = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop to schedule on
            return
        try:
            loop.create_task(self.engine.stop())
        except RuntimeError as exc:  # pragma: no cover - loop already closed
            logger.debug("could not stop the engine on disconnect: %s", exc)


def build_app(config: AppConfig, page: ft.Page) -> MyFtpApp:
    """``ft.run`` entry point: build the GUI, mount it, start the engine."""
    app = MyFtpApp(page, config)
    app.mount()
    return app
