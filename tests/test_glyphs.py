import unittest

from padmode import buttons
from padmode.settings.glyphs import family_of, glyph
from padmode.settings.model import BUTTON_ORDER


class GlyphTest(unittest.TestCase):
    def test_families(self):
        self.assertEqual(family_of("Generic X-Box pad", 0x24C6), "xbox")
        self.assertEqual(family_of("Sony Interactive Entertainment Wireless Controller", 0x054C), "playstation")
        self.assertEqual(family_of("DualSense Wireless Controller", None), "playstation")
        self.assertEqual(family_of("Nintendo Switch Pro Controller", 0x057E), "nintendo")
        self.assertEqual(family_of(None, None), "xbox")

    def test_face_buttons_by_position(self):
        self.assertEqual([glyph(b, "playstation")[0] for b in "ABXY"], ["✕", "○", "△", "□"])
        self.assertEqual([glyph(b, "nintendo")[0] for b in "ABXY"], ["B", "A", "X", "Y"])
        self.assertEqual([glyph(b, "xbox")[0] for b in "ABXY"], ["A", "B", "X", "Y"])

    def test_every_bindable_input_has_a_label_in_every_family(self):
        names = set(BUTTON_ORDER) | set(buttons.BUTTONS) | set(buttons.HATS)
        for family in ("xbox", "playstation", "nintendo"):
            for name in names:
                text, style = glyph(name, family)
                self.assertTrue(text, (family, name))
                self.assertNotEqual(text, name if name.startswith(("LS_", "DPAD")) else None)

    def test_shoulders_and_sticks(self):
        self.assertEqual(glyph("RB", "playstation"), ("R1", "chip"))
        self.assertEqual(glyph("LT", "nintendo"), ("ZL", "chip"))
        self.assertEqual(glyph("LS_UP_RIGHT", "playstation")[0], "L3↗")
        self.assertEqual(glyph("DPAD_UP", "xbox")[0], "D↑")


if __name__ == "__main__":
    unittest.main()
