"""Regression tests for taskbar occlusion, without touching desktop windows."""

import ctypes
import pathlib
import sys
import unittest
from ctypes import wintypes
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

OWN_PID = 4321
FOREIGN_PID = 1234
TASKBAR_RECT = (0, 1032, 1920, 1080)
MONITOR_RECT = (0, 0, 1920, 1080)


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
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(
            widget.Options(".", None, None, None, no_codex=True), w32, Mock())
    app.hwnd = 10
    app.visible = True
    app._apply = Mock()
    return app


class WindowEventTests(unittest.TestCase):
    def test_foreground_change_restores_without_timer(self):
        app = make_app()
        app.on_window_event(widget.EVENT_SYSTEM_FOREGROUND, 20, 0, 0)
        app.w32.keep_topmost.assert_not_called()
        app.w32.post_z_order_check.assert_called_once_with(app.hwnd)
        self.assertEqual(app._dispatch(widget.WM_APP_Z_ORDER, 0), (True, 0))
        app.w32.keep_topmost.assert_called_once_with(app.hwnd)

    def test_repeated_reorders_restore_without_foreground_change(self):
        app = make_app()
        for _ in range(3):
            app.on_window_event(widget.EVENT_OBJECT_REORDER, 40, widget.OBJID_CLIENT, 0)
            app.on_z_order_check()
        self.assertEqual(app.w32.keep_topmost.call_count, 3)

    def test_burst_coalesces_and_own_reorder_does_not_raise_again(self):
        app = make_app()
        for _ in range(20):
            app.on_window_event(widget.EVENT_OBJECT_REORDER, 20, widget.OBJID_WINDOW, 0)
        app.w32.post_z_order_check.assert_called_once()
        app.on_z_order_check()
        app.w32.taskbar_above.return_value = False
        app.on_window_event(widget.EVENT_OBJECT_REORDER, 40, widget.OBJID_CLIENT, 0)
        app.on_z_order_check()
        app.w32.keep_topmost.assert_called_once()

    def test_unrelated_controls_and_null_windows_are_ignored(self):
        app = make_app()
        for args in ((widget.EVENT_OBJECT_REORDER, 20, widget.OBJID_CLIENT, 0),
                     (widget.EVENT_OBJECT_REORDER, 40, widget.OBJID_CLIENT, 1),
                     (widget.EVENT_OBJECT_REORDER, 20, -7, 0),
                     (widget.EVENT_SYSTEM_FOREGROUND, 0, 0, 0),
                     (999, 20, 0, 0)):
            app.on_window_event(*args)
        app.w32.post_z_order_check.assert_not_called()

    def test_failed_post_can_retry(self):
        app = make_app()
        app.w32.post_z_order_check.side_effect = [False, True]
        app.on_window_event(widget.EVENT_SYSTEM_FOREGROUND, 20, 0, 0)
        self.assertFalse(app._z_order_pending)
        app.on_window_event(widget.EVENT_SYSTEM_FOREGROUND, 20, 0, 0)
        self.assertTrue(app._z_order_pending)
        self.assertEqual(app.w32.post_z_order_check.call_count, 2)

    def test_menu_drag_fullscreen_and_autohide_guards(self):
        for mode in ("menu", "drag", "fullscreen", "autohide", "shell_popup"):
            with self.subTest(mode=mode):
                app = make_app()
                if mode == "menu":
                    app.menu_open = True
                elif mode == "drag":
                    app.drag = {"moved": False}
                elif mode == "fullscreen":
                    app.w32.notification_state.return_value = 3
                elif mode == "autohide":
                    app.w32.taskbar_autohide.return_value = True
                else:
                    app.w32.class_name.return_value = "NotifyIconOverflowWindow"
                    app.w32.topmost_allowed.return_value = False
                app.on_z_order_check()
                app.w32.keep_topmost.assert_not_called()
                if mode in ("fullscreen", "autohide"):
                    app.w32.show_window.assert_called_once_with(app.hwnd, widget.SW_HIDE)

    def test_hidden_or_exiting_widget_does_not_raise(self):
        for attribute, value in (("visible", False), ("exiting", True), ("hwnd", 0)):
            with self.subTest(attribute=attribute):
                app = make_app()
                setattr(app, attribute, value)
                app.on_z_order_check()
                app.w32.keep_topmost.assert_not_called()

    def test_busy_state_requires_actual_foreground_taskbar_coverage(self):
        for covers in (False, True):
            with self.subTest(covers=covers):
                app = make_app()
                app.w32.notification_state.return_value = widget.BUSY_NOTIFICATION_STATE
                app.w32.foreground_covers_taskbar.return_value = covers
                app.refresh_view(reassert_topmost=True)
                if covers:
                    app.w32.show_window.assert_called_once_with(app.hwnd, widget.SW_HIDE)
                    app.w32.keep_topmost.assert_not_called()
                else:
                    app.w32.show_window.assert_not_called()
                    app.w32.keep_topmost.assert_called_once_with(app.hwnd)

    def test_reentrant_refresh_preserves_topmost_request(self):
        app = make_app()
        calls = []

        def refresh_once(force, reassert):
            calls.append(reassert)
            if len(calls) == 1:
                app.refresh_view(reassert_topmost=True)

        app._refresh_once = refresh_once
        app.refresh_view()
        self.assertEqual(calls, [False, True])
        self.assertFalse(app._reassert_pending)

    def test_hook_failure_keeps_polling_fallback(self):
        app = make_app()
        app.w32.watch_window_events.side_effect = OSError("hook unavailable")
        app.w32.run_message_loop.return_value = 0
        app._start_timers = Mock()
        self.assertEqual(app.run(), 0)
        app._start_timers.assert_called_once()
        app.w32.unwatch_window_events.assert_called_once()
        app.w32.keep_topmost.reset_mock()
        app.on_timer(widget.TIMER_CHECK)
        app.w32.keep_topmost.assert_called_once()

    def test_exit_unhooks_before_destroy_and_ignores_queued_events(self):
        app = make_app()
        app._arm_watchdog = Mock()
        order = []
        app.w32.unwatch_window_events.side_effect = lambda: order.append("unhook")
        app.w32.destroy_window.side_effect = lambda hwnd: order.append("destroy") or True
        app.request_exit()
        app.on_window_event(widget.EVENT_SYSTEM_FOREGROUND, 20, 0, 0)
        app.on_z_order_check()
        self.assertEqual(order, ["unhook", "destroy"])
        app.w32.post_z_order_check.assert_not_called()
        app.w32.keep_topmost.assert_not_called()


