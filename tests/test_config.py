import tempfile
import unittest
from pathlib import Path

from padmode.config import DEFAULT_BINDINGS, ConfigError, load, parse


class TestDefaults(unittest.TestCase):
    def test_empty_config_yields_defaults(self):
        cfg = parse({})
        self.assertEqual(cfg.modifier, "GUIDE")
        self.assertEqual(cfg.volume_step, 5)
        self.assertEqual(cfg.bindings, DEFAULT_BINDINGS)
        self.assertIsNone(cfg.device_name)

    def test_missing_file_is_not_an_error(self):
        cfg = load(Path("/nonexistent/padmode/config.toml"))
        self.assertEqual(cfg.bindings, DEFAULT_BINDINGS)


class TestDevice(unittest.TestCase):
    def test_name_and_path_are_read(self):
        cfg = parse({"device": {"name": "X-Box", "path": "/dev/input/event9"}})
        self.assertEqual(cfg.device_name, "X-Box")
        self.assertEqual(cfg.device_path, "/dev/input/event9")

    def test_blank_name_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse({"device": {"name": "  "}})

    def test_non_table_section_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse({"device": "event3"})


class TestGeneral(unittest.TestCase):
    def test_modifier_is_normalised(self):
        self.assertEqual(parse({"general": {"modifier": "mode"}}).modifier, "GUIDE")

    def test_unknown_modifier_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse({"general": {"modifier": "BANANA"}})

    def test_dpad_cannot_be_the_modifier(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"general": {"modifier": "DPAD_UP"}})
        self.assertIn("d-pad", str(ctx.exception))

    def test_volume_step_bounds(self):
        self.assertEqual(parse({"general": {"volume_step": 10}}).volume_step, 10)
        for bad in (0, 101, "5", True, 2.5):
            with self.assertRaises(ConfigError):
                parse({"general": {"volume_step": bad}})


class TestBindings(unittest.TestCase):
    def test_user_bindings_merge_over_defaults(self):
        cfg = parse({"bindings": {"B": "media.stop"}})
        self.assertEqual(cfg.bindings["B"], "media.stop")
        # Untouched defaults survive.
        self.assertEqual(cfg.bindings["RB"], "media.next")

    def test_user_binding_overrides_a_default(self):
        cfg = parse({"bindings": {"RB": "media.stop"}})
        self.assertEqual(cfg.bindings["RB"], "media.stop")

    def test_false_unbinds_a_default(self):
        cfg = parse({"bindings": {"RB": False}})
        self.assertNotIn("RB", cfg.bindings)

    def test_unbinding_something_never_bound_is_harmless(self):
        cfg = parse({"bindings": {"B": False}})
        self.assertNotIn("B", cfg.bindings)

    def test_button_names_are_normalised(self):
        cfg = parse({"bindings": {"r1": "media.stop"}})
        self.assertEqual(cfg.bindings["RB"], "media.stop")

    def test_raw_command_binding(self):
        cfg = parse({"bindings": {"B": ["notify-send", "hi"]}})
        self.assertEqual(cfg.bindings["B"], ["notify-send", "hi"])

    def test_unknown_button_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"bindings": {"BANANA": "media.next"}})
        self.assertIn("BANANA", str(ctx.exception))

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"bindings": {"B": "media.teleport"}})
        self.assertIn("media.teleport", str(ctx.exception))

    def test_malformed_command_is_rejected(self):
        for bad in ([], ["ok", 2], 5, {"a": 1}):
            with self.assertRaises(ConfigError):
                parse({"bindings": {"B": bad}})

    def test_modifier_cannot_also_be_bound(self):
        with self.assertRaises(ConfigError) as ctx:
            parse({"bindings": {"GUIDE": "media.next"}})
        self.assertIn("modifier", str(ctx.exception))

    def test_modifier_conflict_respects_a_custom_modifier(self):
        # Binding GUIDE is fine once something else is the modifier...
        cfg = parse({"general": {"modifier": "START"}, "bindings": {"GUIDE": "media.next"}})
        self.assertEqual(cfg.bindings["GUIDE"], "media.next")
        # ...and now START is the one that cannot be bound.
        with self.assertRaises(ConfigError):
            parse({"general": {"modifier": "START"}, "bindings": {"START": "media.next"}})


class TestLoadFromDisk(unittest.TestCase):
    def _write(self, text):
        tmp = Path(tempfile.mkdtemp()) / "config.toml"
        tmp.write_text(text, encoding="utf-8")
        return tmp

    def test_round_trip(self):
        cfg = load(self._write(
            '[device]\nname = "X-Box"\n\n'
            '[general]\nmodifier = "START"\nvolume_step = 2\n\n'
            '[bindings]\nA = "media.stop"\nRB = false\n'
        ))
        self.assertEqual(cfg.device_name, "X-Box")
        self.assertEqual(cfg.modifier, "START")
        self.assertEqual(cfg.volume_step, 2)
        self.assertEqual(cfg.bindings["A"], "media.stop")
        self.assertNotIn("RB", cfg.bindings)

    def test_invalid_toml_is_reported_with_the_path(self):
        path = self._write("[general\nmodifier =")
        with self.assertRaises(ConfigError) as ctx:
            load(path)
        self.assertIn(str(path), str(ctx.exception))
