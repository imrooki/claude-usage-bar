"""Side-by-side Claude and Codex display. Codex absent keeps the single-provider widget."""

import os
import pathlib
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

OLD_STATES = ("nodata", "green", "amber", "red", "full", "stale", "reset")
DUAL_STATES = ("dual_green", "dual_mixed", "dual_codex_reset", "dual_claude_none")
SAMPLE_NOW = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))


def block_logical_width():
    """一个提供方块的逻辑宽度：与单栏列相同，名称在进度条上方。"""
    return (
        widget.LABEL_W + widget.GAP_LABEL_BAR + widget.BAR_W + widget.GAP_BAR_PCT
        + widget.PCT_W + widget.GAP_PCT_RESET + widget.RESET_W
    )


def dual_logical_sum():
    block = block_logical_width()
    return widget.MARGIN_L + block + widget.BLOCK_GAP + block + widget.MARGIN_R


def metric_rows(snapshot, now):
    five = snapshot.get("five_hour") if snapshot else None
    seven = snapshot.get("seven_day") if snapshot else None
    return (
        widget.build_row("5h", five, now, False),
        widget.build_row("7d", seven, now, True),
    )


def make_app(argv=None):
    w32 = Mock()
    w32.taskbar_hwnd.return_value = 0
    w32.taskbar_pos.return_value = ((0, 100, 1000, 140), widget.ABE_BOTTOM)
    w32.taskbar_autohide.return_value = False
    w32.notification_state.return_value = 5
    w32.foreground_covers_taskbar.return_value = False
    w32.scale_for.return_value = 1.0
    w32.tray_notify_rect.return_value = None
    w32.foreground_hwnd.return_value = 30
    w32.class_name.return_value = "Shell_TrayWnd"
    w32.desktop_hwnd.return_value = 40
    w32.taskbar_above.return_value = True
    w32.topmost_allowed.return_value = True
    w32.post_z_order_check.return_value = True
    opts = widget.parse_args([] if argv is None else argv)
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(opts, w32, Mock())
    app.hwnd = 10
    app.visible = True
    app._apply = Mock()
    return app


