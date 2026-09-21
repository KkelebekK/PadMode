"""The on-screen keyboard, without a compositor.

Everything here runs in CI. The GTK half cannot be, so the parts that *can* be
were deliberately kept out of it: the grid, the navigation rules and the
message framing are all plain data structures, and the daemon's half talks to a
pipe it is handed rather than one it opens.

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
from padmode.daemon import (
    Mode,
    State,
    decide,
    osk_move_delta,
    osk_nav_ops,
    pointer_delta,
    stick_tick_ops,
)
from padmode.osk import keys, model, protocol
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


def with_osk(config=None, over=Mode.DESKTOP):
    """The keyboard up over ``over``. The transition is the daemon's to make."""
    state = in_desktop(config)
    state.osk_return = over
    state.mode = Mode.OSK
    return state


class TestTheGridShape(unittest.TestCase):
    """The 60% layout, and the invariant that keeps it resizable."""

    def test_every_row_is_the_same_width_on_both_layers(self):
        # Not cosmetic. Rows that disagree would not line up, and a layer that
        # changed the surface height would hit the resize bug from v0.5: a
        # mapped layer surface only propagates a size change when it *grows*.
        for name, rows in keys.LAYERS.items():
            for index, row in enumerate(rows):
                with self.subTest(layer=name, row=index):
                    self.assertAlmostEqual(
                        sum(key.width for key in row), keys.ROW_UNITS
                    )

    def test_both_layers_have_the_same_number_of_rows(self):
        counts = {len(rows) for rows in keys.LAYERS.values()}
        self.assertEqual(len(counts), 1)

    def test_the_frame_keys_do_not_move_between_layers(self):
        # The whole point of swapping only the middle: nothing a thumb is
        # resting on shifts when you toggle.
        def frame(rows):
            return [
                [(k.action, k.width) for k in row if k.action]
                for row in rows
            ]
        self.assertEqual(frame(keys.LETTER_LAYER), frame(keys.SYMBOL_LAYER))

    def test_the_layer_toggle_sits_where_escape_would(self):
        self.assertEqual(keys.LETTER_LAYER[0][0].action, "layer")

    def test_the_cursor_keys_are_beside_the_space_bar(self):
        bottom = keys.LETTER_LAYER[-1]
        self.assertEqual([k.action for k in bottom][-3:], ["space", "left", "right"])


class TestNavigation(unittest.TestCase):
    def setUp(self):
        self.kb = model.Keyboard()

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
        for layer in keys.LAYERS:
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
        self.kb = model.Keyboard()

    def test_pressing_a_letter_types_it(self):
        emit = self.kb.press()
        self.assertEqual(emit, model.Emit(protocol.TYPE, text="q"))

    def test_shift_applies_to_one_letter_then_clears(self):
        self.kb.act("shift")
        self.assertEqual(self.kb.press().text, "Q")
        self.assertEqual(self.kb.shift, model.SHIFT_OFF)
        self.assertEqual(self.kb.press().text, "q")

    def test_shift_twice_locks(self):
        self.kb.act("shift")
        self.kb.act("shift")
        self.assertEqual(self.kb.shift, model.SHIFT_LOCK)
        self.assertEqual(self.kb.press().text, "Q")
        self.assertEqual(self.kb.press().text, "Q")

    def test_shift_a_third_time_turns_it_off(self):
        for _ in range(3):
            self.kb.act("shift")
        self.assertEqual(self.kb.shift, model.SHIFT_OFF)

    def test_shift_does_not_change_digits(self):
        self.kb.row, self.kb.col = 0, 1   # column 0 is the layer toggle
        self.kb.act("shift")
        # The symbol layer owns those characters; a digit key stays a digit,
        # which is the only version that survives a non-US layout.
        self.assertEqual(self.kb.press().text, "1")

    def test_capslock_locks_shift_outright(self):
        # One press, not the two that shift's off -> once -> lock would need.
        self.kb.act("capslock")
        self.assertEqual(self.kb.shift, model.SHIFT_LOCK)
        self.assertEqual(self.kb.press().text, "Q")
        self.assertEqual(self.kb.press().text, "Q")

    def test_capslock_again_turns_it_off(self):
        self.kb.act("capslock")
        self.kb.act("capslock")
        self.assertEqual(self.kb.shift, model.SHIFT_OFF)

    def test_capslock_clears_a_one_shot_shift(self):
        self.kb.act("shift")
        self.kb.act("capslock")
        self.assertEqual(self.kb.shift, model.SHIFT_LOCK)

    def test_capslock_sends_no_keystroke(self):
        # It locks our own shift rather than pressing a real CapsLock, so the
        # grid can draw in capitals and closing cannot strand the desktop.
        self.assertIsNone(self.kb.act("capslock"))

    def test_named_keys(self):
        for action, key in (("back", "BACKSPACE"), ("enter", "ENTER"),
                            ("tab", "TAB"), ("left", "LEFT"), ("right", "RIGHT")):
            with self.subTest(action=action):
                self.assertEqual(
                    self.kb.act(action), model.Emit(protocol.KEY, keys=(key,))
                )

    def test_space_is_a_character(self):
        self.assertEqual(self.kb.act("space"), model.Emit(protocol.TYPE, text=" "))

    def test_every_character_on_both_layers_can_be_typed(self):
        # The grid promising a character the virtual keyboard cannot send is
        # the one bug this feature must not have.
        for name, rows in keys.LAYERS.items():
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
        self.kb = model.Keyboard()

    def test_pressing_a_modifier_holds_it_down(self):
        emit = self.kb.act("super")
        self.assertEqual(emit, model.Emit(protocol.HOLD, keys=("LEFTMETA",), down=True))
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
            key.action for row in keys.LAYERS[keys.LETTERS] for key in row
        }
        self.assertTrue(set(protocol.MODIFIERS) <= actions)

    def test_the_modifiers_sit_along_the_bottom(self):
        bottom = [key.action for key in keys.LETTER_LAYER[-1]]
        self.assertEqual(bottom[:3], ["ctrl", "super", "alt"])


