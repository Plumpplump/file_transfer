"""Look and feel for the Flet GUI: palette, radar geometry, small factories.

Everything here is plain Flet dataclasses and pure functions - no page access,
nothing network-related - which keeps the visual language in one place and the
app module focused on behaviour. The palette follows ``reference.webp``: a deep
navy surface, one teal accent, white rounded cards on a cool grey background.
"""

from __future__ import annotations

import math

import flet as ft

# -- palette -----------------------------------------------------------------
BG = "#EDF2F6"  # page background: cool, light, slightly blue
SURFACE = "#FFFFFF"
NAVY = "#12314A"  # header / identity surfaces
NAVY_DEEP = "#0C2233"  # what shadows are made of
TEAL = "#0F9D8A"  # the one accent colour
TEAL_DARK = "#0A7A6B"
TEAL_SOFT = "#DCF2EE"
MINT = "#8FE3D2"
INK = "#152430"  # primary text
MUTED = "#6B8494"  # secondary text
DANGER = "#D05060"
SUCCESS = "#13996B"
AMBER = "#D98A2B"
WHITE = "#FFFFFF"

#: The radar is square; devices sit on a ring inside it.
RADAR_SIZE = 228
RADAR_CHIP = 36
CARD_RADIUS = 20


def alpha(opacity: float, color: str) -> str:
    """``color`` at ``opacity`` (an ``#RRGGBBAA`` string Flutter understands)."""
    return ft.Colors.with_opacity(opacity, color)


def shadow(y: float = 6, blur: float = 20, opacity: float = 0.08) -> ft.BoxShadow:
    """One soft drop shadow, used by every card."""
    return ft.BoxShadow(
        blur_radius=blur,
        spread_radius=0,
        color=alpha(opacity, NAVY_DEEP),
        offset=ft.Offset(0, y),
    )


def card(
    content: ft.Control,
    *,
    padding: float = 16,
    radius: float = CARD_RADIUS,
    bgcolor: str = SURFACE,
    on_click: ft.ControlEventHandler | None = None,
    ink: bool = True,
    expand: bool | None = None,
    border: ft.Border | None = None,
    key: str | None = None,
) -> ft.Container:
    """A white, softly shadowed rounded panel - the GUI's one shape."""
    return ft.Container(
        content=content,
        key=key,
        padding=ft.Padding.all(padding),
        border_radius=ft.BorderRadius.all(radius),
        bgcolor=bgcolor,
        border=border,
        on_click=on_click,
        ink=ink,
        expand=expand,
        shadow=shadow(),
    )


def initials(name: str) -> str:
    """One or two letters to put in an avatar; never empty."""
    words = [
        word
        for word in name.replace("_", " ").replace("-", " ").replace(".", " ").split()
        if word
    ]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def avatar(
    label: str,
    *,
    size: int = 42,
    bgcolor: str = TEAL,
    color: str = WHITE,
) -> ft.Container:
    """A round badge with ``label`` in it, for people (names) and things (icons)."""
    return ft.Container(
        content=ft.Text(
            label,
            size=max(10, int(size * 0.36)),
            weight=ft.FontWeight.W_700,
            color=color,
        ),
        width=size,
        height=size,
        shape=ft.BoxShape.CIRCLE,
        bgcolor=bgcolor,
        alignment=ft.Alignment.CENTER,
    )


# -- radar geometry (pure functions, trivial to test) ------------------------
def device_angle(index: int, count: int) -> float:
    """Radians for device ``index`` of ``count``; device 0 sits at the top."""
    if count <= 0:
        return -math.pi / 2
    return -math.pi / 2 + (index % count) * (2 * math.pi / count)


def device_offset(index: int, count: int, radius: float) -> tuple[float, float]:
    """Centre-relative ``(x, y)`` for a radar chip: an evenly spread ring."""
    angle = device_angle(index, count)
    return radius * math.cos(angle), radius * math.sin(angle)
