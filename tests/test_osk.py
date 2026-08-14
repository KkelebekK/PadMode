"""The on-screen keyboard, without a compositor.

Everything here runs in CI. The renderer's GTK half cannot be, so the parts
that *can* be were deliberately kept out of it: the grid, the navigation rules
and the message framing are all plain data structures, and the daemon's half
talks to a pipe it is handed rather than one it opens.

The last class is the load-bearing one. CLAUDE.md §2 says a missing GUI toolkit
disables one feature and never stops the daemon starting -- which is only true
for as long as nothing on the daemon's import path reaches ``gi``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from padmode import buttons
from padmode.config import Config, ConfigError, OskConfig, parse
from padmode.daemon import Mode, State, decide
from padmode.osk import layout, protocol
from padmode.osk.client import OskClient

REPO = Path(__file__).resolve().parent.parent

GUIDE = buttons.code_of("GUIDE")
L3 = buttons.code_of("L3")
A = buttons.code_of("A")
B = buttons.code_of("B")
X = buttons.code_of("X")
Y = buttons.code_of("Y")
SELECT = buttons.code_of("SELECT")
KEY = buttons.EV_KEY
ABS = buttons.EV_ABS


def in_desktop(config=None):
    """A state sitting in desktop mode, the way the daemon would leave it."""
    config = config or Config()
    state = State()
    decide(KEY, GUIDE, 1, state, config)
    decide(KEY, L3, 1, state, config)
    return state


def with_osk(config=None):
    """Desktop mode with the keyboard up. The flag is the daemon's to set."""
    state = in_desktop(config)
    state.osk = True
    return state


class TestNavigation(unittest.TestCase):
    def setUp(self):
        self.kb = layout.Keyboard()

    def test_starts_on_a_letter(self):
        self.assertEqual(self.kb.current.char, "q")

    def test_moving_right_and_down(self):
        self.kb.step("right")
        self.assertEqual(self.kb.current.char, "w")
        self.kb.step("down")
        self.assertEqual(self.kb.current.char, "s")

    def test_columns_wrap(self):
        self.kb.col = len(self.kb.rows[self.kb.row]) - 1
        self.kb.step("right")
        self.assertEqual(self.kb.col, 0)
        self.kb.step("left")
        self.assertEqual(self.kb.col, len(self.kb.rows[self.kb.row]) - 1)

    def test_rows_wrap_onto_the_action_row(self):
        self.kb.row = 0
        self.kb.step("up")
        self.assertEqual(self.kb.row, len(self.kb.rows) - 1)
        self.assertIsNotNone(self.kb.current.action)

    def test_column_is_clamped_onto_a_shorter_row(self):
        # The action row is much shorter than a letter row; moving down from
        # the far right must land on a key that exists.
        self.kb.row = len(self.kb.rows) - 2
        self.kb.col = len(self.kb.rows[self.kb.row]) - 1
        self.kb.step("down")
        self.assertLess(self.kb.col, len(self.kb.rows[self.kb.row]))
        self.assertIsNotNone(self.kb.current)


class TestTypingAndShift(unittest.TestCase):
    def setUp(self):
        self.kb = layout.Keyboard()

    def test_pressing_a_letter_types_it(self):
        emit = self.kb.press()
        self.assertEqual(emit, layout.Emit(protocol.TYPE, text="q"))

    def test_shift_applies_to_one_letter_then_clears(self):
        self.kb.act("shift")
        self.assertEqual(self.kb.press().text, "Q")
        self.assertEqual(self.kb.shift, layout.SHIFT_OFF)
        self.assertEqual(self.kb.press().text, "q")

    def test_shift_twice_locks(self):
        self.kb.act("shift")
        self.kb.act("shift")
        self.assertEqual(self.kb.shift, layout.SHIFT_LOCK)
        self.assertEqual(self.kb.press().text, "Q")
        self.assertEqual(self.kb.press().text, "Q")

    def test_shift_a_third_time_turns_it_off(self):
        for _ in range(3):
            self.kb.act("shift")
        self.assertEqual(self.kb.shift, layout.SHIFT_OFF)

    def test_shift_does_not_change_digits(self):
        self.kb.row, self.kb.col = 0, 0
        self.kb.act("shift")
        # The symbol layer owns those characters; a digit key stays a digit,
        # which is the only version that survives a non-US layout.
        self.assertEqual(self.kb.press().text, "1")

    def test_backspace_and_enter_are_named_keys(self):
        self.assertEqual(self.kb.act("back"), layout.Emit(protocol.KEY, keys=("BACKSPACE",)))
        self.assertEqual(self.kb.act("enter"), layout.Emit(protocol.KEY, keys=("ENTER",)))

    def test_space_is_a_character(self):
        self.assertEqual(self.kb.act("space"), layout.Emit(protocol.TYPE, text=" "))

    def test_every_character_on_both_layers_can_be_typed(self):
        # The grid promising a character the virtual keyboard cannot send is
        # the one bug this feature must not have.
        for name, rows in layout.LAYERS.items():
            for row in rows:
                for key in row:
                    if key.char is None:
                        continue
                    with self.subTest(layer=name, char=key.char):
                        self.assertIsNotNone(buttons.keys_for_char(key.char))
                        self.assertIsNotNone(buttons.keys_for_char(key.char.upper()))


