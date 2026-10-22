"""The settings window's themes: complete, and turned into CSS that scopes."""

import re
import unittest

from padmode.config import THEMES as CONFIG_THEMES
from padmode.settings import model
from padmode.settings.themes import THEMES, all_css, css


class ThemeTest(unittest.TestCase):
    def test_every_theme_the_config_accepts_exists_and_is_offered(self):
        self.assertEqual(set(THEMES), set(CONFIG_THEMES))
        self.assertEqual({v for v, _ in model.THEME.options}, set(CONFIG_THEMES))

    def test_each_is_scoped_to_its_own_class(self):
        for name, theme in THEMES.items():
            text = css(theme)
            scopes = set(re.findall(r"\.theme-(\w+)", text))
            self.assertEqual(scopes, {name})

    def test_own_palettes_set_libadwaitas_variables_system_sets_none(self):
        self.assertIn("--accent-bg-color: #bd93f9", css(THEMES["dracula"]))
        self.assertNotIn("--window-bg-color", css(THEMES["system"]))

    def test_colours_are_valid_hex(self):
        for theme in THEMES.values():
            for value in re.findall(r"#[0-9a-fA-F]+\b", css(theme)):
                self.assertIn(len(value), (4, 7), (theme.name, value))

    def test_the_shapes_differ_not_just_the_colours(self):
        radii = {t.radius for t in THEMES.values()}
        focus = {t.focus for t in THEMES.values()}
        self.assertGreaterEqual(len(radii), 3)
        self.assertEqual(focus, {"fill", "ring", "bar"})

    def test_all_css_has_them_all(self):
        text = all_css()
        for name in THEMES:
            self.assertIn(f".theme-{name}", text)


if __name__ == "__main__":
    unittest.main()