class TestLayers(unittest.TestCase):
    def test_switching_layer_keeps_the_highlight_inside_the_grid(self):
        kb = model.Keyboard()
        for row in range(len(kb.rows)):
            for col in range(len(kb.rows[row])):
                kb.layer, kb.row, kb.col = keys.LETTERS, row, col
                kb._sync_x()
                kb.act("layer")
                with self.subTest(row=row, col=col):
                    self.assertLess(kb.col, len(kb.rows[kb.row]))
                    self.assertIsNotNone(kb.current)
                kb.act("layer")

    def test_switching_layer_does_not_move_the_highlight_sideways(self):
        # The frame is identical between layers, so a key that exists on both
        # must stay put -- Backspace jumping mid-word would be maddening.
        kb = model.Keyboard()
        kb.row, kb.col = 0, len(kb.rows[0]) - 1   # backspace
        kb._sync_x()
        where = kb.x
        kb.act("layer")
        self.assertEqual(kb.current.action, "back")
        self.assertAlmostEqual(kb.x, where)

    def test_a_character_key_lands_under_the_same_point(self):
        kb = model.Keyboard()
        kb.row, kb.col = 2, 1     # 'a', directly after caps
        kb._sync_x()
        low, high = keys.SPANS[keys.LETTERS][2][1]
        kb.act("layer")
        # The symbols row 2 has 1.5U keys, so the index differs -- but the key
        # we land on must still cover where the highlight visually was.
        span_low, span_high = keys.SPANS[keys.SYMBOLS][kb.row][kb.col]
        self.assertLessEqual(span_low, (low + high) / 2)
        self.assertGreater(span_high, (low + high) / 2)

    def test_the_toggle_key_says_where_it_goes(self):
        kb = model.Keyboard()
        toggle = kb.rows[0][0]
        self.assertEqual(kb.label_of(toggle), "?#")
        kb.act("layer")
        self.assertEqual(kb.label_of(kb.rows[0][0]), "abc")

    def test_layer_toggles_back(self):
        kb = model.Keyboard()
        kb.act("layer")
        kb.act("layer")
        self.assertEqual(kb.layer, keys.LETTERS)


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
        client.move(1.0, 2.0)

    def test_a_move_carries_both_distances(self):
        client, proc = self.client()
        client.move(12.34, -5.0)
        kind, fields = protocol.decode(proc.stdin.written)
        self.assertEqual(kind, protocol.MOVE)
        # Rounded: the surface keeps the remainder, and the tick sends 60 of
        # these a second while the stick is pushed.
        self.assertEqual((fields["dx"], fields["dy"]), (12.3, -5.0))

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
    """What the pad does while the keyboard is up.

    It is a mode, so the answer is "whatever ``[osk.bindings]`` says, and
    nothing otherwise". The one exception is the exit button.
    """

    def setUp(self):
        self.config = Config()

    def test_the_default_desktop_binding_opens_it(self):
        state = in_desktop(self.config)
        decision = decide(KEY, Y, 1, state, self.config)
        self.assertEqual([op.kind for op in decision.osk], ["open"])

    def test_asking_for_it_does_not_take_the_mode(self):
        # Only the daemon knows whether the renderer came up, so decide() asks
        # rather than transitioning. A mode that said "keyboard" with nothing on
        # screen would swallow the whole pad.
        state = in_desktop(self.config)
        decide(KEY, Y, 1, state, self.config)
        self.assertIs(state.mode, Mode.DESKTOP)

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

    def test_a_button_the_keyboard_does_not_use_does_nothing(self):
        # SELECT is Esc in desktop mode, and while the keyboard is up it is
        # bound to nothing -- so it does nothing. This is the whole difference
        # from the layer this replaced, where it kept its desktop job. The
        # layout has no Escape key, so that is a real thing given up; press B
        # first if you want it.
        state = with_osk(self.config)
        decision = decide(KEY, SELECT, 1, state, self.config)
        self.assertFalse(decision.osk)
        self.assertFalse(decision.virtual)

    def test_the_release_of_a_button_does_nothing_either(self):
        # No desktop binding is reachable from here, so there is no key that a
        # stray release could let up.
        state = with_osk(self.config)
        decide(KEY, A, 1, state, self.config)
        self.assertFalse(decide(KEY, A, 0, state, self.config).virtual)
        self.assertFalse(decide(KEY, SELECT, 0, state, self.config).virtual)

    def test_the_exit_button_still_leaves_the_mode(self):
        # Checked before anything else the keyboard could claim: it is the one
        # escape that must never need thinking about.
        state = with_osk(self.config)
        decision = decide(KEY, GUIDE, 1, state, self.config)
        self.assertIs(decision.mode, Mode.IDLE)
        self.assertIs(state.mode, Mode.IDLE)

    def test_the_exit_button_releases_the_grab(self):
        from padmode.daemon import Grab

        state = with_osk(self.config)
        self.assertIs(decide(KEY, GUIDE, 1, state, self.config).grab, Grab.RELEASE)

    def test_opening_from_idle_enters_desktop_mode_first(self):
        # Not straight to OSK: the keyboard needs the virtual devices desktop
        # mode opens, and desktop is where a renderer that will not start
        # should leave you.
        config = parse({"bindings": {"RB": "osk"}})
        state = State()
        decide(KEY, GUIDE, 1, state, config)
        decision = decide(KEY, buttons.code_of("RB"), 1, state, config)
        self.assertIs(decision.mode, Mode.DESKTOP)
        self.assertEqual([op.kind for op in decision.osk], ["open"])