class TestLatchingModifiers(unittest.TestCase):
    """Ctrl/Alt/Super hold until pressed again, unlike shift."""

    def setUp(self):
        self.kb = layout.Keyboard()

    def test_pressing_a_modifier_holds_it_down(self):
        emit = self.kb.act("super")
        self.assertEqual(emit, layout.Emit(protocol.HOLD, keys=("LEFTMETA",), down=True))
        self.assertIn("super", self.kb.mods)

    def test_pressing_it_again_lets_it_up(self):
        self.kb.act("super")
        emit = self.kb.act("super")
        self.assertEqual(emit.down, False)
        self.assertNotIn("super", self.kb.mods)

    def test_a_latch_survives_a_keystroke(self):
        # The whole difference from shift: Super+A must not clear Super, or
        # holding Alt to step a window switcher would be impossible.
        self.kb.act("super")
        self.kb.press()
        self.assertIn("super", self.kb.mods)

    def test_several_at_once(self):
        for name in ("ctrl", "alt", "super"):
            self.kb.act(name)
        self.assertEqual(self.kb.mods, {"ctrl", "alt", "super"})

    def test_leaving_the_screen_releases_every_latch(self):
        # A modifier left held is a desktop nobody can use.
        self.kb.act("ctrl")
        self.kb.act("super")
        emits = self.kb.release_mods()
        self.assertEqual({e.keys[0] for e in emits}, {"LEFTCTRL", "LEFTMETA"})
        self.assertTrue(all(e.down is False for e in emits))
        self.assertEqual(self.kb.mods, set())

    def test_releasing_nothing_emits_nothing(self):
        self.assertEqual(self.kb.release_mods(), ())

    def test_every_modifier_names_a_real_key(self):
        for action, key in protocol.MODIFIERS.items():
            with self.subTest(action=action):
                self.assertIn(key, buttons.KEYS)
                self.assertIn(action, protocol.ACTIONS)

    def test_they_are_reachable_on_the_grid(self):
        # Bindable to pad buttons too, but nothing should *require* memorising
        # a shortcut.
        actions = {
            key.action for row in layout.LAYERS[layout.LETTERS] for key in row
        }
        self.assertTrue(set(protocol.MODIFIERS) <= actions)

    def test_the_action_row_is_as_wide_as_a_letter_row(self):
        rows = layout.LAYERS[layout.LETTERS]
        self.assertEqual(sum(key.width for key in rows[-1]), len(rows[0]))


class TestLayers(unittest.TestCase):
    def test_switching_layer_keeps_the_highlight_inside_the_grid(self):
        kb = layout.Keyboard()
        kb.row = 0
        kb.col = len(kb.rows[0]) - 1  # far right of a 10-wide row
        kb.act("layer")
        self.assertEqual(kb.layer, layout.SYMBOLS)
        self.assertLess(kb.col, len(kb.rows[kb.row]))

    def test_layer_toggles_back(self):
        kb = layout.Keyboard()
        kb.act("layer")
        kb.act("layer")
        self.assertEqual(kb.layer, layout.LETTERS)


class TestProtocol(unittest.TestCase):
    def test_round_trip(self):
        line = protocol.encode(protocol.STEP, dir="left")
        self.assertEqual(protocol.decode(line), ("step", {"dir": "left"}))

    def test_messages_are_newline_terminated(self):
        self.assertTrue(protocol.encode(protocol.HIDE).endswith(b"\n"))

    def test_garbage_is_ignored_rather_than_raised(self):
        # A renderer that prints something unexpected must not be able to take
        # the daemon down with it.
        for junk in (b"", b"\n", b"not json", b"[1,2]", b'{"no":"type"}', b"\xff\xfe"):
            with self.subTest(junk=junk):
                self.assertIsNone(protocol.decode(junk))


class FakeProc:
    """Just enough of Popen to exercise the client's framing and liveness.

    The write end stays open until :meth:`die` is called, because a live
    renderer is not at EOF. Closing it up front made ``ready`` look broken:
    the client saw READY and the end of the pipe in the same poll, and
    correctly forgot everything about a process that had gone.
    """

    def __init__(self, replies: bytes = b""):
        read_fd, self._write_fd = os.pipe()
        if replies:
            os.write(self._write_fd, replies)
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin = _Sink()
        self.returncode = None
        self.terminated = False

    def die(self) -> None:
        """The renderer exits: its stdout reaches EOF."""
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None
        self.returncode = 0

    def cleanup(self) -> None:
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None
        try:
            self.stdout.close()
        except OSError:
            pass

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode

    def kill(self):
        self.returncode = -9


