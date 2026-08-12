"""Window backends: the argv we build, and the text we parse back.

CI has no compositor, so every backend test injects a fake runner and asserts
on the two things that can actually be wrong without one: the command line we
build, and how we read the output. The output samples below are real, captured
from kdotool 0.2.3 on Plasma 6.7 and xdotool 3.20211022.1.

Detection gets the same treatment with a fake environment and a fake
``which``: it is pure rule-following, and the rules are the part that has to
survive somebody else's session.
"""

from __future__ import annotations

import sys
import unittest

from padmode.windows import Detection, KwinBackend, X11Backend, backend_names, detect
from padmode.windows.backend import (
    Rect,
    WindowBackend,
    as_coord,
    first_line,
    parse_shell,
    run_tool,
)

# Real kdotool output. The geometry report is prose, not a parseable format,
# which is exactly why it is pinned here.
KWIN_GEOMETRY = """Window {da465e33-2ea3-49a4-bc0d-c561aa268d4b}
  Position: 1544,42
  Geometry: 2544x1390
"""
# Also real, from a window on a scaled screen. KWin reports geometry in
# logical coordinates, which are not always whole pixels -- parsing this with
# int() raises, and the failure is silent: parking just stops working.
KWIN_GEOMETRY_FRACTIONAL = """Window {8b4dda44-015c-43ba-984c-8d3d14df46fc}
  Position: 8,464
  Geometry: 1520x813.5999999999999
"""
KWIN_MOUSE = "x:2684 y:1238 screen:1 window:{da465e33-2ea3-49a4-bc0d-c561aa268d4b}\n"
WINDOW = "{da465e33-2ea3-49a4-bc0d-c561aa268d4b}"
OTHER = "{96defac8-361d-424f-ae4f-59640dcf53c8}"

X11_GEOMETRY = "WINDOW=2097152\nX=100\nY=50\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n"
X11_MOUSE = "X=3800\nY=1069\nSCREEN=0\nWINDOW=860\n"


class FakeTool:
    """Stands in for the window tool: records argv, returns canned output."""

    def __init__(self, **responses: str | None) -> None:
        #: Keyed by subcommand, which is always argv[1] for both tools.
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str | None:
        self.calls.append(argv)
        return self.responses.get(argv[1])

    @property
    def last(self) -> list[str]:
        return self.calls[-1]


class TestKwinBackend(unittest.TestCase):
    def backend(self, **responses) -> tuple[KwinBackend, FakeTool]:
        tool = FakeTool(**responses)
        return KwinBackend(runner=tool), tool

    def test_find_window_searches_by_class(self):
        backend, tool = self.backend(search=f"{WINDOW}\n")
        self.assertEqual(backend.find_window("discord"), WINDOW)
        self.assertEqual(tool.last, ["kdotool", "search", "--class", "discord"])

    def test_find_window_takes_the_first_of_several(self):
        backend, _ = self.backend(search=f"{WINDOW}\n{OTHER}\n")
        self.assertEqual(backend.find_window("kitty"), WINDOW)

    def test_no_match_is_not_an_error(self):
        # kdotool exits 0 and prints nothing when nothing matches.
        backend, _ = self.backend(search="")
        self.assertIsNone(backend.find_window("nothing"))

    def test_output_that_is_not_an_id_is_ignored(self):
        # Anything that reaches stdout without braces is a message, not a
        # window, and activating it would be a confusing no-op.
        backend, _ = self.backend(search="Error: something went wrong\n")
        self.assertIsNone(backend.find_window("discord"))

    def test_get_active_window(self):
        backend, tool = self.backend(getactivewindow=f"{WINDOW}\n")
        self.assertEqual(backend.get_active_window(), WINDOW)
        self.assertEqual(tool.last, ["kdotool", "getactivewindow"])

    def test_get_active_window_when_the_tool_fails(self):
        backend, _ = self.backend(getactivewindow=None)
        self.assertIsNone(backend.get_active_window())

    def test_activate_window(self):
        backend, tool = self.backend(windowactivate="")
        backend.activate_window(WINDOW)
        self.assertEqual(tool.last, ["kdotool", "windowactivate", WINDOW])

    def test_window_geometry_parses_the_prose_report(self):
        backend, _ = self.backend(getwindowgeometry=KWIN_GEOMETRY)
        self.assertEqual(backend.window_geometry(WINDOW), Rect(1544, 42, 2544, 1390))

    def test_window_geometry_survives_fractional_logical_pixels(self):
        # Found by running the real thing: every window on a scaled screen has
        # a fractional size, and the first version of this parser returned None
        # for all of them.
        backend, _ = self.backend(getwindowgeometry=KWIN_GEOMETRY_FRACTIONAL)
        self.assertEqual(backend.window_geometry(WINDOW), Rect(8, 464, 1520, 814))

    def test_window_geometry_when_the_report_is_incomplete(self):
        backend, _ = self.backend(getwindowgeometry="Window {x}\n  Position: 1,2\n")
        self.assertIsNone(backend.window_geometry(WINDOW))

    def test_pointer_position(self):
        backend, _ = self.backend(getmouselocation=KWIN_MOUSE)
        self.assertEqual(backend.pointer_position(), (2684, 1238))

    def test_window_class(self):
        backend, tool = self.backend(getwindowclassname="kitty\n")
        self.assertEqual(backend.window_class(WINDOW), "kitty")
        self.assertEqual(tool.last, ["kdotool", "getwindowclassname", WINDOW])

    def test_can_park(self):
        backend, _ = self.backend()
        self.assertTrue(backend.can_park)


