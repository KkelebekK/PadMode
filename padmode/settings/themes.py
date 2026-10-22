"""The settings window's themes, as data, and the CSS they become.

Stdlib only, so the themes can be checked without a display. A theme is more
than a palette: it carries its corner radius, how the focused row is marked
(a filled background, a ring, or a bar down its left edge), and the colours of
the pad's face-button badges. ``system`` sets none of the colours -- it lets
libadwaita follow the desktop's light or dark scheme and accent -- but keeps
the same shapes.

Colours go through libadwaita's own CSS variables where it has one
(``--window-bg-color``, ``--accent-bg-color`` ...), so its widgets -- dialogs,
switches, spin buttons -- follow the theme without being restyled one by one;
the window's own pieces (the tab rail, the pill, the badges, the hint bar) use
``--pm-*`` variables defined here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Face-button badge colours, Xbox order: A, B, X, Y. Text on them is white
#: unless a theme says otherwise.
XBOX_BADGES = ("#3b6d11", "#a32d2d", "#185fa5", "#ba7517")


@dataclass(frozen=True)
class Theme:
    name: str
    label: str
    #: Dark palette, or None to follow the system (the ``system`` theme).
    window: str | None = None      # page background
    rail: str | None = None        # tab rail and hint bar
    card: str | None = None        # rows
    fg: str | None = None
    muted: str | None = None
    accent: str | None = None
    accent_fg: str | None = None
    pill: str | None = None        # the sliding tab highlight
    pill_fg: str | None = None     # a tab's label while the pill is under it
    radius: int = 12
    #: How the focused row shows: "fill", "ring" or "bar".
    focus: str = "fill"
    badges: tuple[str, str, str, str] = XBOX_BADGES
    badge_fg: str = "#ffffff"

    @property
    def follows_system(self) -> bool:
        return self.window is None


THEMES: dict[str, Theme] = {t.name: t for t in (
    Theme("system", "Follow the system", radius=12, focus="fill"),
    # Dracula's own palette (draculatheme.com): background, current line,
    # foreground, comment, and the accents for the badges.
    Theme("dracula", "Dracula", window="#282a36", rail="#21222c",
          card="#343746", fg="#f8f8f2", muted="#6272a4", accent="#bd93f9",
          accent_fg="#282a36", pill="#44475a", pill_fg="#f8f8f2", radius=12,
          focus="bar", badges=("#50fa7b", "#ff5555", "#8be9fd", "#f1fa8c"),
          badge_fg="#282a36"),
    Theme("night", "Night blue", window="#11151c", rail="#0c0f14",
          card="#171c25", fg="#e8edf6", muted="#8a94a6", accent="#4c8dff",
          accent_fg="#ffffff", pill="#24324a", pill_fg="#e8edf6", radius=16,
          focus="fill"),
    Theme("green", "Console green", window="#0e0f0e", rail="#151715",
          card="#171917", fg="#f1f3f1", muted="#8e968e", accent="#3fbf3f",
          accent_fg="#0e0f0e", pill="#3fbf3f", pill_fg="#0e0f0e", radius=6,
          focus="ring"),
    Theme("amber", "Warm amber", window="#1d1915", rail="#251f1a",
          card="#2a231d", fg="#f6eee5", muted="#a89a8a", accent="#f0993b",
          accent_fg="#1d1915", pill="#3a2f25", pill_fg="#f6eee5", radius=22,
          focus="bar"),
)}


def css(theme: Theme) -> str:
    """The stylesheet for one theme, scoped to ``.theme-<name>``."""
    scope = f".padmode-settings.theme-{theme.name}"
    lines = [f"{scope} {{"]
    if not theme.follows_system:
        for var, value in (
            ("window-bg-color", theme.window), ("window-fg-color", theme.fg),
            ("view-bg-color", theme.window), ("view-fg-color", theme.fg),
            ("headerbar-bg-color", theme.rail), ("headerbar-fg-color", theme.fg),
            ("headerbar-backdrop-color", theme.rail),
            ("card-bg-color", theme.card), ("card-fg-color", theme.fg),
            ("dialog-bg-color", theme.card), ("dialog-fg-color", theme.fg),
            ("popover-bg-color", theme.card), ("popover-fg-color", theme.fg),
            ("sidebar-bg-color", theme.rail), ("sidebar-fg-color", theme.fg),
            ("accent-bg-color", theme.accent), ("accent-fg-color", theme.accent_fg),
            ("accent-color", theme.accent),
            ("pm-rail", theme.rail), ("pm-muted", theme.muted),
            ("pm-pill", theme.pill), ("pm-pill-fg", theme.pill_fg),
        ):
            lines.append(f"  --{var}: {value};")
    else:
        lines += ["  --pm-rail: var(--sidebar-bg-color);",
                  "  --pm-muted: alpha(currentColor, 0.6);",
                  "  --pm-pill: alpha(currentColor, 0.1);",
                  "  --pm-pill-fg: currentColor;"]
    a, b, x, y = theme.badges
    lines += [f"  --pm-a: {a}; --pm-b: {b}; --pm-x: {x}; --pm-y: {y};",
              f"  --pm-badge-fg: {theme.badge_fg};",
              f"  --pm-radius: {theme.radius}px;",
              "}"]
    lines.append(_focus_rule(scope, theme))
    lines.append(f"{scope} .boxed-list, {scope} .boxed-list-separate > row "
                 f"{{ border-radius: {theme.radius}px; }}")
    lines.append(f"{scope} .pm-tabs .tab-pill {{ border-radius: "
                 f"{max(theme.radius - 4, 3)}px; }}")
    return "\n".join(lines)


#: Depth, decided with the maintainer: the focused row *floats* -- lifted a
#: little, with a shadow under it -- on top of whatever the theme marks it
#: with. The curve overshoots, so it springs up rather than slides.
FLOAT_SCALE = 1.03
FLOAT_SHADOW = "0 10px 24px rgba(0, 0, 0, 0.45)"
FLOAT_EASE = "cubic-bezier(0.3, 1.4, 0.6, 1)"


def _focus_rule(scope: str, theme: Theme) -> str:
    row = f"{scope} row:focus-within"
    lift = f"transform: scale({FLOAT_SCALE}); outline: none;"
    if theme.focus == "ring":
        return (f"{row} {{ box-shadow: inset 0 0 0 2px var(--accent-bg-color), "
                f"{FLOAT_SHADOW}; {lift} }}")
    if theme.focus == "bar":
        return (f"{row} {{ box-shadow: inset 4px 0 0 var(--accent-bg-color), "
                f"{FLOAT_SHADOW}; background-color: var(--pm-pill); {lift} }}")
    return (f"{row} {{ background-color: var(--accent-bg-color); "
            f"color: var(--accent-fg-color); box-shadow: {FLOAT_SHADOW}; {lift} }}\n"
            f"{row} .dim-label, {row} .subtitle {{ color: var(--accent-fg-color); }}")


def depth_css() -> str:
    """Rows as separate surfaces with a gap between them, so the focused one
    can rise out of the list rather than out of a box. Shared by every theme."""
    return f"""
.padmode-settings list.boxed-list {{ background: none; box-shadow: none; }}
.padmode-settings list.boxed-list > row {{
  background-color: var(--card-bg-color); border: none; margin-bottom: 6px;
  border-radius: var(--pm-radius);
  transition: transform 200ms {FLOAT_EASE}, box-shadow 200ms ease-out,
              background-color 150ms ease-out;
}}
.padmode-settings list.boxed-list > row:focus-within {{ margin-top: 2px; margin-bottom: 8px; }}
"""


def all_css() -> str:
    return "\n\n".join([depth_css(), *(css(theme) for theme in THEMES.values())])
