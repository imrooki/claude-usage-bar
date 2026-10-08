"""Display helpers: colour tiers, percentage rounding and caps, reset text, and per-row state."""

import calendar
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

NOW = 1791000000.0


class ColorTierTests(unittest.TestCase):
    def test_tier_boundaries_are_under_fifty_under_eighty_and_above(self):
        cases = ((0, "green"), (49.9, "green"), (50, "amber"), (79.9, "amber"), (80, "red"), (100, "red"))
        for pct, tier in cases:
            with self.subTest(pct=pct):
                self.assertEqual(widget.color_tier(pct), tier)

    def test_tier_uses_the_unrounded_percentage(self):
        """49.5 is written as 50% but stays green: the tier is decided on the float."""
        self.assertEqual(widget.display_percent(49.5), 50)
        self.assertEqual(widget.color_tier(49.5), "green")


class PercentRoundingTests(unittest.TestCase):
    def test_percent_int_rounds_half_up(self):
        cases = ((49.4, 49), (49.5, 50), (0.5, 1), (2.5, 3), (99.5, 100), (-0.4, 0))
        for pct, expected in cases:
            with self.subTest(pct=pct):
                self.assertEqual(widget.percent_int(pct), expected)

    def test_display_percent_is_clamped_to_zero_through_999(self):
        cases = ((0, 0), (49.5, 50), (998.4, 998), (998.5, 999), (999, 999),
                 (1000, 999), (1e9, 999), (1e300, 999), (-3, 0), (-0.4, 0))
        for pct, expected in cases:
            with self.subTest(pct=pct):
                self.assertEqual(widget.display_percent(pct), expected)

    def test_fill_percent_is_clamped_to_zero_through_one_hundred(self):
        cases = ((0, 0), (42.4, 42), (99.5, 100), (100.4, 100), (150, 100), (-20, 0), (1e300, 100))
        for pct, expected in cases:
            with self.subTest(pct=pct):
                self.assertEqual(widget.fill_percent(pct), expected)


class ResetTextTests(unittest.TestCase):
    def setUp(self):
        # Pin localtime to UTC so the expected clock text does not depend on the machine's zone.
        patcher = patch.object(widget.time, "localtime", side_effect=time.gmtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_five_hour_row_shows_clock_time_with_two_digit_fields(self):
        self.assertEqual(widget.reset_text(calendar.timegm((2026, 10, 7, 14, 58, 0, 0, 0, 0)), False), "14:58")
        self.assertEqual(widget.reset_text(calendar.timegm((2026, 10, 5, 0, 5, 0, 0, 0, 0)), False), "00:05")

    def test_seven_day_row_shows_a_fixed_english_weekday(self):
        cases = ((5, "Mon"), (6, "Tue"), (7, "Wed"), (8, "Thu"), (9, "Fri"), (10, "Sat"), (11, "Sun"))
        for day, weekday in cases:
            with self.subTest(day=day):
                resets_at = calendar.timegm((2026, 10, day, 12, 0, 0, 0, 0, 0))
                self.assertEqual(widget.reset_text(resets_at, True), weekday)

    def test_unrepresentable_reset_times_show_a_dash(self):
        for resets_at in (1e20, float("inf"), 10 ** 30):
            for seven_day in (False, True):
                with self.subTest(resets_at=resets_at, seven_day=seven_day):
                    self.assertEqual(widget.reset_text(resets_at, seven_day), "--")


class RowStateTests(unittest.TestCase):
    def test_missing_window_is_unknown(self):
        self.assertEqual(widget.row_state(None, NOW), "unknown")

    def test_fresh_window_before_its_reset_is_normal(self):
        self.assertEqual(widget.row_state(widget.Win(10.0, NOW + 60.0, NOW), NOW), "normal")

    def test_reset_takes_precedence_over_staleness(self):
        """At or after resets_at the row is "reset", even if the observation is also old."""
        self.assertEqual(widget.row_state(widget.Win(10.0, NOW, NOW), NOW), "reset")
        self.assertEqual(widget.row_state(widget.Win(10.0, NOW - 1.0, NOW), NOW), "reset")
        self.assertEqual(widget.row_state(widget.Win(10.0, NOW, NOW - 7200.0), NOW), "reset")

    def test_stale_only_once_the_observation_is_older_than_stale_seconds(self):
        exactly = widget.Win(10.0, NOW + 60.0, NOW - widget.STALE_SECONDS)
        over = widget.Win(10.0, NOW + 60.0, NOW - widget.STALE_SECONDS - 1)
        self.assertEqual(widget.row_state(exactly, NOW), "normal")
        self.assertEqual(widget.row_state(over, NOW), "stale")

    def test_observation_after_now_is_not_stale(self):
        """A clock that stepped back leaves observations in the future; those are not stale."""
        self.assertEqual(widget.row_state(widget.Win(10.0, NOW + 60.0, NOW + 5000.0), NOW), "normal")


if __name__ == "__main__":
    unittest.main()