class TestTheKeyboardIsAMode(unittest.TestCase):
    """Entering and leaving, and what it goes back to.

    The keyboard used to be a flag over desktop mode. Making it a mode is what
    lets every button be either bound here or inert, but it means the mode
    underneath has to be remembered and handed back correctly -- including an
    overlay, which is owed a return to the game.
    """

    def daemon(self, osk=None, **kwargs):
        from padmode.daemon import Daemon

        self.osk = _Osk() if osk is None else osk
        self.virtual = _Virtual()
        daemon = Daemon(
            parse({}), _Reader(), _Runner(), self.virtual,
            kwargs.get("windows"), self.osk,
        )
        daemon.state.mode = kwargs.get("mode", Mode.DESKTOP)
        return daemon

    def test_opening_over_desktop_remembers_desktop(self):
        daemon = self.daemon()
        daemon._open_osk()
        self.assertIs(daemon.state.mode, Mode.OSK)
        self.assertIs(daemon.state.osk_return, Mode.DESKTOP)

    def test_closing_goes_back_to_desktop(self):
        daemon = self.daemon()
        daemon._open_osk()
        daemon._close_osk()
        self.assertIs(daemon.state.mode, Mode.DESKTOP)

    def test_opening_over_an_overlay_goes_back_to_the_overlay(self):
        # Not to the desktop. Closing the keyboard is not leaving the overlay,
        # and landing on the desktop would strand the user one press further
        # from the game than they were.
        daemon = self.daemon(mode=Mode.OVERLAY)
        daemon._open_osk()
        self.assertIs(daemon.state.osk_return, Mode.OVERLAY)
        daemon._close_osk()
        self.assertIs(daemon.state.mode, Mode.OVERLAY)

    def test_a_renderer_that_will_not_start_leaves_you_in_desktop_mode(self):
        # Deliberately not _abort()'s landing of IDLE. A keyboard that cannot
        # draw should cost you the keyboard, not the pointer as well.
        daemon = self.daemon(osk=_DeadOsk())
        daemon._open_osk()
        self.assertIs(daemon.state.mode, Mode.DESKTOP)

    def test_a_renderer_that_dies_hands_the_mode_back(self):
        daemon = self.daemon()
        daemon._open_osk()
        self.osk.alive = False
        daemon._pump_osk()
        self.assertIs(daemon.state.mode, Mode.DESKTOP)

    def test_the_renderer_saying_it_cannot_run_hands_the_mode_back(self):
        daemon = self.daemon()
        daemon._open_osk()
        daemon._handle_osk(protocol.FAILED, {"reason": "no layer shell"})
        self.assertIs(daemon.state.mode, Mode.DESKTOP)

    def test_closing_from_the_grid_hands_the_mode_back(self):
        daemon = self.daemon()
        daemon._open_osk()
        daemon._handle_osk(protocol.CLOSED, {})
        self.assertIs(daemon.state.mode, Mode.DESKTOP)

    def test_opening_twice_is_harmless(self):
        daemon = self.daemon()
        daemon._open_osk()
        daemon._open_osk()
        self.assertEqual(self.osk.shown, 1)
        self.assertIs(daemon.state.osk_return, Mode.DESKTOP)

    def test_it_cannot_be_opened_from_idle(self):
        # The virtual devices are desktop mode's to open, and OSK inherits the
        # grab from the mode underneath rather than taking one of its own.
        daemon = self.daemon(mode=Mode.IDLE)
        daemon._open_osk()
        self.assertIs(daemon.state.mode, Mode.IDLE)
        self.assertEqual(self.osk.shown, 0)

    def test_a_direction_held_while_it_opens_does_not_travel(self):
        # reset_inputs on the way in. Otherwise the deflection that was driving
        # the pointer a frame ago is read as a push on the grid.
        daemon = self.daemon()
        daemon.state.sticks[buttons.ABS_X] = 0.95
        daemon._open_osk()
        self.assertEqual(daemon.state.sticks, {})
        self.assertIsNone(daemon.state.osk_nav)

    def test_leaving_the_keyboard_over_an_overlay_still_owes_the_return(self):
        # Guide from the keyboard leaves everything, and the overlay underneath
        # is still owed its return to the game. _handle resolves the previous
        # mode through osk_return for exactly this.
        windows = _Windows()
        daemon = self.daemon(mode=Mode.OVERLAY, windows=windows)
        daemon._open_osk()
        daemon._return_to = "the-game"
        daemon._handle(KEY, GUIDE, 1)
        self.assertIs(daemon.state.mode, Mode.IDLE)
        self.assertIsNotNone(daemon._pending_return)

    def test_and_the_keyboard_is_taken_off_screen_first(self):
        # Our layer surface is enumerable by the window backends, so nothing of
        # ours may be on screen while windows are being searched for.
        daemon = self.daemon(mode=Mode.OVERLAY, windows=_Windows())
        daemon._open_osk()
        daemon._handle(KEY, GUIDE, 1)
        self.assertFalse(self.osk.alive)

    def test_going_in_and_out_is_not_a_mode_entry(self):
        # Otherwise every open would tear down and rebuild the virtual devices,
        # and every close over an overlay would fire a return to the game.
        daemon = self.daemon(windows=_Windows())
        daemon._open_osk()
        daemon._close_osk()
        self.assertIsNone(daemon._pending_return)
        self.assertTrue(self.osk.alive)


