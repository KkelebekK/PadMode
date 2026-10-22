"""The settings window: GTK 4 and libadwaita, driven by pad, keys or mouse.

``python -m padmode.settings`` lands here, and nothing else imports this
module: GTK must never reach the daemon's import path (CLAUDE.md §2).

Everything the window changes goes through :class:`~padmode.settings.model.
Session`; this file only draws it and routes input. The pad arrives already
digested by the daemon (:mod:`padmode.control`): ``nav`` steps move the same
focus the keyboard's arrows do, and ``act`` events are the fixed actions of
CLAUDE.md §3.5. Each row the window builds registers a :class:`Handler`, so
"select", "left", "right" and "reset" mean the right thing for a number, a
switch, a choice, a button binding or a text field -- and keyboard and mouse
users get the stock behaviour of the same widgets.

**The pad is the window's only while the window is active.** Focus moving to a
game lets go of it, so a window left open behind a game can never take the
controller away from it.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import logging
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import config  # noqa: E402
from ..actions.builtin import ACTIONS  # noqa: E402
from . import glyphs, model, motion, themes  # noqa: E402
from .link import DaemonLink  # noqa: E402
from .service import Service  # noqa: E402

log = logging.getLogger("padmode.settings")

APP_ID = "io.github.KkelebekK.PadMode.Settings"

#: How far one wheel click of the right stick scrolls a page, in pixels.
SCROLL_STEP = 48

#: Page slide, and the gap between rows appearing after it, in milliseconds.
PAGE_SLIDE_MS = 300
ROW_STAGGER_MS = 55

#: How long the tab highlight takes to slide to a new tab, in milliseconds.
#: Long enough for the squash and stretch to read (motion.py).
TAB_SLIDE_MS = 380

#: Nunito, shipped with the window under the OFL (fonts/OFL.txt): the
#: typography decided with the maintainer, and not something to expect
#: installed. One variable file covers every weight.
FONT_FILE = Path(__file__).resolve().parent / "fonts" / "Nunito[wght].ttf"

CSS = b"""
.padmode-settings { font-family: "Nunito"; font-size: 1.3em; }
.padmode-settings .heading, .padmode-settings .title-1,
.padmode-settings .title-2, .padmode-settings .title-3 { font-weight: 800; }
.padmode-settings preferencesgroup .heading { font-size: 1.35em; }
.padmode-settings row .title { font-weight: 600; }
.pm-tabs .tab label { font-weight: 700; }
.pm-hints label { font-weight: 600; }
.padmode-settings .pm-rail { background-color: var(--pm-rail); padding: 18px 12px; }
.padmode-settings .pm-rail .pm-name { font-weight: bold; font-size: 1.15em;
  margin: 2px 10px 14px; }
.pm-tabs .tab { background: none; box-shadow: none; min-width: 200px;
  padding: 14px 16px; color: var(--pm-muted); }
.pm-tabs .tab:hover { background-color: alpha(currentColor, 0.05); }
.pm-tabs .tab.on { color: var(--pm-pill-fg); }
.pm-tabs .tab label { font-size: 1.08em; }
.pm-tabs .tab-pill { background-color: var(--pm-pill); }
.padmode-settings row { min-height: 76px; }
.padmode-settings row .title { font-size: 1.05em; }
.padmode-settings preferencespage > scrolledwindow > viewport > clamp > box {
  margin-top: 18px; margin-bottom: 24px; }
.padmode-settings .capturing { box-shadow: inset 0 0 0 2px var(--accent-bg-color); }
.pm-badge { min-width: 32px; min-height: 32px; padding: 0 3px; border-radius: 16px;
  font-weight: 800; font-size: 0.8em; color: var(--pm-badge-fg); }
.pm-badge.pm-a { background-color: var(--pm-a); }
.pm-badge.pm-b { background-color: var(--pm-b); }
.pm-badge.pm-x { background-color: var(--pm-x); }
.pm-badge.pm-y { background-color: var(--pm-y); }
.pm-badge.pm-chip { background-color: alpha(currentColor, 0.15); color: inherit;
  border-radius: 6px; }
