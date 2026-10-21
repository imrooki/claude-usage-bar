"""Widget skins: usage levels, skin persistence, and the Progress Bar Skin menu."""

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget


NOW = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))


def win(pct, observed_ago=60.0, resets_in=3600.0):
    return widget.Win(float(pct), NOW + resets_in, NOW - observed_ago)


def snapshot(five, seven):
    return {"five_hour": five, "seven_day": seven}


def height_for(scale):
    return widget.scaled(widget.LOGICAL_H, scale)


def display_fills(display):
    return tuple(row.fill for row in display.rows)


def make_skin_app(data_dir, probe=None):
    """WidgetApp with a fake w32 and no data sources, so only skin code is on stage."""
    w32 = Mock()
    w32.cursor_pos.return_value = (4, 8)
    w32.set_timer.return_value = True
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False), \
            patch.object(widget, "load_style", return_value=widget.DEFAULT_STYLE):
        app = widget.WidgetApp(
            widget.Options(data_dir, None, None, None, no_codex=True), w32, Mock(), probe=probe)
    app.hwnd = 10
    app.visible = True
    app._apply = Mock()
    app.app_refresher = None
    return app


class LevelPaletteTests(unittest.TestCase):
    def test_levels_are_the_four_documented_steps(self):
        self.assertEqual(
            (widget.LEVEL_SAFE, widget.LEVEL_MODERATE,
             widget.LEVEL_WARNING, widget.LEVEL_CRITICAL),
            ("safe", "moderate", "warning", "critical"))

    def test_level_colours_match_the_specification(self):
        self.assertEqual(widget.LEVEL_RGB, {
            "safe": (0x6F, 0xA5, 0x81),
            "moderate": (0x74, 0xA1, 0xE8),
            "warning": (0xE8, 0xB8, 0x6D),
            "critical": (0xE9, 0x83, 0x86),
        })

    def test_every_level_has_a_distinct_colour(self):
        self.assertEqual(len(set(widget.LEVEL_RGB.values())), 4)
        self.assertNotIn(widget.LEVEL_NONE, widget.LEVEL_RGB)

    def test_skin_labels_and_order_are_frozen(self):
        self.assertEqual(widget.SKINS, ("neon", "mecha"))
        self.assertEqual(widget.DEFAULT_SKIN, "neon")
        self.assertEqual(
            widget.SKIN_LABELS, {"neon": "Neon Minimal", "mecha": "Mecha Tech"})


class FillGradientTests(unittest.TestCase):
    def test_anchor_values_are_the_documented_stops(self):
        self.assertEqual(widget.fill_rgb(0.0), (0xF2, 0x52, 0x5E))
        self.assertEqual(widget.fill_rgb(25.0), (0xAE, 0x57, 0xD2))
        self.assertEqual(widget.fill_rgb(50.0), (0x4D, 0x84, 0xF5))
        self.assertEqual(widget.fill_rgb(75.0), (0x28, 0xC5, 0xC8))
        self.assertEqual(widget.fill_rgb(100.0), (0x23, 0xD8, 0xA5))

    def test_values_clamp_to_the_ends_and_blend_between_stops(self):
        self.assertEqual(widget.fill_rgb(-5.0), widget.fill_rgb(0.0))
        self.assertEqual(widget.fill_rgb(250.0), widget.fill_rgb(100.0))
        blended = widget.fill_rgb(62.0)
        self.assertNotEqual(blended, widget.fill_rgb(50.0))
        self.assertNotEqual(blended, widget.fill_rgb(75.0))

    def test_remaining_reads_green_and_exhausted_reads_red(self):
        plenty = widget.fill_rgb(95.0)
        empty = widget.fill_rgb(5.0)
        self.assertGreater(plenty[1], plenty[0], plenty)
        self.assertGreater(empty[0], empty[1], empty)
        self.assertGreater(widget.fill_rgb(0.0)[0], widget.fill_rgb(0.0)[1] + 100)

    def test_ramp_never_passes_through_yellow_or_orange(self):
        for step in range(0, 101):
            rgb = widget.fill_rgb(float(step))
            orangeish = (rgb[0] > rgb[1] > rgb[2]
                         and rgb[0] - rgb[2] > 120 and rgb[1] - rgb[2] > 40)
            yellowish = rgb[0] > 150 and rgb[1] > 150 and rgb[2] < 120
            self.assertFalse(orangeish or yellowish, (step, rgb))

    def test_invalid_readings_have_no_colour(self):
        for value in (None, True, "33", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value):
                self.assertIsNone(widget.fill_rgb(value))


class SkinFileTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name

    def skin_path(self):
        return os.path.join(self.root, widget.SKIN_FILE_NAME)

    def write_skin_file(self, text):
        with open(self.skin_path(), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_missing_file_falls_back_to_the_default_skin(self):
        self.assertEqual(widget.load_skin(self.skin_path()), widget.DEFAULT_SKIN)

    def test_corrupt_or_unknown_files_fall_back_to_the_default_skin(self):
        cases = (
            "{not json",
            "",
            "[]",
            '"mecha"',
            '{"schema": 99, "skin": "mecha"}',
            '{"schema": 1}',
            '{"schema": 1, "skin": "glitter"}',
        )
        for text in cases:
            with self.subTest(text=text):
                self.write_skin_file(text)
                self.assertEqual(widget.load_skin(self.skin_path()), widget.DEFAULT_SKIN)

    def test_roundtrip_saves_and_loads_a_chosen_skin(self):
        self.assertTrue(widget.save_skin(self.root, widget.SKIN_MECHA))
        with open(self.skin_path(), encoding="utf-8") as handle:
            self.assertEqual(
                json.load(handle), {"schema": widget.SKIN_SCHEMA, "skin": "mecha"})
        self.assertEqual(widget.load_skin(self.skin_path()), "mecha")
        self.assertTrue(widget.save_skin(self.root, widget.SKIN_NEON))
        self.assertEqual(widget.load_skin(self.skin_path()), "neon")

    def test_saving_is_refused_for_unknown_skins_and_missing_directories(self):
        self.assertFalse(widget.save_skin(self.root, "glitter"))
        self.assertFalse(os.path.exists(self.skin_path()))
        self.assertFalse(
            widget.save_skin(os.path.join(self.root, "gone"), widget.SKIN_MECHA))


class SkinDisplayTests(unittest.TestCase):
    def test_build_display_defaults_to_the_default_skin(self):
        display = widget.build_display(
            snapshot(win(31.0), win(12.0)), NOW, "light", 1.0, height_for(1.0))
        self.assertEqual(display.skin, widget.DEFAULT_SKIN)

    def test_build_display_carries_the_chosen_skin(self):
        display = widget.build_display(
            snapshot(win(31.0), win(12.0)), NOW, "light", 1.0, height_for(1.0),
            skin=widget.SKIN_MECHA)
        self.assertEqual(display.skin, widget.SKIN_MECHA)
        self.assertEqual(
            display.rows[0].fill, widget.fill_percent(win(31.0).pct))

    def test_both_skins_render_the_same_size_and_differ(self):
        snap = snapshot(win(65.0), win(33.0, resets_in=2 * 86400.0))
        images = {}
        for skin in widget.SKINS:
            with self.subTest(skin=skin):
                display = widget.build_display(
                    snap, NOW, "light", 1.0, height_for(1.0), skin=skin)
                image = widget.render_display(display)
                self.assertEqual(image.size, (display.width, display.height))
                images[skin] = image
        self.assertEqual(images["neon"].size, images["mecha"].size)
        self.assertNotEqual(images["neon"].tobytes(), images["mecha"].tobytes())

    def test_nodata_display_renders_in_either_skin(self):
        for skin in widget.SKINS:
            with self.subTest(skin=skin):
                display = widget.build_display(
                    None, NOW, "light", 1.0, height_for(1.0), skin=skin)
                self.assertEqual(display.kind, "nodata")
                self.assertEqual(display.skin, skin)
                image = widget.render_display(display)
                self.assertEqual(image.size, (display.width, display.height))


class AnimationFillsTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.app = make_skin_app(holder.name)
        self.app.w32.animations_enabled.return_value = True

    def display(self, five_pct, seven_pct, now):
        return widget.build_display(
            snapshot(win(five_pct), win(seven_pct, resets_in=2 * 86400.0)),
            now, "light", 1.0, height_for(1.0))

    def test_first_sight_arms_nothing_but_remembers_the_fills(self):
        display = self.display(31.0, 12.0, NOW)
        self.app._track_animation(display, NOW)
        self.assertIsNone(self.app._anim)
        self.assertEqual(self.app._fills_shown, display_fills(display))
        self.assertNotIn(widget.TIMER_ANIM, self.app.timers)

    def test_a_changed_fill_slides_from_the_old_length(self):
        first = self.display(31.0, 12.0, NOW)
        self.app._track_animation(first, NOW)
        second = self.display(64.0, 12.0, NOW + 1.0)
        self.app._track_animation(second, NOW + 1.0)
        self.assertIsNotNone(self.app._anim)
        start, origin, targets = self.app._anim
        self.assertEqual(origin, display_fills(first))
        self.assertEqual(targets, display_fills(second))
        self.assertIn(widget.TIMER_ANIM, self.app.timers)

    def test_easing_runs_from_origin_to_target_over_the_span(self):
        origin = (10.0, 20.0)
        targets = (30.0, 40.0)
        self.app._anim = (NOW, origin, targets)
        self.assertEqual(self.app._anim_fills(NOW), origin)
        middle = self.app._anim_fills(NOW + widget.ANIM_SECONDS / 2.0)
        self.assertEqual(middle, (20.0, 30.0))
        self.assertEqual(self.app._anim_fills(NOW + widget.ANIM_SECONDS), targets)
        self.assertEqual(self.app._anim_fills(NOW + 10 * widget.ANIM_SECONDS), targets)

    def test_anim_timer_kills_itself_and_redraws(self):
        self.app.timers.add(widget.TIMER_ANIM)
        self.app.refresh_view = Mock()
        self.app.on_timer(widget.TIMER_ANIM)
        self.app.w32.kill_timer.assert_called_once_with(self.app.hwnd, widget.TIMER_ANIM)
        self.app.refresh_view.assert_called_once_with()
        self.assertNotIn(widget.TIMER_ANIM, self.app.timers)

    def test_system_animation_setting_gates_new_animations(self):
        self.app.w32.animations_enabled.return_value = False
        first = self.display(31.0, 12.0, NOW)
        self.app._track_animation(first, NOW)
        second = self.display(64.0, 12.0, NOW + 1.0)
        self.app._track_animation(second, NOW + 1.0)
        self.assertIsNone(self.app._anim)
        self.assertEqual(self.app._fills_shown, display_fills(second))

    def test_flash_keeps_the_static_fill(self):
        self.app._flashing = True
        first = self.display(31.0, 12.0, NOW)
        self.app._track_animation(first, NOW)
        second = self.display(64.0, 12.0, NOW + 1.0)
        self.app._track_animation(second, NOW + 1.0)
        self.assertIsNone(self.app._anim)

    def test_painted_frames_slide_to_the_exact_target_and_settle(self):
        # refresh_view 的次序：先按真实填充长度推进动画，再把缓动帧换上去画。
        first = self.display(31.0, 12.0, NOW)
        self.app._track_animation(first, NOW)
        second = self.display(64.0, 12.0, NOW + 1.0)
        self.app._track_animation(second, NOW + 1.0)
        self.assertIsNotNone(self.app._anim)
        # 换帧那一帧换完与上一帧长度相同（只有文字是新的），不会先跳一帧真实值。
        at_start = widget.apply_fills(second, self.app._anim_fills(NOW + 1.0))
        self.assertEqual(display_fills(at_start), display_fills(first))
        # 中途一帧落在起点与真实目标之间。
        at_middle = widget.apply_fills(
            second, self.app._anim_fills(NOW + 1.0 + widget.ANIM_SECONDS / 2.0))
        for value, begin, end in zip(
                display_fills(at_middle), display_fills(first), display_fills(second)):
            self.assertLessEqual(min(begin, end), value)
            self.assertLessEqual(value, max(begin, end))
        # 到点后画出的长度与真实目标完全相等（命中静态缓存），动画收掉。
        later = NOW + 1.0 + widget.ANIM_SECONDS + 1.0
        end_display = self.display(64.0, 12.0, later)
        self.app._track_animation(end_display, later)
        final = widget.apply_fills(end_display, self.app._anim_fills(later))
        self.assertEqual(display_fills(final), display_fills(second))
        self.assertEqual(display_fills(final), self.app._fills_shown)
        self.assertIsNone(self.app._anim)
        self.assertNotIn(widget.TIMER_ANIM, self.app.timers)

    def test_apply_fills_with_none_returns_the_same_display(self):
        display = self.display(31.0, 12.0, NOW)
        self.assertIs(widget.apply_fills(display, None), display)

    def test_apply_fills_reaches_both_blocks_of_a_dual_display(self):
        display = widget.sample_display("dual_green", "light", 1.0)
        before = tuple(row.fill for block in display.rows for row in block.rows)
        boosted = tuple(value - 7 for value in before)
        filled = widget.apply_fills(display, boosted)
        # 双栏槽位顺序 5h、7d、5h、7d：两块各自按 offset 对上自己的两条。
        self.assertEqual(
            tuple(row.fill for block in filled.rows for row in block.rows), boosted)
        self.assertNotEqual(filled, display)

    def test_apply_fills_leaves_a_display_without_bars_alone(self):
        display = widget.build_display(None, NOW, "light", 1.0, height_for(1.0))
        self.assertEqual(display.kind, "nodata")
        self.assertIs(widget.apply_fills(display, (50.0, 50.0)), display)


class Win32SkinMenuTests(unittest.TestCase):
    def make_win32(self):
        w32 = widget.Win32.__new__(widget.Win32)
        w32.user32 = Mock()
        w32.kernel32 = Mock()
        return w32

    def skin_item_calls(self, w32):
        found = {}
        for item in w32.user32.AppendMenuW.call_args_list:
            if len(item.args) >= 3 and item.args[2] in (
                    widget.MENU_SKIN_NEON, widget.MENU_SKIN_MECHA):
                found[item.args[2]] = item
        return found

    def test_the_skin_menu_is_a_submenu_with_the_active_skin_checked(self):
        for skin, checked in (("neon", "neon"), ("mecha", "mecha"), (None, None)):
            with self.subTest(skin=skin):
                w32 = self.make_win32()
                w32.user32.CreatePopupMenu.side_effect = [101, 202, 303]
                w32.user32.TrackPopupMenu.return_value = 0
                self.assertEqual(w32.track_menu(7, 5, 6, skin), 0)
                items = self.skin_item_calls(w32)
                self.assertEqual(
                    set(items), {widget.MENU_SKIN_NEON, widget.MENU_SKIN_MECHA})
                for skin_name, command in widget.SKIN_MENU_ITEMS:
                    with self.subTest(skin_name=skin_name):
                        item = items[command]
                        self.assertEqual(item.args[3], widget.SKIN_LABELS[skin_name])
                        self.assertEqual(
                            bool(item.args[1] & widget.MF_CHECKED), skin_name == checked)
                popup_calls = [
                    item for item in w32.user32.AppendMenuW.call_args_list
                    if len(item.args) >= 2 and item.args[1] == widget.MF_POPUP]
                self.assertEqual(len(popup_calls), 2)
                self.assertEqual(popup_calls[0].args[2], 202)
                self.assertEqual(popup_calls[0].args[3], "Display")
                self.assertEqual(popup_calls[1].args[2], 303)
                self.assertEqual(popup_calls[1].args[3], "Progress Bar Skin")

    def test_track_menu_returns_the_command_and_cleans_up(self):
        w32 = self.make_win32()
        w32.user32.CreatePopupMenu.side_effect = [101, 202, 303]
        w32.user32.TrackPopupMenu.return_value = widget.MENU_SKIN_MECHA
        self.assertEqual(w32.track_menu(7, 5, 6, "neon"), widget.MENU_SKIN_MECHA)
        w32.user32.SetForegroundWindow.assert_called_once_with(7)
        w32.user32.TrackPopupMenu.assert_called_once_with(
            101, widget.TPM_RETURNCMD | widget.TPM_RIGHTBUTTON | widget.TPM_NONOTIFY,
            5, 6, 0, 7, None)
        w32.user32.PostMessageW.assert_called_once_with(7, 0, 0, 0)
        w32.user32.DestroyMenu.assert_called_once_with(101)


class SkinSwitchAppTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name

    def test_start_resumes_the_saved_skin(self):
        with open(os.path.join(self.root, widget.SKIN_FILE_NAME), "w",
                  encoding="utf-8") as handle:
            handle.write('{"schema": 1, "skin": "mecha"}\n')
        app = make_skin_app(self.root)
        self.assertEqual(app.skin, "mecha")

    def test_start_without_a_file_uses_the_default_skin(self):
        app = make_skin_app(self.root)
        self.assertEqual(app.skin, widget.DEFAULT_SKIN)

    def test_menu_switch_saves_the_skin_and_redraws_with_it(self):
        app = make_skin_app(self.root)
        app.refresh_view = Mock()
        app.w32.track_menu.return_value = widget.MENU_SKIN_MECHA
        app.on_right_up()
        self.assertEqual(app.skin, "mecha")
        app.refresh_view.assert_called_once_with(force=True)
        app.w32.track_menu.assert_called_once_with(10, 4, 8, "neon", widget.DEFAULT_STYLE)
        self.assertEqual(
            widget.load_skin(os.path.join(self.root, widget.SKIN_FILE_NAME)), "mecha")

    def test_choosing_the_current_skin_changes_nothing(self):
        app = make_skin_app(self.root)
        app.refresh_view = Mock()
        app.w32.track_menu.return_value = widget.MENU_SKIN_NEON
        app.on_right_up()
        self.assertEqual(app.skin, "neon")
        app.refresh_view.assert_not_called()
        self.assertFalse(
            os.path.exists(os.path.join(self.root, widget.SKIN_FILE_NAME)))

    def test_an_unwritable_data_dir_does_not_block_the_switch(self):
        app = make_skin_app(os.path.join(self.root, "absent"))
        app.refresh_view = Mock()
        app.w32.track_menu.return_value = widget.MENU_SKIN_MECHA
        app.on_right_up()
        self.assertEqual(app.skin, "mecha")
        app.refresh_view.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
