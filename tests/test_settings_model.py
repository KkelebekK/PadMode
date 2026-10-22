"""The settings window's model: every edit it can make, without a display."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from padmode.config import ConfigError, default_path, parse
from padmode.settings import model
from padmode.settings.model import (
    DESKTOP_MODE,
    FEEL,
    GUIDE,
    UNLOCK_MODE,
    Session,
    action_label,
    button_label,
    default_of,
)

HAND_WRITTEN = """\
# mine
[windows]
park_pointer = true

[overlays.discord]
class = "discord"
command = ["discord"]

[bindings]
SELECT = "overlay.discord"   # chat
B = "frontend"

[frontend]
class = "org.pegasus-frontend.pegasus-fe"
command = ["pegasus-fe"]

[unlock]
# Türkçe yorum
enabled = true
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.toml"
        self.path.write_text(HAND_WRITTEN)
        self.s = Session(self.path)

    def tearDown(self):
        self.dir.cleanup()


class FieldTest(Base):
    def test_every_feel_field_has_a_default_and_its_range_is_the_parsers(self):
        for _, fields in FEEL:
            for field in fields:
                default = default_of(field)
                self.assertEqual(self.s.value(field), default, field.key)
                if isinstance(field, model.Number):
                    for v in (field.minimum, field.maximum):
                        table = {field.key: int(v) if field.integer else v}
                        data = ({**table} if field.table == ("general",)
                                else {field.table[0]: table})
                        parse({"general": data} if field.table == ("general",)
                              else data)

    def test_set_and_put_back(self):
        speed = FEEL[0][1][0]
        self.s.set(speed, 1200)
        self.assertIn("[desktop]\npointer_speed = 1200.0\n", self.s.doc.text)
        self.assertEqual(self.s.value(speed), 1200.0)
        self.assertTrue(self.s.dirty)
        self.s.set(speed, 900)   # the default: the line goes, not a 900.0
        self.assertNotIn("pointer_speed", self.s.doc.text)
        self.assertFalse(self.s.dirty)  # the empty [desktop] went too

    def test_numbers_are_clamped_and_integers_stay_integers(self):
        step = FEEL[2][1][0]
        self.s.set(step, 250.7)
        self.assertIn("volume_step = 100\n", self.s.doc.text)

    def test_choices_and_toggles(self):
        osk = dict((f.key, f) for f in FEEL[3][1])
        self.s.set(osk["position"], "top")
        self.s.set(osk["auto_open"], False)
        self.assertEqual(self.s.config.osk.position, "top")
        self.assertFalse(self.s.config.osk.auto_open)


class BindingTest(Base):
    def test_effective_bindings_include_the_defaults(self):
        b = self.s.bindings(GUIDE)
        self.assertEqual(b["RB"], "media.next")
        self.assertEqual(b["SELECT"], "overlay.discord")
        self.assertEqual(b["R3"], "settings")

    def test_moving_a_default_writes_both_ends(self):
        lost = self.s.move(GUIDE, "RB", "LB")
        self.assertEqual(lost, ("LB", "media.previous"))
        b = self.s.bindings(GUIDE)
        self.assertEqual(b["LB"], "media.next")
        self.assertNotIn("RB", b)
        self.assertIn('LB = "media.next"', self.s.doc.text)
        self.assertIn("RB = false", self.s.doc.text)
        self.assertIsNone(self.s.problem)

    def test_moving_back_cleans_up(self):
        self.s.move(GUIDE, "RB", "LT")
        self.s.move(GUIDE, "LT", "RB")
        self.assertEqual(self.s.bindings(GUIDE)["RB"], "media.next")
        self.assertNotIn("RB =", self.s.doc.text)
        self.assertNotIn("LT =", self.s.doc.text)

    def test_the_comment_beside_a_binding_survives_a_move(self):
        self.s.move(GUIDE, "SELECT", "START")
        self.assertNotIn("SELECT = \"overlay", self.s.doc.text)
        self.assertIn('START = "overlay.discord"', self.s.doc.text)

    def test_unbind_a_default_and_a_custom_one(self):
        self.s.unbind(GUIDE, "A")
        self.s.unbind(GUIDE, "SELECT")
        b = self.s.bindings(GUIDE)
        self.assertNotIn("A", b)
        self.assertNotIn("SELECT", b)
        self.assertIn("A = false", self.s.doc.text)
        self.assertNotIn("SELECT", self.s.doc.text)

    def test_desktop_and_unlock_tables(self):
        self.s.move(DESKTOP_MODE, "Y", "X")
        self.assertEqual(self.s.bindings(DESKTOP_MODE)["X"], "osk")
        self.s.move(UNLOCK_MODE, "X", "LB")
        self.assertEqual(self.s.bindings(UNLOCK_MODE)["LB"], "9")
        self.assertIsNone(self.s.problem)

    def test_unlock_layouts(self):
        self.s.unlock_layout("xbox")
        b = self.s.bindings(UNLOCK_MODE)
        self.assertEqual((b["DPAD_LEFT"], b["LT"], b["X"]), ("2", "5", "0"))
        self.assertFalse(any(k.startswith("LS_") for k in b))
        self.s.unlock_layout("dial")
        self.assertEqual(self.s.bindings(UNLOCK_MODE)["LS_UP"], "1")
        self.assertNotIn("unlock.bindings", self.s.doc.text)
        self.assertIn("# Türkçe yorum", self.s.doc.text)


