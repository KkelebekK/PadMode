"""Notice when a text field takes focus, so the keyboard can open itself.

Runs in the renderer process, never the daemon: it needs ``gi``, and the
daemon's import path must stay free of it (CLAUDE.md §2, enforced by a test).
The renderer is pre-warmed on entering a pad-driven mode and killed on leaving
it, so the accessibility connection exists exactly as long as desktop mode
does -- not for the whole session.

**This reads roles and states, never content.** It can see that a password
field has focus; it cannot see what is in one, and must never try.

**AT-SPI has to be switched on**, and on a stock KDE install it is not.
Measured on Plasma 6.7: with ``org.a11y.Status IsEnabled`` false, AT-SPI lists
six system helpers, none with windows, and no focus event ever arrives no
matter what you click. With it true the same desktop reports seventeen
applications, and Qt/QML apps expose their text fields correctly -- vicinae's
search box, plasmashell's, and the lock screen's password field. So the
failure mode is silence, which is why :func:`unavailable_reason` exists: the
daemon says what is wrong once instead of leaving the feature quietly dead.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Roles that mean "you can type here", checked alongside the EDITABLE state.
#: A role alone is not enough -- a read-only document is still ``text`` -- and
#: the state alone misses terminals, which are editable in spirit only.
EDITABLE_ROLES = frozenset({
    "text",
    "entry",
    "password text",
    "terminal",
    "document text",
})

#: Seconds to wait on any one accessibility call. An unresponsive application
#: must not be able to stall the renderer's main loop, which is also what draws
#: the keyboard.
CALL_TIMEOUT = 0.4

#: How deep to look inside an active window, and how many nodes to look at
#: before giving up. Both are guards against a pathological tree, not tuning:
#: a browser's accessible tree is enormous, and this runs on a click.
MAX_DEPTH = 16
MAX_NODES = 2000


def _atspi():
    """The Atspi module, or None when it is not installed."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        return None
    return Atspi


def unavailable_reason() -> str | None:
    """Why auto-open cannot work here, or None if it can.

    Distinguishes "not installed" from "installed but switched off", because
    the fix is completely different and the symptom is identical: nothing ever
    happens.
    """
    atspi = _atspi()
    if atspi is None:
        return (
            "the Atspi typelib is missing (Arch: at-spi2-core, "
            "python-gobject)"
        )
    try:
        desktop = atspi.get_desktop(0)
        windows = sum(
            desktop.get_child_at_index(i).get_child_count()
            for i in range(desktop.get_child_count())
        )
    except Exception as exc:  # noqa: BLE001 -- any failure means unusable
        return f"accessibility is not answering ({exc})"
    if windows == 0:
        return (
            "accessibility is switched off, so no application reports its "
            "text fields. Turn it on with: gsettings set "
            "org.gnome.desktop.interface toolkit-accessibility true"
        )
    return None