.pm-hints { background-color: var(--pm-rail); padding: 12px 20px; }
.pm-hints .pm-hint { color: var(--pm-muted); margin-right: 26px; }
.pm-hints.idle { opacity: 0.55; }
"""

_DIRECTIONS = {
    "up": Gtk.DirectionType.UP,
    "down": Gtk.DirectionType.DOWN,
    "left": Gtk.DirectionType.LEFT,
    "right": Gtk.DirectionType.RIGHT,
}


@dataclass
class Handler:
    """What the pad does to one row."""

    select: Callable[[], None] | None = None
    #: Left / right: ``adjust(-1)`` / ``adjust(+1)``. Returns False to let the
    #: step move focus instead.
    adjust: Callable[[int], bool] | None = None
    reset: Callable[[], None] | None = None


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, session: model.Session) -> None:
        super().__init__(application=app, title="padmode settings")
        self.session = session
        self.link = DaemonLink()
        self._handlers: dict[Gtk.Widget, Handler] = {}
        self._watch: int | None = None
        self._capturing: Callable[[str], None] | None = None
        self._capture_row: Gtk.Widget | None = None
        self._let_go = False   # Guide pressed: do not take the pad back
        self._updating = False
        self._dialog: Adw.Dialog | None = None
        #: The text row A put the caret in, until B takes it back out.
        self._editing: Adw.EntryRow | None = None
        # Couch density (decided with the maintainer): read from across the
        # room, so the window opens big.
        self.set_default_size(1280, 900)
        self.add_css_class("padmode-settings")
        self._theme: str | None = None
        self.sounds = Sounds()
        self.sounds.set(session.value(model.SOUNDS), session.value(model.SOUND_VOLUME))

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # The console layout (CLAUDE.md §3.5): tabs down the left, the page on
        # the right, what the pad's buttons do along the bottom.
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="padmode settings"))
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda _b: self.apply())
        header.pack_end(self.apply_button)
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda _b: self._banner_action())
        self._banner_do: Callable[[], None] | None = None
        view.add_top_bar(self.banner)

        # A Gtk.Stack, not libadwaita's: it can animate the change. Motion
        # decided with the maintainer ("fluid"): pages slide along the rail's
        # direction -- the next tab down comes up from below -- and their rows
        # fade in one after another (_stagger).
        self.stack = Gtk.Stack(hexpand=True, transition_duration=PAGE_SLIDE_MS,
                               transition_type=Gtk.StackTransitionType.SLIDE_UP_DOWN)
        self._quiet_pages = False   # rebuilding after an edit: no animation
        self.stack.connect("notify::visible-child-name",
                           lambda *_: self._quiet_pages or self._stagger())
        self.tabs = TabBar(self.stack)
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["pm-rail"])
        rail.append(self.tabs)
        body = Gtk.Box()
        body.append(rail)
        body.append(self.stack)
        view.set_content(body)
        self._detected = "xbox"     # the pad's family, once the daemon says
        self._view = view
        self.hints = hint_bar(self.family())
        view.add_bottom_bar(self.hints)

        self.pages: list[tuple[str, Callable[[], Adw.PreferencesPage]]] = [
            ("general", self._general_page),
            ("buttons", self._buttons_page),
            ("feel", self._feel_page),
            ("apps", self._apps_page),
            ("steam", self._steam_page),
        ]
        self._tab_colours = {"general": model.SYSTEM, "buttons": model.KEYBOARD,
                             "feel": model.POINTER, "apps": model.APPS,
                             "steam": model.HOME}
        self._titles = {"general": ("General", "emblem-system-symbolic"),
                        "buttons": ("Buttons", "input-gaming-symbolic"),
                        "feel": ("Feel", "input-mouse-symbolic"),
                        "apps": ("Apps", "view-app-grid-symbolic"),
                        "steam": ("Steam", "applications-games-symbolic")}
        for name, build in self.pages:
            title, icon = self._titles[name]
            self.stack.add_titled(build(), name, title)
            self.tabs.add(name, title, icon, self._tab_colours[name])

        self.connect("close-request", self._on_close_request)
        # Sounds for the keyboard as well as the pad (the maintainer noticed
        # arrows were silent); the mouse stays quiet. A move sounds when focus
        # lands on a new row soon after a key or a pad step -- one place, so
        # the two cannot both sound for the same move.
        self._steered_at = 0.0
        self._sounded_row = None
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("notify::focus-widget", lambda *_: self._focus_moved())
        # Start on the first row, once there is something mapped to focus: a
        # callback queued before that is silently lost.
        self.connect("map", lambda _w: GLib.timeout_add(80, self._focus_first))
        self.connect("notify::is-active", lambda *_: self._activity_changed())
        self.apply_theme(session.value(model.THEME))
        self.hints.add_css_class("idle")
        self._refresh_status()

    # -- the daemon ----------------------------------------------------
    def _connect(self) -> bool:
        if self.link.connected:
            return True
        if not self.link.connect():
            return False
        self._watch = GLib.io_add_watch(
            self.link.fileno(), GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP, self._on_link,
        )
        return True

    def _activity_changed(self) -> None:
        if self.is_active():
            if not self._let_go:
                self.hold()
        else:
            self._let_go = False
            if self.link.connected:
                self.link.send({"op": "release"})

    _reconnect_id: int | None = None

    def _start_reconnect(self) -> None:
        if self._reconnect_id is None:
            self._reconnect_id = GLib.timeout_add(1000, self._try_reconnect)

    def _try_reconnect(self) -> bool:
        if not self.link.connected and not self._connect():
            return True   # not back yet
        self._reconnect_id = None
        if self.is_active() and not self._let_go:
            self.link.send({"op": "hold"})
        self._refresh_status()
        if self.stack.get_visible_child_name() == "general":
            self.changed("general")   # Running / Not running
        return False

    def hold(self) -> None:
        self._let_go = False
        if self._connect():
            self.link.send({"op": "hold"})
        self._refresh_status()

    def _on_link(self, _fd, _cond) -> bool:
        events = self.link.read()
        if events is None:
            # The daemon went away -- restarted from this window, stopped,
            # updated, crashed. Keep knocking until one is back, then take the
            # pad again: after a Restart the window was left deaf to the pad
            # while padmode itself ran on (reported).
            self._watch = None
            self._pad_held = False
            self.hints.add_css_class("idle")
            self._stop_capture()
            self._refresh_status()
            self._start_reconnect()
            return False
        for event in events:
            self._on_event(event)
        return True

    def _on_event(self, event: dict) -> None:
        kind = event.get("event")
        log.debug("pad: %s", event)
        if kind == "held":
            self._pad_held = True
            pad = event.get("pad") or {}
            self._detect(glyphs.family_of(pad.get("name"), pad.get("vendor")))
            self.hints.remove_css_class("idle")
        elif kind == "released":
            self._pad_held = False
            self.hints.add_css_class("idle")
            self._let_go = True
            self._stop_capture()
        elif kind == "refused":
            self._pad_held = False
            self.toast(f"The pad is busy: {event.get('reason')}")
        elif kind == "nav":
            self.navigate(event.get("dir", ""))
        elif kind == "act":
            self.act(event.get("action", ""))
        elif kind == "scroll":
            self.scroll(int(event.get("amount", 0)))
        elif kind == "captured":
            callback, self._capturing = self._capturing, None
            self._clear_capture_style()
            self.sounds.play("select")
            if callback is not None:
                callback(event.get("input", ""))
        elif kind == "cancelled":
            self._stop_capture(tell_daemon=False)
        self._refresh_status()

    _pad_held = False

    # -- pad input -----------------------------------------------------
    def navigate(self, direction: str) -> None:
        self.set_focus_visible(True)
        handler = self._handler_for(self.get_focus())
        if direction in ("left", "right") and handler and handler.adjust:
            if handler.adjust(-1 if direction == "left" else 1):
                return
        if self._dialog is not None:
            # Through the dialog's fields and buttons in order. libadwaita
            # leaves focus on an internal widget that directional focus cannot
            # leave, measured: four presses, focus never moved.
            stops = _dialog_stops(self._dialog)
            if stops:
                focus = self.get_focus()
                current = next((i for i, w in enumerate(stops)
                                if focus is w or (focus and focus.is_ancestor(w))),
                               None)
                step = 1 if direction in ("down", "right") else -1
                i = 0 if current is None else max(0, min(len(stops) - 1,
                                                        current + step))
                self._steered()
                stops[i].grab_focus()
            return
        focus = self.get_focus()
        row = _row_of(focus)
        if row is None:
            self._focus_first()  # from nowhere, or from the header bar
            return
        if direction in ("up", "down"):
            # Row to row, like a console menu. GTK's own directional focus
            # steps *into* a row -- onto its trash button -- which is right
            # for a mouse-and-keyboard form and wrong for a pad.
            rows = [r for r in _rows(self.stack.get_visible_child()) if _usable(r)]
            i = rows.index(row) if row in rows else 0
            j = max(0, min(len(rows) - 1, i + (1 if direction == "down" else -1)))
            self._steered()
            rows[j].grab_focus()
            return
        # Left and right walk the row's own controls: the row, then whatever
        # buttons it carries, and stop at either end.
        stops = [row, *[w for w in _focusables(row) if w is not row]]
        current = next((w for w in stops if focus is w or (
            focus is not None and focus.is_ancestor(w) and w is not row)), row)
        i = stops.index(current)
        j = max(0, min(len(stops) - 1, i + (1 if direction == "right" else -1)))
        stops[j].grab_focus()

    def act(self, action: str) -> None:
        self._steered()
        self.set_focus_visible(True)
        self.sounds.play({"select": "select", "back": "back",
                          "page_prev": "nav", "page_next": "nav",
                          "reset": "back"}.get(action, ""))
        if self._dialog is not None:
            if action == "back":
                self._dialog.close()
            elif action == "select":
                self._activate(self.get_focus())
            return
        if action == "select":
            self._activate(self.get_focus())
        elif action == "back":
            if self._editing is not None:
                # Out of the text field A put us in, not out of the window. A
                # number's box also holds text focus, but nobody "entered" it.
                row, self._editing = self._editing, None
                row.grab_focus()
            else:
                self.close()
        elif action in ("page_prev", "page_next"):
            names = [name for name, _ in self.pages]
            i = names.index(self.stack.get_visible_child_name())
            i = (i + (1 if action == "page_next" else -1)) % len(names)
            self.stack.set_visible_child_name(names[i])
            GLib.idle_add(self._focus_first)
        elif action == "apply":
            self.apply()
        elif action == "reset":
            handler = self._handler_for(self.get_focus())
            if handler and handler.reset:
                handler.reset()

    def scroll(self, amount: int) -> None:
        page = self.stack.get_visible_child()
        scrolled = _find(page, Gtk.ScrolledWindow)
        if scrolled is None:
            return
        adj = scrolled.get_vadjustment()
        adj.set_value(adj.get_value() + amount * SCROLL_STEP)

    def _activate(self, widget: Gtk.Widget | None) -> None:
        handler = self._handler_for(widget)
        if handler and handler.select:
            handler.select()
        elif isinstance(widget, Gtk.Button):
            widget.emit("clicked")
        elif widget is not None:
            widget.activate()

    def _steered(self) -> None:
        """A key or a pad step is moving focus: the next move may sound."""
        self._steered_at = GLib.get_monotonic_time()

    def _focus_moved(self) -> None:
        focus = self.get_focus()
        row = _row_of(focus) or focus
        if row is self._sounded_row:
            return
        self._sounded_row = row
        now = GLib.get_monotonic_time()
        # Focus that moved *because* of a select or a back -- a list opening
        # and taking it, a dialog closing and giving it back -- was not a
        # step, and sounding it played two tones on top of each other
        # (reported on the theme list).
        if now - self.sounds.last_action < 250_000:
            return
        if now - self._steered_at < 300_000:
            self.sounds.play("nav")

    def _on_key(self, _controller, keyval, _code, state) -> bool:
        name = Gdk.keyval_name(keyval) or ""
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        # Pages from the keyboard, as games do it -- Q and E for LB and RB,
        # the maintainer's expectation -- and as desktop apps do it, with
        # Ctrl+PageUp/PageDown and Ctrl+Tab. Q and E are letters inside a
        # text field, so they only turn pages outside one.
        focus = self.get_focus()
        # A text row, not a number's box: those hold text focus too, and Q
        # there is no digit anyway (measured: Q did nothing on the Feel page).
        typing = focus is not None and focus.get_ancestor(Adw.EntryRow) is not None
        if self._dialog is None and (
                (not typing and not ctrl and name in ("q", "e", "Q", "E"))
                or (ctrl and name in ("Page_Up", "Page_Down", "Tab",
                                      "ISO_Left_Tab"))):
            back = name in ("q", "Q", "Page_Up", "ISO_Left_Tab")
            self.act("page_prev" if back else "page_next")
            return True
        if name in ("Up", "Down", "Left", "Right", "Tab", "ISO_Left_Tab",
                    "Page_Up", "Page_Down", "Home", "End"):
            self._steered()
        elif name in ("Return", "KP_Enter", "space"):
            self.sounds.play("select")
        elif name == "Escape":
            self.sounds.play("back")
        return False   # only listening: the key still does its job

    def _handler_for(self, widget: Gtk.Widget | None) -> Handler | None:
        while widget is not None:
            if widget in self._handlers:
                return self._handlers[widget]
            widget = widget.get_parent()
        return None

    def _focus_first(self) -> bool:
        """The first row of the page -- not the first focusable widget, which
        is a header button, and starting there made the first step of the pad
        land somewhere the user was not looking."""
        if self._dialog is not None:
            stops = _dialog_stops(self._dialog)
            if stops:
                stops[0].grab_focus()
            return False
        rows = _rows(self.stack.get_visible_child())
        if rows:
            rows[0].grab_focus()
        return False

    # -- press-to-assign -----------------------------------------------
    def capture(self, row: Gtk.Widget, prompt: str,
                done: Callable[[str], None]) -> None:
        """Ask the pad for a button; ``done`` gets its name."""
        if not (self.link.connected and self._pad_held):
            self._pick_from_list(prompt, done)
            return
        self._capturing = done
        self._capture_row = row
        row.add_css_class("capturing")
        self.link.send({"op": "capture"})
        self.toast(f"{prompt} — press a button on the pad. Guide cancels.", 3)

    def _stop_capture(self, tell_daemon: bool = True) -> None:
        if self._capturing is not None and tell_daemon and self.link.connected:
            self.link.send({"op": "capture", "cancel": True})
        self._capturing = None
        self._clear_capture_style()

    def _clear_capture_style(self) -> None:
        if self._capture_row is not None:
            self._capture_row.remove_css_class("capturing")
            self._capture_row = None

    def _pick_from_list(self, prompt: str, done: Callable[[str], None]) -> None:
        """The fallback without a pad: choose the button by name."""
        names = [n for n in model.BUTTON_ORDER]
        labels = Gtk.StringList.new([model.button_label(n) for n in names])
        drop = Gtk.DropDown(model=labels)
        self._handlers[drop] = _drop_handler(drop)
        dialog = Adw.AlertDialog(heading=prompt, body="Which button?")
        dialog.set_extra_child(drop)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Use it")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: r == "ok" and done(
            names[drop.get_selected()]))
        self._present(dialog)

    # -- apply and close -----------------------------------------------
    def apply(self) -> bool:
        if not self.session.dirty:
            self.toast("Nothing to apply")
            return True
        try:
            self.session.apply()
        except config.ConfigError as exc:
            self.toast(f"Not saved: {exc}", 6)
            self._refresh_status()
            return False
        reply = self.link.reload()
        if reply is None:
            self.toast("Saved. padmode is not running; it will use these "
                       "settings when it starts.", 5)
        elif not reply.get("ok"):
            self.toast(f"Saved, but padmode refused it: {reply.get('error')}", 6)
        elif reply.get("restart"):
            toast = Adw.Toast(title="Applied. Some changes need a restart: "
                              + ", ".join(reply["restart"]), timeout=8)
            toast.set_button_label("Restart padmode")
            toast.connect("button-clicked", lambda _t: _restart_daemon())
            self.toasts.add_toast(toast)
        else:
            self.toast("Applied")
        self.sounds.play("apply")
        self._refresh_status()
        return True

    def _on_close_request(self, _win) -> bool:
        if not self.session.dirty:
            self._shutdown()
            return False
        dialog = Adw.AlertDialog(
            heading="Apply your changes?",
            body="Some changes have not been applied yet.",
        )
        dialog.add_response("cancel", "Go back")
        dialog.add_response("discard", "Close without applying")
        dialog.add_response("apply", "Apply and close")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")

        def chosen(_d, response):
            if response == "apply" and self.apply():
                self._shutdown()
                self.destroy()
            elif response == "discard":
                self.session.discard()
                self._shutdown()
                self.destroy()

        dialog.connect("response", chosen)
        self._present(dialog)
        return True

    def _shutdown(self) -> None:
        if self.link.connected:
            self.link.send({"op": "release"})
            self.link.close()

    # -- status --------------------------------------------------------
    def _refresh_status(self) -> None:
        self.apply_button.set_sensitive(self.session.dirty
                                        and self.session.problem is None)
        if self.session.problem:
            self._banner("Can't apply yet: " + self.session.problem, None, None)
        elif not self.link.connected and not self._connect_quietly():
            service = self.session.service
            if service is not None and service.running():
                # Running, but no socket: a padmode from before the settings
                # window, still in memory after an update.
                self._banner("padmode is running but can't hear this window — "
                             "restart it to use these settings", "Restart",
                             self._restart_and_hold)
            else:
                self._banner("padmode is not running — changes are saved, and "
                             "used when it starts", None, None)
        elif not self._pad_held and self._let_go:
            self._banner("The pad is free again.", "Use the pad", self.hold)
        else:
            self.banner.set_revealed(False)

    def _restart_and_hold(self) -> None:
        error = self.session.service.start(restart=True)
        if error:
            self.toast(f"Could not restart padmode: {error}", 5)
            return
        # The socket appears once the new daemon is up: keep trying.
        self._start_reconnect()

    def _connect_quietly(self) -> bool:
        return self._connect()

    def _banner(self, text: str, button: str | None,
                action: Callable[[], None] | None) -> None:
        self.banner.set_title(text)
        self.banner.set_button_label(button)
        self._banner_do = action
        self.banner.set_revealed(True)

    def _banner_action(self) -> None:
        if self._banner_do is not None:
            self._banner_do()

    def toast(self, text: str, seconds: int = 2) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text),
                                        timeout=seconds))

    def _present(self, dialog: Adw.Dialog) -> None:
        self._dialog = dialog
        dialog.connect("closed", lambda _d: self._dialog_closed())
        dialog.present(self)
        GLib.idle_add(self._focus_first)

    def _dialog_closed(self) -> None:
        self._dialog = None

    def changed(self, rebuild: str | None = None) -> None:
        """After any edit: the Apply button, the banner, maybe a page."""
        if rebuild is not None:
            self._rebuild(rebuild)
        self._refresh_status()

    def _rebuild(self, name: str) -> None:
        focus_index = _row_index(self.get_focus())
        old = self.stack.get_child_by_name(name)
        for widget in list(self._handlers):
            if widget.is_ancestor(old):
                del self._handlers[widget]
        build = dict(self.pages)[name]
        title, icon = self._titles[name]
        visible = self.stack.get_visible_child_name() == name
        # Rebuilt after an edit, not navigated to: no slide, no stagger.
        self._quiet_pages = True
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.remove(old)
        page = build()
        self.stack.add_titled(page, name, title)
        # Keep the page order the user knows -- the slide's direction
        # depends on it.
        names = [n for n, _ in self.pages]
        for n in names[names.index(name) + 1:]:
            child = self.stack.get_child_by_name(n)
            self.stack.remove(child)
            self.stack.add_titled(child, n, self._titles[n][0])
        if visible:
            self.stack.set_visible_child_name(name)
            GLib.idle_add(lambda: _refocus(page, focus_index))
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_UP_DOWN)
        self._quiet_pages = False

    def _stagger(self) -> None:
        """The new page's rows appear one after another, rising a little."""
        rows = [r for r in _rows(self.stack.get_visible_child())
                if not isinstance(r.get_parent(), Gtk.ListBox) or
                r.get_parent().get_ancestor(Adw.ExpanderRow) is None]
        for row in rows:
            row.add_css_class("pm-enter")
        for k, row in enumerate(rows):
            GLib.timeout_add(ROW_STAGGER_MS * (k + 1),
                             lambda r=row: (r.remove_css_class("pm-enter"), False)[1])

    # -- rows ----------------------------------------------------------
    def _field_row(self, field: model.Field) -> Adw.PreferencesRow:
        session = self.session
        if isinstance(field, model.Number):
            adj = Gtk.Adjustment(lower=field.minimum, upper=field.maximum,
                                 step_increment=field.step,
                                 page_increment=field.step * 10,
                                 value=session.value(field))
            row = Adw.SpinRow(title=field.label, subtitle=field.help,
                              adjustment=adj, digits=field.digits)
            row.add_prefix(tile(*model.icon_for_field(field)))

            def on_value(_a):
                if not self._updating:
                    session.set(field, adj.get_value())
                    self.changed()

            adj.connect("value-changed", on_value)

            def adjust(delta):
                adj.set_value(adj.get_value() + delta * field.step)
                return True

            def reset():
                session.reset(field)
                self._quietly(lambda: adj.set_value(session.value(field)))
                self.changed()

            self._handlers[row] = Handler(adjust=adjust, reset=reset)
            return row
        if isinstance(field, model.Toggle):
            row = Adw.SwitchRow(title=field.label, subtitle=field.help,
                                active=bool(session.value(field)))
            row.add_prefix(tile(*model.icon_for_field(field)))

            def on_active(*_):
                if not self._updating:
                    session.set(field, row.get_active())
                    self.changed()

            row.connect("notify::active", on_active)

            def adjust(delta):
                row.set_active(delta > 0)
                return True

            def reset():
                session.reset(field)
                self._quietly(lambda: row.set_active(bool(session.value(field))))
                self.changed()

            self._handlers[row] = Handler(select=lambda: row.set_active(
                not row.get_active()), adjust=adjust, reset=reset)
            return row
        values = [v for v, _ in field.options]
        row = Adw.ComboRow(title=field.label, subtitle=field.help,
                           model=Gtk.StringList.new([l for _, l in field.options]))
        row.add_prefix(tile(*model.icon_for_field(field)))
        row.set_selected(values.index(session.value(field)))

        def on_selected(*_):
            if not self._updating:
                session.set(field, values[row.get_selected()])
                self.changed()

        row.connect("notify::selected", on_selected)

        def adjust(delta):
            row.set_selected((row.get_selected() + delta) % len(values))
            return True

        def reset():
            session.reset(field)
            self._quietly(lambda: row.set_selected(values.index(session.value(field))))
            self.changed()

        def choose():
            self._choose(field.label, field.options, row.get_selected(),
                         lambda i: row.set_selected(i))

        self._handlers[row] = Handler(select=choose, adjust=adjust, reset=reset)
        return row

    def _choose(self, title: str, options, current: int,
                done: Callable[[int], None]) -> None:
        """A on a choice: the options as a list to pick from, the current one
        marked -- what a drop-down is, in a form the pad can walk. A
        drop-down's own popover is a separate surface the pad's row-to-row
        navigation does not reach; stepping to the next option on every
        press, which is what A did first, was reported as wrong."""
        dialog = Adw.AlertDialog(heading=title)
        # Added last-first: a narrow alert dialog stacks its responses with
        # the last added on top, and a list should read top to bottom.
        for i, (_value, label) in reversed(list(enumerate(options))):
            dialog.add_response(str(i), label)
        dialog.set_response_appearance(str(current), Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response(str(current))
        dialog.set_close_response(str(current))
        dialog.set_prefer_wide_layout(False)
        dialog.connect("response", lambda _d, r: done(int(r)))
        self._present(dialog)

    def _quietly(self, change: Callable[[], None]) -> None:
        self._updating = True
        try:
            change()
        finally:
            self._updating = False

    def _binding_row(self, kind: str, button: str, value) -> Adw.ActionRow:
        row = Adw.ActionRow(title=GLib.markup_escape_text(
            model.action_label(kind, value)), activatable=True)
        row.add_prefix(tile(*model.icon_for_binding(kind, value)))
        row.add_suffix(badge(button, self.family()))
        remove = Gtk.Button(icon_name="user-trash-symbolic",
                            valign=Gtk.Align.CENTER, tooltip_text="Unbind")
        remove.add_css_class("flat")
        remove.connect("clicked", lambda _b: self._unbind(kind, button))
        row.add_suffix(remove)

        def chosen(new):
            if new == button:
                return
            lost = self.session.move(kind, button, new)
            message = f"Now on {model.button_label(new)}"
            if lost:
                message += (f" — {model.action_label(kind, lost[1])} is no "
                            "longer bound")
            self.toast(message, 4)
            self.changed("buttons")

        def select():
            self.capture(row, model.action_label(kind, value), chosen)

        row.connect("activated", lambda _r: select())
        self._handlers[row] = Handler(select=select,
                                      reset=lambda: self._reset_binding(kind, button))
        return row

    def _unbind(self, kind: str, button: str) -> None:
        self.session.unbind(kind, button)
        self.toast(f"{model.button_label(button)} does nothing now")
        self.changed("buttons")

    def _reset_binding(self, kind: str, button: str) -> None:
        table = model._TABLES[kind]
        self.session.doc.remove(table, button)
        self.session._settle()
        self.changed("buttons")

    # -- pages ---------------------------------------------------------
    def family(self) -> str:
        """Whose button labels to draw: the setting, or the pad in hand."""
        chosen = self.session.value(model.GLYPHS)
        return self._detected if chosen == "auto" else chosen

    def _detect(self, family: str) -> None:
        before = self.family()
        self._detected = family
        if self.family() != before:
            self.relabel()

    def relabel(self) -> None:
        """Redraw everything that shows a pad button, in the current family."""
        idle = self.hints.has_css_class("idle")
        self._view.remove(self.hints)
        self.hints = hint_bar(self.family())
        if idle:
            self.hints.add_css_class("idle")
        self._view.add_bottom_bar(self.hints)
        self._rebuild("buttons")

    def apply_theme(self, name: str) -> None:
        """Show a theme now. Saved only by Apply, like every other setting."""
        if name == self._theme:
            return
        if self._theme is not None:
            self.remove_css_class(f"theme-{self._theme}")
        self.add_css_class(f"theme-{name}")
        self._theme = name
        theme = themes.THEMES[name]
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.DEFAULT if theme.follows_system
            else Adw.ColorScheme.FORCE_DARK)

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        look = Adw.PreferencesGroup(title="Appearance")
        theme_row = self._field_row(model.THEME)
        theme_row.connect("notify::selected", lambda *_: self.apply_theme(
            self.session.value(model.THEME)))
        look.add(theme_row)
        glyph_row = self._field_row(model.GLYPHS)
        glyph_row.connect("notify::selected", lambda *_: self.relabel())
        look.add(glyph_row)

        def resound(*_):
            self.sounds.set(self.session.value(model.SOUNDS),
                            self.session.value(model.SOUND_VOLUME))
            self.sounds.play("select")   # so the level can be heard

        sounds_row = self._field_row(model.SOUNDS)
        sounds_row.connect("notify::selected", resound)
        look.add(sounds_row)
        volume_row = self._field_row(model.SOUND_VOLUME)
        volume_row.get_adjustment().connect("value-changed", resound)
        look.add(volume_row)
        page.add(look)
        group = Adw.PreferencesGroup(title="padmode")
        service = self.session.service
        if service is None or self.session.autostart_saved is None:
            group.add(Adw.ActionRow(
                title="padmode isn't installed as a service",
                subtitle="Run install.sh to have it start with your session, "
                         "or start it by hand with padmode run"))
            page.add(group)
            return page

        switch = Adw.SwitchRow(title="Start padmode when you log in",
                               subtitle="So the pad works as soon as your desktop "
                                        "is up. Saved when you press Apply",
                               active=bool(self.session.autostart))
        switch.add_prefix(tile("system-run-symbolic", model.POINTER))

        def toggled(*_):
            if not self._updating:
                self.session.set_autostart(switch.get_active())
                self.changed()

        switch.connect("notify::active", toggled)

        def reset():
            self.session.set_autostart(bool(self.session.autostart_saved))
            self._quietly(lambda: switch.set_active(bool(self.session.autostart)))
            self.changed()

        self._handlers[switch] = Handler(
            select=lambda: switch.set_active(not switch.get_active()),
            adjust=lambda d: (switch.set_active(d > 0), True)[1], reset=reset)
        group.add(switch)

        running = service.running()
        status = Adw.ActionRow(
            title="Running" if running else "Not running",
            subtitle="The pad does what these settings say" if running else
                     "Nothing reads the pad until it starts")
        status.add_prefix(tile("object-select-symbolic" if running
                               else "media-playback-stop-symbolic",
                               model.POINTER if running else model.HOME))
        button = Gtk.Button(label="Restart" if running else "Start",
                            valign=Gtk.Align.CENTER)

        def start(_b=None):
            error = service.start(restart=running)
            self.toast(f"Could not start padmode: {error}" if error else
                       ("padmode restarted" if running else "padmode started"), 4)
            self._start_reconnect()   # a started or restarted daemon: take the pad
            GLib.timeout_add(1500, lambda: (self.changed("general"), False)[1])

        button.connect("clicked", start)
        status.add_suffix(button)
        if running:
            stop = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
            stop.add_css_class("destructive-action")
            stop.connect("clicked", lambda _b: self._confirm_stop())
            status.add_suffix(stop)
        group.add(status)
        page.add(group)
        return page

    def _confirm_stop(self) -> None:
        """Stopping padmode takes the pad away from this window too, so it
        asks first -- and the way back is the mouse, which the dialog says."""
        dialog = Adw.AlertDialog(
            heading="Stop padmode?",
            body="The pad stops doing anything -- here too -- until padmode "
                 "starts again. Start it from this page with the mouse, or "
                 "log in again if it starts at login.")
        dialog.add_response("cancel", "Keep it running")
        dialog.add_response("stop", "Stop")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def chosen(_d, response):
            if response != "stop":
                return
            error = self.session.service.stop()
            self.toast(f"Could not stop padmode: {error}" if error
                       else "padmode stopped", 4)
            GLib.timeout_add(800, lambda: (self.changed("general"), False)[1])

        dialog.connect("response", chosen)
        self._present(dialog)

    def _buttons_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        s = self.session
        for kind, title, description in (
            (model.GUIDE, "Hold Guide and press",
             "Select a row, then press the button you want for it"),
            (model.DESKTOP_MODE, "Desktop mode",
             "What the buttons do when the pad works as a mouse (Guide + L3)"),
        ):
            group = Adw.PreferencesGroup(title=title, description=description)
            bindings = s.bindings(kind)
            for button in model.sort_buttons(bindings):
                group.add(self._binding_row(kind, button, bindings[button]))
            group.add(self._add_binding_row(kind))
            group.add(self._restore_row(kind))
            page.add(group)

        group = Adw.PreferencesGroup(
            title="Unlock with the pad",
            description="Types your password at the lock screen. Needs a "
            "password made only of digits — easier to guess, and usually your "
            "sudo password too. Use a long one.")
        group.add(self._field_row(model.UNLOCK_SWITCH))
        layouts = Adw.ActionRow(title="Layout", subtitle="Stick dial, or the "
                                "Xbox lock screen's buttons")
        for name, label in (("dial", "Dial"), ("xbox", "Xbox")):
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b, n=name: self._layout(n))
            layouts.add_suffix(button)
        group.add(layouts)
        bindings = s.bindings(model.UNLOCK_MODE)
        by_action = sorted(bindings.items(), key=lambda kv: (
            _digit_order(kv[1]), model.sort_buttons([kv[0]])))
        for button, value in by_action:
            group.add(self._binding_row(model.UNLOCK_MODE, button, value))
        page.add(group)
        return page

    def _layout(self, name: str) -> None:
        self.session.unlock_layout(name)
        self.toast("Dial layout" if name == "dial" else "Xbox layout")
        self.changed("buttons")

    def _restore_row(self, kind: str) -> Adw.ButtonRow:
        row = Adw.ButtonRow(title="Restore the defaults")

        def restore(*_):
            self.session.reset_bindings(kind)
            self.toast("Defaults restored")
            self.changed("buttons")

        row.connect("activated", restore)
        return row

    def _add_binding_row(self, kind: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title="Add a button…", activatable=True)
        row.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        choices = self._binding_choices(kind)

        def select():
            labels = Gtk.StringList.new([label for _, label in choices])
            drop = Gtk.DropDown(model=labels)
            self._handlers[drop] = _drop_handler(drop)
            dialog = Adw.AlertDialog(heading="What should it do?")
            dialog.set_extra_child(drop)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Choose a button")
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

            def picked(_d, response):
                if response != "ok":
                    return
                value, label = choices[drop.get_selected()]

                def bound(button):
                    self.session.bind(kind, button, value)
                    self.toast(f"{label}: {model.button_label(button)}")
                    self.changed("buttons")

                GLib.idle_add(lambda: self.capture(row, label, bound) and False)

            dialog.connect("response", picked)
            self._present(dialog)

        row.connect("activated", lambda _r: select())
        self._handlers[row] = Handler(select=select)
        return row

    def _binding_choices(self, kind: str) -> list[tuple[object, str]]:
        if kind == model.DESKTOP_MODE:
            values = ["click.left", "click.right", "click.middle", "osk",
                      "key.enter", "key.esc", "key.tab", "key.shift+tab",
                      "key.alt", "key.super", "key.up", "key.down", "key.left",
                      "key.right", "key.pageup", "key.pagedown", "key.f11"]
        else:
            cfg = self.session.config
            values = [*ACTIONS, *(f"overlay.{o}" for o in cfg.overlays),
                      config.OSK_BINDING, config.SETTINGS_BINDING]
            if cfg.frontend is not None:
                values.append(config.FRONTEND_BINDING)
        return [(v, model.action_label(kind, v)) for v in values]

    def _feel_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        for title, fields in model.FEEL:
            group = Adw.PreferencesGroup(title=title)
            for field in fields:
                group.add(self._field_row(field))
            page.add(group)
        return page

    def _apps_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        cfg = self.session.config
        group = Adw.PreferencesGroup(
            title="Overlays",
            description="Apps that open on top of your game with a Guide combo, and "
                        "close again to put you straight back in it")
        for name, overlay in sorted(cfg.overlays.items()):
            group.add(self._overlay_row(name, overlay))
        add = Adw.ActionRow(title="Add an app…", activatable=True)
        add.add_prefix(Gtk.Image(icon_name="list-add-symbolic"))
        add.connect("activated", lambda _r: self._new_overlay())
        self._handlers[add] = Handler(select=self._new_overlay)
        group.add(add)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Frontend",
            description="Your game launcher, like Pegasus: where the home button "
                        "takes you, and where you land if a game closes under an "
                        "overlay")
        fe = cfg.frontend
        cls = self._entry_row("Window class (padmode windows lists them)",
                              fe.window_class if fe else "",
                              lambda text: self._frontend(text, None))
        cmd = self._entry_row("Command that starts it (optional)",
                              shlex.join(fe.command) if fe and fe.command
                              else "", lambda text: self._frontend(None, text))
        group.add(cls)
        group.add(cmd)
        if fe is not None:
            remove = Adw.ButtonRow(title="Remove the frontend")
            remove.add_css_class("destructive-action")
            remove.connect("activated", lambda _r: (self.session.remove_frontend(),
                                                    self.changed("apps"),
                                                    self.changed("buttons")))
            group.add(remove)
        page.add(group)

        group = Adw.PreferencesGroup(title="When an app opens over a game")
        for field in model.OVERLAY_TIMING:
            group.add(self._field_row(field))
        page.add(group)
        return page

    def _overlay_row(self, name: str, overlay) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(title=name, subtitle=overlay.window_class)
        row.add_prefix(tile("view-app-grid-symbolic", model.APPS))
        state = {"class": overlay.window_class,
                 "command": shlex.join(overlay.command) if overlay.command else ""}

        def save():
            try:
                command = shlex.split(state["command"]) or None
            except ValueError:
                return
            self.session.set_overlay(name, state["class"], command)
            self.changed()

        row.add_row(self._entry_row("Window class (padmode windows lists them)",
                                    state["class"],
                                    lambda t: (state.update({"class": t}), save())))
        row.add_row(self._entry_row("Command that starts it (optional)",
                                    state["command"],
                                    lambda t: (state.update({"command": t}), save())))
        remove = Adw.ButtonRow(title=f"Remove {name}")
        remove.add_css_class("destructive-action")
        remove.connect("activated", lambda _r: (self.session.remove_overlay(name),
                                                self.changed("apps"),
                                                self.changed("buttons")))
        row.add_row(remove)
        self._handlers[row] = Handler(select=lambda: row.set_expanded(
            not row.get_expanded()))
        return row

    def _new_overlay(self) -> None:
        dialog = Adw.AlertDialog(heading="Add an app",
                                 body="It comes up over your game on a Guide combo.")
        box = Gtk.ListBox(css_classes=["boxed-list"])
        name = Adw.EntryRow(title="Name (letters, digits, - or _)")
        cls = Adw.EntryRow(title="Window class, e.g. discord")
        cmd = Adw.EntryRow(title="Command to start it (optional)")
        for row in (name, cls, cmd):
            box.append(row)
            self._handlers[row] = Handler(select=lambda r=row: self._type_into(r))
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def added(_d, response):
            if response != "add":
                return
            key = name.get_text().strip()
            if not key or not key.replace("_", "").replace("-", "").isalnum():
                self.toast("That name won't work: letters, digits, - or _")
                return
            try:
                command = shlex.split(cmd.get_text()) or None
            except ValueError:
                command = None
            self.session.set_overlay(key, cls.get_text().strip() or key, command)
            self.toast(f"Added {key}. Give it a button on the Buttons page.", 4)
            self.changed("apps")
            self.changed("buttons")

        dialog.connect("response", added)
        self._present(dialog)

    def _frontend(self, window_class: str | None, command: str | None) -> None:
        fe = self.session.config.frontend
        cls = window_class if window_class is not None else (fe.window_class if fe else "")
        if not cls.strip():
            return
        if command is None:
            argv = fe.command if fe else None
        else:
            try:
                argv = shlex.split(command) or None
            except ValueError:
                return
        self.session.set_frontend(cls.strip(), argv)
        self.changed()

    def _entry_row(self, title: str, text: str,
                   changed: Callable[[str], None]) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title, text=text)
        row.connect("changed", lambda r: not self._updating and changed(r.get_text()))
        self._handlers[row] = Handler(select=lambda: self._type_into(row))
        return row

    def _type_into(self, row: Adw.EntryRow) -> None:
        """A on a text field: put the caret in it and the keyboard up."""
        row.grab_focus()
        if row.get_ancestor(Adw.Dialog) is None:
            self._editing = row
        if self.link.connected and self._pad_held:
            self.link.send({"op": "keyboard"})

    def _regenerate_steam(self) -> None:
        _regenerate(self)

    def _steam_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Steam library",
            description="Which installed apps go into your frontend.")
        try:
            apps = model.steam_apps()
        except OSError:
            apps = []
        ignore = self.session.ignore()
        if not apps:
            group.add(Adw.ActionRow(title="No Steam library found"))
        else:
            update = Adw.ButtonRow(title="Update the frontend now")
            update.connect("activated", lambda _r: self._regenerate_steam())
            group.add(update)
        for app, reason in apps:
            shown_by_default = reason is None
            shown = (not ignore[app.appid]) if app.appid in ignore else shown_by_default
            row = Adw.SwitchRow(title=GLib.markup_escape_text(app.name),
                                subtitle=_steam_reason(reason), active=shown)
            row.add_prefix(tile("applications-games-symbolic",
                                model.HOME if reason is None else model.SYSTEM))

            def toggled(r, *_a, appid=app.appid, default=shown_by_default):
                if self._updating:
                    return
                show = r.get_active()
                self.session.set_hidden(appid, None if show == default else not show)
                self.changed()

            row.connect("notify::active", toggled)

            def reset(r=row, appid=app.appid, default=shown_by_default):
                self.session.set_hidden(appid, None)
                self._quietly(lambda: r.set_active(default))
                self.changed()

            self._handlers[row] = Handler(
                select=lambda r=row: r.set_active(not r.get_active()),
                adjust=lambda d, r=row: (r.set_active(d > 0), True)[1],
                reset=reset)
            group.add(row)
        page.add(group)
        return page