class TestTheLeftStickDrivesTheGrid(unittest.TestCase):
    """Both sticks belong to the keyboard while it is up.

    The pointer has nothing to click with by then -- the keyboard's shortcuts
    take all three mouse buttons -- so a cursor you can move but not click was
    worth less than a second d-pad and a way to shove the panel aside.
    """

    FULL = 32767

    def setUp(self):
        self.config = Config()
        self.state = with_osk(self.config)
        self.state.axis_ranges = {
            code: (-32768, 32767)
            for code in (buttons.ABS_X, buttons.ABS_Y,
                         buttons.ABS_RX, buttons.ABS_RY)
        }

    def push(self, axis, fraction, now=0.0):
        return decide(ABS, axis, int(self.FULL * fraction), self.state,
                      self.config, now)

    def test_pushing_the_stick_steps_the_grid(self):
        decision = self.push(buttons.ABS_X, 0.9)
        self.assertEqual([(op.kind, op.value) for op in decision.osk],
                         [("step", "right")])
        self.assertFalse(decision.virtual)

    def test_all_four_directions(self):
        for axis, fraction, expected in (
            (buttons.ABS_X, 0.9, "right"), (buttons.ABS_X, -0.9, "left"),
            (buttons.ABS_Y, 0.9, "down"), (buttons.ABS_Y, -0.9, "up"),
        ):
            with self.subTest(expected=expected):
                self.setUp()
                self.assertEqual(self.push(axis, fraction).osk[0].value, expected)

    def test_the_pointer_is_never_asked_while_the_keyboard_is_up(self):
        # There is no branch for the keyboard in pointer_delta any more: the
        # tick simply does not call it in OSK mode. Proving the parking now
        # means proving the mode is what decides.
        self.push(buttons.ABS_X, 0.9)
        self.assertNotIn(self.state.mode, (Mode.DESKTOP, Mode.OVERLAY))

    def test_the_pointer_moves_again_once_the_keyboard_is_gone(self):
        self.state.sticks[buttons.ABS_X] = 0.9
        self.state.mode = Mode.DESKTOP
        dx, _, _, _ = pointer_delta(self.state, self.config, 1 / 60)
        self.assertGreater(dx, 0.0)

    def test_a_flick_steps_once_not_twice(self):
        # The disarm-until-recentred rule, inherited whole from desktop mode.
        # A stick released from full deflection rings back past centre, and
        # without this a flick right also fires left.
        self.assertTrue(self.push(buttons.ABS_X, 0.95, now=0.0).osk)
        for value in (0.99, 0.8, 0.7):
            with self.subTest(value=value):
                self.assertFalse(self.push(buttons.ABS_X, value, now=0.01).osk)
        # The rebound past centre must not fire the opposite direction either.
        self.assertFalse(self.push(buttons.ABS_X, -0.8, now=0.02).osk)

    def test_it_re_arms_once_the_stick_is_back_at_centre(self):
        self.push(buttons.ABS_X, 0.95, now=0.0)
        self.push(buttons.ABS_X, 0.0, now=1.0)      # observed near centre
        self.assertTrue(self.push(buttons.ABS_X, 0.95, now=1.1).osk)

    def test_a_diagonal_push_steps_the_dominant_way_only(self):
        # A mostly-vertical push must not trip the horizontal direction. It
        # does step *vertically*, which is the sector model showing through:
        # the stick is asked where it points, not which axis just moved, so a
        # horizontal event on a stick held up answers "up".
        self.state.sticks[buttons.ABS_Y] = -0.95
        decision = self.push(buttons.ABS_X, 0.65)
        self.assertEqual([op.value for op in decision.osk], ["up"])

    def test_but_a_push_near_the_diagonal_still_steps(self):
        # What is *not* inherited is the size of the margin. Desktop mode pays
        # for a wedge where neither direction fires, because there the same
        # stick chooses between stepping and scrolling and guessing wrong is
        # worse than doing nothing. The keyboard's stick only navigates, so at
        # the desktop's 2.0 each direction would get a 53 degree cone and 147
        # degrees of the circle would step nowhere. Found with a thumb: rolling
        # the stick around the circle, the highlight stopped answering at the
        # diagonals while the d-pad was fine.
        self.state.sticks[buttons.ABS_Y] = -0.65
        self.assertEqual(self.push(buttons.ABS_X, 0.7).osk[0].value, "right")

    def test_and_raising_the_knob_brings_the_wedge_back(self):
        # Proof the knob is wired to the grid rather than merely parsed.
        self.config = parse({"osk": {"stick_dominance": 2.0}})
        self.state = with_osk(self.config)
        self.state.axis_ranges = {
            code: (-32768, 32767)
            for code in (buttons.ABS_X, buttons.ABS_Y,
                         buttons.ABS_RX, buttons.ABS_RY)
        }
        self.state.sticks[buttons.ABS_Y] = -0.65
        self.assertFalse(self.push(buttons.ABS_X, 0.7).osk)

    def test_the_right_stick_neither_scrolls_nor_tabs(self):
        # It is moving the keyboard instead. A stick cannot do both, and being
        # able to push the panel off the field you are typing into is not
        # available anywhere else. RS_RIGHT is Tab in the defaults and would
        # otherwise fire on every push meant to move the panel.
        decision = self.push(buttons.ABS_RX, 0.9)
        self.assertFalse(decision.osk)
        self.assertFalse(decision.virtual)

    def test_the_tick_does_not_smuggle_a_bound_direction_through(self):
        # The tick re-reads the same axes from scratch, so routing the event
        # path away from the desktop bindings and leaving the tick alone would
        # have Tab firing anyway, once, some frames later. RS_RIGHT is Tab in
        # the defaults, and here the right stick is moving the panel.
        from padmode.daemon import Daemon

        daemon = Daemon(parse({}), _Reader(), _Runner(), _Virtual(), None, _Osk())
        daemon.state.mode = Mode.OSK
        daemon.state.sticks[buttons.ABS_RX] = 0.9
        daemon.state.sticks[buttons.ABS_X] = 0.9
        daemon._tick()
        self.assertEqual(daemon._virtual.keys_tapped, [])

    def test_the_right_stick_moves_the_keyboard(self):
        self.state.sticks[buttons.ABS_RX] = 0.9
        self.state.sticks[buttons.ABS_RY] = -0.9
        dx, dy = osk_move_delta(self.state, self.config, 1 / 60)
        self.assertGreater(dx, 0.0)
        self.assertLess(dy, 0.0)

    def test_it_does_not_move_when_the_keyboard_is_down(self):
        self.state.mode = Mode.DESKTOP
        self.state.sticks[buttons.ABS_RX] = 0.9
        self.assertEqual(osk_move_delta(self.state, self.config, 1 / 60), (0.0, 0.0))

    def test_a_resting_right_stick_moves_nothing(self):
        # The daemon skips the message entirely when this is zero, so an idle
        # stick costs no pipe traffic at all.
        self.state.sticks[buttons.ABS_RX] = 0.05
        self.assertEqual(osk_move_delta(self.state, self.config, 1 / 60), (0.0, 0.0))

    def test_a_bound_direction_still_works_once_the_keyboard_is_gone(self):
        # The mode took the whole stick; leaving must hand all of it back.
        self.state.mode = Mode.DESKTOP
        self.assertTrue(
            stick_tick_ops(self.state, self.config.desktop, 1.0) == ()
        )
        self.state.sticks[buttons.ABS_RX] = 0.9
        self.assertTrue(stick_tick_ops(self.state, self.config.desktop, 1.0))