class OverlayTest(Base):
    def test_removing_an_overlay_takes_its_buttons_with_it(self):
        self.s.remove_overlay("discord")
        self.assertIsNone(self.s.problem)
        self.assertNotIn("discord", self.s.doc.text)

    def test_removing_the_frontend_too(self):
        self.s.remove_frontend()
        self.assertIsNone(self.s.problem)
        self.assertIsNone(self.s.config.frontend)

    def test_add_and_edit_an_overlay(self):
        self.s.set_overlay("music", "YouTube Music", ["ytm"])
        self.s.bind(GUIDE, "START", "overlay.music")
        self.assertIsNone(self.s.problem)
        self.s.set_overlay("music", "YouTube Music", None)
        self.assertIsNone(self.s.config.overlays["music"].command)

    def test_a_dangling_overlay_is_a_problem_not_a_crash(self):
        self.s.doc.remove_table(("overlays", "discord"))
        self.s._settle()
        self.assertIn("discord", self.s.problem)
        with self.assertRaises(ConfigError):
            self.s.apply()
        self.assertEqual(self.path.read_text(), HAND_WRITTEN)


class SteamTest(Base):
    def test_hide_show_and_default(self):
        self.s.set_hidden(1234567, True)
        self.s.set_hidden(228980, False)
        self.assertEqual(self.s.ignore(), {1234567: True, 228980: False})
        self.s.set_hidden(1234567, None)
        self.assertEqual(self.s.ignore(), {228980: False})
        self.assertIsNone(self.s.problem)


class ApplyTest(Base):
    def test_apply_writes_and_is_clean_after(self):
        self.s.set(FEEL[0][1][0], 1500)
        self.s.apply()
        self.assertIn("pointer_speed = 1500.0", self.path.read_text())
        self.assertFalse(self.s.dirty)
        self.assertIn("# Türkçe yorum", self.path.read_text())

    def test_discard(self):
        self.s.unbind(GUIDE, "A")
        self.s.discard()
        self.assertFalse(self.s.dirty)
        self.assertEqual(self.s.bindings(GUIDE)["A"], "media.play_pause")

    def test_no_file_yet(self):
        path = Path(self.dir.name) / "new" / "config.toml"
        s = Session(path)
        self.assertFalse(s.dirty)
        s.set(FEEL[0][1][0], 1000)
        s.apply()
        self.assertEqual(path.read_text(), "[desktop]\npointer_speed = 1000.0\n")

    def test_a_broken_file_opens_with_its_problem(self):
        self.path.write_text('[bindings]\nRB = "no.such"\n')
        s = Session(self.path)
        self.assertIn("no.such", s.problem)


class LabelTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(button_label("LS_UP_RIGHT"), "Left stick ↗")
        self.assertEqual(button_label("DPAD_LEFT"), "D-pad ←")
        self.assertEqual(button_label("RB"), "RB")
        self.assertEqual(action_label(GUIDE, "media.next"), "Next track")
        self.assertEqual(action_label(GUIDE, "overlay.chat"), "Open chat")
        self.assertEqual(action_label(GUIDE, ["notify-send", "hi"]), "Run notify-send hi")
        self.assertEqual(action_label(DESKTOP_MODE, "key.shift+tab"), "Key Shift+Tab")
        self.assertEqual(action_label(DESKTOP_MODE, "click.left"), "Left click")
        self.assertEqual(action_label(UNLOCK_MODE, "7"), "Digit 7")


if __name__ == "__main__":
    unittest.main()


class FakeSystemctl:
    def __init__(self, enabled="enabled", fail=False):
        self.state = enabled
        self.fail = fail
        self.calls = []

    def __call__(self, argv, **kw):
        import subprocess
        verb = argv[2]
        self.calls.append(verb)
        if verb == "is-enabled":
            code = 4 if self.state == "not-found" else (0 if self.state == "enabled" else 1)
            return subprocess.CompletedProcess(argv, code, self.state + "\n", "")
        if verb in ("enable", "disable"):
            if self.fail:
                return subprocess.CompletedProcess(argv, 1, "", "Access denied")
            self.state = "enabled" if verb == "enable" else "disabled"
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "active\n", "")


class AutostartTest(Base):
    def session(self, **kw):
        from padmode.settings.service import Service
        self.ctl = FakeSystemctl(**kw)
        return Session(self.path, service=Service(run=self.ctl))

    def test_pending_until_apply(self):
        s = self.session()
        self.assertTrue(s.autostart)
        s.set_autostart(False)
        self.assertTrue(s.dirty)
        self.assertNotIn("disable", self.ctl.calls)
        s.apply()
        self.assertEqual(self.ctl.state, "disabled")
        self.assertFalse(s.dirty)
        self.assertEqual(self.path.read_text(), HAND_WRITTEN)  # file untouched

    def test_discard_puts_it_back(self):
        s = self.session(enabled="disabled")
        s.set_autostart(True)
        s.discard()
        self.assertFalse(s.dirty)
        self.assertFalse(s.autostart)

    def test_no_service_no_switch(self):
        s = self.session(enabled="not-found")
        self.assertIsNone(s.autostart)
        s.set_autostart(True)
        self.assertFalse(s.dirty)

    def test_systemctl_refusing_is_said(self):
        s = self.session(fail=True)
        s.set_autostart(False)
        with self.assertRaises(ConfigError) as ctx:
            s.apply()
        self.assertIn("Access denied", str(ctx.exception))
        self.assertTrue(s.dirty)