class FocusWatcher:
    """Calls back with True/False as editable things gain focus."""

    def __init__(self, on_focus) -> None:
        self._on_focus = on_focus
        self._atspi = None
        self._listener = None
        #: The field the keyboard was opened for, so we can notice when *it*
        #: goes rather than asking the unanswerable "did focus leave".
        self._field = None

    def start(self) -> str | None:
        """Begin watching. Returns None, or why it could not."""
        reason = unavailable_reason()
        if reason is not None:
            return reason
        atspi = _atspi()
        try:
            atspi.set_timeout(int(CALL_TIMEOUT * 1000), int(CALL_TIMEOUT * 1000))
        except Exception:  # noqa: BLE001 -- older versions lack it
            pass
        listener = atspi.EventListener.new(self._handle)
        if not listener.register("object:state-changed:focused"):
            return "could not register an accessibility listener"
        self._atspi, self._listener = atspi, listener
        return None

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.deregister("object:state-changed:focused")
            except Exception:  # noqa: BLE001
                pass
        self._listener = self._atspi = None

    def current_editable(self) -> bool:
        """Whether the thing with focus *right now* can be typed into.

        A poll, deliberately, alongside the subscription. AT-SPI reports focus
        *changes*, so clicking a field that already had focus produces no event
        whatsoever -- and that is the ordinary case of clicking back into the
        field you were just typing in. Without this, the keyboard opens the
        first time you click a field and never again.
        """
        node = self._find_typeable()
        if node is None:
            return False
        # Remembered so the keyboard can be closed again when *this* field goes
        # away, rather than when "nothing typeable is focused anywhere" -- which
        # is a different question and, measured, is never answered no: the
        # panel and the desktop have editable fields of their own.
        self._field = node
        return True

    def field_still_focused(self) -> bool:
        """Is the field we opened for still focused, and still alive?

        The close signal. "Focus left a text field" is not something AT-SPI
        reports: clicking a blank area does not move focus at all, and when a
        window vanishes the event never comes. So instead of waiting to be
        told, we hold on to the object that caused the opening and ask after it.
        """
        field, atspi = self._field, self._atspi
        if field is None or atspi is None:
            return False
        try:
            states = field.get_state_set()
            if states.contains(atspi.StateType.DEFUNCT):
                return False  # the widget, or its whole application, is gone
            return bool(
                states.contains(atspi.StateType.FOCUSED)
                and states.contains(atspi.StateType.SHOWING)
            )
        except Exception:  # noqa: BLE001 -- a dead object raises rather than answering
            return False

    def forget_field(self) -> None:
        self._field = None

    def _find_typeable(self):
        """The focused, on-screen, typeable node, or None."""
        atspi = self._atspi
        if atspi is None:
            return None
        try:
            desktop = atspi.get_desktop(0)
            for i in range(desktop.get_child_count()):
                found = self._typeable_in(desktop.get_child_at_index(i))
                if found is not None:
                    return found
        except Exception as exc:  # noqa: BLE001
            log.debug("could not read the focused object: %s", exc)
        return None

    def _typeable_in(self, app):
        """The typeable focused node in this application's active window."""
        try:
            for i in range(app.get_child_count()):
                window = app.get_child_at_index(i)
                # Only the active window can hold focus, and skipping the rest
                # is what keeps this cheap enough to run on every click.
                if not window.get_state_set().contains(self._atspi.StateType.ACTIVE):
                    continue
                found = self._descend(window, 0, [0])
                if found is not None:
                    return found
        except Exception:  # noqa: BLE001
            return None
        return None

    def _descend(self, node, depth, budget):
        """Look for a focused, on-screen, typeable node.

        Not "the first node claiming focus" -- measured on Kate, Qt marks
        **twenty** nodes FOCUSED at once, including status-bar buttons, page
        tabs and list items in closed dropdowns. A depth-first search returning
        the first hit landed on a filter widget's list item and concluded you
        could not type, while the editor sat two levels up plainly marked
        ``text``/editable. So every focused node has to be considered, and
        SHOWING weeds out the ones parked in hidden popups.
        """
        if depth > MAX_DEPTH or budget[0] > MAX_NODES:
            return None
        budget[0] += 1
        try:
            states = node.get_state_set()
            if states.contains(self._atspi.StateType.FOCUSED) and states.contains(
                self._atspi.StateType.SHOWING
            ) and self._is_editable(node):
                return node
            for i in range(node.get_child_count()):
                found = self._descend(node.get_child_at_index(i), depth + 1, budget)
                if found is not None:
                    return found
        except Exception:  # noqa: BLE001 -- the tree changes as we walk it
            return None
        return None

    # -- internals -----------------------------------------------------
    def _handle(self, event) -> None:
        # detail1 is 1 when focus is gained, 0 when lost. A loss is always
        # followed by a gain somewhere, so acting only on gains keeps the
        # keyboard from flickering between the two events.
        if getattr(event, "detail1", 0) != 1:
            return
        try:
            editable = self._is_editable(event.source)
            if editable:
                # This one is exact -- the event names the object that gained
                # focus, with none of the ambiguity of searching for it.
                self._field = event.source
            self._on_focus(editable)
        except Exception as exc:  # noqa: BLE001
            log.debug("focus callback failed: %s", exc)

    def _is_editable(self, source) -> bool:
        try:
            role = source.get_role_name()
        except Exception:  # noqa: BLE001 -- the object may already be gone
            return False
        try:
            editable = source.get_state_set().contains(
                self._atspi.StateType.EDITABLE
            )
        except Exception:  # noqa: BLE001
            editable = False
        return bool(editable) or role in EDITABLE_ROLES
