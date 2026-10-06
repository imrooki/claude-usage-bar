"""Stale age, grey fill, refresh captions, and the caption timer lifecycle."""

import json
import math
import pathlib
import sys
import time
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget
from test_refresh_feedback import make_app


NOW = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))
SCALES = (1.0, 1.25, 1.5, 2.0)


def win(pct, observed_ago, resets_in=3600.0):
    return widget.Win(float(pct), NOW + resets_in, NOW - observed_ago)


def height_for(scale):
    return widget.scaled(widget.LOGICAL_H, scale)


def snapshot(five, seven):
    return {"five_hour": five, "seven_day": seven}


def fresh_snapshot(five_pct=30.0, seven_pct=10.0):
    return snapshot(win(five_pct, 60.0), win(seven_pct, 60.0, resets_in=2 * 86400.0))


def at(hour, minute):
    return time.mktime((2026, 10, 2, hour, minute, 0, 0, 0, -1))


def max_text_alpha(image, box, rgb):
    """Largest alpha of pixels with this RGB above the invisible background floor."""
    x0, y0, x1, y1 = box
    pixels = image.load()
    best = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            red, green, blue, alpha = pixels[x, y]
            if (red, green, blue) == rgb and alpha > widget.BACKGROUND_ALPHA and alpha > best:
                best = alpha
    return best


def ink_columns(image, x0, x1, y0, y1):
    """Columns in [x0, x1) x [y0, y1) that contain a pixel above the background floor."""
    pixels = image.load()
    found = []
    for x in range(max(0, x0), min(image.width, x1)):
        for y in range(max(0, y0), min(image.height, y1)):
            if pixels[x, y][3] > widget.BACKGROUND_ALPHA:
                found.append(x)
                break
    return found


class FormatAgeTests(unittest.TestCase):
    def test_boundaries_use_minutes_then_hours_then_days(self):
        cases = (
            (0, "1m"),
            (59, "1m"),
            (60, "1m"),
            (119, "1m"),
            (120, "2m"),
            (1800, "30m"),
            (3599, "59m"),
            (3600, "60m"),
            (7199, "119m"),
            (7200, "2h"),
            (86399, "23h"),
            (86400, "24h"),
            (172799, "47h"),
            (172800, "2d"),
            (259200, "3d"),
        )
        for seconds, text in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(widget.format_age(seconds), text)

    def test_float_ages_floor_before_the_unit_break(self):
        self.assertEqual(widget.format_age(1800.9), "30m")
        self.assertEqual(widget.format_age(7199.99), "119m")
        self.assertEqual(widget.format_age(7200.0), "2h")

    def test_negative_and_non_finite_ages_are_one_minute(self):
        for seconds in (-5, -0.5, float("inf"), float("-inf"), float("nan")):
            with self.subTest(seconds=seconds):
                self.assertEqual(widget.format_age(seconds), "1m")

    def test_very_large_ages_stay_in_days(self):
        self.assertEqual(widget.format_age(10 ** 12), "999d")
        self.assertEqual(widget.format_age(10 ** 30), "999d")

    def test_days_are_capped_at_999(self):
        cases = (
            (999 * 86400 - 1, "998d"),
            (998 * 86400, "998d"),
            (999 * 86400, "999d"),
            (1000 * 86400, "999d"),
            (10 ** 12, "999d"),
            (10 ** 30, "999d"),
            (998 * 86400.0, "998d"),
            (999 * 86400.0, "999d"),
            (1000 * 86400.0, "999d"),
            (1e300, "999d"),
            (1.7976931348623157e308, "999d"),
            (-1000 * 86400, "1m"),
            (-1e300, "1m"),
        )
        for seconds, text in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(widget.format_age(seconds), text)
                self.assertTrue(text.isascii())
                self.assertLessEqual(len(text), 4)