class _Sink:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    def close(self):
        pass


class TestClient(unittest.TestCase):
    def client(self, replies=b""):
        proc = FakeProc(replies)
        self.addCleanup(proc.cleanup)
        # A preload path is set so start() never has to look for the real
        # library: whether this machine has gtk4-layer-shell is not what these
        # tests are about.
        client = OskClient(
            OskConfig(preload="/nonexistent/but/set.so"), spawn=lambda *a, **k: proc
        )
        self.assertTrue(client.start())
        return client, proc

    def test_two_messages_arriving_together_are_both_read(self):
        # A buffered reader would pull both into Python's buffer, and then
        # select() reports the fd as not ready while a message is already
        # sitting there. That silently loses keystrokes; it cost a debugging
        # session to find in the driver script, so it is pinned here.
        replies = protocol.encode(protocol.TYPE, text="a") + protocol.encode(
            protocol.TYPE, text="b"
        )
        client, _ = self.client(replies)
        got = client.poll()
        self.assertEqual(
            [fields["text"] for _, fields in got if _ == protocol.TYPE], ["a", "b"]
        )

    def test_a_split_message_is_held_until_it_is_complete(self):
        client, proc = self.client(b'{"t":"type","te')
        self.assertEqual(client.poll(), [])

    def test_eof_means_the_renderer_is_gone(self):
        client, proc = self.client(protocol.encode(protocol.READY))
        client.poll()
        self.assertTrue(client.alive)
        proc.die()
        client.poll()
        self.assertFalse(client.alive)
        self.assertFalse(client.ready)

    def test_ready_is_noticed(self):
        client, _ = self.client(protocol.encode(protocol.READY))
        self.assertFalse(client.ready)
        client.poll()
        self.assertTrue(client.ready)

    def test_sending_after_death_does_not_raise(self):
        client, _ = self.client()
        client.stop()
        client.show({})  # must be a no-op, not an exception
        client.step("up")

    def test_disabled_never_starts(self):
        client = OskClient(OskConfig(enabled=False), spawn=_no_spawn)
        self.assertFalse(client.start())

    def test_a_missing_library_disables_rather_than_raises(self):
        config = OskConfig(preload=None)
        client = OskClient(config, spawn=_no_spawn)
        with mock.patch("padmode.osk.client.preload_library", return_value=None):
            self.assertFalse(client.start())


def _no_spawn(*args, **kwargs):
    raise AssertionError("should not have spawned a renderer")


class TestRouting(unittest.TestCase):
    """What the pad does while the keyboard is up."""

    def setUp(self):
        self.config = Config()

    def test_the_default_desktop_binding_opens_it(self):
        state = in_desktop(self.config)
        decision = decide(KEY, Y, 1, state, self.config)
        self.assertEqual([op.kind for op in decision.osk], ["open"])

    def test_opening_is_press_only(self):
        state = in_desktop(self.config)
        self.assertFalse(decide(KEY, Y, 0, state, self.config).osk)

    def test_dpad_steps_the_grid_instead_of_arrowing(self):
        state = with_osk(self.config)
        decision = decide(ABS, buttons.ABS_HAT0Y, -1, state, self.config)
        self.assertEqual(decision.osk, (decision.osk[0],))
        self.assertEqual(decision.osk[0].kind, "step")
        self.assertEqual(decision.osk[0].value, "up")
        self.assertFalse(decision.virtual)

    def test_dpad_returning_to_centre_does_nothing(self):
        state = with_osk(self.config)
        self.assertFalse(decide(ABS, buttons.ABS_HAT0Y, 0, state, self.config).osk)

    def test_a_commits_instead_of_clicking(self):
        state = with_osk(self.config)
        decision = decide(KEY, A, 1, state, self.config)
        self.assertFalse(decision.virtual)
        self.assertEqual(decision.osk[0].value, "commit")

    def test_b_closes(self):
        state = with_osk(self.config)
        self.assertEqual(decide(KEY, B, 1, state, self.config).osk[0].kind, "close")

    def test_a_button_the_keyboard_does_not_use_keeps_its_desktop_job(self):
        # SELECT is Esc in desktop mode and has no keyboard meaning, so it
        # must still be Esc while the keyboard is up.
        state = with_osk(self.config)
        decision = decide(KEY, SELECT, 1, state, self.config)
        self.assertFalse(decision.osk)
        self.assertEqual(decision.virtual[0].kind, "key")

    def test_the_release_of_a_claimed_button_is_swallowed(self):
        # Otherwise it reaches the desktop bindings alone and releases a
        # virtual button that was never pressed.
        state = with_osk(self.config)
        decide(KEY, A, 1, state, self.config)
        self.assertFalse(decide(KEY, A, 0, state, self.config).virtual)

    def test_sticks_still_drive_the_pointer(self):
        # The whole reason the keyboard is a layer and not a mode.
        state = with_osk(self.config)
        state.axis_ranges = {buttons.ABS_X: (-32768, 32767)}
        decide(ABS, buttons.ABS_X, 32767, state, self.config)
        self.assertGreater(state.sticks[buttons.ABS_X], 0.9)

    def test_the_exit_button_still_leaves_the_mode(self):
        state = with_osk(self.config)
        decision = decide(KEY, GUIDE, 1, state, self.config)
        self.assertIs(decision.mode, Mode.IDLE)
        self.assertFalse(state.osk)

    def test_leaving_clears_the_keyboard_flag(self):
        state = with_osk(self.config)
        decide(KEY, GUIDE, 1, state, self.config)
        self.assertFalse(state.osk)

    def test_opening_from_idle_enters_desktop_mode_with_it_up(self):
        config = parse({"bindings": {"RB": "osk"}})
        state = State()
        decide(KEY, GUIDE, 1, state, config)
        decision = decide(KEY, buttons.code_of("RB"), 1, state, config)
        self.assertIs(decision.mode, Mode.DESKTOP)
        self.assertEqual([op.kind for op in decision.osk], ["open"])