class DualDisplayTests(unittest.TestCase):
    def test_single_provider_matches_previous_call(self):
        ignored = {
            "five_hour": widget.Win(99.0, SAMPLE_NOW + 3600.0, SAMPLE_NOW),
            "seven_day": widget.Win(88.0, SAMPLE_NOW + 86400.0, SAMPLE_NOW),
        }
        for name in OLD_STATES:
            snapshot = widget._sample_snapshot(name, SAMPLE_NOW)
            for theme, scale in widget.SAMPLE_COMBOS:
                with self.subTest(name=name, theme=theme, scale=scale):
                    height = widget.scaled(widget.LOGICAL_H, scale)
                    previous = widget.build_display(snapshot, SAMPLE_NOW, theme, scale, height)
                    current = widget.build_display(
                        snapshot, SAMPLE_NOW, theme, scale, height,
                        codex=ignored, codex_available=False)
                    self.assertEqual(previous, current)
                    self.assertIn(previous.kind, ("data", "nodata"))

    def test_existing_sample_names_stay_in_front(self):
        self.assertEqual(widget.SAMPLE_STATES[:len(OLD_STATES)], OLD_STATES)
        self.assertEqual(
            widget.SAMPLE_STATES[len(OLD_STATES):],
            ("dual_green", "dual_mixed", "dual_codex_reset", "dual_claude_none"))

    def test_single_columns_ignore_dual_widths(self):
        columns, _total = widget.logical_columns()
        self.assertEqual(set(columns), {"label", "bar", "pct", "reset"})
        bar_left = widget.MARGIN_L + widget.LABEL_W + widget.GAP_LABEL_BAR
        self.assertEqual(columns["bar"], (bar_left, bar_left + widget.BAR_W))
        pixels = widget.pixel_columns(1.25)
        self.assertEqual(
            pixels["bar"],
            (widget.scaled(columns["bar"][0], 1.25), widget.scaled(columns["bar"][1], 1.25)))

    def test_dual_blocks_order_tags_and_rows(self):
        for name in DUAL_STATES:
            with self.subTest(name=name):
                snapshot = widget._sample_snapshot(name, SAMPLE_NOW)
                codex = widget._sample_codex_snapshot(name, SAMPLE_NOW)
                height = widget.scaled(widget.LOGICAL_H, 1.0)
                display = widget.build_display(
                    snapshot, SAMPLE_NOW, "dark", 1.0, height,
                    codex=codex, codex_available=True)
                self.assertEqual(display.kind, "dual")
                self.assertEqual(tuple(block.tag for block in display.rows), widget.PROVIDER_TAGS)
                self.assertEqual(display.rows[0].tag, "Claude")
                self.assertEqual(display.rows[1].tag, "Codex")
                self.assertEqual(display.rows[0].rows, metric_rows(snapshot, SAMPLE_NOW))
                self.assertEqual(display.rows[1].rows, metric_rows(codex, SAMPLE_NOW))
                self.assertIsInstance(display.rows[0], widget.BlockView)

    def test_dual_sample_row_states(self):
        height = widget.scaled(widget.LOGICAL_H, 1.0)

        green = widget.sample_display("dual_green", "light", 1.0)
        self.assertEqual(green.rows[0].rows[0].pct_text, "30%")
        self.assertEqual(green.rows[0].rows[1].pct_text, "10%")
        self.assertEqual(green.rows[1].rows[0].pct_text, "3%")
        self.assertEqual(green.rows[1].rows[1].pct_text, "36%")
        self.assertTrue(all(row.state == "normal" for block in green.rows for row in block.rows))

        mixed = widget.build_display(
            widget._sample_snapshot("dual_mixed", SAMPLE_NOW), SAMPLE_NOW, "light", 1.0, height,
            codex=widget._sample_codex_snapshot("dual_mixed", SAMPLE_NOW), codex_available=True)
        self.assertEqual(mixed.rows[0].rows[0].state, "normal")
        self.assertEqual(mixed.rows[0].rows[0].tier, "amber")
        self.assertEqual(tuple(row.state for row in mixed.rows[1].rows), ("stale", "stale"))

        reset = widget.build_display(
            widget._sample_snapshot("dual_codex_reset", SAMPLE_NOW), SAMPLE_NOW, "light", 1.0, height,
            codex=widget._sample_codex_snapshot("dual_codex_reset", SAMPLE_NOW), codex_available=True)
        self.assertEqual(reset.rows[1].rows[0].state, "reset")
        self.assertEqual(reset.rows[1].rows[0].pct_text, "--")
        self.assertEqual(reset.rows[1].rows[0].reset_text, "reset")
        self.assertEqual(reset.rows[1].rows[1].state, "normal")

        empty = widget.build_display(
            None, SAMPLE_NOW, "light", 1.0, height,
            codex=widget._sample_codex_snapshot("dual_claude_none", SAMPLE_NOW), codex_available=True)
        self.assertEqual(empty.kind, "dual")
        self.assertEqual(tuple(row.state for row in empty.rows[0].rows), ("unknown", "unknown"))
        self.assertEqual(empty.rows[0].rows[0].pct_text, "--")
        self.assertEqual(empty.rows[0].rows[0].reset_text, "")
        self.assertEqual(empty.rows[1].rows[0].pct_text, "30%")
        self.assertEqual(empty.rows[1].rows[0].state, "normal")

    def test_nodata_when_both_sides_empty(self):
        height = 40
        previous = widget.build_display(None, SAMPLE_NOW, "dark", 1.0, height)
        for claude, codex in (
            (None, None),
            (None, {"five_hour": None, "seven_day": None}),
            ({"five_hour": None, "seven_day": None}, None),
            ({"five_hour": None, "seven_day": None}, {"five_hour": None, "seven_day": None}),
        ):
            with self.subTest(claude=claude, codex=codex):
                display = widget.build_display(
                    claude, SAMPLE_NOW, "dark", 1.0, height,
                    codex=codex, codex_available=True)
                self.assertEqual(display, previous)
                self.assertEqual(display.kind, "nodata")

    def test_one_sided_data_still_dual(self):
        height = 40
        only_claude = {"five_hour": widget.Win(30.0, SAMPLE_NOW + 3600.0, SAMPLE_NOW), "seven_day": None}
        display = widget.build_display(
            only_claude, SAMPLE_NOW, "dark", 1.0, height,
            codex=None, codex_available=True)
        self.assertEqual(display.kind, "dual")
        self.assertEqual(display.rows[0].rows[0].state, "normal")
        self.assertEqual(display.rows[1].rows[0].state, "unknown")
        self.assertEqual(display.rows[1].rows[1].state, "unknown")

    def test_dual_width_matches_logical_sum(self):
        logical = dual_logical_sum()
        self.assertEqual(logical, 432)
        self.assertEqual(widget.dual_logical_width(), 432)
        snapshot = widget._sample_snapshot("dual_green", SAMPLE_NOW)
        codex = widget._sample_codex_snapshot("dual_green", SAMPLE_NOW)
        for scale in (1.0, 1.25, 1.5):
            with self.subTest(scale=scale):
                height = widget.scaled(widget.LOGICAL_H, scale)
                display = widget.build_display(
                    snapshot, SAMPLE_NOW, "light", scale, height,
                    codex=codex, codex_available=True)
                expected = logical if scale == 1.0 else widget.scaled(logical, scale)
                self.assertEqual(expected, widget.scaled(logical, scale))
                if scale == 1.0:
                    self.assertEqual(display.width, 432)
                self.assertEqual(display.width, expected)
                self.assertEqual(display.width, widget.display_width("dual", scale, height))
                columns, block_width = widget.dual_block_columns()
                self.assertEqual(block_width, block_logical_width())
                origin = widget.MARGIN_L
                for edges in widget.dual_pixel_columns(scale):
                    for name, (lo, hi) in columns.items():
                        self.assertEqual(
                            edges[name],
                            (widget.scaled(origin + lo, scale), widget.scaled(origin + hi, scale)))
                    origin += block_width + widget.BLOCK_GAP
                self.assertLessEqual(widget.dual_pixel_columns(scale)[1]["reset"][1], display.width)

    def test_render_new_states(self):
        for name in DUAL_STATES:
            for theme, scale in widget.SAMPLE_COMBOS:
                with self.subTest(name=name, theme=theme, scale=scale):
                    display = widget.sample_display(name, theme, scale)
                    image = widget.render_display(display)
                    self.assertEqual(display.kind, "dual")
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, (display.width, display.height))
                    self.assertEqual(display.height, widget.scaled(widget.LOGICAL_H, scale))

    def test_stale_header_text_is_dimmer_than_a_normal_header(self):
        height = widget.scaled(widget.LOGICAL_H, 1.0)
        display = widget.build_display(
            widget._sample_snapshot("dual_mixed", SAMPLE_NOW), SAMPLE_NOW, "light", 1.0, height,
            codex=widget._sample_codex_snapshot("dual_mixed", SAMPLE_NOW), codex_available=True)
        image = widget.render_display(display)
        columns = widget.dual_pixel_columns(1.0)
        header_height = widget.dual_render_metrics(1.0, height)["header_height"]
        text = widget.TEXT_RGB["light"]

        def peak_alpha(edges):
            left, right = edges["bar"]
            band = image.crop((left, 0, right, header_height))
            best = 0
            pixels = band.load()
            for x in range(band.width):
                for y in range(band.height):
                    pixel = pixels[x, y]
                    if pixel[0] == text[0] and pixel[1] == text[1] and pixel[2] == text[2]:
                        best = max(best, pixel[3])
            return best

        normal_alpha = peak_alpha(columns[0])
        stale_alpha = peak_alpha(columns[1])
        self.assertGreater(normal_alpha, stale_alpha)
        self.assertGreater(stale_alpha, 0)