class StaleAgeTests(unittest.TestCase):
    def test_two_stale_windows_use_the_older_observation(self):
        five = win(65, 31 * 60.0)
        seven = win(33, 41 * 60.0, resets_in=2 * 86400.0)
        self.assertEqual(widget.stale_age_seconds(five, seven, NOW), 41 * 60.0)

    def test_a_fresh_window_does_not_pull_the_age(self):
        five = win(65, 35 * 60.0)
        seven = win(33, 60.0, resets_in=2 * 86400.0)
        self.assertEqual(widget.stale_age_seconds(five, seven, NOW), 2100.0)

    def test_fresh_or_missing_windows_are_not_stale(self):
        fresh = win(30, 60.0)
        self.assertIsNone(widget.stale_age_seconds(fresh, win(10, 60.0), NOW))
        self.assertIsNone(widget.stale_age_seconds(None, None, NOW))
        stale = win(40, 41 * 60.0)
        self.assertEqual(widget.stale_age_seconds(None, stale, NOW), 41 * 60.0)

    def test_reset_window_is_not_stale_even_when_the_observation_is_old(self):
        reset = widget.Win(50.0, NOW - 10.0, NOW - 7200.0)
        self.assertIsNone(widget.stale_age_seconds(reset, None, NOW))
        stale = win(40, 31 * 60.0)
        self.assertEqual(widget.stale_age_seconds(reset, stale, NOW), 31 * 60.0)

    def test_stale_boundary_is_exclusive(self):
        exactly = win(10, widget.STALE_SECONDS)
        just_over = win(10, widget.STALE_SECONDS + 1)
        self.assertIsNone(widget.stale_age_seconds(exactly, None, NOW))
        self.assertEqual(widget.stale_age_seconds(just_over, None, NOW), 1801.0)

    def test_single_display_stores_an_age_only_while_stale(self):
        height = height_for(1.0)
        fresh = widget.build_display(fresh_snapshot(), NOW, "light", 1.0, height)
        self.assertEqual(fresh.ages, ())
        self.assertEqual(fresh.captions, ())

        stale = widget.build_display(
            snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0)),
            NOW, "light", 1.0, height)
        self.assertEqual(stale.ages, ("31m",))

        mixed = widget.build_display(
            snapshot(win(65, 35 * 60.0), win(33, 60.0, resets_in=2 * 86400.0)),
            NOW, "light", 1.0, height)
        self.assertEqual(mixed.ages, ("35m",))

    def test_dual_display_keeps_one_age_string_per_provider(self):
        height = height_for(1.0)
        codex_stale = snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0))
        mixed = widget.build_display(
            fresh_snapshot(), NOW, "light", 1.0, height,
            codex=codex_stale, codex_available=True)
        self.assertEqual(mixed.ages, ("", "31m"))

        both = widget.build_display(
            snapshot(win(65, 41 * 60.0), win(33, 41 * 60.0, resets_in=2 * 86400.0)),
            NOW, "light", 1.0, height,
            codex=snapshot(win(3, 3 * 3600.0), win(36, 3 * 3600.0, resets_in=2 * 86400.0)),
            codex_available=True)
        self.assertEqual(both.ages, ("41m", "3h"))

        fresh = widget.build_display(
            fresh_snapshot(), NOW, "light", 1.0, height,
            codex=fresh_snapshot(3, 36), codex_available=True)
        self.assertEqual(fresh.ages, ())

    def test_dual_display_caps_a_malformed_age(self):
        height = height_for(1.0)
        codex_stale = snapshot(
            widget.Win(65.0, NOW + 3600.0, -1e300),
            win(33, 60.0, resets_in=2 * 86400.0))
        display = widget.build_display(
            fresh_snapshot(), NOW, "light", 1.0, height,
            codex=codex_stale, codex_available=True)
        self.assertEqual(display.ages, ("", "999d"))
        self.assertEqual(display.width, widget.display_width("dual", 1.0, height))
        self.assertEqual(widget.render_display(display).width, display.width)

    def test_nodata_keeps_only_the_caption_and_never_ages(self):
        plain = widget.build_display(None, NOW, "light", 1.0, 40)
        self.assertEqual(plain.kind, "nodata")
        self.assertEqual(plain.ages, ())
        self.assertEqual(plain.captions, ())
        for captions in (None, (), ("",), ("", "")):
            with self.subTest(captions=captions):
                self.assertEqual(
                    widget.build_display(None, NOW, "light", 1.0, 40, captions=captions),
                    plain)
        kept = widget.build_display(None, NOW, "light", 1.0, 40, captions=("no data", ""))
        self.assertEqual(kept.kind, "nodata")
        self.assertEqual(kept.ages, ())
        self.assertEqual(kept.captions, ("no data",))
        self.assertNotEqual(kept, plain)
        self.assertEqual(kept.rows, plain.rows)

        cases = (
            (("no data", "read error"), ("read error",)),
            (("read error", "no data"), ("read error",)),
            (("no data", "no data"), ("no data",)),
            (("no data", ""), ("no data",)),
            (("", "no data"), ("no data",)),
            (("", ""), ()),
            (None, ()),
        )
        for captions, expected in cases:
            with self.subTest(captions=captions):
                display = widget.build_display(
                    None, NOW, "light", 1.0, 40,
                    codex=None, codex_available=True, captions=captions)
                self.assertEqual(display.kind, "nodata")
                self.assertEqual(display.ages, ())
                self.assertEqual(display.captions, expected)

    def test_six_argument_display_and_row_tuples_keep_their_old_shape(self):
        row = widget.RowView("5h", "normal", 30, "green", "30%", "16:50")
        rows = (row, widget.RowView("7d", "normal", 10, "green", "10%", "Sun"))
        display = widget.Display("data", rows, "light", 1.0, 212, 40)
        self.assertEqual(display.ages, ())
        self.assertEqual(display.captions, ())
        block = widget.BlockView("Claude", rows)
        self.assertEqual(block.tag, "Claude")
        self.assertEqual(block.rows[0].label, "5h")

    def test_existing_non_stale_samples_leave_the_new_fields_empty(self):
        skipped = {"stale", "dual_mixed"}
        for name in widget.SAMPLE_STATES:
            if name in skipped:
                continue
            for theme, scale in widget.SAMPLE_COMBOS:
                with self.subTest(name=name, theme=theme, scale=scale):
                    display = widget.sample_display(name, theme, scale)
                    self.assertEqual(display.ages, ())
                    self.assertEqual(display.captions, ())
                    self.assertEqual(display, widget.Display(
                        display.kind, display.rows, display.theme, display.scale,
                        display.width, display.height))


def mixed_stale_display(theme):
    """One stale amber-tier row over one fresh green row, at scale 1."""
    return widget.build_display(
        snapshot(win(65, 31 * 60.0), win(33, 60.0, resets_in=2 * 86400.0)),
        NOW, theme, 1.0, height_for(1.0))


def bar_sample_points(display):
    """Bar x and the two row centers used to sample fill pixels."""
    edges = widget.pixel_columns(1.0)
    row_height = display.height / 2.0
    return edges, int(row_height * 0.5), int(row_height * 1.5)