class TestX11Backend(unittest.TestCase):
    def backend(self, **responses) -> tuple[X11Backend, FakeTool]:
        tool = FakeTool(**responses)
        return X11Backend(runner=tool), tool

    def test_find_window_only_matches_visible_windows(self):
        # X11 apps keep unmapped windows of the same class around; activating
        # one of those focuses something the user cannot see.
        backend, tool = self.backend(search="2097152\n")
        self.assertEqual(backend.find_window("discord"), "2097152")
        self.assertEqual(
            tool.last,
            ["xdotool", "search", "--onlyvisible", "--class", "discord"],
        )

    def test_ids_must_be_decimal(self):
        backend, _ = self.backend(search="XGetClassHint failed\n")
        self.assertIsNone(backend.find_window("discord"))

    def test_get_active_window(self):
        backend, _ = self.backend(getactivewindow="2097152\n")
        self.assertEqual(backend.get_active_window(), "2097152")

    def test_activate_window(self):
        backend, tool = self.backend(windowactivate="")
        backend.activate_window("2097152")
        self.assertEqual(tool.last, ["xdotool", "windowactivate", "2097152"])

    def test_window_geometry_uses_the_shell_form(self):
        backend, tool = self.backend(getwindowgeometry=X11_GEOMETRY)
        self.assertEqual(backend.window_geometry("2097152"), Rect(100, 50, 800, 600))
        self.assertIn("--shell", tool.last)

    def test_pointer_position(self):
        backend, _ = self.backend(getmouselocation=X11_MOUSE)
        self.assertEqual(backend.pointer_position(), (3800, 1069))

    def test_window_class_rejects_xdotools_null(self):
        # xdotool prints "(null)" and still exits 0 for a window with no
        # WM_CLASS, which is not a class name anybody should see echoed back.
        backend, _ = self.backend(getwindowclassname="(null)\n")
        self.assertIsNone(backend.window_class("860"))

    def test_can_park(self):
        backend, _ = self.backend()
        self.assertTrue(backend.can_park)


class TestBaseBackend(unittest.TestCase):
    """A backend that implements only the required three still works."""

    class Minimal(WindowBackend):
        name = "minimal"

        def find_window(self, window_class):
            return None

        def activate_window(self, window_id):
            pass

        def get_active_window(self):
            return None

    def test_optional_operations_default_to_cannot(self):
        backend = self.Minimal()
        self.assertIsNone(backend.window_geometry("w"))
        self.assertIsNone(backend.pointer_position())
        self.assertIsNone(backend.window_class("w"))

    def test_can_park_is_false_without_both_operations(self):
        self.assertFalse(self.Minimal().can_park)


class TestRunTool(unittest.TestCase):
    """The one part that really does spawn a process."""

    def test_success_returns_stdout(self):
        out = run_tool([sys.executable, "-c", "print('hello')"])
        self.assertEqual((out or "").strip(), "hello")

    def test_failure_returns_none(self):
        self.assertIsNone(run_tool([sys.executable, "-c", "raise SystemExit(3)"]))

    def test_missing_binary_returns_none(self):
        # A tool that vanished after we probed for it must not take the daemon
        # down with it.
        self.assertIsNone(run_tool(["padmode-no-such-tool-xyz"]))

    def test_timeout_returns_none(self):
        out = run_tool(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2
        )
        self.assertIsNone(out)