class TabBar(Gtk.Fixed):
    """The page tabs, with a highlight that slides from tab to tab.

    Down the left of the window, the console way (the layout the maintainer
    chose). libadwaita's own switcher jumps; this one glides, the way a console's
    settings menu does -- asked for by the maintainer, then with squash and
    stretch on top (:mod:`padmode.settings.motion`). The highlight is a
    pill drawn *under* the row of buttons (a :class:`Gtk.Fixed` paints its
    children in the order they were added), and an animation moves and
    resizes it between the old tab's bounds and the new one's. Pad, mouse
    and keyboard all change the stack's visible page, so all three slide.
    """

    def __init__(self, stack: Gtk.Stack) -> None:
        super().__init__()
        self.add_css_class("pm-tabs")
        self._stack = stack
        self._pill = Gtk.Box(css_classes=["tab-pill"])
        self._row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.put(self._pill, 0, 0)
        self.put(self._row, 0, 0)
        self._buttons: dict[str, Gtk.Button] = {}
        self._at = (0.0, 0.0, 0.0, 0.0)          # x, y, width, height now
        self._animation: Adw.TimedAnimation | None = None
        stack.connect("notify::visible-child-name", lambda *_: self.slide())
        # Sizes exist only after the first layout; place it there, unanimated.
        self.connect("map", lambda *_: GLib.timeout_add(30, self._snap))

    def add(self, name: str, title: str, icon: str, colour: int) -> None:
        content = Gtk.Box(spacing=14, halign=Gtk.Align.START)
        content.append(tile(icon, colour))
        content.append(Gtk.Label(label=title))
        button = Gtk.Button(css_classes=["flat", "tab"], child=content)
        button.connect("clicked",
                       lambda _b: self._stack.set_visible_child_name(name))
        self._row.append(button)
        self._buttons[name] = button

    def _bounds(self) -> tuple[float, float, float, float] | None:
        button = self._buttons.get(self._stack.get_visible_child_name() or "")
        if button is None:
            return None
        ok, rect = button.compute_bounds(self)
        if not ok or rect.get_width() <= 0:
            return None
        return (rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height())

    def _put_pill(self, x: float, y: float, w: float, h: float) -> None:
        self.move(self._pill, x, y)
        self._pill.set_size_request(round(w), round(h))
        self._at = (x, y, w, h)

    def _snap(self) -> bool:
        self._mark()
        bounds = self._bounds()
        if bounds is None:
            return True   # not laid out yet; try again next tick
        self._put_pill(*bounds)
        return False

    def _mark(self) -> None:
        name = self._stack.get_visible_child_name()
        for key, button in self._buttons.items():
            (button.add_css_class if key == name else button.remove_css_class)("on")

    def slide(self) -> None:
        self._mark()
        target = self._bounds()
        if target is None:
            return
        start = self._at
        if start[2] <= 0:          # never placed: nothing to slide from
            self._put_pill(*target)
            return
        if self._animation is not None:
            self._animation.skip()

        # Down the rail: the pill travels along y and squashes in x, the
        # same curves turned on their side.
        edges_from = (start[1], start[1] + start[3])
        edges_to = (target[1], target[1] + target[3])
        left, width = target[0], target[2]

        def step(t: float) -> None:
            top, bottom, w = motion.pill(t, edges_from, edges_to, width)
            self._put_pill(left + (width - w) / 2, top, w, bottom - top)

        # Linear: the curves live in motion.pill, one per edge.
        animation = Adw.TimedAnimation(
            widget=self, value_from=0.0, value_to=1.0, duration=TAB_SLIDE_MS,
            target=Adw.CallbackAnimationTarget.new(step),
            easing=Adw.Easing.LINEAR)
        self._animation = animation
        animation.play()