class StaleFillPixelTests(unittest.TestCase):
    def test_stale_fill_is_grey_not_the_tier_color(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                display = mixed_stale_display(theme)
                image = widget.render_display(display)
                edges, stale_y, _fresh_y = bar_sample_points(display)
                pixel = image.getpixel((edges["bar"][0] + 10, stale_y))
                self.assertEqual(pixel, widget.STALE_FILL_RGB + (widget.FILL_ALPHA,))
                self.assertNotEqual(pixel, widget.TIER_RGB["amber"] + (255,))

    def test_fresh_row_fill_keeps_its_tier_color(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                display = mixed_stale_display(theme)
                image = widget.render_display(display)
                edges, _stale_y, fresh_y = bar_sample_points(display)
                pixel = image.getpixel((edges["bar"][0] + 10, fresh_y))
                self.assertEqual(pixel, widget.TIER_RGB["green"] + (widget.FILL_ALPHA,))

    def test_unfilled_part_of_a_stale_bar_stays_the_track(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                display = mixed_stale_display(theme)
                image = widget.render_display(display)
                edges, stale_y, _fresh_y = bar_sample_points(display)
                pixel = image.getpixel((edges["bar"][0] + 90, stale_y))
                self.assertEqual(pixel, widget.TRACK_RGBA[theme])

    def test_reset_row_has_no_fill(self):
        display = widget.sample_display("reset", "light", 1.0)
        image = widget.render_display(display)
        edges = widget.pixel_columns(1.0)
        y = int(display.height / 2.0 * 0.5)
        pixel = image.getpixel((edges["bar"][0] + 10, y))
        self.assertEqual(pixel, widget.TRACK_RGBA["light"])

    def test_stale_row_text_is_dimmer_than_the_fresh_row(self):
        display = mixed_stale_display("light")
        image = widget.render_display(display)
        edges, _stale_y, _fresh_y = bar_sample_points(display)
        text = widget.TEXT_RGB["light"]
        split = int(display.height / 2.0)
        stale_alpha = max_text_alpha(image, (edges["label"][0], 0, edges["label"][1], split), text)
        fresh_alpha = max_text_alpha(
            image, (edges["label"][0], split, edges["label"][1], display.height), text)
        self.assertGreater(stale_alpha, 0)
        self.assertLessEqual(stale_alpha, widget.TEXT_ALPHA_STALE)
        self.assertGreater(fresh_alpha, stale_alpha)


class DualHeaderTests(unittest.TestCase):
    def _dual(self, claude, codex, scale=1.0, captions=None):
        height = height_for(scale)
        return widget.build_display(
            claude, NOW, "light", scale, height,
            codex=codex, codex_available=True, captions=captions)

    def test_stale_codex_header_carries_the_age_and_is_dim(self):
        display = self._dual(
            fresh_snapshot(),
            snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0)))
        self.assertEqual(widget.dual_header(display, 0), ("Claude", widget.TEXT_ALPHA))
        self.assertEqual(widget.dual_header(display, 1), ("Codex 31m", widget.TEXT_ALPHA_STALE))

    def test_stale_claude_header_carries_the_age_and_is_dim(self):
        display = self._dual(
            snapshot(win(65, 41 * 60.0), win(33, 41 * 60.0, resets_in=2 * 86400.0)),
            fresh_snapshot(3, 36))
        self.assertEqual(widget.dual_header(display, 0), ("Claude 41m", widget.TEXT_ALPHA_STALE))
        self.assertEqual(widget.dual_header(display, 1), ("Codex", widget.TEXT_ALPHA))

    def test_one_fresh_row_keeps_the_header_at_full_alpha(self):
        display = self._dual(
            snapshot(win(65, 41 * 60.0), win(33, 60.0, resets_in=2 * 86400.0)),
            fresh_snapshot(3, 36))
        self.assertEqual(widget.dual_header(display, 0), ("Claude 41m", widget.TEXT_ALPHA))

    def test_captions_replace_both_headers_at_full_alpha(self):
        display = self._dual(
            fresh_snapshot(),
            snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0)),
            captions=("new 14:59", "same 14:29"))
        self.assertEqual(widget.dual_header(display, 0), ("new 14:59", widget.TEXT_ALPHA))
        self.assertEqual(widget.dual_header(display, 1), ("same 14:29", widget.TEXT_ALPHA))

    def test_a_short_caption_tuple_restores_the_second_header(self):
        display = self._dual(
            fresh_snapshot(),
            snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0)),
            captions=("new 14:59",))
        self.assertEqual(widget.dual_header(display, 0), ("new 14:59", widget.TEXT_ALPHA))
        self.assertEqual(widget.dual_header(display, 1), ("Codex 31m", widget.TEXT_ALPHA_STALE))

    def test_dual_width_and_height_ignore_ages_and_captions(self):
        claude_stale = snapshot(win(65, 41 * 60.0), win(33, 41 * 60.0, resets_in=2 * 86400.0))
        codex_stale = snapshot(win(3, 3 * 3600.0), win(36, 3 * 3600.0, resets_in=2 * 86400.0))
        for scale in SCALES:
            with self.subTest(scale=scale):
                height = height_for(scale)
                expected = widget.scaled(widget.dual_logical_width(), scale)
                displays = (
                    self._dual(fresh_snapshot(), fresh_snapshot(3, 36), scale),
                    self._dual(claude_stale, codex_stale, scale),
                    self._dual(fresh_snapshot(), fresh_snapshot(3, 36), scale,
                               captions=("new 14:59", "same 14:29")),
                )
                for display in displays:
                    self.assertEqual(display.width, expected)
                    self.assertEqual(display.height, height)
                if scale == 1.0:
                    self.assertEqual(expected, 432)

    def test_caption_changes_only_the_header_band(self):
        claude = fresh_snapshot()
        codex = fresh_snapshot(3, 36)
        plain = self._dual(claude, codex)
        captioned = self._dual(claude, codex, captions=("new 14:59", "same 14:29"))
        before = widget.render_display(plain)
        after = widget.render_display(captioned)
        header = widget.dual_render_metrics(1.0, plain.height)["header_height"]
        self.assertNotEqual(
            before.crop((0, 0, before.width, header)).tobytes(),
            after.crop((0, 0, after.width, header)).tobytes())
        self.assertEqual(
            before.crop((0, header, before.width, before.height)).tobytes(),
            after.crop((0, header, after.width, after.height)).tobytes())

    def test_making_codex_stale_changes_only_the_right_half(self):
        claude = fresh_snapshot()
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                fresh = self._dual(claude, fresh_snapshot(3, 36), scale)
                stale = self._dual(
                    claude,
                    snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0)),
                    scale)
                left = widget.render_display(fresh)
                right = widget.render_display(stale)
                split = widget.scaled(216, scale)
                self.assertEqual(
                    left.crop((0, 0, split, left.height)).tobytes(),
                    right.crop((0, 0, split, right.height)).tobytes())
                self.assertNotEqual(
                    left.crop((split, 0, left.width, left.height)).tobytes(),
                    right.crop((split, 0, right.width, right.height)).tobytes())