class OptionsAndPollTests(unittest.TestCase):
    def test_options_defaults_keep_four_argument_calls(self):
        opts = widget.Options(".", None, None, None)
        self.assertEqual(opts.data_dir, ".")
        self.assertIsNone(opts.exit_after)
        self.assertIsNone(opts.selftest_render)
        self.assertIsNone(opts.selftest_gdi)
        self.assertFalse(opts.no_codex)
        self.assertIsNone(opts.codex_home)

    def test_parse_args_codex_flags(self):
        flagged = widget.parse_args(["--no-codex"])
        self.assertTrue(flagged.no_codex)
        self.assertIsNone(flagged.codex_home)

        home = os.path.abspath(os.path.join("rel", "codex-home"))
        spaced = widget.parse_args(["--codex-home", home])
        self.assertFalse(spaced.no_codex)
        self.assertEqual(spaced.codex_home, home)

        inline = widget.parse_args(["--codex-home=%s" % home, "--no-codex"])
        self.assertTrue(inline.no_codex)
        self.assertEqual(inline.codex_home, home)

        missing = widget.parse_args(["--codex-home", "--exit-after", "2"])
        self.assertIsNone(missing.codex_home)
        self.assertEqual(missing.exit_after, 2.0)
        self.assertFalse(missing.no_codex)

        empty = widget.parse_args(["--codex-home="])
        self.assertIsNone(empty.codex_home)

    def test_poll_snapshot_refreshes_codex_and_survives_errors(self):
        app = make_app()
        app.reader = Mock()
        app.reader.last_reason = ""
        app.refresh_view = Mock()
        app.codex = Mock()
        app.codex.last_reason = "no_rate_limits"

        parent = Mock()
        parent.attach_mock(app.reader.refresh, "claude")
        parent.attach_mock(app.codex.refresh, "codex")
        parent.attach_mock(app.refresh_view, "view")
        app.poll_snapshot()
        self.assertEqual(
            [call[0] for call in parent.mock_calls],
            ["claude", "codex", "view"])
        app.reader.refresh.assert_called_once_with(force=False)
        app.codex.refresh.assert_called_once_with(force=False)
        app.log.log.assert_called_once_with("CodexRead", "no_rate_limits")
        app.refresh_view.assert_called_once()

        app.log.reset_mock()
        app.poll_snapshot()
        app.log.log.assert_not_called()

        app.log.reset_mock()
        app.codex.last_reason = ""
        app.poll_snapshot()
        app.codex.last_reason = "no_rate_limits"
        app.poll_snapshot()
        app.log.log.assert_called_once_with("CodexRead", "no_rate_limits")

        app.reader.refresh.reset_mock()
        app.codex.refresh.reset_mock()
        app.log.reset_mock()
        app.refresh_view.reset_mock()
        app.codex.last_reason = "no_codex"
        app.poll_snapshot(force=True)
        app.codex.refresh.assert_called_once_with(force=True)
        logged_kinds = [call.args[0] for call in app.log.log.call_args_list if call.args]
        self.assertNotIn("CodexRead", logged_kinds)
        app.refresh_view.assert_called_once()

        app.reader.refresh.reset_mock()
        app.refresh_view.reset_mock()
        app.codex.refresh.side_effect = RuntimeError("codex broke")
        app.poll_snapshot()
        app.reader.refresh.assert_called_once_with(force=False)
        app.refresh_view.assert_called_once()
        app.log.log_exception.assert_called()
        self.assertIsInstance(app.log.log_exception.call_args[0][0], RuntimeError)

    def test_no_codex_disables_reader_and_keeps_single_display(self):
        app = make_app(["--no-codex"])
        self.assertIsNone(app.codex)
        now = time.time()
        app.reader.refresh = Mock()
        app.reader.last_reason = ""
        app.reader.snapshot = {
            "five_hour": widget.Win(30.0, now + 3600.0, now),
            "seven_day": widget.Win(10.0, now + 86400.0, now),
        }
        app.refresh_view = Mock()
        app.poll_snapshot(force=True)
        app.reader.refresh.assert_called_once_with(force=True)
        app.refresh_view.assert_called_once()

        app._refresh_once(False, False)
        display = app._apply.call_args[0][0]
        self.assertEqual(display.kind, "data")
        self.assertEqual(display.rows[0].label, "5h")
        self.assertEqual(display.rows[0].pct_text, "30%")
        self.assertEqual(display.rows[1].label, "7d")
        self.assertEqual(display.width, widget.display_width("data", 1.0, display.height))

    def test_unavailable_codex_and_override_stay_single(self):
        app = make_app()
        now = time.time()
        claude = {
            "five_hour": widget.Win(30.0, now + 3600.0, now),
            "seven_day": widget.Win(10.0, now + 86400.0, now),
        }
        codex = {
            "five_hour": widget.Win(3.0, now + 3600.0, now),
            "seven_day": widget.Win(36.0, now + 86400.0, now),
        }
        app.reader.snapshot = claude
        app.codex.available = False
        app.codex.snapshot = codex
        app._refresh_once(False, False)
        hidden = app._apply.call_args[0][0]
        self.assertEqual(hidden.kind, "data")
        self.assertEqual(hidden.rows[0].pct_text, "30%")

        app.codex.available = True
        app._apply.reset_mock()
        app._refresh_once(False, False)
        shown = app._apply.call_args[0][0]
        self.assertEqual(shown.kind, "dual")
        self.assertEqual(tuple(block.tag for block in shown.rows), ("Claude", "Codex"))

        app.snapshot_override = claude
        app._apply.reset_mock()
        app._refresh_once(False, False)
        override = app._apply.call_args[0][0]
        self.assertEqual(override.kind, "data")
        self.assertEqual(override.rows[0].pct_text, "30%")