# -- helpers ----------------------------------------------------------------
def _steam_reason(reason: str | None) -> str:
    """Why padmode leaves an app out, said for someone who is not a Steam
    expert. Empty for a game: it needs no explaining."""
    if reason is None:
        return ""
    plain = {"compatibility tool": "a tool Steam uses to run games, not a game",
             "dedicated server": "a game server, not something to play"}
    return "Hidden by default: " + plain.get(reason, reason)


SOUND_DIR = Path(__file__).resolve().parent / "sounds"


class Sounds:
    """The console tones, played when the pad moves, selects, goes back and
    applies -- never for the mouse. Decided with the maintainer by ear; the
    files are rendered by tools/make_sounds.py.

    Loaded once, at start: a media file's first play pays for opening it, and
    a navigation sound that arrives late is worse than none.
    """

    def __init__(self) -> None:
        self._files: dict[str, Gtk.MediaFile] = {}
        for name in ("nav", "select", "back", "apply"):
            path = SOUND_DIR / f"{name}.wav"
            if path.exists():
                self._files[name] = Gtk.MediaFile.new_for_filename(str(path))
        self.enabled = True
        self.volume = 0.6
        #: When something other than a move last sounded, in microseconds.
        self.last_action = 0

    def set(self, kind: str, volume: int) -> None:
        self.enabled = kind != "off"
        self.volume = max(0, min(100, volume)) / 100

    def play(self, name: str) -> None:
        if name != "nav":
            self.last_action = GLib.get_monotonic_time()
        media = self._files.get(name)
        if not self.enabled or media is None or self.volume <= 0:
            return
        media.set_volume(self.volume)
        media.seek(0)
        media.play()


