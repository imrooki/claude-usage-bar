"""Refresh now flashes the widget so an unchanged snapshot still looks acknowledged."""

import pathlib
import sys
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget


def make_app():
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
    w32.set_timer.return_value = True
    w32.cursor_pos.return_value = (4, 8)
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(
            widget.Options(".", None, None, None, no_codex=True), w32, Mock())
    app.hwnd = 10
    app.visible = True
    app._apply = Mock()
    return app


class DimImageTests(unittest.TestCase):
    def test_scales_alpha_keeps_rgb_and_size_and_does_not_mutate(self):
        source = Image.new("RGBA", (2, 1))
        source.putdata([(10, 20, 30, 0), (40, 50, 60, 200)])
        before = source.tobytes()
        result = widget.dim_image(source, widget.FLASH_ALPHA_SCALE)
        self.assertEqual(result.size, (2, 1))
        self.assertEqual(result.mode, "RGBA")
        self.assertEqual(list(result.getdata()), [
            (10, 20, 30, widget.BACKGROUND_ALPHA),
            (40, 50, 60, int(200 * widget.FLASH_ALPHA_SCALE)),
        ])
        self.assertEqual(source.tobytes(), before)
        self.assertIsNot(result, source)

    def test_alpha_uses_floor_when_floor_and_round_differ(self):
        # 8 * 0.35 = 2.8; floor is 2, round is 3. BACKGROUND_ALPHA is 1, so 2 stands.
        source = Image.new("RGBA", (1, 1), (9, 8, 7, 8))
        result = widget.dim_image(source, widget.FLASH_ALPHA_SCALE)
        self.assertEqual(result.getpixel((0, 0)), (9, 8, 7, 2))
        self.assertEqual(source.getpixel((0, 0)), (9, 8, 7, 8))

    def test_rejects_non_rgba(self):
        with self.assertRaises(ValueError):
            widget.dim_image(Image.new("RGB", (1, 1), (1, 2, 3)), 0.5)


class RefreshFlashTests(unittest.TestCase):
    def choose(self, app, command):
        app.w32.track_menu.return_value = command
        app.on_right_up()

    def test_refresh_polls_then_flashes_and_redraws(self):
        app = make_app()
        order = []
        flashing = []
        app.poll_snapshot = Mock(side_effect=lambda force=False: order.append(("poll", force)))

        def on_refresh(force=False, reassert_topmost=False):
            order.append(("refresh", force))
            flashing.append(app._flashing)

        app.refresh_view = Mock(side_effect=on_refresh)
        self.choose(app, widget.MENU_REFRESH)
        self.assertEqual(order, [("poll", True), ("refresh", True)])
        self.assertEqual(flashing, [True])
        self.assertTrue(app._flashing)
        app.w32.set_timer.assert_called_once_with(app.hwnd, widget.TIMER_FLASH, widget.FLASH_MS)
        self.assertIn(widget.TIMER_FLASH, app.timers)
        app.on_timer(widget.TIMER_FLASH)
        self.assertEqual(flashing, [True, False])
        self.assertEqual(order, [("poll", True), ("refresh", True), ("refresh", True)])
        self.assertFalse(app._flashing)

    def test_timer_ends_flash_and_redraws(self):
        app = make_app()
        app._flashing = True
        app.shown_display = object()
        app.timers.add(widget.TIMER_FLASH)
        flashing = []

        def on_refresh(force=False, reassert_topmost=False):
            flashing.append(app._flashing)

        app.refresh_view = Mock(side_effect=on_refresh)
        app.on_timer(widget.TIMER_FLASH)
        self.assertEqual(flashing, [False])
        self.assertFalse(app._flashing)
        self.assertIsNone(app.shown_display)
        self.assertNotIn(widget.TIMER_FLASH, app.timers)
        app.w32.kill_timer.assert_called_once_with(app.hwnd, widget.TIMER_FLASH)
        app.refresh_view.assert_called_once_with(force=True)

    def test_failed_timer_does_not_leave_widget_dim(self):
        app = make_app()
        app.w32.set_timer.return_value = False
        app.shown_display = object()
        app.poll_snapshot = Mock()
        flashing = []

        def on_refresh(force=False, reassert_topmost=False):
            flashing.append(app._flashing)

        app.refresh_view = Mock(side_effect=on_refresh)
        self.choose(app, widget.MENU_REFRESH)
        self.assertEqual(flashing, [True, False])
        self.assertFalse(app._flashing)
        self.assertIsNone(app.shown_display)
        self.assertNotIn(widget.TIMER_FLASH, app.timers)
        self.assertEqual(app.refresh_view.call_args_list, [
            call(force=True),
            call(force=True),
        ])

    def test_second_refresh_restarts_timer_without_clearing_flash(self):
        app = make_app()
        app.poll_snapshot = Mock()
        app.refresh_view = Mock()
        self.choose(app, widget.MENU_REFRESH)
        self.choose(app, widget.MENU_REFRESH)
        self.assertTrue(app._flashing)
        self.assertEqual(app.refresh_view.call_count, 1)
        self.assertEqual(app.w32.set_timer.call_count, 2)
        app.w32.set_timer.assert_called_with(app.hwnd, widget.TIMER_FLASH, widget.FLASH_MS)

    def test_snap_and_quit_do_not_flash(self):
        for command in (widget.MENU_SNAP, widget.MENU_QUIT):
            with self.subTest(command=command):
                app = make_app()
                app.poll_snapshot = Mock()
                app.refresh_view = Mock()
                app.request_exit = Mock()
                pos_path = pathlib.Path(app.data_dir) / widget.POS_FILE_NAME
                tmp_path = pathlib.Path(str(pos_path) + ".tmp")
                before = (pos_path.exists(), tmp_path.exists())
                with patch.object(app, "_save_position") as save_position:
                    self.choose(app, command)
                    if command == widget.MENU_SNAP:
                        save_position.assert_called_once_with()
                    else:
                        save_position.assert_not_called()
                self.assertEqual((pos_path.exists(), tmp_path.exists()), before)
                self.assertFalse(app._flashing)
                app.w32.set_timer.assert_not_called()
                app.poll_snapshot.assert_not_called()

    def test_recreate_and_exit_clear_flash_without_rearming(self):
        app = make_app()
        app._flashing = True
        app.timers.add(widget.TIMER_FLASH)
        app.w32.is_window.return_value = False
        app.w32.create_window.return_value = 11
        app.refresh_view = Mock()
        app._recreate_window("owner disappeared")
        self.assertFalse(app._flashing)
        flash_arms = [call for call in app.w32.set_timer.call_args_list if call.args[1] == widget.TIMER_FLASH]
        self.assertEqual(flash_arms, [])

        app = make_app()
        app._flashing = True
        app.timers.add(widget.TIMER_FLASH)
        app.w32.destroy_window.return_value = True
        app._arm_watchdog = Mock()
        app.request_exit()
        self.assertFalse(app._flashing)
        app.w32.set_timer.assert_not_called()
        app._arm_watchdog.assert_called_once_with()

        app = make_app()
        app.hwnd = 0
        app._flashing = True
        app.w32.create_window.return_value = 12
        app.refresh_view = Mock()
        app.on_thread_message(widget.WM_APP_RECREATE)
        self.assertFalse(app._flashing)
        flash_arms = [call for call in app.w32.set_timer.call_args_list if call.args[1] == widget.TIMER_FLASH]
        self.assertEqual(flash_arms, [])