class TestParsingHelpers(unittest.TestCase):
    def test_first_line_skips_blanks(self):
        self.assertEqual(first_line("\n\n  hello \nworld\n"), "hello")

    def test_first_line_of_nothing(self):
        self.assertIsNone(first_line(""))
        self.assertIsNone(first_line(None))

    def test_parse_shell(self):
        self.assertEqual(parse_shell('X=1\nNAME="two"\n'), {"X": "1", "NAME": "two"})

    def test_parse_shell_ignores_junk(self):
        self.assertEqual(parse_shell("not a pair\nX=1\n"), {"X": "1"})

    def test_as_coord(self):
        self.assertEqual(as_coord("42"), 42)
        self.assertEqual(as_coord("-1"), -1)
        self.assertIsNone(as_coord("(null)"))
        self.assertIsNone(as_coord(None))

    def test_as_coord_rounds_logical_pixels(self):
        self.assertEqual(as_coord("813.5999999999999"), 814)
        self.assertEqual(as_coord("8.2"), 8)

    def test_rect_centre(self):
        self.assertEqual(Rect(100, 50, 800, 600).centre, (500, 350))


def env(session=None, desktop=None, wayland=None, display=None) -> dict[str, str]:
    raw = {
        "XDG_SESSION_TYPE": session,
        "XDG_CURRENT_DESKTOP": desktop,
        "WAYLAND_DISPLAY": wayland,
        "DISPLAY": display,
    }
    return {key: value for key, value in raw.items() if value is not None}


def having(*tools: str):
    """A fake ``shutil.which`` that finds exactly the named tools."""
    return lambda tool: f"/usr/bin/{tool}" if tool in tools else None


class TestDetection(unittest.TestCase):
    def test_kde_wayland_uses_kdotool(self):
        found = detect("auto", env("wayland", "KDE"), having("kdotool"))
        self.assertIsInstance(found.backend, KwinBackend)
        self.assertIn("kdotool", found.reason)

    def test_kde_wayland_without_kdotool(self):
        found = detect("auto", env("wayland", "KDE"), having())
        self.assertIsNone(found.backend)
        self.assertIn("kdotool is not installed", found.reason)

    def test_plasma_spelling_is_also_kde(self):
        found = detect("auto", env("wayland", "plasma"), having("kdotool"))
        self.assertIsInstance(found.backend, KwinBackend)

    def test_x11_session_uses_xdotool_even_on_kde(self):
        # kdotool works on KDE X11 too, but xdotool is the better-trodden path
        # there and does not inject a script into the compositor.
        found = detect("auto", env("x11", "KDE", display=":0"), having("kdotool", "xdotool"))
        self.assertIsInstance(found.backend, X11Backend)

    def test_gnome_wayland_says_why_not(self):
        found = detect("auto", env("wayland", "GNOME"), having("kdotool", "xdotool"))
        self.assertIsNone(found.backend)
        self.assertIn("GNOME", found.reason)

    def test_unknown_wayland_compositor_names_itself(self):
        found = detect("auto", env("wayland", "sway"), having("xdotool"))
        self.assertIsNone(found.backend)
        self.assertIn("sway", found.reason)

    def test_wayland_display_beats_display(self):
        # A Wayland session almost always has DISPLAY set too, for XWayland.
        # Testing DISPLAY first would call every KDE Wayland session an X11 one.
        found = detect(
            "auto", env(desktop="KDE", wayland="wayland-0", display=":0"),
            having("kdotool", "xdotool"),
        )
        self.assertIsInstance(found.backend, KwinBackend)

    def test_no_session_information_at_all(self):
        found = detect("auto", env(), having("kdotool", "xdotool"))
        self.assertIsNone(found.backend)
        self.assertIn("cannot tell", found.reason)

    def test_explicit_backend_skips_session_sniffing(self):
        # Somebody running a compositor we have never heard of may well know
        # that xdotool works there. Second-guessing them helps nobody.
        found = detect("x11", env("wayland", "sway"), having("xdotool"))
        self.assertIsInstance(found.backend, X11Backend)

    def test_explicit_backend_still_needs_its_tool(self):
        found = detect("kwin", env("wayland", "KDE"), having())
        self.assertIsNone(found.backend)
        self.assertIn("not installed", found.reason)

    def test_none_disables_detection(self):
        found = detect("none", env("wayland", "KDE"), having("kdotool"))
        self.assertIsNone(found.backend)
        self.assertIn("disabled", found.reason)

    def test_detection_is_truthy_only_with_a_backend(self):
        self.assertTrue(detect("auto", env("wayland", "KDE"), having("kdotool")))
        self.assertFalse(Detection(None, "nope"))

    def test_backend_names_are_the_accepted_config_values(self):
        self.assertEqual(backend_names(), ["auto", "kwin", "x11", "none"])


if __name__ == "__main__":
    unittest.main()