class SingleAsideTests(unittest.TestCase):
    def test_single_aside_prefers_caption_text_over_age(self):
        height = height_for(1.0)
        fresh = widget.build_display(fresh_snapshot(), NOW, "light", 1.0, height)
        self.assertEqual(widget.single_aside(fresh)[0], "")

        stale_data = snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0))
        stale = widget.build_display(stale_data, NOW, "light", 1.0, height)
        self.assertEqual(widget.single_aside(stale), ("31m", widget.TEXT_ALPHA_STALE))

        captioned = widget.build_display(
            fresh_snapshot(), NOW, "light", 1.0, height, captions=("same 14:29",))
        self.assertEqual(widget.single_aside(captioned), ("same 14:29", widget.TEXT_ALPHA))

        both = widget.build_display(
            stale_data, NOW, "light", 1.0, height, captions=("same 14:29",))
        self.assertEqual(widget.single_aside(both), ("same 14:29", widget.TEXT_ALPHA))

    def test_width_grows_only_when_the_aside_has_text(self):
        stale_data = snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0))
        for scale in SCALES:
            with self.subTest(scale=scale):
                height = height_for(scale)
                fresh = widget.build_display(fresh_snapshot(), NOW, "light", scale, height)
                base = widget.scaled(widget.logical_columns()[1], scale)
                self.assertEqual(fresh.width, base)
                self.assertEqual(fresh.width, widget.display_width("data", scale, height))
                if scale == 1.0:
                    self.assertEqual(fresh.width, 212)
                stale = widget.build_display(stale_data, NOW, "light", scale, height)
                self.assertGreater(stale.width, fresh.width)
                self.assertEqual(stale.width, widget.display_width("data", scale, height, "31m"))

    def test_a_malformed_observed_at_cannot_widen_the_single_widget(self):
        raw = json.dumps({
            "schema": 1,
            "written_at": NOW,
            "windows": {
                "five_hour": {
                    "used_percentage": 65,
                    "resets_at": NOW + 3600.0,
                    "observed_at": -1e300,
                },
                "seven_day": {
                    "used_percentage": 33,
                    "resets_at": NOW + 2 * 86400.0,
                    "observed_at": -1e300,
                },
            },
        }).encode("utf-8")
        parsed, reason = widget.parse_snapshot(raw)
        self.assertEqual(reason, "")
        self.assertEqual(parsed["five_hour"].observed_at, -1e300)
        for scale in SCALES:
            with self.subTest(scale=scale):
                height = height_for(scale)
                display = widget.build_display(parsed, NOW, "light", scale, height)
                self.assertEqual(display.ages, ("999d",))
                widest = widget.display_width("data", scale, height, "9999d")
                self.assertLessEqual(display.width, widest)
                self.assertEqual(
                    display.width, widget.display_width("data", scale, height, "999d"))
                self.assertEqual(widget.render_display(display).width, display.width)

    def test_aside_left_sits_one_gap_past_the_reset_column(self):
        self.assertEqual(widget.ASIDE_GAP, 4)
        for scale in SCALES:
            with self.subTest(scale=scale):
                reset_right = widget.pixel_columns(scale)["reset"][1]
                self.assertEqual(
                    widget.aside_left(scale),
                    reset_right + widget.scaled(widget.ASIDE_GAP, scale))

    def test_right_edge_stays_put_when_the_aside_widens_the_widget(self):
        taskbar = (0, 1000, 4000, 1080)
        stale_data = snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0))
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                height = height_for(scale)
                fresh = widget.build_display(fresh_snapshot(), NOW, "light", scale, height)
                stale = widget.build_display(stale_data, NOW, "light", scale, height)
                captioned = widget.build_display(
                    fresh_snapshot(), NOW, "light", scale, height, captions=("same 14:29",))
                layouts = []
                for display in (fresh, stale, captioned):
                    layouts.append(widget.compute_layout(
                        taskbar, widget.ABE_BOTTOM, scale, display.width,
                        widget.MODE_AUTO, 330))
                rights = [item[0] + item[2] for item in layouts]
                self.assertEqual(rights[0], rights[1])
                self.assertEqual(rights[0], rights[2])
                widths = [item[2] for item in layouts]
                for earlier, later in ((0, 1), (0, 2), (1, 2)):
                    if widths[earlier] < widths[later]:
                        self.assertLess(layouts[later][0], layouts[earlier][0])

    def test_stale_aside_ink_is_in_the_upper_half_and_not_clipped(self):
        stale_data = snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0))
        for scale in SCALES:
            with self.subTest(scale=scale):
                height = height_for(scale)
                display = widget.build_display(stale_data, NOW, "light", scale, height)
                image = widget.render_display(display)
                left = widget.aside_left(scale)
                reset_right = widget.pixel_columns(scale)["reset"][1]
                mid = image.height // 2
                upper = ink_columns(image, left, image.width, 0, mid)
                lower = ink_columns(image, left, image.width, mid, image.height)
                gap = ink_columns(image, reset_right, left, 0, image.height)
                self.assertTrue(upper)
                self.assertEqual(lower, [])
                self.assertLess(max(upper), display.width - 1)
                self.assertEqual(gap, [])

    def test_fresh_single_has_no_ink_past_the_reset_column(self):
        for scale in (1.0, 1.5):
            with self.subTest(scale=scale):
                height = height_for(scale)
                display = widget.build_display(fresh_snapshot(), NOW, "light", scale, height)
                image = widget.render_display(display)
                reset_right = widget.pixel_columns(scale)["reset"][1]
                self.assertEqual(ink_columns(image, reset_right, image.width, 0, image.height), [])
                self.assertEqual(
                    image.width, widget.scaled(widget.logical_columns()[1], scale))


class CaptionTextTests(unittest.TestCase):
    def test_missing_before_with_an_after_is_new(self):
        self.assertEqual(widget.caption_text(None, at(9, 5), ""), "new 09:05")

    def test_clock_follows_after_whether_newer_same_or_older(self):
        stamp = at(14, 58)
        self.assertEqual(widget.caption_text(stamp - 60.0, stamp, ""), "new 14:58")
        self.assertEqual(widget.caption_text(stamp, stamp, ""), "same 14:58")
        self.assertEqual(widget.caption_text(stamp + 60.0, stamp, ""), "same 14:58")

    def test_missing_after_is_no_data(self):
        self.assertEqual(widget.caption_text(None, None, ""), "no data")
        self.assertEqual(widget.caption_text(at(9, 5), None, ""), "no data")

    def test_read_failure_wins_over_before_and_after(self):
        reasons = (
            "bad_json", "bad_schema", "too_large", "not_object", "bad_windows",
            "read_error:PermissionError", "stat_error:PermissionError",
        )
        pairs = ((None, None), (at(9, 5), None), (at(9, 5), at(14, 58)), (None, at(9, 5)))
        for reason in reasons:
            for before, after in pairs:
                with self.subTest(reason=reason, before=before, after=after):
                    self.assertEqual(widget.caption_text(before, after, reason), "read error")

    def test_benign_reasons_follow_before_and_after(self):
        self.assertEqual(
            widget.CAPTION_BENIGN_REASONS,
            frozenset({"missing", "no_codex", "no_rate_limits"}))
        self.assertEqual(widget.caption_text(None, at(9, 5), ""), "new 09:05")
        self.assertEqual(widget.caption_text(None, at(9, 5), "missing"), "new 09:05")
        self.assertEqual(widget.caption_text(None, None, "no_codex"), "no data")
        self.assertEqual(widget.caption_text(None, None, "no_rate_limits"), "no data")
        self.assertEqual(widget.caption_text(at(9, 5), at(9, 5), "no_rate_limits"), "same 09:05")
        self.assertEqual(widget.caption_text(None, at(9, 5), "no_rate_limits"), "new 09:05")
        self.assertEqual(widget.CAPTION_READ_ERROR, "read error")

    def test_clock_text_is_zero_padded(self):
        self.assertEqual(widget.caption_text(None, at(0, 7), ""), "new 00:07")
        self.assertEqual(widget.caption_text(None, at(9, 5), ""), "new 09:05")
        evening = at(23, 59)
        self.assertEqual(widget.caption_text(evening, evening, ""), "same 23:59")

    def test_out_of_range_after_uses_a_placeholder_clock(self):
        self.assertEqual(widget.caption_text(None, 1e20, ""), "new --:--")

    def test_every_caption_is_short_ascii(self):
        befores = (None, at(9, 5))
        afters = (None, at(0, 7), at(23, 59), 1e20, at(9, 5))
        reasons = ("", "missing", "bad_json")
        seen = set()
        for before in befores:
            for after in afters:
                for reason in reasons:
                    text = widget.caption_text(before, after, reason)
                    seen.add(text)
                    with self.subTest(before=before, after=after, reason=reason):
                        self.assertLessEqual(len(text), 10)
                        self.assertTrue(text.isascii())
        self.assertIn("read error", seen)
        self.assertIn("no data", seen)
        self.assertTrue(any(text.startswith("new ") for text in seen))
        self.assertTrue(any(text.startswith("same ") for text in seen))


