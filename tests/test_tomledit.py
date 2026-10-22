"""The in-place TOML editor: change one thing, keep everything else.

What matters is the diff, so most tests assert on the whole text after an
edit -- not just that it parses. A file the user wrote by hand has comments,
odd spacing and its own order, and every byte of that is theirs.
"""

from __future__ import annotations

import tomllib
import unittest

from padmode.tomledit import Document, EditError, render

SAMPLE = """\
# my padmode config
[general]
modifier = "GUIDE"   # keep it on Guide
volume_step = 5

[bindings]
B = "frontend"
START = ["notify-send", "hi"]  # a command

[desktop]
pointer_speed = 900.0
dead_zone = 0.15

[desktop.bindings]
Y = "osk"

[overlays.discord]
class = "discord"
command = ["discord"]

[overlays.music]
class = "YouTube Music Desktop App"

[frontend]
class = "pegasus"

[metadata.steam.ignore]
228980 = false
"""


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    a, b = before.splitlines(), after.splitlines()
    return [(x, y) for x, y in zip(a, b) if x != y]


class SetTest(unittest.TestCase):
    def test_a_value_changes_where_it_stands(self):
        doc = Document(SAMPLE)
        doc.set(["general"], "volume_step", 10)
        self.assertEqual(doc.text, SAMPLE.replace("volume_step = 5",
                                                  "volume_step = 10"))

    def test_the_comment_on_the_line_survives(self):
        doc = Document(SAMPLE)
        doc.set(["general"], "modifier", "SELECT")
        self.assertIn('modifier = "SELECT"   # keep it on Guide\n', doc.text)
        self.assertEqual(len(changed_lines(SAMPLE, doc.text)), 1)

    def test_a_list_value(self):
        doc = Document(SAMPLE)
        doc.set(["bindings"], "START", ["notify-send", 'say "hi"'])
        self.assertIn('START = ["notify-send", "say \\"hi\\""]  # a command',
                      doc.text)
        self.assertEqual(doc.data["bindings"]["START"],
                         ["notify-send", 'say "hi"'])

    def test_a_missing_key_joins_its_table(self):
        doc = Document(SAMPLE)
        doc.set(["desktop"], "curve", 2.5)
        self.assertIn("dead_zone = 0.15\ncurve = 2.5\n\n[desktop.bindings]",
                      doc.text)

    def test_it_takes_the_tables_indentation(self):
        doc = Document("[osk]\n    height = 30\n")
        doc.set(["osk"], "position", "top")
        self.assertEqual(doc.text,
                         '[osk]\n    height = 30\n    position = "top"\n')

    def test_a_missing_table_goes_at_the_end(self):
        doc = Document(SAMPLE)
        doc.set(["unlock"], "enabled", True)
        self.assertTrue(doc.text.startswith(SAMPLE))
        self.assertTrue(doc.text.endswith("\n\n[unlock]\nenabled = true\n"))

    def test_an_empty_file(self):
        doc = Document("")
        doc.set(["unlock", "bindings"], "X", "9")
        self.assertEqual(doc.text, '[unlock.bindings]\nX = "9"\n')

    def test_a_file_without_a_final_newline(self):
        doc = Document("[a]\nx = 1")
        doc.set(["a"], "y", 2)
        doc.set(["b"], "z", 3)
        self.assertEqual(doc.text, "[a]\nx = 1\ny = 2\n\n[b]\nz = 3\n")

    def test_numeric_keys(self):
        doc = Document(SAMPLE)
        doc.set(["overlays", "music"], "command", ["ytm"])
        doc.set(["metadata", "steam", "ignore"], "1234567", True)
        self.assertIn('class = "YouTube Music Desktop App"\ncommand = ["ytm"]\n',
                      doc.text)
        self.assertIn("228980 = false\n1234567 = true\n", doc.text)
        self.assertEqual(doc.data["overlays"]["music"]["command"],
                         ["ytm"])

    def test_a_key_that_needs_quotes(self):
        doc = Document("[overlays]\n")
        doc.set(["overlays", "my app"], "class", "x")
        self.assertIn('[overlays."my app"]\nclass = "x"\n', doc.text)

    def test_a_top_level_key_goes_above_the_first_table(self):
        doc = Document("top = 1\n\n[a]\nx = 1\n")
        doc.set([], "other", 2)
        self.assertEqual(doc.text, "top = 1\nother = 2\n\n[a]\nx = 1\n")

    def test_setting_the_same_value_writes_nothing(self):
        doc = Document("x = 1.50\n")
        doc.set([], "x", 1.5)
        self.assertEqual(doc.text, "x = 1.50\n")

    def test_a_multiline_array_is_replaced_whole(self):
        text = '[bindings]\nSTART = [\n  "a",  # first\n  "b",\n]\nX = "y"\n'
        doc = Document(text)
        doc.set(["bindings"], "START", ["c"])
        self.assertEqual(doc.text, '[bindings]\nSTART = ["c"]\nX = "y"\n')

    def test_a_multiline_string_is_replaced_whole(self):
        text = 'a = """\nline "one"\nline two"""\nb = 1\n'
        doc = Document(text)
        doc.set([], "a", "short")
        self.assertEqual(doc.text, 'a = "short"\nb = 1\n')

    def test_crlf_files_stay_crlf(self):
        doc = Document("[a]\r\nx = 1\r\n")
        doc.set(["a"], "y", 2)
        doc.set(["b"], "z", 3)
        self.assertEqual(doc.text, "[a]\r\nx = 1\r\ny = 2\r\n\r\n[b]\r\nz = 3\r\n")