class TestConfig(unittest.TestCase):
    def test_osk_is_a_valid_desktop_binding(self):
        config = parse({"desktop": {"bindings": {"RB": "osk"}}})
        self.assertEqual(config.desktop.bindings["RB"], "osk")

    def test_y_opens_it_by_default(self):
        self.assertEqual(Config().desktop.bindings["Y"], "osk")

    def test_binding_it_while_disabled_is_an_error(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"osk": {"enabled": False}, "desktop": {"bindings": {"RB": "osk"}}})
        self.assertIn("enabled = false", str(caught.exception))

    def test_the_dpad_cannot_be_rebound(self):
        with self.assertRaises(ConfigError) as caught:
            parse({"osk": {"bindings": {"DPAD_UP": "commit"}}})
        self.assertIn("navigates", str(caught.exception))

    def test_unknown_action(self):
        with self.assertRaises(ConfigError):
            parse({"osk": {"bindings": {"RB": "explode"}}})

    def test_position(self):
        self.assertEqual(parse({"osk": {"position": "top"}}).osk.position, "top")
        with self.assertRaises(ConfigError):
            parse({"osk": {"position": "middle"}})

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse({"osk": {"colour": "blue"}})

    def test_height_is_a_percentage_of_the_screen(self):
        self.assertEqual(Config().osk.height, 30.0)
        self.assertEqual(parse({"osk": {"height": 22}}).osk.height, 22.0)
        self.assertEqual(parse({"osk": {"height": 17.5}}).osk.height, 17.5)

    def test_absurd_heights_are_refused_with_a_reason(self):
        for value in (0, 5, 90, 200):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                parse({"osk": {"height": value}})
            self.assertIn("percent", str(caught.exception))

    def test_height_must_be_a_number(self):
        for value in ("30%", True, [30]):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse({"osk": {"height": value}})

    def test_every_default_binding_names_a_real_action(self):
        for button, action in Config().osk.bindings.items():
            with self.subTest(button=button):
                self.assertIn(action, protocol.ACTIONS)


class TestTheDaemonNeverImportsTheRenderer(unittest.TestCase):
    """The promise that a missing toolkit disables one feature, checked.

    An accidental ``from .renderer import ...`` at the top of a module is
    exactly the kind of thing that survives review and turns "no GTK" into "no
    daemon" on someone else's machine.
    """

    def imports_of(self, module: str) -> set[str]:
        code = (
            f"import {module}, sys; "
            "print([m for m in sys.modules "
            "if m.startswith('gi') or m == 'padmode.osk.renderer'])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
            check=True,
        )
        return set(eval(out.stdout))  # noqa: S307 -- our own literal list

    def test_the_daemon_pulls_in_no_toolkit(self):
        self.assertEqual(self.imports_of("padmode.daemon"), set())

    def test_nor_does_the_client_that_spawns_it(self):
        self.assertEqual(self.imports_of("padmode.osk.client"), set())

    def test_nor_does_the_command_line(self):
        self.assertEqual(self.imports_of("padmode.__main__"), set())

    def test_nor_does_config(self):
        # config imports the protocol for its action names, which is fine only
        # for as long as the protocol stays stdlib.
        self.assertEqual(self.imports_of("padmode.config"), set())


if __name__ == "__main__":
    unittest.main()