class TestHoldingADirectionTravels(unittest.TestCase):
    """Hold a direction and the highlight keeps going.

    The grid is thirteen keys wide, and one step per push meant thirteen
    pushes to reach Backspace -- which is what "stiff" meant. The first step is
    still immediate, from the event; the rest come off the tick, because a
    stick held at deflection and a d-pad held down both sit there emitting
    nothing at all.
    """

    FULL = 32767
    DELAY = Config().osk.repeat_delay
    INTERVAL = Config().osk.repeat_interval

    def setUp(self):
        self.config = Config()
        self.state = with_osk(self.config)
        self.state.axis_ranges = {
            code: (-32768, 32767)
            for code in (buttons.ABS_X, buttons.ABS_Y)
        }

    def push(self, fraction, now=0.0, axis=buttons.ABS_X):
        return decide(ABS, axis, int(self.FULL * fraction), self.state,
                      self.config, now)

    def hat(self, value, now=0.0, axis=buttons.ABS_HAT0X):
        return decide(ABS, axis, value, self.state, self.config, now)

    def steps(self, now):
        return [op.value for op in osk_nav_ops(self.state, self.config, now)]

    # -- the stick -----------------------------------------------------
    def test_the_first_step_is_immediate_and_the_next_one_waits(self):
        self.assertEqual([op.value for op in self.push(0.9).osk], ["right"])
        self.assertEqual(self.steps(self.DELAY - 0.01), [])
        self.assertEqual(self.steps(self.DELAY), ["right"])

    def test_then_it_repeats_at_the_configured_rate(self):
        self.push(0.9)
        self.assertEqual(self.steps(self.DELAY), ["right"])
        self.assertEqual(self.steps(self.DELAY + self.INTERVAL / 2), [])
        self.assertEqual(self.steps(self.DELAY + self.INTERVAL), ["right"])

    def test_letting_go_stops_it(self):
        self.push(0.9)
        self.push(0.1, now=self.DELAY / 2)
        self.assertIsNone(self.state.osk_nav)
        self.assertEqual(self.steps(self.DELAY * 4), [])

    def test_relaxing_a_little_keeps_it_going(self):
        # No thumb holds a stick at full deflection for a second. Stopping the
        # travel because it drifted to 0.5 would be the stiffness back again.
        self.push(0.95)
        self.push(0.55, now=self.DELAY / 2)
        self.assertEqual(self.steps(self.DELAY), ["right"])

    def test_the_spring_back_after_a_long_hold_does_not_step_the_other_way(self):
        # The settle window is set when the direction *fires*, and a hold
        # outlasts it -- so by the time the stick is let go there is nothing
        # covering the ring back past centre, and it takes a step left on the
        # way home. Letting go blocks the opposite direction instead.
        self.push(0.95, now=0.0)
        self.steps(5.0)                      # held for five seconds
        self.push(0.1, now=5.0)              # let go: passes through centre
        self.assertFalse(self.push(-0.8, now=5.01).osk)  # the overshoot

    def test_a_direction_can_be_pushed_again_after_recentring(self):
        self.push(0.95, now=0.0)
        self.push(0.0, now=1.0)
        self.assertTrue(self.push(0.95, now=1.5).osk)

    def test_pushing_the_same_way_again_is_not_delayed_by_the_release(self):
        # Only the rebound direction is blocked. Blocking the whole axis for a
        # settle window would swallow this push -- and a swallowed push stays
        # swallowed until the stick goes home, so the user just sees the
        # keyboard ignore them.
        self.push(0.95, now=0.0)
        self.steps(2.0)                       # held, travelling
        self.push(0.05, now=2.0)              # released, passing centre
        self.assertTrue(self.push(0.95, now=2.02).osk)

    def test_pushing_the_other_way_travels_the_other_way(self):
        self.push(0.9, now=0.0)
        self.push(0.0, now=1.0)
        self.push(-0.9, now=1.5)
        self.assertEqual(self.steps(1.5 + self.DELAY), ["left"])

    # -- the d-pad -----------------------------------------------------
    def test_the_dpad_travels_too(self):
        self.assertEqual([op.value for op in self.hat(1).osk], ["right"])
        self.assertEqual(self.steps(self.DELAY), ["right"])

    def test_releasing_the_dpad_stops_it(self):
        self.hat(1)
        self.hat(0, now=0.1)
        self.assertIsNone(self.state.osk_nav)
        self.assertEqual(self.steps(self.DELAY * 4), [])

    def test_releasing_the_other_axis_does_not_stop_it(self):
        # Roll from right to up and back off the vertical: the horizontal is
        # still held, and it is the one that should still be travelling.
        self.hat(1, axis=buttons.ABS_HAT0X)
        self.hat(-1, now=0.1, axis=buttons.ABS_HAT0Y)
        self.hat(0, now=0.2, axis=buttons.ABS_HAT0X)
        self.assertEqual(self.steps(0.1 + self.DELAY), ["up"])

    # -- turning it off, and not travelling when there is nothing to ---
    def test_zero_interval_gives_one_step_per_push(self):
        config = parse({"osk": {"repeat_interval": 0}})
        state = with_osk(config)
        state.axis_ranges = {buttons.ABS_X: (-32768, 32767)}
        self.assertTrue(decide(ABS, buttons.ABS_X, self.FULL, state, config, 0.0).osk)
        self.assertEqual(osk_nav_ops(state, config, 10.0), ())

    def test_it_stops_when_the_keyboard_goes_away(self):
        self.push(0.9)
        self.state.mode = Mode.DESKTOP
        self.assertEqual(self.steps(self.DELAY), [])
        # Cleared rather than left to go stale, so putting the keyboard back up
        # does not resume a push from before it closed.
        self.assertIsNone(self.state.osk_nav)