class ApplyDimTests(unittest.TestCase):
    def _app_for_apply(self):
        app = make_app()
        app._apply = widget.WidgetApp._apply.__get__(app, widget.WidgetApp)
        app.geom = None
        return app

    def _display(self):
        return widget.build_display(None, 0.0, "dark", 1.0, 36)

    def test_apply_dims_only_while_flashing(self):
        display = self._display()
        rendered = widget.render_display(display)
        dimmed = widget.dim_image(rendered, widget.FLASH_ALPHA_SCALE)
        expected = {
            False: widget.premultiply_bgra(rendered)[3::4],
            True: widget.premultiply_bgra(dimmed)[3::4],
        }
        for flashing in (False, True):
            with self.subTest(flashing=flashing):
                app = self._app_for_apply()
                app._flashing = flashing
                app._apply(display, (1, 2, display.width, display.height), True)
                data = app.w32.update_layered.call_args.args[5]
                self.assertEqual(data[3::4], expected[flashing])


class StuckDimTests(unittest.TestCase):
    """Flash end can be skipped by the refresh guards; the next plain refresh must undim."""

    def choose(self, app, command):
        app.w32.track_menu.return_value = command
        app.on_right_up()

    def live_app(self):
        app = make_app()
        app._apply = widget.WidgetApp._apply.__get__(app, widget.WidgetApp)
        app.poll_snapshot = Mock()
        return app

    def start_flash(self, app):
        self.choose(app, widget.MENU_REFRESH)
        self.assertTrue(app._flashing)
        self.assertIn(widget.TIMER_FLASH, app.timers)
        return app.w32.update_layered.call_args.args[5]

    def assert_later_refresh_undims(self, app, dimmed):
        app.refresh_view()
        data = app.w32.update_layered.call_args.args[5]
        expected = widget.premultiply_bgra(widget.render_display(app.shown_display))
        self.assertEqual(data[3::4], expected[3::4])
        self.assertNotEqual(data[3::4], dimmed[3::4])
        self.assertFalse(app._flashing)

    def test_blocked_end_then_plain_refresh_submits_undimmed(self):
        for name in ("menu", "drag", "presentation"):
            with self.subTest(name=name):
                app = self.live_app()
                dimmed = self.start_flash(app)
                if name == "menu":
                    app.menu_open = True
                elif name == "drag":
                    app.drag = {"moved": False}
                else:
                    app.w32.notification_state.return_value = 3
                app.w32.update_layered.reset_mock()
                app.on_timer(widget.TIMER_FLASH)
                self.assertFalse(app._flashing)
                self.assertIsNone(app.shown_display)
                self.assertNotIn(widget.TIMER_FLASH, app.timers)
                app.w32.update_layered.assert_not_called()
                if name == "menu":
                    app.menu_open = False
                elif name == "drag":
                    app.drag = None
                else:
                    app.w32.notification_state.return_value = 5
                self.assert_later_refresh_undims(app, dimmed)

    def test_start_redraw_oserror_still_arms_timer_and_flash_ends(self):
        app = self.live_app()
        calls = {"n": 0}

        def layered(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("layered")

        app.w32.update_layered.side_effect = layered
        app.w32.track_menu.return_value = widget.MENU_REFRESH
        app.window_proc(app.hwnd, widget.WM_RBUTTONUP, 0, 0)
        self.assertTrue(app._flashing)
        self.assertIn(widget.TIMER_FLASH, app.timers)
        app.on_timer(widget.TIMER_FLASH)
        self.assertFalse(app._flashing)
        self.assertNotIn(widget.TIMER_FLASH, app.timers)
        self.assertEqual(calls["n"], 2)
        data = app.w32.update_layered.call_args.args[5]
        expected = widget.premultiply_bgra(widget.render_display(app.shown_display))
        self.assertEqual(data[3::4], expected[3::4])


if __name__ == "__main__":
    unittest.main()