GEOMETRY_QUERIES = ("taskbar_pos", "taskbar_autohide", "notification_state", "scale_for", "tray_notify_rect")


def geometry_calls(app):
    return sum(getattr(app.w32, name).call_count for name in GEOMETRY_QUERIES)


class UnchangedReader:
    """SnapshotReader / CodexReader 的替身：refresh() 报告没有变化。"""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.last_reason = ""
        self.available = True

    def refresh(self, force=False):
        return False


def polling_app(snapshot=None, codex=True):
    app = make_app()
    app.reader = UnchangedReader(snapshot)
    app.codex = UnchangedReader() if codex else None
    # 与 _start_timers 一样，两秒一次的 TIMER_CHECK 在跑。
    app.set_timer(widget.TIMER_CHECK, widget.WINDOW_CHECK_MS)
    return app


class PollRedrawTests(unittest.TestCase):
    """TIMER_POLL 只在数据变了或要挂反馈文字时重绘；几何查询与随时间变化的文字交给 TIMER_CHECK。"""

    def test_unchanged_poll_skips_the_geometry_pass(self):
        app = polling_app()
        app.poll_snapshot()
        app.poll_snapshot(force=True)
        self.assertEqual(geometry_calls(app), 0)
        app._apply.assert_not_called()

    def test_a_changed_reader_still_redraws_at_once(self):
        for name in ("reader", "codex"):
            with self.subTest(reader=name):
                app = polling_app()
                getattr(app, name).refresh = Mock(return_value=True)
                app.poll_snapshot()
                self.assertGreater(geometry_calls(app), 0)
                app._apply.assert_called_once()

    def test_an_armed_caption_redraws_even_when_nothing_changed(self):
        app = polling_app(codex=False)
        app._caption_before = (None, None)  # what Refresh now leaves in place while it polls
        app.poll_snapshot()
        self.assertIsNotNone(app._caption)
        app._apply.assert_called_once()

    def test_without_the_check_timer_the_poll_still_redraws(self):
        app = polling_app()
        app.kill_timer(widget.TIMER_CHECK)
        app.poll_snapshot()
        app._apply.assert_called_once()

    def test_a_codex_error_still_redraws(self):
        app = polling_app()
        app.codex.refresh = Mock(side_effect=RuntimeError("codex broke"))
        app.poll_snapshot()
        app.log.log_exception.assert_called_once()
        app._apply.assert_called_once()

    def test_the_check_tick_rebuilds_time_driven_text_without_a_poll(self):
        observed = SAMPLE_NOW
        snapshot = {
            "five_hour": widget.Win(30.0, observed + 3600.0, observed),
            "seven_day": widget.Win(10.0, observed + 86400.0, observed),
        }
        app = polling_app(snapshot)
        app.poll_snapshot()
        app._apply.assert_not_called()
        later = observed + widget.STALE_SECONDS + 60.0
        with patch.object(widget.time, "time", return_value=later):
            app.on_timer(widget.TIMER_CHECK)
        self.assertGreater(geometry_calls(app), 0)
        app._apply.assert_called_once()
        self.assertEqual(app._apply.call_args[0][0].ages[0], widget.format_age(later - observed))


if __name__ == "__main__":
    unittest.main()