class _Reader:
    def __init__(self):
        self.axis_ranges = {}
        self.trigger_axes = {}
        self.grabbed = False

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.grabbed = False

    def set_tick(self, interval):
        pass


class _Runner:
    def preflight(self, bindings):
        pass

    def run(self, binding):
        pass


class _Windows:
    """A window backend that does nothing, so a return can be scheduled."""

    def find_window(self, window_class):
        return None

    def activate_window(self, window_id):
        pass

    def get_active_window(self):
        return None


class _Virtual:
    def __init__(self):
        self.clicks = []
        self.held = set()
        self.keys_tapped = []

    def open(self):
        return True

    def click(self, code, pressed):
        self.clicks.append((code, pressed))
        if pressed:
            self.held.add(code)
        else:
            self.held.discard(code)

    def key(self, code, pressed):
        pass

    def tap_keys(self, codes):
        self.keys_tapped.append(tuple(codes))

    def move(self, dx, dy):
        self.pointer = (dx, dy)

    def scroll(self, dx, dy):
        self.wheel = (dx, dy)

    def release_all(self):
        # The real one clears everything it is holding; a fake that did not
        # would hide exactly the bug these tests exist for.
        self.held.clear()

    def close(self):
        pass


class _Osk:
    """Stands in for the renderer process."""

    def __init__(self):
        self.shown = 0
        self.hidden = 0
        self.asked = 0
        self.alive = True
        self.steps = []
        self.moves = []

    def ask_focus(self):
        self.asked += 1

    def step(self, direction):
        self.steps.append(direction)

    def move(self, dx, dy):
        self.moves.append((dx, dy))

    def start(self):
        return True

    def stop(self):
        self.alive = False

    def show(self, hints):
        self.shown += 1

    def hide(self):
        self.hidden += 1

    def poll(self):
        return []


class _DeadOsk(_Osk):
    """A renderer that cannot run: no toolkit, or no layer-shell."""

    def start(self):
        return False


