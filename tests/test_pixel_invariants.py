"""Exact pixel rules for the bar: pill masks, and the composed track and fill."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

BAR_W = 40
BAR_H = 7
RADIUS = widget.BAR_R
# Translucent on purpose, so the alpha arithmetic is exercised and not just the colour.
FILL_RGBA = (0x2E, 0xA0, 0x43, 120)
TRACKS = (widget.TRACK_RGBA["light"], widget.TRACK_RGBA["dark"])


def coverage(fill_width, x, y):
    """Mask values (fill, track) at one pixel, read from the same masks _compose_bar uses."""
    track_value = widget._pill_mask(BAR_W, BAR_H, RADIUS).getpixel((x, y))
    fill_value = 0
    if 0 < fill_width and x < fill_width:
        fill_value = widget._pill_mask(fill_width, BAR_H, RADIUS).getpixel((x, y))
    return fill_value, track_value


class ComposeBarPixelTests(unittest.TestCase):
    def test_pixels_fully_covered_by_the_fill_are_exactly_the_fill(self):
        for track in TRACKS:
            with self.subTest(track=track):
                image = widget._compose_bar(BAR_W, BAR_H, RADIUS, 20, track, FILL_RGBA)
                hits = 0
                for y in range(BAR_H):
                    for x in range(BAR_W):
                        fill_value, _track_value = coverage(20, x, y)
                        if fill_value == 255:
                            hits += 1
                            self.assertEqual(image.getpixel((x, y)), FILL_RGBA, (x, y))
                self.assertGreater(hits, 0)

    def test_pixels_fully_covered_by_the_track_are_exactly_the_track(self):
        for track in TRACKS:
            with self.subTest(track=track):
                image = widget._compose_bar(BAR_W, BAR_H, RADIUS, 20, track, FILL_RGBA)
                hits = 0
                for y in range(BAR_H):
                    for x in range(BAR_W):
                        fill_value, track_value = coverage(20, x, y)
                        if fill_value == 0 and track_value == 255:
                            hits += 1
                            self.assertEqual(image.getpixel((x, y)), track, (x, y))
                self.assertGreater(hits, 0)

    def test_corners_are_fully_transparent_with_and_without_a_fill(self):
        corners = ((0, 0), (BAR_W - 1, 0), (0, BAR_H - 1), (BAR_W - 1, BAR_H - 1))
        for fill in (FILL_RGBA, None):
            with self.subTest(fill=fill):
                image = widget._compose_bar(BAR_W, BAR_H, RADIUS, 20, TRACKS[0], fill)
                for corner in corners:
                    self.assertEqual(image.getpixel(corner), (0, 0, 0, 0), corner)

    def test_fill_stops_exactly_at_fill_width(self):
        middle = BAR_H // 2
        image = widget._compose_bar(BAR_W, BAR_H, RADIUS, 20, TRACKS[0], FILL_RGBA)
        self.assertEqual(image.getpixel((19, middle)), FILL_RGBA)
        self.assertEqual(image.getpixel((20, middle)), TRACKS[0])

    def test_fill_width_is_clamped_to_the_bar(self):
        track = TRACKS[0]
        empty = widget._compose_bar(BAR_W, BAR_H, RADIUS, 0, track, FILL_RGBA).tobytes()
        full = widget._compose_bar(BAR_W, BAR_H, RADIUS, BAR_W, track, FILL_RGBA).tobytes()
        self.assertNotEqual(empty, full)
        for fill_width in (-1, -BAR_W):
            with self.subTest(fill_width=fill_width):
                image = widget._compose_bar(BAR_W, BAR_H, RADIUS, fill_width, track, FILL_RGBA)
                self.assertEqual(image.tobytes(), empty)
        for fill_width in (BAR_W + 1, 3 * BAR_W):
            with self.subTest(fill_width=fill_width):
                image = widget._compose_bar(BAR_W, BAR_H, RADIUS, fill_width, track, FILL_RGBA)
                self.assertEqual(image.tobytes(), full)

    def test_zero_fill_draws_only_the_track(self):
        track = TRACKS[0]
        track_only = widget._compose_bar(BAR_W, BAR_H, RADIUS, 0, track, None).tobytes()
        for fill in (FILL_RGBA, None):
            with self.subTest(fill=fill):
                image = widget._compose_bar(BAR_W, BAR_H, RADIUS, 0, track, fill)
                self.assertEqual(image.tobytes(), track_only)
                pixels = image.load()
                for y in range(BAR_H):
                    for x in range(BAR_W):
                        red, green, blue, alpha = pixels[x, y]
                        if alpha:
                            self.assertEqual((red, green, blue), track[:3], (x, y))


class PillMaskTests(unittest.TestCase):
    def test_mask_is_symmetric_with_an_opaque_centre(self):
        for width, height, radius in ((40, 7, 3), (21, 7, 3), (12, 12, 3), (30, 9, 4), (100, 7, 3)):
            with self.subTest(size=(width, height, radius)):
                mask = widget._pill_mask(width, height, radius)
                self.assertEqual(mask.size, (width, height))
                self.assertEqual(mask.mode, "L")
                pixels = mask.load()
                for x in range(width):
                    for y in range(height):
                        self.assertEqual(pixels[x, y], pixels[width - 1 - x, y], (x, y))
                        self.assertEqual(pixels[x, y], pixels[x, height - 1 - y], (x, y))
                self.assertEqual(pixels[width // 2, height // 2], 255)

    def test_corners_are_transparent_at_the_bar_radius(self):
        for width, height, radius in ((40, 7, RADIUS), (21, 7, RADIUS), (8, 8, 3), (30, 9, 4)):
            with self.subTest(size=(width, height, radius)):
                mask = widget._pill_mask(width, height, radius)
                corners = (
                    mask.getpixel((0, 0)),
                    mask.getpixel((width - 1, 0)),
                    mask.getpixel((0, height - 1)),
                    mask.getpixel((width - 1, height - 1)),
                )
                self.assertEqual(corners, (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
