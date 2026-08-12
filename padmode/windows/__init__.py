"""Window backends: find a window, focus a window, ask what is focused.

Autodetected from the session, overridable with ``[windows] backend``. Only two
backends ship (CLAUDE.md §2): ``kwin`` for KDE Wayland and ``x11`` for any X11
session. Everything else -- GNOME Wayland above all -- gets no backend, and
overlay mode disables itself with one line saying why.

That is not an oversight to be fixed later. GNOME's Wayland compositor refuses
window activation requests from clients by design, so there is nothing for a
backend to call. Saying so in the README and in the log is the whole strategy
(CLAUDE.md §3.3).

Detection returns a *reason* alongside the backend, so ``padmode windows`` and
the startup log can both explain themselves without duplicating the rules.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .backend import Rect, WindowBackend, WindowId, run_tool
from .kwin import TOOL as KWIN_TOOL
from .kwin import KwinBackend
from .x11 import TOOL as X11_TOOL
from .x11 import X11Backend

log = logging.getLogger(__name__)

__all__ = [
    "Detection",
    "KwinBackend",
    "Rect",
    "WindowBackend",
    "WindowId",
    "X11Backend",
    "backend_names",
    "detect",
    "run_tool",
]

Which = Callable[[str], "str | None"]

#: Accepted values of ``[windows] backend``.
BACKENDS = {"kwin": KwinBackend, "x11": X11Backend}


def backend_names() -> list[str]:
    """Everything accepted by ``[windows] backend``, for error messages."""
    return ["auto", *sorted(BACKENDS), "none"]


@dataclass(frozen=True)
class Detection:
    """A backend, or nothing, and a sentence explaining which and why."""

    backend: WindowBackend | None
    reason: str

    def __bool__(self) -> bool:
        return self.backend is not None


def detect(
    preference: str = "auto",
    env: Mapping[str, str] | None = None,
    which: Which | None = None,
) -> Detection:
    """Choose a window backend for this session.

    ``preference`` comes from config: ``auto``, a backend name, or ``none``.
    An explicit choice is honoured without sniffing the session -- someone
    running a compositor we have never heard of may well know that ``xdotool``
    works there, and second-guessing them helps nobody.
    """
    env = os.environ if env is None else env
    which = which or shutil.which

    if preference == "none":
        return Detection(None, 'disabled by config ([windows] backend = "none")')
    if preference in BACKENDS:
        return _build(preference, which, f'[windows] backend = "{preference}"')
    if preference != "auto":  # unreachable via validated config
        return Detection(None, f"unknown backend {preference!r}")

    session = _session_type(env)
    if session == "x11":
        return _build("x11", which, "X11 session")
    if session == "wayland":
        if _is_kde(env):
            return _build("kwin", which, "KDE Wayland session")
        desktop = env.get("XDG_CURRENT_DESKTOP") or "this compositor"
        if "gnome" in desktop.lower():
            return Detection(
                None,
                "GNOME on Wayland refuses window activation from clients, so "
                "overlays cannot work there. Everything else still does.",
            )
        return Detection(
            None,
            f"{desktop} on Wayland has no window backend -- only KDE Wayland "
            "and X11 ship with one. A backend for it would be welcome.",
        )
    return Detection(
        None,
        "cannot tell what kind of session this is (no XDG_SESSION_TYPE, "
        "WAYLAND_DISPLAY or DISPLAY). Set [windows] backend to choose one.",
    )


def _build(name: str, which: Which, because: str) -> Detection:
    tool = KWIN_TOOL if name == "kwin" else X11_TOOL
    if which(tool) is None:
        return Detection(None, f"{because}, but {tool} is not installed")
    return Detection(BACKENDS[name](), f"{because}; using {tool}")


def _session_type(env: Mapping[str, str]) -> str | None:
    """``wayland``, ``x11``, or None if the session won't say.

    ``XDG_SESSION_TYPE`` is authoritative when set. The fallback checks
    ``WAYLAND_DISPLAY`` *first* on purpose: a Wayland session almost always has
    ``DISPLAY`` set too, for XWayland, so testing ``DISPLAY`` first would call
    every KDE Wayland session an X11 one.
    """
    declared = env.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("wayland", "x11"):
        return declared
    if env.get("WAYLAND_DISPLAY"):
        return "wayland"
    if env.get("DISPLAY"):
        return "x11"
    return None


def _is_kde(env: Mapping[str, str]) -> bool:
    desktop = env.get("XDG_CURRENT_DESKTOP", "").lower()
    return any(part in desktop for part in ("kde", "plasma"))