class TestAutoOpen(unittest.TestCase):
    """Opening is deliberately hard; closing is deliberately narrow."""

    def daemon(self, **osk_overrides):
        from padmode.daemon import Daemon

        table = {"osk": osk_overrides} if osk_overrides else {}
        config = parse(table)
        self.osk = _Osk()
        self.virtual = _Virtual()
        daemon = Daemon(config, _Reader(), _Runner(), self.virtual, None, self.osk)
        daemon.state.mode = Mode.DESKTOP
        return daemon

    def up(self, daemon):
        return daemon.state.mode is Mode.OSK

    def click(self, daemon):
        """What the daemon does when the pad presses the left mouse button."""
        from padmode.daemon import VirtualOp

        daemon._apply(VirtualOp("click", (buttons.CLICKS["LEFT"],), pressed=True))

    def test_a_text_field_after_our_click_opens_it(self):
        daemon = self.daemon()
        self.click(daemon)
        daemon._focus_changed(True)
        self.assertTrue(self.up(daemon))
        self.assertEqual(self.osk.shown, 1)

    def test_a_text_field_with_no_click_of_ours_does_not(self):
        # An application focusing its own search box at startup. Opening then
        # would cost the pointer for a keyboard nobody asked for.
        daemon = self.daemon()
        daemon._focus_changed(True)
        self.assertFalse(self.up(daemon))
        self.assertEqual(self.osk.shown, 0)

    def test_the_click_window_expires(self):
        daemon = self.daemon()
        self.click(daemon)
        daemon._clicked_at -= 60.0  # a minute ago, not just now
        daemon._focus_changed(True)
        self.assertFalse(self.up(daemon))

    def test_leaving_a_text_field_closes_what_opened_itself(self):
        daemon = self.daemon()
        self.click(daemon)
        daemon._focus_changed(True)
        daemon._focus_changed(False)
        self.assertFalse(self.up(daemon))
        self.assertEqual(self.osk.hidden, 1)

    def test_it_does_not_close_one_you_asked_for(self):
        # The whole point of tracking how it opened: a keyboard you summoned
        # should not vanish because focus wandered.
        daemon = self.daemon()
        daemon._open_osk()               # as the Y button does
        daemon._focus_changed(False)
        self.assertTrue(self.up(daemon))
        self.assertEqual(self.osk.hidden, 0)

    def test_a_manual_open_stays_manual_even_after_a_text_field(self):
        daemon = self.daemon()
        daemon._open_osk()
        self.click(daemon)
        daemon._focus_changed(True)      # already open: changes nothing
        daemon._focus_changed(False)
        self.assertTrue(self.up(daemon))

    def test_disabled_means_nothing_happens(self):
        daemon = self.daemon(auto_open=False)
        self.click(daemon)
        daemon._focus_changed(True)
        self.assertFalse(self.up(daemon))

    def test_nothing_happens_outside_a_pad_mode(self):
        daemon = self.daemon()
        daemon.state.mode = Mode.IDLE
        self.click(daemon)
        daemon._focus_changed(True)
        self.assertFalse(self.up(daemon))

    def test_opening_does_not_leave_the_mouse_button_held(self):
        # Auto-open lands *between* the press and the release of the very click
        # that caused it. The keyboard claims A, so the release is swallowed as
        # a keyboard button -- and BTN_LEFT stays down across the whole
        # desktop, which not even a physical mouse can clear.
        daemon = self.daemon()
        self.click(daemon)
        self.assertIn(buttons.CLICKS["LEFT"], self.virtual.held)
        daemon._focus_changed(True)
        self.assertEqual(self.virtual.held, set(),
                         "the keyboard opened holding a mouse button down")

    def test_opening_drops_a_debounced_release_still_in_flight(self):
        # Same hazard by a different route: a release held back for debounce
        # would fire after the keyboard took the button over.
        daemon = self.daemon()
        self.click(daemon)
        daemon.state.pending_release["A"] = ("whatever", 0.0)
        daemon._focus_changed(True)
        self.assertEqual(daemon.state.pending_release, {})

    def test_a_click_schedules_a_look_at_what_has_focus(self):
        # Clicking a field that already had focus raises no AT-SPI event at
        # all, and that is the ordinary case of clicking back into the field
        # you were just typing in. So the click asks, rather than waiting.
        daemon = self.daemon()
        self.click(daemon)
        self.assertIsNotNone(daemon._focus_check_at)
        daemon._focus_check_at = 0.0        # due
        daemon._pump_osk()
        self.assertEqual(self.osk.asked, 1)
        self.assertIsNone(daemon._focus_check_at, "asked more than once")

    def test_an_automatic_open_keeps_watching_the_field(self):
        # There is no "focus left" event -- measured, AT-SPI never sends one --
        # so the only way to notice the field going away is to keep asking.
        daemon = self.daemon()
        self.click(daemon)
        daemon._focus_changed(True)
        self.assertIsNotNone(daemon._focus_check_at)
        daemon._focus_check_at = 0.0
        daemon._pump_osk()
        self.assertEqual(self.osk.asked, 1)
        self.assertIsNotNone(daemon._focus_check_at, "stopped watching after one look")

    def test_a_manual_open_is_not_watched(self):
        daemon = self.daemon()
        daemon._open_osk()
        self.assertIsNone(daemon._focus_check_at)

    def test_the_watch_stops_once_it_closes(self):
        daemon = self.daemon()
        self.click(daemon)
        daemon._focus_changed(True)
        daemon._focus_changed(False)          # the field went away
        self.assertFalse(self.up(daemon))
        self.assertIsNone(daemon._focus_check_at)

    def test_it_does_not_ask_while_the_keyboard_is_already_up(self):
        daemon = self.daemon()
        daemon._open_osk()
        self.click(daemon)
        self.assertIsNone(daemon._focus_check_at)

    def test_it_does_not_ask_when_auto_open_is_off(self):
        daemon = self.daemon(auto_open=False)
        self.click(daemon)
        self.assertIsNone(daemon._focus_check_at)

    def test_closing_it_by_hand_clears_the_automatic_flag(self):
        daemon = self.daemon()
        self.click(daemon)
        daemon._focus_changed(True)
        daemon._close_osk()
        self.assertFalse(daemon._osk_auto)


class TestTheTickDrivesTheKeyboard(unittest.TestCase):
    """The two continuous jobs, end to end through the daemon.

    Both are invisible to the event path: a stick held at deflection stops
    emitting, so travelling across the grid and sliding the panel both have to
    come off the tick or not happen at all.
    """

    def daemon(self):
        from padmode.daemon import Daemon

        self.osk = _Osk()
        daemon = Daemon(parse({}), _Reader(), _Runner(), _Virtual(), None, self.osk)
        daemon.state.mode = Mode.OSK
        daemon.state.osk_return = Mode.DESKTOP
        return daemon

    def test_a_held_direction_keeps_stepping(self):
        from padmode.daemon import OskNav

        daemon = self.daemon()
        daemon.state.sticks[buttons.ABS_X] = 0.9
        daemon.state.axis_disarmed.add(buttons.ABS_X)  # already fired once
        daemon.state.osk_nav = OskNav("right", 0.0, buttons.ABS_X, 1)
        daemon._tick()
        self.assertEqual(self.osk.steps, ["right"])

    def test_a_pushed_right_stick_slides_the_panel(self):
        daemon = self.daemon()
        daemon.state.sticks[buttons.ABS_RX] = 0.9
        daemon._tick()
        self.assertEqual(len(self.osk.moves), 1)
        self.assertGreater(self.osk.moves[0][0], 0.0)

    def test_a_resting_stick_sends_nothing(self):
        daemon = self.daemon()
        daemon._tick()
        self.assertEqual(self.osk.moves, [])
        self.assertEqual(self.osk.steps, [])

    def test_the_pointer_is_not_driven_at_all(self):
        # Not "driven to zero": the tick returns before it asks. A pointer that
        # was still being computed would be one more thing to keep parked.
        daemon = self.daemon()
        daemon.state.sticks[buttons.ABS_X] = 0.9
        daemon._tick()
        self.assertFalse(hasattr(daemon._virtual, "pointer"))


