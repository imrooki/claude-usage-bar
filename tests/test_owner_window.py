"""Taskbar ownership and window recovery checks, without touching the real taskbar."""

import ctypes
import os
import pathlib
import sys
import threading
import unittest
from ctypes import wintypes
from unittest.mock import Mock, call, mock_open, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget
from test_window_events import make_app


class OwnerWindowTests(unittest.TestCase):
    def make_owner_app(self):
        app = make_app()
        app.owner = 20
        app.w32.taskbar_hwnd.return_value = 20
        app.w32.window_owner.return_value = 20
        app.w32.create_window.return_value = 11
        app.w32.is_window.return_value = True
        app.w32.set_timer.return_value = True
        app.w32.post_thread_message.return_value = True
        app.refresh_view = Mock()
        app._arm_watchdog = Mock()
        return app

    def destroy_with_message(self, app):
        def destroy(hwnd):
            self.assertEqual(hwnd, app.hwnd)
            self.assertEqual(app._dispatch(widget.WM_DESTROY, 0), (True, 0))
            return True
        app.w32.destroy_window.side_effect = destroy

    def assert_regular_timers(self, app):
        self.assertEqual(app.timers, {widget.TIMER_CHECK, widget.TIMER_POLL, widget.TIMER_THEME})
        self.assertEqual(app.w32.set_timer.call_args_list, [
            call(11, widget.TIMER_CHECK, widget.WINDOW_CHECK_MS),
            call(11, widget.TIMER_POLL, widget.SNAPSHOT_POLL_MS),
            call(11, widget.TIMER_THEME, widget.THEME_POLL_MS),
        ])

    def test_startup_uses_primary_taskbar_owner_or_none(self):
        for tray in (20, 0):
            with self.subTest(tray=tray):
                app = self.make_owner_app()
                app.hwnd = 0
                app.w32.taskbar_hwnd.return_value = tray
                app.w32.run_message_loop.return_value = 0
                app.poll_snapshot = Mock()
                self.assertEqual(app.run(), 0)
                app.w32.create_window.assert_called_once_with(
                    0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, tray or None)
                self.assertEqual((app.hwnd, app.owner), (11, tray))
                self.assertEqual(app._owner_failed_tray, 0)
                app.w32.run_message_loop.assert_called_once_with(app.on_thread_message, app._log_exception)
                app.w32.watch_window_events.assert_called_once_with(app.on_window_event)
                self.assert_regular_timers(app)
                self.assertIsNone(widget._APP)

    def test_owned_creation_falls_back_and_same_taskbar_is_not_retried(self):
        app = self.make_owner_app()
        app.hwnd = 0
        error = OSError("owned create failed")
        app.w32.create_window.side_effect = [error, 11]
        app.hwnd = app._create_widget_window()
        self.assertEqual((app.hwnd, app.owner, app._owner_failed_tray), (11, 0, 20))
        self.assertEqual(app.w32.create_window.call_args_list, [
            call(0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, 20),
            call(0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, None),
        ])
        app.log.log_exception.assert_called_once_with(error)
        app.w32.post_quit.assert_not_called()
        app._owner_check()
        app.w32.destroy_window.assert_not_called()
        self.assertEqual(app.w32.create_window.call_count, 2)
        self.assertEqual((app.hwnd, app.owner, app._owner_failed_tray), (11, 0, 20))
        app.refresh_view.assert_not_called()
        app.w32.window_owner.assert_not_called()

    def test_unowned_creation_failure_is_not_retried(self):
        app = self.make_owner_app()
        app.w32.taskbar_hwnd.return_value = 0
        error = OSError("unowned create failed")
        app.w32.create_window.side_effect = error
        with self.assertRaises(OSError) as raised:
            app._create_widget_window()
        self.assertIs(raised.exception, error)
        app.w32.create_window.assert_called_once_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, None)
        app.log.log_exception.assert_not_called()

    def test_check_recreates_for_new_owner_without_quitting_or_unhooking(self):
        app = self.make_owner_app()
        app.w32.taskbar_hwnd.return_value = 30
        app.w32.window_owner.return_value = 30
        app.opts = app.opts._replace(selftest_gdi=10)
        old_timers = {widget.TIMER_CHECK, widget.TIMER_POLL, widget.TIMER_THEME,
                      widget.TIMER_GDI_BEGIN, widget.TIMER_GDI_TICK, widget.TIMER_GDI_END}
        app.timers.update(old_timers)
        app.geom = (1, 2, 3, 4)
        app.shown_display = object()
        app._z_order_pending = True

        def destroy(hwnd):
            self.assertTrue(app._recreating)
            self.assertEqual(app.timers, set())
            self.assertEqual(app._dispatch(widget.WM_DESTROY, 0), (True, 0))
            self.assertEqual(app.hwnd, hwnd)
            # 同步销毁期间的系统消息不能再次进入所有权重建。
            app._owner_check()
            return True

        app.w32.destroy_window.side_effect = destroy
        app.on_timer(widget.TIMER_CHECK)
        app.w32.window_owner.assert_not_called()
        app.w32.is_window.assert_called_once_with(10)
        app.w32.destroy_window.assert_called_once_with(10)
        app.w32.create_window.assert_called_once_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, 30)
        self.assertEqual((app.hwnd, app.owner), (11, 30))
        self.assertFalse(app._recreating)
        self.assertFalse(app.visible)
        self.assertIsNone(app.geom)
        self.assertIsNone(app.shown_display)
        self.assertFalse(app._z_order_pending)
        self.assertEqual({entry.args for entry in app.w32.kill_timer.call_args_list},
                         {(10, timer) for timer in old_timers})
        operation_names = [entry[0] for entry in app.w32.mock_calls]
        destroy_index = operation_names.index("destroy_window")
        self.assertTrue(all(name != "kill_timer" for name in operation_names[destroy_index:]))
        self.assertLess(destroy_index, operation_names.index("create_window"))
        self.assert_regular_timers(app)
        app.refresh_view.assert_any_call(force=True, reassert_topmost=True)
        app.w32.post_quit.assert_not_called()
        app.w32.post_thread_message.assert_not_called()
        app.w32.unwatch_window_events.assert_not_called()
        app.w32.watch_window_events.assert_not_called()
        app.log.log.assert_called_once_with(
            "OwnerChanged", "taskbar replaced 0x%X -> 0x%X" % (20, 30))

    def test_matching_owner_or_missing_taskbar_does_not_recreate(self):
        for tray in (20, 0):
            with self.subTest(tray=tray):
                app = self.make_owner_app()
                app.w32.taskbar_hwnd.return_value = tray
                app.on_timer(widget.TIMER_CHECK)
                app.w32.destroy_window.assert_not_called()
                app.w32.create_window.assert_not_called()
                app.refresh_view.assert_called_once_with(reassert_topmost=True)
                app.log.log.assert_not_called()
                if not tray:
                    app.w32.window_owner.assert_not_called()
                    app.w32.is_window.assert_not_called()
                else:
                    app.w32.is_window.assert_called_once_with(20)

    def test_transient_owner_mismatch_only_logs_without_recreating(self):
        for actual_owner in (0, 30):
            with self.subTest(actual_owner=actual_owner):
                app = self.make_owner_app()
                app.w32.window_owner.return_value = actual_owner
                app._owner_recreated_at = 0.0
                with patch.object(widget.time, "monotonic", return_value=2.0):
                    app.on_timer(widget.TIMER_CHECK)
                app.w32.is_window.assert_called_once_with(20)
                app.w32.window_owner.assert_called_once_with(10)
                app.log.log.assert_called_once_with(
                    "OwnerMismatch", "GW_OWNER=0x%X expected 0x%X; no recreate" % (actual_owner, 20))
                app.w32.destroy_window.assert_not_called()
                app.w32.create_window.assert_not_called()
                app.refresh_view.assert_called_once_with(reassert_topmost=True)
                self.assertEqual((app.hwnd, app.owner), (10, 20))
                self.assertEqual(app._owner_recreated_at, 0.0)
                self.assertFalse(app._owner_recreate_suppressed)

    def test_transient_owner_mismatch_log_is_deduplicated_for_ten_minutes(self):
        app = self.make_owner_app()
        app.w32.window_owner.return_value = 30
        app.log = widget.ErrorLog(".")
        with patch.object(widget.os.path, "isdir", return_value=True), \
                patch.object(widget.os.path, "getsize", return_value=0), \
                patch("builtins.open", mock_open()) as log_file:
            for now in (0.0, 2.0, 30.0, 599.999):
                with patch.object(widget.time, "monotonic", return_value=now):
                    app._owner_check()
            log_file().write.assert_called_once()
            self.assertTrue(log_file().write.call_args.args[0].endswith(
                b" OwnerMismatch: GW_OWNER=0x1E expected 0x14; no recreate\n"))
            with patch.object(widget.time, "monotonic", return_value=600.0):
                app._owner_check()
            self.assertEqual(log_file().write.call_count, 2)
        app.w32.destroy_window.assert_not_called()
        app.w32.create_window.assert_not_called()
        self.assertIsNone(app._owner_recreated_at)

    def test_invalid_recorded_owner_recreates_even_when_taskbar_handle_is_unchanged(self):
        app = self.make_owner_app()
        app.w32.is_window.side_effect = lambda hwnd: hwnd != 20
        self.destroy_with_message(app)
        app._owner_check()
        self.assertEqual(app.w32.is_window.call_args_list, [call(20), call(10)])
        app.w32.window_owner.assert_not_called()
        app.w32.destroy_window.assert_called_once_with(10)
        app.w32.create_window.assert_called_once_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, 20)
        self.assertEqual((app.hwnd, app.owner), (11, 20))
        self.assert_regular_timers(app)
        app.refresh_view.assert_called_once_with(force=True, reassert_topmost=True)
        app.log.log.assert_called_once_with(
            "OwnerChanged", "owner 0x%X no longer valid, taskbar 0x%X" % (20, 20))

    def test_unowned_window_recreates_when_taskbar_appears(self):
        app = self.make_owner_app()
        app.owner = 0
        app.w32.taskbar_hwnd.return_value = 0
        app._owner_check()
        app.w32.create_window.assert_not_called()
        app.w32.destroy_window.assert_not_called()
        app.w32.taskbar_hwnd.return_value = 30
        self.destroy_with_message(app)
        app._owner_check()
        app.w32.window_owner.assert_not_called()
        app.w32.destroy_window.assert_called_once_with(10)
        app.w32.create_window.assert_called_once_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, 30)
        self.assertEqual((app.hwnd, app.owner), (11, 30))
        self.assert_regular_timers(app)
        app.refresh_view.assert_called_once_with(force=True, reassert_topmost=True)
        app.log.log.assert_called_once_with(
            "OwnerChanged", "unowned window, taskbar 0x%X" % 30)

    def test_invalid_owner_recreate_is_suppressed_once_within_thirty_seconds(self):
        app = self.make_owner_app()
        # 记录的所有者 20 无效，其它句柄有效，所以有所有者创建成功后仍会再判无效。
        app.w32.is_window.side_effect = lambda hwnd: hwnd != 20
        cause = "owner 0x%X no longer valid, taskbar 0x%X" % (20, 20)
        expected_creates = ((0.0, 1), (2.0, 1), (29.999, 1), (30.0, 2),
                            (32.0, 2), (59.999, 2), (60.0, 3))
        for now, expected in expected_creates:
            with self.subTest(now=now):
                with patch.object(widget.time, "monotonic", return_value=now):
                    app._owner_check()
                self.assertEqual(app.w32.create_window.call_count, expected)
        suppressed = call(
            "OwnerRecreateSuppressed", "recreate deferred for 30 s: " + cause)
        self.assertEqual(app.log.log.call_args_list.count(suppressed), 2)
        self.assertEqual(
            app.log.log.call_args_list.count(call("OwnerChanged", cause)), 3)
        self.assertEqual(app.w32.destroy_window.call_args_list.count(call(10)), 1)
        self.assertEqual(app.w32.destroy_window.call_args_list.count(call(11)), 2)
        self.assertEqual(app._owner_recreated_at, 60.0)

    def test_different_taskbar_retries_after_owned_create_failed(self):
        app = self.make_owner_app()
        error = OSError("owned create failed")
        app.w32.create_window.side_effect = [error, 11, 12]
        app.hwnd = app._create_widget_window()
        self.assertEqual((app.owner, app._owner_failed_tray), (0, 20))
        app.w32.taskbar_hwnd.return_value = 30
        self.destroy_with_message(app)
        app._owner_check()
        app.w32.destroy_window.assert_called_once_with(11)
        app.w32.create_window.assert_called_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, 30)
        self.assertEqual((app.hwnd, app.owner), (12, 30))

    def test_taskbar_created_retries_failed_owner_but_setting_change_does_not(self):
        app = self.make_owner_app()
        error = OSError("owned create failed")
        app.w32.create_window.side_effect = [error, 11, 12]
        app.hwnd = app._create_widget_window()
        self.assertEqual(app._owner_failed_tray, 20)
        app._owner_check()
        app.w32.destroy_window.assert_not_called()
        self.assertEqual(app.w32.create_window.call_count, 2)
        app.msg_taskbar_created = 0xC123
        with patch.object(widget, "read_light_theme", return_value=app.light):
            self.assertEqual(app._dispatch(widget.WM_SETTINGCHANGE, 0), (True, 0))
        self.assertEqual(app._owner_failed_tray, 20)
        app.w32.destroy_window.assert_not_called()
        self.assertEqual(app.w32.create_window.call_count, 2)
        self.destroy_with_message(app)
        with patch.object(widget, "read_light_theme", return_value=app.light):
            self.assertEqual(app._dispatch(app.msg_taskbar_created, 0), (True, 0))
        self.assertEqual(app._owner_failed_tray, 0)
        app.w32.destroy_window.assert_called_once_with(11)
        app.w32.create_window.assert_called_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, 20)
        self.assertEqual((app.hwnd, app.owner), (12, 20))

    def test_owned_create_success_clears_failed_tray_but_no_taskbar_keeps_it(self):
        app = self.make_owner_app()
        app._owner_failed_tray = 20
        app.w32.taskbar_hwnd.return_value = 30
        hwnd = app._create_widget_window()
        self.assertEqual((hwnd, app.owner, app._owner_failed_tray), (11, 30, 0))
        app._owner_failed_tray = 20
        app.w32.taskbar_hwnd.return_value = 0
        hwnd = app._create_widget_window()
        self.assertEqual((hwnd, app.owner, app._owner_failed_tray), (11, 0, 20))

    def test_system_destroy_logs_window_destroyed(self):
        app = self.make_owner_app()
        self.assertEqual(app._dispatch(widget.WM_DESTROY, 0), (True, 0))
        app.log.log.assert_any_call(
            "WindowDestroyed", "window destroyed by the system; recovering via thread message")

    def test_owner_check_skips_menu_drag_busy_exit_recreate_or_missing_window(self):
        for attribute, value in (("menu_open", True), ("drag", {}), ("_busy", True),
                                 ("exiting", True), ("_recreating", True), ("hwnd", 0)):
            with self.subTest(attribute=attribute):
                app = self.make_owner_app()
                setattr(app, attribute, value)
                app._owner_check()
                app.w32.taskbar_hwnd.assert_not_called()
                app.w32.destroy_window.assert_not_called()
                app.w32.create_window.assert_not_called()

    def test_system_change_inside_layered_update_or_move_defers_owner_recreation(self):
        for operation in ("update_layered", "move_window"):
            with self.subTest(operation=operation):
                app = self.make_owner_app()
                app.refresh_view = widget.WidgetApp.refresh_view.__get__(app)
                app._apply = widget.WidgetApp._apply.__get__(app)
                app.refresh_view(force=True)
                app.w32.taskbar_hwnd.return_value = 30
                app.w32.taskbar_pos.return_value = ((0, 100, 1010, 140), widget.ABE_BOTTOM)

                def reenter(*args):
                    self.assertTrue(app._busy)
                    app.window_proc(app.hwnd, widget.WM_SETTINGCHANGE, 0, 0)

                native_call = getattr(app.w32, operation)
                native_call.reset_mock()
                native_call.side_effect = reenter
                with patch.object(widget, "read_light_theme", return_value=app.light):
                    app.refresh_view(force=operation == "update_layered")
                native_call.assert_called_once()
                self.assertEqual((app.hwnd, app.owner), (10, 20))
                self.assertIsNotNone(app.geom)
                self.assertIsNotNone(app.shown_display)
                app.w32.taskbar_hwnd.assert_not_called()
                app.w32.destroy_window.assert_not_called()
                app.log.log_exception.assert_not_called()
                native_call.side_effect = None
                self.destroy_with_message(app)
                app.on_timer(widget.TIMER_CHECK)
                app.w32.destroy_window.assert_called_once_with(10)
                self.assertEqual((app.hwnd, app.owner), (11, 30))

    def test_failed_taskbar_is_not_retried_on_later_checks(self):
        app = self.make_owner_app()
        app.owner = 0
        self.assertEqual(app._owner_failed_tray, 0)
        error = OSError("owned create failed")
        app.w32.create_window.side_effect = [error, 11, error, 12]
        self.destroy_with_message(app)
        cause = "unowned window, taskbar 0x%X" % 20
        for now in (0.0, 2.0, 29.999, 30.0, 32.0, 59.999, 60.0):
            with self.subTest(now=now), patch.object(widget.time, "monotonic", return_value=now):
                app.on_timer(widget.TIMER_CHECK)
        self.assertEqual(app.w32.create_window.call_count, 2)
        self.assertEqual(app.w32.destroy_window.call_count, 1)
        self.assertEqual((app.owner, app._owner_failed_tray), (0, 20))
        app.w32.window_owner.assert_not_called()
        app.log.log.assert_called_once_with("OwnerChanged", cause)
        self.assertNotIn(
            call("OwnerRecreateSuppressed", "recreate deferred for 30 s: " + cause),
            app.log.log.call_args_list)
        self.assertEqual(app._owner_recreated_at, 0.0)
        self.assertFalse(app._owner_recreate_suppressed)
        # 系统销毁后的恢复不看上次失败的任务栏句柄。
        app.on_destroy()
        with patch.object(widget.time, "monotonic", return_value=61.0):
            app.on_thread_message(widget.WM_APP_RECREATE)
        self.assertEqual(app.w32.create_window.call_count, 4)
        self.assertEqual(app._owner_failed_tray, 20)
        app.w32.post_quit.assert_not_called()
        app.log.log.assert_any_call(
            "WindowDestroyed", "window destroyed by the system; recovering via thread message")

    def test_system_changes_and_taskbar_created_check_the_owner(self):
        taskbar_created = 0xC123
        for message in (*sorted(widget.SYSTEM_CHANGE_MESSAGES), taskbar_created):
            with self.subTest(message=message):
                app = self.make_owner_app()
                app.msg_taskbar_created = taskbar_created
                app.w32.taskbar_hwnd.return_value = 30
                self.destroy_with_message(app)
                with patch.object(widget, "read_light_theme", return_value=True):
                    self.assertEqual(app._dispatch(message, 0), (True, 0))
                app.w32.destroy_window.assert_called_once_with(10)
                self.assertEqual(app.owner, 30)
                self.assertTrue(app.light)
                app.refresh_view.assert_any_call(force=True, reassert_topmost=True)

    def test_system_destroy_posts_thread_recovery_and_recovers_unowned(self):
        app = self.make_owner_app()
        app.timers.update({widget.TIMER_CHECK, widget.TIMER_POLL, widget.TIMER_THEME})
        app.geom = (1, 2, 3, 4)
        app.shown_display = object()
        app._z_order_pending = True
        self.assertEqual(app._dispatch(widget.WM_DESTROY, 0), (True, 0))
        self.assertEqual(app.hwnd, 0)
        self.assertFalse(app.visible)
        self.assertEqual(app.timers, set())
        app.w32.kill_timer.assert_not_called()
        app.w32.unwatch_window_events.assert_not_called()
        app.w32.post_quit.assert_not_called()
        app.w32.post_thread_message.assert_called_once_with(widget.WM_APP_RECREATE)
        app.w32.taskbar_hwnd.return_value = 0
        app.on_thread_message(widget.WM_APP_RECREATE)
        app.w32.create_window.assert_called_once_with(
            0, 0, widget.INITIAL_WINDOW_PX, widget.INITIAL_WINDOW_PX, None)
        self.assertEqual((app.hwnd, app.owner), (11, 0))
        self.assertIsNone(app.geom)
        self.assertIsNone(app.shown_display)
        self.assertFalse(app._z_order_pending)
        self.assert_regular_timers(app)
        app.refresh_view.assert_called_once_with(force=True, reassert_topmost=True)
        app.on_thread_message(widget.WM_APP_RECREATE)
        self.assertEqual(app.w32.create_window.call_count, 1)
        self.assertEqual(app.w32.set_timer.call_count, 3)
        self.assertEqual(app.refresh_view.call_count, 1)
        # 任务栏稍后出现时，两秒检查把临时无所有者窗口换成有所有者窗口。
        app.w32.taskbar_hwnd.return_value = 30
        self.destroy_with_message(app)
        app._owner_check()
        self.assertEqual(app.owner, 30)
        self.assertEqual(app.w32.create_window.call_count, 2)

    def test_thread_recovery_ignores_unrelated_messages_and_exit(self):
        for message, exiting in ((widget.WM_APP_Z_ORDER, False), (widget.WM_APP_RECREATE, True)):
            with self.subTest(message=message, exiting=exiting):
                app = self.make_owner_app()
                app.hwnd = 0
                app.exiting = exiting
                app.on_thread_message(message)
                app.w32.create_window.assert_not_called()
                app.w32.set_timer.assert_not_called()
                app.refresh_view.assert_not_called()

    def test_failed_thread_post_logs_and_quits(self):
        app = self.make_owner_app()
        app.w32.post_thread_message.return_value = False
        app.on_destroy()
        self.assertEqual(app.log.log.call_args_list, [
            call("WindowDestroyed", "window destroyed by the system; recovering via thread message"),
            call("RecreateWindow", "PostThreadMessageW failed"),
        ])
        app.w32.post_quit.assert_called_once_with()

    def test_menu_return_reposts_recovery_after_system_destroy(self):
        app = self.make_owner_app()
        app.w32.cursor_pos.return_value = (1, 2)

        def track_menu(*args):
            self.assertTrue(app.menu_open)
            app.on_destroy()
            return 0

        app.w32.track_menu.side_effect = track_menu
        app.on_right_up()
        self.assertFalse(app.menu_open)
        self.assertEqual(app.w32.post_thread_message.call_args_list,
                         [call(widget.WM_APP_RECREATE)] * 2)
        app.w32.create_window.assert_not_called()
        app.w32.post_quit.assert_not_called()
        app.on_thread_message(widget.WM_APP_RECREATE)
        self.assertEqual(app.hwnd, 11)
        self.assert_regular_timers(app)

    def test_menu_return_does_not_repost_for_live_window_or_exit(self):
        for hwnd, exiting in ((10, False), (0, True)):
            with self.subTest(hwnd=hwnd, exiting=exiting):
                app = self.make_owner_app()
                app.w32.cursor_pos.return_value = (1, 2)

                def track_menu(*args):
                    app.hwnd, app.exiting = hwnd, exiting
                    return 0

                app.w32.track_menu.side_effect = track_menu
                app.on_right_up()
                self.assertFalse(app.menu_open)
                app.w32.post_thread_message.assert_not_called()
                app.w32.post_quit.assert_not_called()

    def test_failed_menu_recovery_post_logs_and_quits(self):
        app = self.make_owner_app()
        app.w32.cursor_pos.return_value = (1, 2)
        app.w32.track_menu.side_effect = lambda *args: setattr(app, "hwnd", 0) or 0
        app.w32.post_thread_message.return_value = False
        app.on_right_up()
        app.w32.post_thread_message.assert_called_once_with(widget.WM_APP_RECREATE)
        app.log.log.assert_called_once_with("RecreateWindow", "PostThreadMessageW failed")
        app.w32.post_quit.assert_called_once_with()

    def test_recreate_skips_already_invalid_window(self):
        app = self.make_owner_app()
        app.w32.is_window.return_value = False
        app._recreate_window("owner disappeared")
        app.w32.destroy_window.assert_not_called()
        self.assertEqual(app.hwnd, 11)
        self.assert_regular_timers(app)
        app.refresh_view.assert_called_once_with(force=True, reassert_topmost=True)

    def test_create_failure_during_recreate_requests_exit(self):
        app = self.make_owner_app()
        self.destroy_with_message(app)
        owned_error, unowned_error = OSError("owned create failed"), OSError("unowned create failed")
        app.w32.create_window.side_effect = [owned_error, unowned_error]
        app._recreate_window("owner changed")
        self.assertEqual(app.log.log_exception.call_args_list, [call(owned_error), call(unowned_error)])
        self.assertEqual(app.w32.create_window.call_count, 2)
        self.assertEqual(app.owner, 0)
        self.assertTrue(app.exiting)
        self.assertFalse(app._recreating)
        self.assertEqual(app.hwnd, 0)
        app.w32.unwatch_window_events.assert_called_once_with()
        app.w32.post_quit.assert_called_once_with()
        app.w32.set_timer.assert_not_called()
        app.refresh_view.assert_not_called()

    def test_create_failure_during_thread_recovery_logs_and_quits(self):
        app = self.make_owner_app()
        app.hwnd = 0
        owned_error, unowned_error = OSError("owned create failed"), OSError("unowned create failed")
        app.w32.create_window.side_effect = [owned_error, unowned_error]
        app.on_thread_message(widget.WM_APP_RECREATE)
        self.assertEqual(app.log.log_exception.call_args_list, [call(owned_error), call(unowned_error)])
        self.assertEqual(app.w32.create_window.call_count, 2)
        self.assertEqual(app.owner, 0)
        app.w32.post_quit.assert_called_once_with()
        app.w32.set_timer.assert_not_called()
        app.refresh_view.assert_not_called()

    def test_run_logs_thread_recovery_refresh_error_and_keeps_dispatching(self):
        app = self.make_owner_app()
        app.hwnd = 0
        app.poll_snapshot = Mock()
        error = RuntimeError("recovery refresh failed")
        app.refresh_view.side_effect = [None, error]
        app.w32.user32 = Mock()
        messages = iter(((None, widget.WM_APP_RECREATE), (11, widget.WM_NULL)))

        def get_message(message_ref, hwnd, low, high):
            item = next(messages, None)
            if item is None:
                return 0
            if item[1] == widget.WM_APP_RECREATE:
                app.on_destroy()
            message_ref._obj.hWnd, message_ref._obj.message = item
            return 1

        app.w32.user32.GetMessageW.side_effect = get_message
        app.w32.run_message_loop.side_effect = lambda *args: widget.Win32.run_message_loop(app.w32, *args)
        self.assertEqual(app.run(), 0)
        app.log.log_exception.assert_called_once_with(error)
        self.assertEqual(app.w32.create_window.call_count, 2)
        self.assertEqual(app.hwnd, 11)
        app.w32.user32.TranslateMessage.assert_called_once()
        app.w32.user32.DispatchMessageW.assert_called_once()
        app.w32.post_quit.assert_not_called()
        self.assertIsNone(widget._APP)

    def test_exit_deadline_survives_recreate_and_system_destroy(self):
        app = self.make_owner_app()
        app.opts = app.opts._replace(exit_after=30.0, selftest_gdi=10)
        with patch.object(widget.time, "monotonic", return_value=100.0):
            app._start_timers()
        self.assertEqual(app._exit_deadline, 130.0)
        self.destroy_with_message(app)
        for now, expected_ms in ((117.0, 13000), (130.0, 10), (135.0, 10)):
            with self.subTest(now=now):
                app.w32.set_timer.reset_mock()
                with patch.object(widget.time, "monotonic", return_value=now):
                    app._recreate_window("owner changed")
                self.assertEqual(app._exit_deadline, 130.0)
                self.assertEqual(app.w32.set_timer.call_args_list, [
                    call(11, widget.TIMER_CHECK, widget.WINDOW_CHECK_MS),
                    call(11, widget.TIMER_POLL, widget.SNAPSHOT_POLL_MS),
                    call(11, widget.TIMER_THEME, widget.THEME_POLL_MS),
                    call(11, widget.TIMER_EXIT, expected_ms),
                ])
        app.w32.set_timer.reset_mock()
        app.on_destroy()
        with patch.object(widget.time, "monotonic", return_value=136.0):
            app.on_thread_message(widget.WM_APP_RECREATE)
        app.w32.set_timer.assert_any_call(11, widget.TIMER_EXIT, 10)
        self.assertEqual(app._exit_deadline, 130.0)
        self.assertEqual(app.timers, {widget.TIMER_CHECK, widget.TIMER_POLL,
                                      widget.TIMER_THEME, widget.TIMER_EXIT})

    def test_failed_exit_timer_is_not_rearmed_as_active(self):
        app = self.make_owner_app()
        app.opts = app.opts._replace(exit_after=30.0)
        app.w32.set_timer.side_effect = lambda hwnd, timer, interval: timer != widget.TIMER_EXIT
        app._start_timers()
        self.assertIsNone(app._exit_deadline)
        app.w32.set_timer.reset_mock()
        app._recreate_window("owner changed")
        self.assert_regular_timers(app)

    def test_explicit_exit_unhooks_before_destroy_and_quits_on_destroy(self):
        app = self.make_owner_app()
        order = []
        app.w32.unwatch_window_events.side_effect = lambda: order.append("unhook")

        def destroy(hwnd):
            order.append("destroy")
            app.window_proc(hwnd, widget.WM_DESTROY, 0, 0)
            return True

        app.w32.destroy_window.side_effect = destroy
        app.request_exit()
        self.assertEqual(order, ["unhook", "destroy", "unhook"])
        self.assertTrue(app.exiting)
        self.assertEqual(app.hwnd, 0)
        app.w32.post_quit.assert_called_once_with()
        app.w32.post_thread_message.assert_not_called()
        self.assertNotIn(
            call("WindowDestroyed", "window destroyed by the system; recovering via thread message"),
            app.log.log.call_args_list)