class Win32EventTests(unittest.TestCase):
    def make_win32(self):
        w32 = widget.Win32.__new__(widget.Win32)
        w32.user32 = Mock()
        w32._event_hooks = []
        w32._event_callback = None
        w32.class_name = Mock(return_value="")
        w32.window_rect = Mock(return_value=(0, 100, 100, 140))
        return w32

    def make_foreground_win32(self, foreground_class="", rect=None, pid=FOREIGN_PID,
                              monitor=MONITOR_RECT):
        """前台是 hwnd 30；类名、矩形、进程号与任务栏所在显示器的矩形都用替身。"""
        w32 = self.make_win32()
        w32.kernel32 = Mock()
        w32.kernel32.GetCurrentProcessId.return_value = OWN_PID
        w32.foreground_hwnd = Mock(return_value=30)
        w32.class_name.return_value = foreground_class
        w32.window_rect.return_value = rect
        w32.window_pid = Mock(return_value=pid)
        w32.monitor_rect_for = Mock(return_value=monitor)
        return w32

    def test_partial_registration_failure_removes_first_hook(self):
        w32 = self.make_win32()
        w32.user32.SetWinEventHook.side_effect = [100, 0]
        w32.user32.UnhookWinEvent.return_value = True
        with self.assertRaises(OSError):
            w32.watch_window_events(Mock())
        w32.user32.UnhookWinEvent.assert_called_once_with(100)
        self.assertEqual(w32._event_hooks, [])
        self.assertIsNone(w32._event_callback)

    def test_callback_retained_until_unhook_succeeds(self):
        w32 = self.make_win32()
        w32.user32.SetWinEventHook.side_effect = [100, 101]
        w32.watch_window_events(Mock())
        callback = w32._event_callback
        w32.user32.UnhookWinEvent.side_effect = [False, True, True]
        w32.unwatch_window_events()
        self.assertEqual(w32._event_hooks, [100])
        self.assertIs(w32._event_callback, callback)
        w32.unwatch_window_events()
        self.assertIsNone(w32._event_callback)

    def test_taskbar_z_order_and_cycle_detection(self):
        for chain, expected in (({10: 30, 30: 20, 20: 0}, True),
                                ({10: 30, 30: 0}, False),
                                ({10: 30, 30: 10}, False)):
            with self.subTest(chain=chain):
                w32 = self.make_win32()
                w32.user32.FindWindowW.return_value = 20
                w32.user32.GetWindow.side_effect = lambda hwnd, flag: chain[hwnd]
                self.assertEqual(w32.taskbar_above(10), expected)

    def test_nonactivating_shell_popup_above_taskbar_prevents_raise(self):
        w32 = self.make_win32()
        w32.user32.FindWindowW.return_value = 20
        w32.user32.IsWindowVisible.return_value = True
        chain = {10: 20, 20: 30, 30: 0}
        w32.user32.GetWindow.side_effect = lambda hwnd, flag: chain[hwnd]
        w32.class_name.return_value = "NotifyIconOverflowWindow"
        self.assertFalse(w32.taskbar_above(10))
        self.assertFalse(w32.topmost_allowed(10))

    def test_non_overlapping_shell_panels_allow_normal_z_order_restore(self):
        for popup_class in ("Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"):
            with self.subTest(popup_class=popup_class):
                w32 = self.make_win32()
                w32.user32.FindWindowW.return_value = 20
                w32.user32.IsWindowVisible.return_value = True
                chain = {10: 20, 20: 30, 30: 0}
                w32.user32.GetWindow.side_effect = lambda hwnd, flag: chain[hwnd]
                w32.class_name.return_value = popup_class
                rectangles = {10: (800, 1036, 1000, 1076), 30: (0, 100, 1920, 1032)}
                w32.window_rect.side_effect = rectangles.__getitem__
                self.assertTrue(w32.taskbar_above(10))
                self.assertTrue(w32.topmost_allowed(10))

    def test_foreground_coverage_distinguishes_maximized_and_fullscreen(self):
        w32 = self.make_foreground_win32()
        for rect, expected in (((-8, -8, 1928, 1040), False),
                               ((560, 282, 1360, 1032), False),
                               ((0, 0, 1920, 1080), True),
                               ((1920, 0, 3840, 1080), False),
                               (None, False)):
            with self.subTest(rect=rect):
                w32.window_rect.return_value = rect
                self.assertEqual(w32.foreground_covers_taskbar(TASKBAR_RECT), expected)

    def test_taskbar_and_desktop_foreground_never_count_as_fullscreen(self):
        self.assertEqual(widget.SHELL_SURFACE_CLASSES,
                         frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                                    "Progman", "WorkerW"}))
        # 每组 (矩形, 显示器矩形) 下，普通应用窗口的结果见第三项；外壳窗口无论几何如何
        # 都必须不算。前三组是普通窗口会被判成全屏的几何（点桌面、点跨显示器的整块
        # 桌面、查不到显示器时点任务栏），最后一组是显示器已知时点任务栏。
        for rect, monitor, ordinary_expected in ((MONITOR_RECT, MONITOR_RECT, True),
                                                 ((0, 0, 3840, 1080), MONITOR_RECT, True),
                                                 (TASKBAR_RECT, None, True),
                                                 (TASKBAR_RECT, MONITOR_RECT, False)):
            with self.subTest(foreground_class="ordinary", rect=rect, monitor=monitor):
                ordinary = self.make_foreground_win32("", rect, monitor=monitor)
                self.assertEqual(ordinary.foreground_covers_taskbar(TASKBAR_RECT),
                                 ordinary_expected)
            for foreground_class in sorted(widget.SHELL_SURFACE_CLASSES):
                with self.subTest(foreground_class=foreground_class, rect=rect, monitor=monitor):
                    shell = self.make_foreground_win32(foreground_class, rect, monitor=monitor)
                    self.assertFalse(shell.foreground_covers_taskbar(TASKBAR_RECT))

    def test_own_process_foreground_is_not_a_fullscreen_app(self):
        # 进程号取不到（0）时按外来窗口处理，由矩形决定
        for pid, expected in ((OWN_PID, False), (FOREIGN_PID, True), (0, True)):
            with self.subTest(pid=pid):
                w32 = self.make_foreground_win32(rect=MONITOR_RECT, pid=pid)
                self.assertEqual(w32.foreground_covers_taskbar(TASKBAR_RECT), expected)
                w32.window_pid.assert_called_with(30)

    def test_ordinary_window_must_fill_the_taskbar_monitor(self):
        w32 = self.make_foreground_win32()
        for rect, expected in ((MONITOR_RECT, True),
                               ((-1, -1, 1921, 1081), True),
                               ((0, 1000, 1920, 1080), False),
                               ((0, 0, 1920, 1079), False)):
            with self.subTest(rect=rect):
                w32.window_rect.return_value = rect
                self.assertEqual(w32.foreground_covers_taskbar(TASKBAR_RECT), expected)
        w32.monitor_rect_for.assert_called_with(TASKBAR_RECT)

    def test_unknown_monitor_falls_back_to_taskbar_rectangle(self):
        w32 = self.make_foreground_win32(monitor=None)
        for rect, expected in (((0, 1000, 1920, 1080), True),
                               (MONITOR_RECT, True),
                               ((0, 1040, 1920, 1080), False),
                               ((-8, -8, 1928, 1040), False),
                               ((1920, 0, 3840, 1080), False)):
            with self.subTest(rect=rect):
                w32.window_rect.return_value = rect
                self.assertEqual(w32.foreground_covers_taskbar(TASKBAR_RECT), expected)
        w32.monitor_rect_for.assert_called_with(TASKBAR_RECT)

    def test_no_foreground_window_is_not_fullscreen(self):
        w32 = self.make_foreground_win32(rect=MONITOR_RECT)
        w32.foreground_hwnd.return_value = 0
        self.assertFalse(w32.foreground_covers_taskbar(TASKBAR_RECT))
        w32.window_pid.assert_not_called()
        w32.window_rect.assert_not_called()

    def test_busy_state_keeps_widget_visible_when_taskbar_or_desktop_is_foreground(self):
        # 回归：忙碌状态下用户点任务栏空白处或桌面，前台成了外壳窗口，挂件不能因此隐藏。
        # 前台判定走真实的 Win32.foreground_covers_taskbar，只把底层查询换成替身。
        taskbar = (0, 100, 1000, 140)
        monitor = (0, 0, 1000, 140)
        for foreground_class, rect in (("Shell_TrayWnd", taskbar),
                                       ("Progman", monitor),
                                       ("WorkerW", monitor)):
            with self.subTest(foreground_class=foreground_class):
                app = make_app()
                app.w32.taskbar_pos.return_value = (taskbar, widget.ABE_BOTTOM)
                app.w32.notification_state.return_value = widget.BUSY_NOTIFICATION_STATE
                w32 = self.make_foreground_win32(foreground_class, rect, monitor=monitor)
                app.w32.foreground_covers_taskbar = w32.foreground_covers_taskbar
                app.refresh_view(reassert_topmost=True)
                app.w32.show_window.assert_not_called()
                self.assertTrue(app.visible)
                app.w32.keep_topmost.assert_called_once_with(app.hwnd)

    def test_monitor_rect_for_returns_the_full_monitor_rectangle(self):
        w32 = self.make_win32()
        seen = {}

        def get_monitor_info(monitor, info_ref):
            info = info_ref._obj
            seen["monitor"] = monitor
            seen["cb_size"] = info.cbSize
            info.rcMonitor = wintypes.RECT(*MONITOR_RECT)
            info.rcWork = wintypes.RECT(0, 0, 1920, 1032)
            return 1

        w32.user32.MonitorFromRect.return_value = 77
        w32.user32.GetMonitorInfoW.side_effect = get_monitor_info
        # 返回 rcMonitor 而不是扣掉任务栏的 rcWork
        self.assertEqual(w32.monitor_rect_for(TASKBAR_RECT), MONITOR_RECT)
        query_ref, flags = w32.user32.MonitorFromRect.call_args.args
        query = query_ref._obj
        self.assertEqual((query.left, query.top, query.right, query.bottom), TASKBAR_RECT)
        self.assertEqual(flags, widget.MONITOR_DEFAULTTONULL)
        self.assertEqual(widget.MONITOR_DEFAULTTONULL, 0)
        self.assertEqual(ctypes.sizeof(widget.MONITORINFO), 40)
        self.assertEqual(seen, {"monitor": 77, "cb_size": ctypes.sizeof(widget.MONITORINFO)})

    def test_monitor_rect_for_reports_failure_as_none(self):
        for monitor in (0, None):
            with self.subTest(monitor=monitor):
                w32 = self.make_win32()
                w32.user32.MonitorFromRect.return_value = monitor
                self.assertIsNone(w32.monitor_rect_for(TASKBAR_RECT))
                w32.user32.GetMonitorInfoW.assert_not_called()
        with self.subTest(monitor_info="failed"):
            w32 = self.make_win32()
            w32.user32.MonitorFromRect.return_value = 77
            w32.user32.GetMonitorInfoW.return_value = 0
            self.assertIsNone(w32.monitor_rect_for(TASKBAR_RECT))
            w32.user32.GetMonitorInfoW.assert_called_once()

    def test_window_pid_reads_process_id_and_reports_failure_as_zero(self):
        w32 = self.make_win32()

        def get_thread_and_pid(hwnd, pid_ref):
            pid_ref._obj.value = FOREIGN_PID
            return 99

        w32.user32.GetWindowThreadProcessId.side_effect = get_thread_and_pid
        self.assertEqual(w32.window_pid(30), FOREIGN_PID)
        w32.user32.GetWindowThreadProcessId.assert_called_once()
        self.assertEqual(w32.user32.GetWindowThreadProcessId.call_args.args[0], 30)

        # 线程号为 0 表示失败，此时 pid 的内容不可信
        def fail_after_writing_pid(hwnd, pid_ref):
            pid_ref._obj.value = FOREIGN_PID
            return 0

        w32.user32.GetWindowThreadProcessId.side_effect = fail_after_writing_pid
        self.assertEqual(w32.window_pid(30), 0)
        w32.user32.GetWindowThreadProcessId.reset_mock()
        self.assertEqual(w32.window_pid(0), 0)
        w32.user32.GetWindowThreadProcessId.assert_not_called()

    def test_new_win32_calls_are_declared_with_explicit_signatures(self):
        # 64 位下不声明 argtypes 与 restype，句柄会按 32 位 int 截断
        w32 = widget.Win32.__new__(widget.Win32)
        w32.user32, w32.gdi32, w32.shell32, w32.kernel32 = Mock(), Mock(), Mock(), Mock()
        w32.shcore = None
        w32._declare()
        handle = ctypes.c_void_p
        expected = {
            "user32.GetWindowThreadProcessId": (
                w32.user32.GetWindowThreadProcessId,
                [handle, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "user32.MonitorFromRect": (
                w32.user32.MonitorFromRect,
                [ctypes.POINTER(wintypes.RECT), wintypes.DWORD], handle),
            "user32.GetMonitorInfoW": (
                w32.user32.GetMonitorInfoW,
                [handle, ctypes.POINTER(widget.MONITORINFO)], ctypes.c_int),
            "kernel32.GetCurrentProcessId": (
                w32.kernel32.GetCurrentProcessId, [], wintypes.DWORD),
        }
        for name, (func, argtypes, restype) in expected.items():
            with self.subTest(function=name):
                self.assertEqual(func.argtypes, argtypes)
                self.assertIs(func.restype, restype)

    def test_popup_bounds_and_missing_rectangles(self):
        w32 = self.make_win32()
        w32.user32.IsWindowVisible.return_value = True
        w32.class_name.return_value = "XamlExplorerHostIslandWindow"
        for popup_rect, expected in (((0, 0, 100, 100), False),
                                     ((0, 0, 100, 101), True),
                                     ((200, 100, 300, 140), False),
                                     (None, True)):
            with self.subTest(popup_rect=popup_rect):
                w32.window_rect.side_effect = lambda hwnd: popup_rect if hwnd == 30 else (0, 100, 100, 140)
                self.assertEqual(w32.shell_popup_overlaps(30, 10), expected)


if __name__ == "__main__":
    unittest.main()