class TestWhereThePanelSits(unittest.TestCase):
    """Where the keyboard sits, as a corner inside the screen's usable area.

    The surface needs a compositor and CI has none, so the arithmetic lives in
    ``model`` where it can be checked without one. What it produces is a
    top-left corner, which the surface hands over as a pair of layer-shell
    margins -- the keyboard is its own surface, so moving it moves the surface.
    """

    OUTPUT = (1920.0, 1080.0)
    PANEL = (1200.0, 300.0)

    def test_it_starts_centred_against_the_bottom(self):
        x, y = model.home_position(self.PANEL, self.OUTPUT, "bottom")
        self.assertEqual((x, y), (360.0, 780.0))

    def test_or_against_the_top(self):
        x, y = model.home_position(self.PANEL, self.OUTPUT, "top")
        self.assertEqual((x, y), (360.0, 0.0))

    def test_it_cannot_be_pushed_off_the_screen(self):
        self.assertEqual(
            model.clamp_position(-500.0, 5000.0, self.PANEL, self.OUTPUT),
            (0.0, 780.0),
        )
        self.assertEqual(
            model.clamp_position(5000.0, -500.0, self.PANEL, self.OUTPUT),
            (720.0, 0.0),
        )

    def test_a_position_inside_the_screen_is_left_alone(self):
        self.assertEqual(
            model.clamp_position(10.5, 20.25, self.PANEL, self.OUTPUT),
            (10.5, 20.25),
        )

    def test_a_panel_larger_than_the_output_goes_to_the_corner(self):
        # Better to lose the far edge than to start the highlight off-screen.
        self.assertEqual(
            model.clamp_position(50.0, 50.0, (4000.0, 4000.0), self.OUTPUT),
            (0.0, 0.0),
        )

    def test_the_box_is_the_usable_area_not_the_monitor(self):
        # The distinction that put the keyboard below the bottom of the screen.
        # A layer-shell margin is measured from the usable area, so homing
        # against the monitor's geometry overshoots by whatever the panels
        # reserve -- 34px of it on the machine this was found on.
        monitor, usable = (1920.0, 1080.0), (1920.0, 1046.0)
        _, against_monitor = model.home_position(self.PANEL, monitor, "bottom")
        _, against_usable = model.home_position(self.PANEL, usable, "bottom")
        self.assertEqual(against_monitor - against_usable, 34.0)
        # And the bottom of the keyboard lands exactly on the usable edge.
        self.assertEqual(against_usable + self.PANEL[1], usable[1])


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

    def test_the_repeat_and_move_knobs(self):
        config = parse({"osk": {
            "repeat_delay": 0.5, "repeat_interval": 0.1, "move_speed": 1500,
        }})
        self.assertEqual(config.osk.repeat_delay, 0.5)
        self.assertEqual(config.osk.repeat_interval, 0.1)
        self.assertEqual(config.osk.move_speed, 1500.0)

    def test_the_keyboard_has_its_own_dominance_margin(self):
        # Deliberately different from the desktop's, and the difference is the
        # point: one job wants the whole circle to answer, two jobs want a
        # wedge where neither fires.
        self.assertEqual(Config().osk.stick_dominance, 1.0)
        self.assertEqual(Config().desktop.stick_dominance, 2.0)
        self.assertEqual(
            parse({"osk": {"stick_dominance": 1.5}}).osk.stick_dominance, 1.5
        )

    def test_a_dominance_below_one_is_refused(self):
        # Below 1.0 the weaker axis would win, which is not a preference.
        for value in (0.5, 0, -1, "wide"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse({"osk": {"stick_dominance": value}})

    def test_nonsense_repeat_and_move_values(self):
        for key, value in (
            ("repeat_delay", 0.0), ("repeat_delay", 5.0), ("repeat_delay", "fast"),
            ("repeat_interval", -1), ("repeat_interval", 2.0),
            ("move_speed", 0), ("move_speed", True),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ConfigError):
                parse({"osk": {key: value}})

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
            "R3": "recenter",
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
        # The view draws a badge by looking the key's action up in the hints,
        # so an action bound to a button but absent from both layers would be a
        # shortcut with nowhere on screen to advertise itself.
        on_grid = {
            key.action
            for rows in keys.LAYERS.values() for row in rows for key in row
            if key.action
        }
        # commit, close and recenter act on the keyboard as a whole rather than
        # on one key, so they are what the legend line exists for. Held against
        # the grid's own list, not a copy of it.
        legend = {action for action, _ in keys.OFF_GRID_HINTS}
        bound = set(Config().osk.bindings.values())
        self.assertEqual(bound - on_grid, legend)


class TestTheDaemonNeverImportsTheRenderer(unittest.TestCase):
    """The promise that a missing toolkit disables one feature, checked.

    An accidental ``from .view import ...`` at the top of a module is exactly
    the kind of thing that survives review and turns "no GTK" into "no daemon"
    on someone else's machine.
    """

    #: The GTK half. Reaching any of these from the daemon is the failure.
    DRAWS = ("padmode.osk.surface", "padmode.osk.view", "padmode.osk.app",
             "padmode.osk.focus")

    def imports_of(self, module: str) -> set[str]:
        drawn = " or ".join(f"m == {name!r}" for name in self.DRAWS)
        code = (
            f"import {module}, sys; "
            f"print([m for m in sys.modules if m.startswith('gi') or {drawn}])"
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

    def test_the_grid_and_its_state_stay_stdlib_too(self):
        # They are the renderer's, but they are also what CI can test, which is
        # only true for as long as importing them needs no toolkit.
        for module in ("padmode.osk.keys", "padmode.osk.model"):
            with self.subTest(module=module):
                self.assertEqual(self.imports_of(module), set())


if __name__ == "__main__":
    unittest.main()