class Win32OwnerTests(unittest.TestCase):
    def make_win32(self):
        w32 = widget.Win32.__new__(widget.Win32)
        w32.user32 = Mock()
        w32.kernel32 = Mock()
        w32.hinstance = 55
        w32.kernel32.GetModuleHandleW.return_value = 55
        w32.user32.LoadCursorW.return_value = 0
        w32.user32.RegisterClassExW.return_value = 1
        return w32

    def test_owner_api_signatures_preserve_64_bit_handles_and_parameters(self):
        w32 = self.make_win32()
        w32.gdi32, w32.shell32 = Mock(), Mock()
        w32.shcore = None
        w32._declare()
        handle = ctypes.c_void_p
        expected = (
            (w32.user32.FindWindowW, [ctypes.c_wchar_p, ctypes.c_wchar_p], handle),
            (w32.user32.GetWindow, [handle, wintypes.UINT], handle),
            (w32.user32.IsWindow, [handle], ctypes.c_int),
            (w32.user32.PostThreadMessageW,
             [wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int),
            (w32.kernel32.GetCurrentThreadId, [], wintypes.DWORD),
        )
        for function, argtypes, restype in expected:
            with self.subTest(function=function):
                self.assertEqual(function.argtypes, argtypes)
                self.assertIs(function.restype, restype)

    def test_create_window_passes_owner_and_keeps_popup_and_extended_styles(self):
        w32 = self.make_win32()
        w32.user32.CreateWindowExW.return_value = 0x123456789
        for owner in (None, 0, 0x23456789A):
            with self.subTest(owner=owner):
                self.assertEqual(w32.create_window(1, 2, 3, 4, owner=owner), 0x123456789)
                w32.user32.CreateWindowExW.assert_called_with(
                    widget.WS_EX_LAYERED | widget.WS_EX_TOOLWINDOW |
                    widget.WS_EX_NOACTIVATE | widget.WS_EX_TOPMOST,
                    widget.WINDOW_CLASS, widget.WINDOW_TITLE, widget.WS_POPUP,
                    1, 2, 3, 4, owner or None, None, 55, None)

    def test_owner_queries_return_handles_or_zero_and_window_validity(self):
        w32 = self.make_win32()
        for handle in (0x123456789, None, 0):
            with self.subTest(handle=handle):
                w32.user32.FindWindowW.return_value = handle
                w32.user32.GetWindow.return_value = handle
                self.assertEqual(w32.taskbar_hwnd(), handle or 0)
                self.assertEqual(w32.window_owner(11), handle or 0)
        w32.user32.FindWindowW.assert_called_with("Shell_TrayWnd", None)
        w32.user32.GetWindow.assert_called_with(11, widget.GW_OWNER)
        self.assertEqual(widget.GW_OWNER, 4)
        for result in (0, 1):
            w32.user32.IsWindow.return_value = result
            self.assertIs(w32.is_window(11), bool(result))
        w32.user32.IsWindow.assert_called_with(11)

    def test_thread_post_targets_the_registered_window_thread(self):
        w32 = self.make_win32()
        w32.kernel32.GetCurrentThreadId.return_value = 321
        wndproc = widget.WNDPROC(lambda hwnd, msg, wparam, lparam: 0)
        w32.register_class(wndproc)
        w32.kernel32.GetCurrentThreadId.return_value = 999
        for result in (1, 0):
            w32.user32.PostThreadMessageW.return_value = result
            self.assertIs(w32.post_thread_message(widget.WM_APP_RECREATE), bool(result))
        w32.user32.PostThreadMessageW.assert_called_with(321, widget.WM_APP_RECREATE, 0, 0)
        w32.kernel32.GetCurrentThreadId.assert_called_once_with()

    def test_message_loop_handles_thread_messages_and_survives_handler_exception(self):
        w32 = self.make_win32()
        other_messages = [(11, widget.WM_APP_Z_ORDER, 0, 0),
                          (11, widget.WM_APP_RECREATE, 0, 0),
                          (None, widget.WM_TIMER, 123, 0x123456789),
                          (None, widget.WM_NULL, 0, 0),
                          (None, widget.WM_APP_Z_ORDER, 0, 0)]
        messages = iter([(None, widget.WM_APP_RECREATE, 0, 0), *other_messages,
                         (None, widget.WM_APP_RECREATE, 0, 0)])
        translated, dispatched = [], []

        def get_message(message_ref, hwnd, low, high):
            item = next(messages, None)
            if item is None:
                return 0
            msg = message_ref._obj
            msg.hWnd, msg.message, msg.wParam, msg.lParam = item
            return 1

        w32.user32.GetMessageW.side_effect = get_message
        w32.user32.TranslateMessage.side_effect = lambda ref: translated.append(
            (ref._obj.hWnd, ref._obj.message, ref._obj.wParam, ref._obj.lParam))
        w32.user32.DispatchMessageW.side_effect = lambda ref: dispatched.append(
            (ref._obj.hWnd, ref._obj.message, ref._obj.wParam, ref._obj.lParam))
        error = RuntimeError("handler failed")
        handler, log_exception = Mock(side_effect=[error, None]), Mock()
        self.assertEqual(w32.run_message_loop(handler, log_exception), 0)
        self.assertEqual(handler.call_args_list, [call(widget.WM_APP_RECREATE)] * 2)
        log_exception.assert_called_once_with(error)
        self.assertEqual(translated, other_messages)
        self.assertEqual(dispatched, translated)
        self.assertEqual(w32.user32.GetMessageW.call_count, 8)

    def test_message_loop_without_handler_dispatches_other_thread_messages_and_returns_error(self):
        w32 = self.make_win32()
        messages = iter(((None, widget.WM_APP_RECREATE, 0),
                         (None, widget.WM_TIMER, 0x123456789), (None, widget.WM_NULL, 0)))
        dispatched = []

        def get_message(message_ref, hwnd, low, high):
            item = next(messages, None)
            if item is None:
                return -1
            msg = message_ref._obj
            msg.hWnd, msg.message, msg.lParam = item
            return 1

        w32.user32.GetMessageW.side_effect = get_message
        w32.user32.DispatchMessageW.side_effect = lambda ref: dispatched.append(
            (ref._obj.hWnd, ref._obj.message, ref._obj.lParam))
        self.assertEqual(w32.run_message_loop(), -1)
        self.assertEqual(dispatched, [(None, widget.WM_TIMER, 0x123456789), (None, widget.WM_NULL, 0)])
        self.assertEqual(w32.user32.TranslateMessage.call_count, 2)

    def test_message_loop_reports_handler_errors_without_an_error_callback(self):
        w32 = self.make_win32()
        messages = iter((widget.WM_APP_RECREATE,))

        def get_message(message_ref, hwnd, low, high):
            message = next(messages, None)
            if message is None:
                return 0
            message_ref._obj.hWnd = None
            message_ref._obj.message = message
            return 1

        w32.user32.GetMessageW.side_effect = get_message
        with patch.object(widget, "say") as report:
            self.assertEqual(w32.run_message_loop(Mock(side_effect=RuntimeError("handler failed"))), 0)
        report.assert_called_once_with("thread message handler failed: RuntimeError: handler failed")


@unittest.skipUnless(os.environ.get("CLAUDE_USAGE_NATIVE_TEST") == "1",
                     "Set CLAUDE_USAGE_NATIVE_TEST=1 to run off-screen native checks")
class NativeOwnerWindowTests(unittest.TestCase):
    def test_real_message_loop_handles_recovery_once_and_dispatches_hidden_window_message(self):
        errors = []

        def run_loop():
            try:
                w32 = widget.Win32()
                hwnd = 0
                dispatched = []

                def window_proc(window, message, wparam, lparam):
                    if message == widget.WM_NULL:
                        dispatched.append(window)
                    return w32.def_window_proc(window, message, wparam, lparam)

                wndproc = widget.WNDPROC(window_proc)
                w32.register_class(wndproc)
                try:
                    hwnd = w32.create_window(-32000, -32000, 8, 8)
                    self.assertFalse(w32.user32.IsWindowVisible(hwnd))
                    self.assertTrue(w32.post_thread_message(widget.WM_APP_RECREATE))
                    self.assertTrue(w32.user32.PostMessageW(hwnd, widget.WM_NULL, 0, 0))
                    w32.post_quit()
                    handler = Mock()
                    self.assertEqual(w32.run_message_loop(handler), 0)
                    handler.assert_called_once_with(widget.WM_APP_RECREATE)
                    self.assertEqual(dispatched, [hwnd])
                    self.assertFalse(w32.user32.IsWindowVisible(hwnd))
                finally:
                    if hwnd:
                        w32.destroy_window(hwnd)
                    w32.unregister_class()
                self.assertFalse(w32.is_window(hwnd))
            except BaseException as exc:
                errors.append(exc)

        # 独立线程的队列不受其它原生用例遗留线程消息影响。
        with patch.object(widget, "WINDOW_CLASS", "ClaudeUsageMessageLoopTest"):
            thread = threading.Thread(target=run_loop, daemon=True)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "Native message loop did not stop after WM_QUIT")
        if errors:
            raise errors[0]

    def test_hidden_in_process_owner_is_reported_for_owned_popup(self):
        w32 = widget.Win32()
        owner, hwnd = 0, 0
        wndproc = widget.WNDPROC(w32.def_window_proc)
        with patch.object(widget, "WINDOW_CLASS", "ClaudeUsageOwnedWindowTest"):
            w32.register_class(wndproc)
            try:
                owner = w32.create_window(-32000, -32000, 8, 8)
                hwnd = w32.create_window(-32000, -32000, 8, 8, owner=owner)
                self.assertEqual(w32.window_owner(hwnd), owner)
                self.assertTrue(w32.is_window(owner))
                self.assertTrue(w32.is_window(hwnd))
                self.assertFalse(w32.user32.IsWindowVisible(owner))
                self.assertFalse(w32.user32.IsWindowVisible(hwnd))
            finally:
                if hwnd:
                    w32.destroy_window(hwnd)
                if owner:
                    w32.destroy_window(owner)
                w32.unregister_class()
        self.assertFalse(w32.is_window(hwnd))
        self.assertFalse(w32.is_window(owner))


if __name__ == "__main__":
    unittest.main()
