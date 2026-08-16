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


class TestTheGridShape(unittest.TestCase):
    """The 60% layout, and the invariant that keeps it resizable."""

    def test_every_row_is_the_same_width_on_both_layers(self):
        # Not cosmetic. Rows that disagree would not line up, and a layer that
        # changed the surface height would hit the resize bug from v0.5: a
        # mapped layer surface only propagates a size change when it *grows*.
        for name, rows in layout.LAYERS.items():
            for index, row in enumerate(rows):
                with self.subTest(layer=name, row=index):
                    self.assertAlmostEqual(
                        sum(key.width for key in row), layout.ROW_UNITS
                    )

    def test_both_layers_have_the_same_number_of_rows(self):
        counts = {len(rows) for rows in layout.LAYERS.values()}
        self.assertEqual(len(counts), 1)

    def test_the_frame_keys_do_not_move_between_layers(self):
        # The whole point of swapping only the middle: nothing a thumb is
        # resting on shifts when you toggle.
        def frame(rows):
            return [
                [(k.action, k.width) for k in row if k.action]
                for row in rows
            ]
        letters, symbols = layout.LETTER_LAYER, layout.SYMBOL_LAYER
        self.assertEqual(frame(letters), frame(symbols))

    def test_the_layer_toggle_sits_where_escape_would(self):
        self.assertEqual(layout.LETTER_LAYER[0][0].action, "layer")

    def test_the_cursor_keys_are_beside_the_space_bar(self):
        bottom = layout.LETTER_LAYER[-1]
        self.assertEqual([k.action for k in bottom][-3:], ["space", "left", "right"])


class TestNavigation(unittest.TestCase):
    def setUp(self):
        self.kb = layout.Keyboard()

    def test_starts_on_a_letter(self):
        # Column 0 of that row is Tab, so the default column is 1.
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

    def test_rows_wrap(self):
        self.kb.row = 0
        self.kb.step("up")
        self.assertEqual(self.kb.row, len(self.kb.rows) - 1)

    def test_vertical_movement_follows_position_not_column_number(self):
        # The space bar is 6.25U and the fourth key of its row; the key above
        # its middle is 'b', not the row above's fourth key. This is the whole
        # reason navigation is not by column index.
        self.kb.row, self.kb.col = 4, 3
        self.kb._sync_x()
        self.assertEqual(self.kb.current.action, "space")
        self.kb.step("up")
        self.assertEqual(self.kb.current.char, "b")

    def test_a_vertical_run_stays_in_a_straight_line(self):
        self.kb.row, self.kb.col = 4, 3
        self.kb._sync_x()
        self.assertEqual(
            [self.kb.step("up") or self.kb.current.char for _ in range(3)],
            ["b", "h", "y"],
        )

    def test_the_remembered_column_survives_a_narrow_row(self):
        # Travelling down past the bottom row and back up must return you to
        # where you started, not drag the highlight sideways.
        self.kb.row, self.kb.col = 3, 9   # '.', near the right of its row
        self.kb._sync_x()
        start = self.kb.current.char
        self.kb.step("down")
        self.kb.step("up")
        self.assertEqual(self.kb.current.char, start)

    def test_every_position_lands_on_a_real_key(self):
        # Rows span the full width with no gaps, so no move can fall off one.
        for layer in layout.LAYERS:
            self.kb.layer = layer
            for row in range(len(self.kb.rows)):
                for col in range(len(self.kb.rows[row])):
                    self.kb.row, self.kb.col = row, col
                    self.kb._sync_x()
                    for direction in ("up", "down", "left", "right"):
                        with self.subTest(layer=layer, row=row, col=col,
                                          direction=direction):
                            before = (self.kb.row, self.kb.col)
                            self.kb.step(direction)
                            self.assertIsNotNone(self.kb.current)
                            self.kb.row, self.kb.col = before


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
        self.kb.row, self.kb.col = 0, 1   # column 0 is the layer toggle
        self.kb.act("shift")
        # The symbol layer owns those characters; a digit key stays a digit,
        # which is the only version that survives a non-US layout.
        self.assertEqual(self.kb.press().text, "1")

    def test_capslock_locks_shift_outright(self):
        # One press, not the two that shift's off -> once -> lock would need.
        self.kb.act("capslock")
        self.assertEqual(self.kb.shift, layout.SHIFT_LOCK)
        self.assertEqual(self.kb.press().text, "Q")
        self.assertEqual(self.kb.press().text, "Q")

    def test_capslock_again_turns_it_off(self):
        self.kb.act("capslock")
        self.kb.act("capslock")
        self.assertEqual(self.kb.shift, layout.SHIFT_OFF)

    def test_capslock_clears_a_one_shot_shift(self):
        self.kb.act("shift")
        self.kb.act("capslock")
        self.assertEqual(self.kb.shift, layout.SHIFT_LOCK)

    def test_capslock_sends_no_keystroke(self):
        # It locks our own shift rather than pressing a real CapsLock, so the
        # grid can draw in capitals and closing cannot strand the desktop.
        self.assertIsNone(self.kb.act("capslock"))

    def test_named_keys(self):
        for action, key in (("back", "BACKSPACE"), ("enter", "ENTER"),
                            ("tab", "TAB"), ("left", "LEFT"), ("right", "RIGHT")):
            with self.subTest(action=action):
                self.assertEqual(
                    self.kb.act(action), layout.Emit(protocol.KEY, keys=(key,))
                )

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

    def test_the_modifiers_sit_along_the_bottom(self):
        bottom = [key.action for key in layout.LETTER_LAYER[-1]]
        self.assertEqual(bottom[:3], ["ctrl", "super", "alt"])


