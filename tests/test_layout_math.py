"""Layout arithmetic: right edge anchoring, window height clamps, compute_layout and the offset round trip."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

BOTTOM_TASKBAR = (0, 1032, 1920, 1080)
TOP_TASKBAR = (0, 0, 1920, 48)
TRAY_INSIDE = (1800, 1036, 1900, 1076)
ABE_LEFT = 0    # not defined in the module; Windows edge codes outside SUPPORTED_EDGES
ABE_RIGHT = 2


class RightEdgeTests(unittest.TestCase):
    def test_manual_mode_is_offset_from_the_taskbar_right_edge_and_ignores_the_tray(self):
        self.assertEqual(
            widget.right_edge_x(BOTTOM_TASKBAR, 1.0, widget.MODE_MANUAL, 330, TRAY_INSIDE),
            1920 - 330)

    def test_manual_offset_is_scaled_by_dpi(self):
        # 330 * 1.25 = 412.5, rounded half up to 413
        self.assertEqual(
            widget.right_edge_x(BOTTOM_TASKBAR, 1.25, widget.MODE_MANUAL, 330, None),
            1920 - 413)

    def test_auto_mode_anchors_to_the_tray_left_edge(self):
        # TRAY_GAP (2) scaled by 1.5 is 3
        self.assertEqual(
            widget.right_edge_x(BOTTOM_TASKBAR, 1.5, widget.MODE_AUTO, 330, TRAY_INSIDE),
            1800 - 3)

    def test_auto_mode_uses_the_default_offset_without_a_usable_tray_rectangle(self):
        straddling_top = (1800, 1020, 1900, 1076)
        beyond_right = (1950, 1036, 2000, 1076)
        for tray in (None, beyond_right, straddling_top):
            with self.subTest(tray=tray):
                self.assertEqual(
                    widget.right_edge_x(BOTTOM_TASKBAR, 1.0, widget.MODE_AUTO, 330, tray),
                    1920 - widget.DEFAULT_OFFSET)


class WindowHeightTests(unittest.TestCase):
    def test_nominal_height_is_forty_logical_pixels_at_scale_one(self):
        self.assertEqual(widget.window_height(BOTTOM_TASKBAR, 1.0), widget.LOGICAL_H)

    def test_height_is_capped_at_taskbar_height_minus_four(self):
        for taskbar_height, expected in ((44, 40), (43, 39), (30, 26)):
            with self.subTest(taskbar_height=taskbar_height):
                self.assertEqual(
                    widget.window_height((0, 100, 1920, 100 + taskbar_height), 1.0), expected)

    def test_very_short_taskbar_hits_the_minimum_height(self):
        for taskbar_height in (14, 6, 0):
            with self.subTest(taskbar_height=taskbar_height):
                self.assertEqual(
                    widget.window_height((0, 100, 1920, 100 + taskbar_height), 1.0),
                    widget.MIN_HEIGHT_PX)

    def test_nominal_height_scales_with_dpi_and_is_still_capped(self):
        self.assertEqual(widget.window_height((0, 0, 1920, 96), 2.0), 80)
        self.assertEqual(widget.window_height((0, 0, 1920, 60), 2.0), 56)


class ComputeLayoutTests(unittest.TestCase):
    def test_bottom_taskbar_places_the_window_centred_vertically(self):
        self.assertEqual(
            widget.compute_layout(BOTTOM_TASKBAR, widget.ABE_BOTTOM, 1.0, 200,
                                  widget.MODE_MANUAL, 330, None),
            (1390, 1036, 200, 40))

    def test_top_taskbar_places_the_window_centred_vertically(self):
        self.assertEqual(
            widget.compute_layout(TOP_TASKBAR, widget.ABE_TOP, 1.0, 200,
                                  widget.MODE_MANUAL, 330, None),
            (1390, 4, 200, 40))

    def test_short_taskbar_shrinks_and_centres_the_window(self):
        self.assertEqual(
            widget.compute_layout((0, 1060, 1920, 1080), widget.ABE_BOTTOM, 1.0, 200,
                                  widget.MODE_MANUAL, 330, None),
            (1390, 1062, 200, 16))

    def test_unsupported_edges_return_none(self):
        for edge in (ABE_LEFT, ABE_RIGHT):
            with self.subTest(edge=edge):
                self.assertIsNone(widget.compute_layout(
                    BOTTOM_TASKBAR, edge, 1.0, 200, widget.MODE_MANUAL, 330, None))

    def test_window_wider_than_the_taskbar_is_pinned_to_its_left_edge(self):
        taskbar = (0, 1032, 500, 1080)
        self.assertEqual(
            widget.compute_layout(taskbar, widget.ABE_BOTTOM, 1.0, 600,
                                  widget.MODE_MANUAL, 0, None),
            (0, 1036, 600, 40))

    def test_auto_mode_places_the_window_against_the_tray(self):
        self.assertEqual(
            widget.compute_layout(BOTTOM_TASKBAR, widget.ABE_BOTTOM, 1.0, 200,
                                  widget.MODE_AUTO, 330, TRAY_INSIDE),
            (1598, 1036, 200, 40))

    def test_negative_offset_cannot_push_the_window_past_the_right_edge(self):
        self.assertEqual(
            widget.compute_layout(BOTTOM_TASKBAR, widget.ABE_BOTTOM, 1.0, 200,
                                  widget.MODE_MANUAL, -500, None),
            (1920 - 200, 1036, 200, 40))


class OffsetRoundTripTests(unittest.TestCase):
    def test_offset_survives_a_round_trip_at_whole_and_fractional_dpi_scales(self):
        """The offset saved after a drag reproduces the same window position."""
        for scale in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0):
            for offset in range(0, 400, 7):
                with self.subTest(scale=scale, offset=offset):
                    edge = widget.right_edge_x(BOTTOM_TASKBAR, scale, widget.MODE_MANUAL, offset, None)
                    x = edge - 150
                    back = widget.offset_from_right_edge(x, 150, BOTTOM_TASKBAR[2], scale)
                    self.assertEqual(back, offset)

    def test_low_scale_round_trip_keeps_the_pixel_position(self):
        """At 0.5 a logical offset can be rounded to a different integer, but the pixels stay put."""
        for offset in range(0, 60):
            with self.subTest(offset=offset):
                edge = widget.right_edge_x(BOTTOM_TASKBAR, 0.5, widget.MODE_MANUAL, offset, None)
                x = edge - 150
                back = widget.offset_from_right_edge(x, 150, BOTTOM_TASKBAR[2], 0.5)
                again = widget.right_edge_x(BOTTOM_TASKBAR, 0.5, widget.MODE_MANUAL, back, None) - 150
                self.assertEqual(again, x)

    def test_offset_rounds_half_up_to_a_logical_pixel(self):
        # gap of 301 physical pixels at 1.25 is 240.8 logical -> 241; 300 is exactly 240
        self.assertEqual(widget.offset_from_right_edge(1000 - 301 - 100, 100, 1000, 1.25), 241)
        self.assertEqual(widget.offset_from_right_edge(1000 - 300 - 100, 100, 1000, 1.25), 240)
        self.assertEqual(widget.offset_from_right_edge(600, 100, 1000, 1.0), 300)


if __name__ == "__main__":
    unittest.main()
