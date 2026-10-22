"""The settings window (v0.8): ``python -m padmode.settings``.

A separate process, like the keyboard, and for the same reason (CLAUDE.md §2):
anything that draws lives outside the daemon, behind an optional toolkit. Only
:func:`unavailable_reason` is safe to import from here without GTK; the window
itself is behind :mod:`padmode.settings.app`.
"""

from __future__ import annotations


def unavailable_reason() -> str | None:
    """Why the window cannot open on this system, or None if it can."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError) as exc:
        return (f"the settings window needs GTK 4, libadwaita and PyGObject "
                f"({exc}). On Arch: sudo pacman -S libadwaita python-gobject")
    return None