def badge(button: str, family: str = "xbox") -> Gtk.Label:
    """A pad button as *this* pad prints it (``glyphs.py``): A, B, X, Y --
    or ✕ ○ △ □ -- as coloured rounds, everything else as a small grey key."""
    text, style = glyphs.glyph(button, family)
    css = {"a": "pm-a", "b": "pm-b", "x": "pm-x", "y": "pm-y",
           "chip": "pm-chip"}.get(style, f"pm-{style}")
    return Gtk.Label(label=text, valign=Gtk.Align.CENTER,
                     halign=Gtk.Align.CENTER, hexpand=False,
                     css_classes=["pm-badge", css])


def hint_bar(family: str = "xbox") -> Gtk.Box:
    """What the pad does here, along the bottom of the window."""
    bar = Gtk.Box(css_classes=["pm-hints"], spacing=0)
    for buttons, text in ((("A",), "Select"), (("B",), "Back"),
                          (("LB", "RB"), "Page"), (("START",), "Apply"),
                          (("Y",), "Default"), (("GUIDE",), "Let go")):
        hint = Gtk.Box(spacing=6, css_classes=["pm-hint"])
        for b in buttons:
            hint.append(badge(b, family))
        hint.append(Gtk.Label(label=text))
        bar.append(hint)
    return bar


