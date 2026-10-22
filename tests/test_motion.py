"""The tab highlight's squash and stretch: shape over time, not pixels."""

import unittest

from padmode.settings.motion import MAX_HEIGHT, MIN_HEIGHT, pill

A, B = (0.0, 100.0), (300.0, 380.0)   # two tabs of different widths


def path(start, end, n=60):
    return [pill(i / n, start, end, 34.0) for i in range(n + 1)]


class PillTest(unittest.TestCase):
    def test_starts_and_rests_exactly_on_the_tabs(self):
        for s, e in ((A, B), (B, A)):
            self.assertEqual(pill(0, s, e, 34)[:2], s)
            self.assertEqual(pill(1, s, e, 34), (e[0], e[1], 34))

    def test_it_stretches_on_the_way(self):
        widths = [r - l for l, r, _ in path(A, B)]
        self.assertGreater(max(widths), 1.5 * max(A[1] - A[0], B[1] - B[0]))

    def test_it_squeezes_on_arrival(self):
        # Near the end the leading edge springs back while the trailing one
        # is still arriving: narrower than the tab it is landing on.
        tail = [r - l for l, r, _ in path(A, B)[-20:-1]]
        self.assertLess(min(tail), B[1] - B[0])

    def test_the_leading_edge_overshoots_a_little(self):
        rights = [r for _, r, _ in path(A, B)]
        self.assertGreater(max(rights), B[1])
        self.assertLess(max(rights), B[1] + 0.15 * (B[1] - A[1]))

    def test_height_answers_width_within_bounds(self):
        for s, e in ((A, B), (B, A)):
            heights = [h for _, _, h in path(s, e)]
            self.assertLess(min(heights), 34)
            self.assertGreaterEqual(min(heights), 34 * MIN_HEIGHT - 1e-9)
            self.assertLessEqual(max(heights), 34 * MAX_HEIGHT + 1e-9)

    def test_moving_left_mirrors_moving_right(self):
        lefts = [l for l, _, _ in path(B, A)]
        self.assertLess(min(lefts), A[0])  # the left edge leads and overshoots

    def test_never_inside_out(self):
        for s, e in ((A, B), (B, A), (A, (5.0, 30.0))):
            for l, r, _ in path(s, e, 200):
                self.assertGreater(r, l)


if __name__ == "__main__":
    unittest.main()