class RemoveTest(unittest.TestCase):
    def test_a_key_and_its_line_go(self):
        doc = Document(SAMPLE)
        self.assertTrue(doc.remove(["bindings"], "START"))
        self.assertEqual(doc.text, SAMPLE.replace(
            'START = ["notify-send", "hi"]  # a command\n', ""))

    def test_an_emptied_section_goes_unless_it_has_a_comment(self):
        doc = Document("[a]\nx = 1\n\n[b]\ny = 2\n")
        doc.remove(["a"], "x")
        self.assertEqual(doc.text, "[b]\ny = 2\n")
        doc = Document("[a]\n# keep me\nx = 1\n")
        doc.remove(["a"], "x")
        self.assertEqual(doc.text, "[a]\n# keep me\n")

    def test_an_absent_key(self):
        doc = Document(SAMPLE)
        self.assertFalse(doc.remove(["bindings"], "LB"))
        self.assertFalse(doc.remove(["nothing"], "here"))
        self.assertEqual(doc.text, SAMPLE)

    def test_a_section(self):
        doc = Document(SAMPLE)
        self.assertTrue(doc.remove_table(["overlays", "discord"]))
        self.assertIn('Y = "osk"\n\n[overlays.music]', doc.text)
        self.assertNotIn("discord", doc.text)

    def test_the_last_section(self):
        doc = Document(SAMPLE)
        doc.remove_table(["metadata", "steam", "ignore"])
        self.assertTrue(doc.text.endswith('[frontend]\nclass = "pegasus"\n'))

    def test_a_section_keeps_sub_tables_written_elsewhere(self):
        doc = Document(SAMPLE)
        doc.remove_table(["desktop"])
        self.assertIn('[desktop.bindings]\nY = "osk"', doc.text)
        self.assertNotIn("pointer_speed", doc.text)
        self.assertEqual(doc.data["desktop"], {"bindings": {"Y": "osk"}})


class RefusalTest(unittest.TestCase):
    """Shapes the editor will not guess about. The text never changes."""

    def assertRefused(self, text, edit):
        doc = Document(text)
        with self.assertRaises(EditError):
            edit(doc)
        self.assertEqual(doc.text, text)

    def test_an_inline_table(self):
        self.assertRefused('osk = { height = 30 }\n',
                           lambda d: d.set(["osk"], "height", 40))

    def test_a_dotted_key(self):
        self.assertRefused('[unlock]\nbindings.X = "9"\n',
                           lambda d: d.set(["unlock", "bindings"], "Y", "0"))
        self.assertRefused('[unlock]\nbindings.X = "9"\n',
                           lambda d: d.remove(["unlock", "bindings"], "X"))

    def test_a_table_defined_by_dotted_keys(self):
        self.assertRefused('[unlock]\nbindings.X = "9"\n',
                           lambda d: d.remove_table(["unlock", "bindings"]))

    def test_arrays_of_tables_and_broken_files(self):
        for text in ("[[x]]\na = 1\n", "a = \n"):
            with self.assertRaises(EditError):
                Document(text)

    def test_values_outside_the_schema(self):
        for value in (float("nan"), {"a": 1}, None):
            with self.assertRaises(EditError):
                render(value)


class RoundTripTest(unittest.TestCase):
    def test_every_value_changed_and_put_back_leaves_no_trace(self):
        """The property the whole module exists for."""
        original = Document(SAMPLE).data

        def leaves(node, path=()):
            for key, value in node.items():
                if isinstance(value, dict):
                    yield from leaves(value, path + (key,))
                else:
                    yield path, key, value

        for table, key, value in leaves(original):
            doc = Document(SAMPLE)
            other = ([*value, "x"] if isinstance(value, list)
                     else (not value) if isinstance(value, bool)
                     else value + 1 if isinstance(value, (int, float))
                     else value + "x")
            doc.set(table, key, other)
            self.assertEqual(changed_lines(SAMPLE, doc.text).__len__(), 1,
                             (table, key))
            doc.set(table, key, value)
            self.assertEqual(tomllib.loads(doc.text), original, (table, key))
            # Same data; same text except where a float was respelled.
            if not isinstance(value, float):
                self.assertEqual(doc.text, SAMPLE, (table, key))


class WriteTest(unittest.TestCase):
    """config.write: the only door to the disk, and it checks first."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "padmode" / "config.toml"

    def tearDown(self):
        self.dir.cleanup()

    def test_writes_what_the_daemon_accepts(self):
        from padmode import config

        doc = Document(SAMPLE)
        doc.set(["unlock"], "enabled", True)
        cfg = config.write(doc.text, self.path)
        self.assertTrue(cfg.unlock.enabled)
        self.assertEqual(self.path.read_text(), doc.text)

    def test_refuses_what_the_daemon_would_refuse(self):
        from padmode import config

        self.path.parent.mkdir(parents=True)
        self.path.write_text(SAMPLE)
        for bad in ('[bindings]\nRB = "no.such.action"\n',
                    "[metadata.steam.ignore]\nnot_an_appid = true\n",
                    "[general\n"):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.write(bad, self.path)
        self.assertEqual(self.path.read_text(), SAMPLE)
        self.assertEqual([p.name for p in self.path.parent.iterdir()],
                         ["config.toml"])

    def test_keeps_the_files_permissions(self):
        import os

        from padmode import config

        self.path.parent.mkdir(parents=True)
        self.path.write_text(SAMPLE)
        os.chmod(self.path, 0o600)
        config.write(SAMPLE.replace("volume_step = 5", "volume_step = 6"),
                     self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