_ICON_FILES: dict[str, Gio.Icon] = {}


def _icon(name: str) -> Gio.Icon:
    """Adwaita's own symbolic icon by file, else the theme's by name.

    By file because the tile colours the icon, and that needs an icon GTK
    can recolour: some icon themes (Tela, on the maintainer's machine) draw
    their symbolic icons in fixed colours, measured -- the sound icons came
    out orange on an orange tile. Adwaita's are true symbolic SVGs, and GTK 4
    depends on them, so they are there wherever the window can open.
    """
    if name not in _ICON_FILES:
        found = None
        for base in os.environ.get("XDG_DATA_DIRS", "/usr/share").split(":"):
            for path in Path(base, "icons/Adwaita/symbolic").glob(f"*/{name}.svg"):
                found = Gio.FileIcon.new(Gio.File.new_for_path(str(path)))
                break
            if found:
                break
        _ICON_FILES[name] = found or Gio.ThemedIcon.new(name)
    return _ICON_FILES[name]


def tile(icon: str, colour: int) -> Gtk.Box:
    """An icon on a coloured rounded square -- the iconography the
    maintainer chose. ``colour`` is a slot in themes.TILE_KINDS."""
    box = Gtk.Box(css_classes=["pm-tile", f"pm-c{colour}"],
                  valign=Gtk.Align.CENTER, halign=Gtk.Align.START,
                  hexpand=False)
    image = Gtk.Image(gicon=_icon(icon), pixel_size=22, hexpand=False,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    image.set_size_request(42, 42)
    box.append(image)
    return box


def _regenerate(window: "Window") -> None:
    if window.session.dirty:
        window.toast("Apply first, so the list you see is the list it writes")
        return
    try:
        subprocess.Popen([sys.executable, "-m", "padmode", "metadata", "steam"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except OSError as exc:
        window.toast(f"Could not run it: {exc}")
        return
    window.toast("Writing the Steam collection…")



def _drop_handler(drop: Gtk.DropDown) -> Handler:
    """Left and right walk a drop-down's choices, so the pad never has to open
    its popover -- popovers are where pad navigation goes to get lost."""
    def adjust(delta):
        count = drop.get_model().get_n_items()
        drop.set_selected((drop.get_selected() + delta) % count)
        return True

    return Handler(select=lambda: adjust(1), adjust=adjust)



def _find(widget: Gtk.Widget | None, cls: type) -> Gtk.Widget | None:
    if widget is None:
        return None
    if isinstance(widget, cls):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _find(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _rows(widget: Gtk.Widget) -> list[Gtk.Widget]:
    out = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesRow):
            out.append(child)
        out.extend(_rows(child))
        child = child.get_next_sibling()
    return out


def _row_of(widget: Gtk.Widget | None) -> Adw.PreferencesRow | None:
    """The outermost row ``widget`` is in -- an expander's own row, not the
    rows nested inside it, unless focus is already on one of those."""
    if widget is None:
        return None
    if isinstance(widget, Adw.PreferencesRow):
        return widget
    return widget.get_ancestor(Adw.PreferencesRow)


def _usable(row: Gtk.Widget) -> bool:
    return row.get_mapped() and row.is_sensitive() and row.get_focusable()


def _dialog_stops(dialog: Gtk.Widget) -> list[Gtk.Widget]:
    """A dialog's text rows, drop-downs and buttons, top to bottom as *drawn*.

    Not in widget order: a narrow alert dialog stacks its buttons in the
    reverse of the order they were added, so walking the tree made "down" go
    up -- reported on the first real run.
    """
    found: list[Gtk.Widget] = []

    def walk(widget: Gtk.Widget) -> None:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, (Adw.EntryRow, Gtk.DropDown, Gtk.Button)):
                if child.get_mapped() and child.is_sensitive():
                    found.append(child)
            else:
                walk(child)
            child = child.get_next_sibling()

    walk(dialog)

    def where(widget: Gtk.Widget) -> tuple[float, float]:
        ok, bounds = widget.compute_bounds(dialog)
        if not ok:
            return (0.0, 0.0)
        return (round(bounds.get_y()), bounds.get_x())

    return sorted(found, key=where)


def _focusables(row: Gtk.Widget) -> list[Gtk.Widget]:
    """Buttons and switches inside a row, in order. Not nested rows."""
    out = []
    child = row.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesRow):
            pass
        elif isinstance(child, (Gtk.Button, Gtk.Switch)) and child.get_mapped():
            out.append(child)
        else:
            out.extend(_focusables(child))
        child = child.get_next_sibling()
    return out


def _row_index(focus: Gtk.Widget | None) -> int | None:
    row = focus if isinstance(focus, Adw.PreferencesRow) else (
        focus.get_ancestor(Adw.PreferencesRow) if focus is not None else None)
    if row is None:
        return None
    page = row.get_ancestor(Adw.PreferencesPage)
    rows = _rows(page) if page is not None else []
    return rows.index(row) if row in rows else None


def _refocus(page: Gtk.Widget, index: int | None) -> bool:
    rows = _rows(page)
    if rows:
        rows[min(index or 0, len(rows) - 1)].grab_focus()
    return False


def _digit_order(value) -> int:
    return {"enter": 10, "backspace": 11, "clear": 12}.get(value, (
        int(value) - 1) % 10 if str(value).isdigit() else 13)


def _load_font() -> None:
    """Make Nunito available to this process only. Nothing is installed: a
    missing or refused file just leaves the system's own font in place."""
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import PangoCairo

    font_map = PangoCairo.FontMap.get_default()
    try:
        if not font_map.add_font_file(str(FONT_FILE)):
            log.debug("could not load %s; using the system font", FONT_FILE)
    except (AttributeError, GLib.Error) as exc:   # Pango before 1.56
        log.debug("no app fonts here (%s); using the system font", exc)


def _restart_daemon() -> None:
    subprocess.Popen(["systemctl", "--user", "restart", "padmode.service"],
                     stdin=subprocess.DEVNULL)


# -- entry point ------------------------------------------------------------
class App(Adw.Application):
    def __init__(self, args: argparse.Namespace) -> None:
        # A screenshot run must never hand itself to the window the user has
        # open -- single-instance would forward it there and raise theirs.
        flags = (Gio.ApplicationFlags.NON_UNIQUE if args.screenshot
                 else Gio.ApplicationFlags.DEFAULT_FLAGS)
        super().__init__(application_id=APP_ID, flags=flags)
        self.args = args
        self.window: Window | None = None

    def do_activate(self) -> None:
        if self.window is not None:
            self.window.present()
            return
        Adw.Application.do_activate(self)
        _load_font()
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS + b"\n" + themes.all_css().encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        try:
            session = model.Session(self.args.config, service=Service())
        except model.EditError as exc:
            dialog = Adw.AlertDialog(
                heading="The config file can't be edited here",
                body=f"{exc}. Edit it by hand: {config.default_path()}")
            dialog.add_response("ok", "Close")
            win = Adw.ApplicationWindow(application=self)
            win.present()
            dialog.connect("response", lambda *_: self.quit())
            dialog.present(win)
            return
        self.window = Window(self, session)
        if self.args.screenshot:
            # Checking a layout, or the e2e harness: never out loud on the
            # speakers of whoever is at the machine.
            self.window.sounds.enabled = False
        if self.args.page:
            self.window.stack.set_visible_child_name(self.args.page)
        self.window.present()
        if self.args.screenshot:
            GLib.timeout_add(int(self.args.screenshot_delay * 1000), self._shoot)

    def _shoot(self) -> bool:
        """Render the window to a PNG and quit. For checking the layout
        without putting a window on anyone's screen (docs/development.md)."""
        win = self.window
        if self.args.focus_rows:
            win.set_focus_visible(True)
            for _ in range(self.args.focus_rows):
                win.navigate("down")
        GLib.timeout_add(300, lambda: self._save(win))
        return False

    def _save(self, win: Window) -> bool:
        width, height = win.get_width(), win.get_height()
        paintable = Gtk.WidgetPaintable.new(win)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            texture = win.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(self.args.screenshot)
            print(f"saved {self.args.screenshot} ({width}x{height})")
        self.quit()
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="padmode settings")
    parser.add_argument("-c", "--config", type=lambda p: __import__("pathlib").Path(p))
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--page", choices=["general", "buttons", "feel", "apps", "steam"])
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    parser.add_argument("--focus-rows", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--screenshot-delay", type=float, default=1.5,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    app = App(args)
    return app.run([sys.argv[0]])