class TestLayers(unittest.TestCase):
    def test_switching_layer_keeps_the_highlight_inside_the_grid(self):
        kb = layout.Keyboard()
        for row in range(len(kb.rows)):
            for col in range(len(kb.rows[row])):
                kb.layer, kb.row, kb.col = layout.LETTERS, row, col
                kb._sync_x()
                kb.act("layer")
                with self.subTest(row=row, col=col):
                    self.assertLess(kb.col, len(kb.rows[kb.row]))
                    self.assertIsNotNone(kb.current)
                kb.act("layer")

    def test_switching_layer_does_not_move_the_highlight_sideways(self):
        # The frame is identical between layers, so a key that exists on both
        # must stay put -- Backspace jumping mid-word would be maddening.
        kb = layout.Keyboard()
        kb.row, kb.col = 0, len(kb.rows[0]) - 1   # backspace
        kb._sync_x()
        where = kb.x
        kb.act("layer")
        self.assertEqual(kb.current.action, "back")
        self.assertAlmostEqual(kb.x, where)

    def test_a_character_key_lands_under_the_same_point(self):
        kb = layout.Keyboard()
        kb.row, kb.col = 2, 1     # 'a', directly after caps
        kb._sync_x()
        low, high = layout.SPANS[layout.LETTERS][2][1]
        kb.act("layer")
        # The symbols row 2 has 1.5U keys, so the index differs -- but the key
        # we land on must still cover where the highlight visually was.
        span_low, span_high = layout.SPANS[layout.SYMBOLS][kb.row][kb.col]
        self.assertLessEqual(span_low, (low + high) / 2)
        self.assertGreater(span_high, (low + high) / 2)

    def test_the_toggle_key_says_where_it_goes(self):
        kb = layout.Keyboard()
        toggle = kb.rows[0][0]
        self.assertEqual(kb.label_of(toggle), "?#")
        kb.act("layer")
        self.assertEqual(kb.label_of(kb.rows[0][0]), "abc")

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

    def test_the_default_shortcut_map(self):
        self.assertEqual(Config().osk.bindings, {
            "A": "commit", "B": "close", "X": "back", "Y": "space",
            "START": "enter", "LB": "left", "RB": "right",
            "LT": "shift", "RT": "layer", "L3": "capslock",
        })

    def test_the_triggers_are_bindable_here(self):
        # LT and RT carry shift and the layer toggle, which is only possible
        # because analogue triggers are translated into buttons. On most pads
        # they are axes and could not be bound at all before v0.5.0.
        for button in ("LT", "RT"):
            with self.subTest(button=button):
                self.assertIn(button, Config().osk.bindings)
                self.assertIn(button, buttons.BUTTONS)

    def test_every_action_with_a_shortcut_can_carry_a_badge(self):
        # The renderer draws a badge by looking the key's action up in the
        # hints, so an action bound to a button but absent from both layers
        # would be a shortcut with nowhere on screen to advertise itself.
        on_grid = {
            key.action
            for rows in layout.LAYERS.values() for row in rows for key in row
            if key.action
        }
        # commit and close act on the grid as a whole rather than on one key,
        # so they are the two the legend line exists for.
        expected_off_grid = {"commit", "close"}
        bound = set(Config().osk.bindings.values())
        self.assertEqual(bound - on_grid, expected_off_grid)


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