class FitTests(unittest.TestCase):
    def test_dual_caption_stays_inside_its_block(self):
        for scale in SCALES:
            for text in ("read error", "same 23:59"):
                with self.subTest(scale=scale, text=text):
                    height = height_for(scale)
                    display = widget.build_display(
                        fresh_snapshot(), NOW, "light", scale, height,
                        codex=fresh_snapshot(3, 36), codex_available=True,
                        captions=(text, text))
                    header_font = widget.get_font(
                        widget.dual_render_metrics(scale, height)["header_font_px"])
                    text_width = header_font.getlength(text)
                    blocks = widget.dual_pixel_columns(scale)
                    for edges in blocks:
                        center = (edges["bar"][0] + edges["bar"][1]) / 2.0
                        self.assertGreaterEqual(center - text_width / 2.0, edges["label"][0])
                        self.assertLessEqual(center + text_width / 2.0, edges["reset"][1])
                    image = widget.render_display(display)
                    header = widget.dual_render_metrics(scale, height)["header_height"]
                    intervals = [(edges["label"][0], edges["reset"][1]) for edges in blocks]
                    pixels = image.load()
                    for y in range(header):
                        for x in range(image.width):
                            if pixels[x, y][3] <= widget.BACKGROUND_ALPHA:
                                continue
                            inside = any(lo <= x < hi for lo, hi in intervals)
                            self.assertTrue(inside, (scale, text, x, y))

    def test_single_caption_fits_past_the_reset_column(self):
        for scale in SCALES:
            for text in ("read error", "same 23:59"):
                with self.subTest(scale=scale, text=text):
                    height = height_for(scale)
                    display = widget.build_display(
                        fresh_snapshot(), NOW, "light", scale, height, captions=(text, ""))
                    font = widget.get_font(widget.render_metrics(scale, height)["font_px"])
                    self.assertGreaterEqual(
                        display.width,
                        widget.aside_left(scale) + math.ceil(font.getlength(text)))
                    image = widget.render_display(display)
                    ink = ink_columns(image, widget.aside_left(scale), image.width, 0, image.height)
                    self.assertTrue(ink)
                    self.assertLess(max(ink), display.width - 1)

    def test_caption_width_replaces_the_age_width(self):
        stale_data = snapshot(win(65, 31 * 60.0), win(33, 31 * 60.0, resets_in=2 * 86400.0))
        for scale in SCALES:
            with self.subTest(scale=scale):
                height = height_for(scale)
                display = widget.build_display(
                    stale_data, NOW, "light", scale, height, captions=("read error", ""))
                error_width = widget.display_width("data", scale, height, "read error")
                age_width = widget.display_width("data", scale, height, "31m")
                self.assertEqual(display.width, error_width)
                if error_width != age_width:
                    self.assertNotEqual(display.width, age_width)


class NodataCaptionTests(unittest.TestCase):
    def test_plain_nodata_is_untouched(self):
        for theme in ("light", "dark"):
            for scale in SCALES:
                with self.subTest(theme=theme, scale=scale):
                    height = height_for(scale)
                    plain = widget.build_display(None, NOW, theme, scale, height)
                    self.assertEqual(widget.nodata_lines(plain), widget.NODATA_LINES)
                    old = widget.Display(
                        "nodata", widget.NODATA_LINES, theme, scale, plain.width, plain.height)
                    self.assertEqual(
                        widget.render_display(plain).tobytes(),
                        widget.render_display(old).tobytes())
                    self.assertEqual(
                        plain.width, widget.display_width("nodata", scale, plain.height))

    def test_caption_replaces_only_the_second_line(self):
        for text in ("no data", "read error"):
            for theme in ("light", "dark"):
                for scale in SCALES:
                    with self.subTest(text=text, theme=theme, scale=scale):
                        height = height_for(scale)
                        plain = widget.build_display(None, NOW, theme, scale, height)
                        captioned = widget.build_display(
                            None, NOW, theme, scale, height, captions=(text, ""))
                        self.assertEqual(
                            widget.nodata_lines(captioned), (widget.NODATA_LINES[0], text))
                        self.assertEqual(captioned.width, plain.width)
                        half = height // 2
                        plain_image = widget.render_display(plain)
                        caption_image = widget.render_display(captioned)
                        self.assertEqual(
                            plain_image.crop((0, 0, plain.width, half)).tobytes(),
                            caption_image.crop((0, 0, captioned.width, half)).tobytes())
                        self.assertNotEqual(
                            plain_image.crop((0, half, plain.width, height)).tobytes(),
                            caption_image.crop((0, half, captioned.width, height)).tobytes())

    def test_caption_ink_stays_inside_the_window(self):
        for text in ("no data", "read error"):
            for theme in ("light", "dark"):
                for scale in SCALES:
                    with self.subTest(text=text, theme=theme, scale=scale):
                        height = height_for(scale)
                        captioned = widget.build_display(
                            None, NOW, theme, scale, height, captions=(text, ""))
                        image = widget.render_display(captioned)
                        half = height // 2
                        ink = ink_columns(image, 0, image.width, half, height)
                        self.assertTrue(ink)
                        self.assertLess(max(ink), captioned.width - 1)
                        self.assertGreaterEqual(min(ink), widget.scaled(widget.MARGIN_L, scale))
                        self.assertEqual(
                            max_text_alpha(
                                image, (0, half, image.width, height), widget.TEXT_RGB[theme]),
                            widget.TEXT_ALPHA)

    def test_width_never_shrinks_and_follows_the_widest_text(self):
        long_text = "same 23:59 plus a deliberately long tail"
        for text in ("no data", "read error"):
            for scale in SCALES:
                with self.subTest(text=text, scale=scale):
                    height = height_for(scale)
                    plain = widget.build_display(None, NOW, "light", scale, height)
                    captioned = widget.build_display(
                        None, NOW, "light", scale, height, captions=(text, ""))
                    self.assertGreaterEqual(captioned.width, plain.width)
                    self.assertEqual(
                        captioned.width,
                        widget.display_width("nodata", scale, height, text))
                    self.assertGreater(
                        widget.display_width("nodata", scale, height, long_text),
                        widget.display_width("nodata", scale, height))
                    wide = widget.build_display(
                        None, NOW, "light", scale, height, captions=(long_text, ""))
                    image = widget.render_display(wide)
                    ink = ink_columns(image, 0, image.width, height // 2, height)
                    self.assertTrue(ink)
                    self.assertLess(max(ink), wide.width - 1)

    def test_dual_nodata_picks_one_caption_and_renders_it(self):
        display = widget.build_display(
            None, NOW, "light", 1.0, 40,
            codex=None, codex_available=True, captions=("no data", "read error"))
        self.assertEqual(display.kind, "nodata")
        self.assertEqual(display.captions, ("read error",))
        self.assertEqual(widget.nodata_lines(display)[1], "read error")


class SampleTests(unittest.TestCase):
    def test_all_sample_states_keep_the_original_prefix(self):
        self.assertEqual(
            widget.ALL_SAMPLE_STATES[:len(widget.SAMPLE_STATES)], widget.SAMPLE_STATES)
        self.assertEqual(
            widget.ALL_SAMPLE_STATES[len(widget.SAMPLE_STATES):], widget.EXTRA_SAMPLE_STATES)
        self.assertEqual(len(widget.ALL_SAMPLE_STATES), len(set(widget.ALL_SAMPLE_STATES)))

    def test_extra_samples_start_at_index_12(self):
        numbered = list(enumerate(widget.ALL_SAMPLE_STATES, 1))
        self.assertEqual(numbered[11], (12, "stale_hours"))
        self.assertEqual(
            widget.sample_filename(12, "stale_hours", "light", 1.0),
            "12_stale_hours_light_1.00.png")

    def test_extra_samples_render_at_every_combo(self):
        for name in widget.EXTRA_SAMPLE_STATES:
            for theme, scale in widget.SAMPLE_COMBOS:
                with self.subTest(name=name, theme=theme, scale=scale):
                    display = widget.sample_display(name, theme, scale)
                    image = widget.render_display(display)
                    self.assertEqual(image.size, (display.width, display.height))

    def test_extra_sample_text_matches_the_fixed_clock(self):
        expected_ages = {
            "stale_hours": ("3h",),
            "stale_one_row": ("41m",),
            "dual_stale_both": ("41m", "3h"),
            "dual_stale_claude": ("41m", ""),
        }
        expected_captions = {
            "caption_new": ("new 14:59",),
            "caption_same": ("same 14:29",),
            "caption_error": ("read error",),
            "dual_caption_new_same": ("new 14:59", "same 14:29"),
            "dual_caption_nodata_error": ("no data", "read error"),
            "caption_nodata": ("no data",),
            "caption_nodata_error": ("read error",),
            "dual_caption_all_nodata": ("read error",),
        }
        for name, ages in expected_ages.items():
            with self.subTest(name=name):
                self.assertEqual(widget.sample_display(name, "light", 1.0).ages, ages)
        for name, captions in expected_captions.items():
            with self.subTest(name=name):
                display = widget.sample_display(name, "light", 1.0)
                self.assertEqual(display.captions, captions)
        same = widget.sample_display("caption_same", "light", 1.0)
        self.assertEqual(same.ages, ("31m",))
        empty = widget.sample_display("dual_caption_nodata_error", "light", 1.0)
        self.assertEqual(empty.kind, "dual")
        self.assertEqual(tuple(row.state for row in empty.rows[0].rows), ("unknown", "unknown"))
        baseline = widget.sample_display("nodata", "light", 1.0)
        for name in ("caption_nodata", "caption_nodata_error", "dual_caption_all_nodata"):
            with self.subTest(name=name):
                display = widget.sample_display(name, "light", 1.0)
                self.assertEqual(display.kind, "nodata")
                self.assertEqual(display.ages, ())
                self.assertGreaterEqual(display.width, baseline.width)

    def test_new_nodata_samples_are_numbered_after_the_old_extras(self):
        self.assertEqual(widget.ALL_SAMPLE_STATES.index("caption_nodata") + 1, 21)
        self.assertEqual(widget.ALL_SAMPLE_STATES.index("caption_nodata_error") + 1, 22)
        self.assertEqual(widget.ALL_SAMPLE_STATES.index("dual_caption_all_nodata") + 1, 23)
        self.assertEqual(
            widget.sample_filename(21, "caption_nodata", "light", 1.0),
            "21_caption_nodata_light_1.00.png")


class ContactSheetTests(unittest.TestCase):
    def test_label_column_fits_the_longest_state_name(self):
        font = widget.get_font(13)
        pad = 10
        gap = 10
        names = widget.ALL_SAMPLE_STATES
        width = widget.contact_sheet_label_width(names, pad, gap)
        longest = max(font.getlength(name) for name in names)
        for background_name, _rgb in widget.SHEET_BACKGROUNDS:
            room = (width - pad - font.getlength(background_name)) - (pad + longest)
            self.assertGreaterEqual(room, gap)
        self.assertEqual(widget.contact_sheet_label_width(("a",)), 170)

    def test_compose_contact_sheet_width_follows_the_label_column(self):
        images = [Image.new("RGBA", (10, 5), (0, 0, 0, 0)) for _ in widget.SAMPLE_COMBOS]
        sheet = widget.compose_contact_sheet([("dual_caption_nodata_error", images)])
        label = widget.contact_sheet_label_width(["dual_caption_nodata_error"], 10)
        expected = label + 10 * len(images) + 10 * (len(images) + 1)
        self.assertEqual(sheet.width, expected)


APP_NOW = at(15, 0)
OBS_OLD = at(14, 0)
OBS_NEW = at(14, 58)


class FakeReader:
    """Stands in for SnapshotReader and CodexReader: refresh() swaps in the queued state."""

    def __init__(self, snap=None, reason="", available=True):
        self.snapshot = snap
        self.last_reason = reason
        self.available = available
        self.queued = None
        self.calls = []

    def refresh(self, force=False):
        self.calls.append(force)
        if self.queued is not None:
            self.snapshot, self.last_reason = self.queued
            self.queued = None
        return True


def data_at(observed):
    """A snapshot whose two windows were both observed at this time and have not reset."""
    return {
        "five_hour": widget.Win(30.0, APP_NOW + 7200.0, observed),
        "seven_day": widget.Win(10.0, APP_NOW + 2 * 86400.0, observed),
    }


def caption_app():
    """App whose Claude reader holds OBS_OLD data and will read OBS_NEW data on the next refresh."""
    app = make_app()
    app.reader = FakeReader(data_at(OBS_OLD))
    app.reader.queued = (data_at(OBS_NEW), "")
    return app


def click_refresh(app, now=APP_NOW):
    app.w32.track_menu.return_value = widget.MENU_REFRESH
    with patch.object(widget.time, "time", return_value=now):
        app.on_right_up()


def first_display(app):
    return app._apply.call_args_list[0][0][0]


def live_apply(app):
    """Use the real _apply so shown_display and update_layered are exercised."""
    app._apply = widget.WidgetApp._apply.__get__(app, widget.WidgetApp)


class RefreshCaptionFlowTests(unittest.TestCase):
    def test_first_redraw_already_carries_the_caption(self):
        app = caption_app()
        click_refresh(app)
        self.assertEqual(app._apply.call_count, 2)
        for index in (0, 1):
            display = app._apply.call_args_list[index][0][0]
            self.assertEqual(display.captions, ("new 14:58",))
        self.assertTrue(app._flashing)
        self.assertEqual(app._caption, (APP_NOW + widget.CAPTION_MS / 1000.0, ("new 14:58", "")))
        self.assertIsNone(app._caption_before)
        self.assertIn(widget.TIMER_CAPTION, app.timers)
        self.assertEqual(app.w32.set_timer.call_args_list, [
            call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
            call(10, widget.TIMER_FLASH, widget.FLASH_MS),
        ])
        self.assertEqual(app.reader.calls, [True])

    def test_second_click_resets_the_deadline_and_reports_same(self):
        app = caption_app()
        click_refresh(app)
        click_refresh(app, APP_NOW + 1.0)
        self.assertEqual(
            app._caption, (APP_NOW + 1.0 + widget.CAPTION_MS / 1000.0, ("same 14:58", "")))
        caption_arms = [
            item for item in app.w32.set_timer.call_args_list
            if item.args[1] == widget.TIMER_CAPTION]
        self.assertEqual(len(caption_arms), 2)
        self.assertEqual(caption_arms[0], caption_arms[1])
        self.assertEqual(caption_arms[0], call(10, widget.TIMER_CAPTION, widget.CAPTION_MS))

    def test_unchanged_data_and_failed_read_and_missing_file(self):
        cases = (
            ("same", None, "same 14:00", None),
            ("bad_json", (data_at(OBS_OLD), "bad_json"), "read error", None),
            ("missing", (None, "missing"), "no data", "nodata"),
        )
        for name, queued, text, kind in cases:
            with self.subTest(name=name):
                app = caption_app()
                if name == "missing":
                    app.reader.snapshot = None
                app.reader.queued = queued
                click_refresh(app)
                self.assertEqual(app._caption[1][0], text)
                if kind == "nodata":
                    display = first_display(app)
                    self.assertEqual(display.kind, "nodata")
                    self.assertEqual(display.captions, ("no data",))

    def test_data_read_while_the_menu_is_open_still_counts_as_new(self):
        app = make_app()
        app.reader = FakeReader(data_at(OBS_OLD))
        during_menu = []

        def menu(hwnd, x, y):
            # The modal menu loop still dispatches WM_TIMER: TIMER_POLL reads the
            # new file into the reader, and menu_open blocks the redraw.
            app.reader.queued = (data_at(OBS_NEW), "")
            app.on_timer(widget.TIMER_POLL)
            during_menu.append((app._apply.call_count, app.reader.snapshot))
            return widget.MENU_REFRESH

        app.w32.track_menu.side_effect = menu
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_right_up()
        self.assertEqual(during_menu, [(0, data_at(OBS_NEW))])
        self.assertEqual(app.reader.calls, [False, True])
        self.assertEqual(app._caption[1][0], "new 14:58")
        self.assertEqual(first_display(app).captions, ("new 14:58",))

    def test_explicit_before_wins_over_a_fresh_observation(self):
        app = caption_app()
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._refresh_now((OBS_NEW, None))
        self.assertEqual(app._caption[1][0], "same 14:58")

    def test_refresh_now_without_before_observes_at_call_time(self):
        app = caption_app()
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._refresh_now()
        self.assertEqual(app._caption[1][0], "new 14:58")

    def test_caption_before_is_cleared_before_the_redraw_runs(self):
        app = caption_app()
        seen = []
        original = app.refresh_view

        def record(*args, **kwargs):
            seen.append((app._caption_before, app._caption is not None))
            return original(*args, **kwargs)

        app.refresh_view = record
        click_refresh(app)
        self.assertTrue(seen)
        self.assertTrue(all(item[0] is None for item in seen))
        self.assertTrue(seen[0][1])

    def test_plain_poll_arms_nothing(self):
        app = caption_app()
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.poll_snapshot()
        self.assertIsNone(app._caption)
        app.w32.set_timer.assert_not_called()
        self.assertEqual(app.reader.calls, [False])

    def test_caption_before_is_cleared_when_polling_raises(self):
        app = caption_app()
        app.poll_snapshot = Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            app._refresh_now()
        self.assertIsNone(app._caption_before)
        self.assertFalse(app._flashing)

    def test_dual_layout_gets_one_text_per_provider(self):
        app = caption_app()
        app.codex = FakeReader(data_at(OBS_OLD))
        app.codex.queued = (data_at(OBS_OLD), "")
        click_refresh(app)
        self.assertEqual(app._caption[1], ("new 14:58", "same 14:00"))
        display = first_display(app)
        self.assertEqual(display.kind, "dual")
        self.assertEqual(widget.dual_header(display, 0)[0], "new 14:58")
        self.assertEqual(widget.dual_header(display, 1)[0], "same 14:00")

    def test_disabled_or_unavailable_codex_adds_no_second_caption(self):
        app = caption_app()
        self.assertIsNone(app.codex)
        click_refresh(app)
        self.assertEqual(app._caption[1], ("new 14:58", ""))
        self.assertEqual(first_display(app).captions, ("new 14:58",))

        app = caption_app()
        app.codex = FakeReader(data_at(OBS_OLD), available=False)
        click_refresh(app)
        display = first_display(app)
        self.assertEqual(display.kind, "data")
        self.assertEqual(display.captions, ("new 14:58",))


class CaptionLifecycleTests(unittest.TestCase):
    def test_text_reaches_build_display_only_before_the_deadline(self):
        app = make_app()
        app.reader.snapshot = data_at(OBS_NEW)
        app._caption = (APP_NOW + 2.5, ("new 14:58", ""))
        with patch.object(widget.time, "time", return_value=APP_NOW + 2.4):
            app._refresh_once(False, False)
        display = app._apply.call_args[0][0]
        self.assertEqual(display.captions, ("new 14:58",))
        self.assertEqual(app._caption, (APP_NOW + 2.5, ("new 14:58", "")))
        with patch.object(widget.time, "time", return_value=APP_NOW + 2.5):
            app._refresh_once(False, False)
        display = app._apply.call_args[0][0]
        self.assertEqual(display.captions, ())
        self.assertIsNone(app._caption)

    def test_caption_timer_erases_even_when_it_fires_early(self):
        app = make_app()
        app._caption = (APP_NOW + 100.0, ("new 14:58", ""))
        app.timers.add(widget.TIMER_CAPTION)
        app.refresh_view = Mock()
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_CAPTION)
        self.assertIsNone(app._caption)
        self.assertNotIn(widget.TIMER_CAPTION, app.timers)
        app.w32.kill_timer.assert_called_once_with(10, widget.TIMER_CAPTION)
        app.refresh_view.assert_called_once_with()

    def test_caption_timer_is_ignored_while_exiting(self):
        app = make_app()
        app._caption = (APP_NOW + 100.0, ("new 14:58", ""))
        app.exiting = True
        app.refresh_view = Mock()
        app.on_timer(widget.TIMER_CAPTION)
        app.refresh_view.assert_not_called()
        self.assertEqual(app._caption, (APP_NOW + 100.0, ("new 14:58", "")))

    def test_failed_caption_timer_is_cleaned_up_by_the_two_second_check(self):
        app = caption_app()
        live_apply(app)
        app.w32.set_timer.return_value = False
        click_refresh(app)
        self.assertNotIn(widget.TIMER_CAPTION, app.timers)
        self.assertEqual(app.shown_display.captions, ("new 14:58",))
        with patch.object(widget.time, "time", return_value=APP_NOW + 1.0):
            app.on_timer(widget.TIMER_CHECK)
        self.assertEqual(app.shown_display.captions, ("new 14:58",))
        app.w32.update_layered.reset_mock()
        with patch.object(widget.time, "time", return_value=APP_NOW + 2.6):
            app.on_timer(widget.TIMER_CHECK)
        self.assertEqual(app.shown_display.captions, ())
        self.assertIsNone(app._caption)
        app.w32.update_layered.assert_called_once()

    def test_blocked_erase_is_picked_up_by_the_next_refresh(self):
        app = caption_app()
        live_apply(app)
        click_refresh(app)
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_FLASH)
        app.menu_open = True
        app.w32.update_layered.reset_mock()
        with patch.object(widget.time, "time", return_value=APP_NOW + 3.0):
            app.on_timer(widget.TIMER_CAPTION)
        self.assertIsNone(app._caption)
        app.w32.update_layered.assert_not_called()
        self.assertEqual(app.shown_display.captions, ("new 14:58",))
        app.menu_open = False
        with patch.object(widget.time, "time", return_value=APP_NOW + 3.0):
            app.refresh_view()
        self.assertEqual(app.shown_display.captions, ())
        app.w32.update_layered.assert_called_once()

    def test_recreate_and_exit_clear_the_caption(self):
        stored = (APP_NOW + 100.0, ("x", ""))

        app = make_app()
        app._caption = stored
        app.timers.add(widget.TIMER_CAPTION)
        app.w32.is_window.return_value = False
        app.w32.create_window.return_value = 11
        app.refresh_view = Mock()
        app._recreate_window("owner disappeared")
        self.assertIsNone(app._caption)
        caption_arms = [
            item for item in app.w32.set_timer.call_args_list
            if item.args[1] == widget.TIMER_CAPTION]
        self.assertEqual(caption_arms, [])
        app.w32.kill_timer.assert_any_call(10, widget.TIMER_CAPTION)

        app = make_app()
        app.hwnd = 0
        app._caption = stored
        app.w32.create_window.return_value = 12
        app.refresh_view = Mock()
        app.on_thread_message(widget.WM_APP_RECREATE)
        self.assertIsNone(app._caption)
        caption_arms = [
            item for item in app.w32.set_timer.call_args_list
            if item.args[1] == widget.TIMER_CAPTION]
        self.assertEqual(caption_arms, [])

        app = make_app()
        app._caption = stored
        app.timers.add(widget.TIMER_CAPTION)
        app.w32.destroy_window.return_value = True
        app._arm_watchdog = Mock()
        app.request_exit()
        self.assertIsNone(app._caption)
        app.w32.set_timer.assert_not_called()
        app.w32.kill_timer.assert_any_call(10, widget.TIMER_CAPTION)

    def test_kill_all_timers_includes_the_caption_timer(self):
        app = make_app()
        self.assertTrue(app.set_timer(widget.TIMER_CAPTION, widget.CAPTION_MS))
        self.assertIn(widget.TIMER_CAPTION, app.timers)
        app._kill_all_timers()
        self.assertEqual(app.timers, set())
        app.w32.kill_timer.assert_any_call(10, widget.TIMER_CAPTION)

    def test_start_timers_never_arms_the_caption_timer(self):
        app = make_app()
        app._start_timers(recreated=True)
        self.assertEqual(
            app.timers, {widget.TIMER_CHECK, widget.TIMER_POLL, widget.TIMER_THEME})
        app._start_timers()
        self.assertNotIn(widget.TIMER_CAPTION, app.timers)

    def test_nodata_caption_redraws_when_it_appears_and_when_it_expires(self):
        app = make_app()
        app.reader = FakeReader(None, "missing")
        live_apply(app)
        click_refresh(app)
        self.assertEqual(app.shown_display.kind, "nodata")
        self.assertEqual(app.shown_display.captions, ("no data",))
        with patch.object(widget.time, "time", return_value=APP_NOW + 0.1):
            app.on_timer(widget.TIMER_FLASH)
        self.assertEqual(app.shown_display.captions, ("no data",))
        app.w32.update_layered.reset_mock()
        with patch.object(widget.time, "time", return_value=APP_NOW + 3.0):
            app.on_timer(widget.TIMER_CAPTION)
        self.assertEqual(app.shown_display.captions, ())
        self.assertIsNone(app._caption)
        app.w32.update_layered.assert_called_once()

    def test_failed_read_on_a_never_loaded_source_shows_read_error(self):
        app = make_app()
        app.reader = FakeReader(None, "bad_json")
        live_apply(app)
        click_refresh(app)
        self.assertEqual(app.shown_display.kind, "nodata")
        self.assertEqual(app.shown_display.captions, ("read error",))

    def test_dual_without_any_window_reports_no_data_for_an_empty_codex_log(self):
        app = make_app()
        app.reader = FakeReader(None, "missing")
        app.codex = FakeReader(None, "no_rate_limits")
        live_apply(app)
        click_refresh(app)
        self.assertEqual(app.shown_display.kind, "nodata")
        self.assertEqual(app.shown_display.captions, ("no data",))
