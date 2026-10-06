"""Codex refresh ping: pure helpers, CodexPinger, WidgetApp wiring, options and source guards."""

import ast
import glob
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget


class NoRealProcessTestCase(unittest.TestCase):
    """Fails loudly if any test path reaches the real subprocess.Popen.

    The patched Popen raises, but application callbacks catch Exception and would swallow
    that error, so a cleanup also asserts the patched Popen was never called.
    """

    def setUp(self):
        patcher = patch.object(
            subprocess, "Popen",
            side_effect=AssertionError("real subprocess.Popen must not run in tests"))
        self.popen_guard = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._assert_popen_untouched)

    def _assert_popen_untouched(self):
        self.assertFalse(self.popen_guard.called, "real subprocess.Popen was reached")


def at(hour, minute):
    return time.mktime((2026, 10, 2, hour, minute, 0, 0, 0, -1))


APP_NOW = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))
OBS_OLD = at(14, 0)
OBS_NEW = at(14, 58)


def data_at(observed):
    """A snapshot whose two windows were both observed at this time and have not reset."""
    return {
        "five_hour": widget.Win(30.0, APP_NOW + 7200.0, observed),
        "seven_day": widget.Win(10.0, APP_NOW + 2 * 86400.0, observed),
    }


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


class FakePinger:
    """Stands in for CodexPinger. Never touches a process."""

    def __init__(self):
        self.running = False
        self.start_result = "started"
        self.poll_results = []
        self.poll_error = None
        self.stop_error = None
        self.start_calls = 0
        self.poll_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        if self.start_result == "started":
            self.running = True
        return self.start_result

    def poll(self):
        self.poll_calls += 1
        if self.poll_error is not None:
            raise self.poll_error
        if self.poll_results:
            result = self.poll_results.pop(0)
            if result is not None:
                self.running = False
            return result
        return None

    def stop(self):
        self.stop_calls += 1
        self.running = False
        if self.stop_error is not None:
            raise self.stop_error


def make_app(claude=None, codex=None, pinger=True, options=None):
    """Widget with fake readers. pinger=True installs FakePinger; False leaves it unset."""
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
    if options is None:
        options = widget.Options(
            ".", None, None, None, codex_home=os.path.join("nowhere", "codex-home"))
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(options, w32, Mock())
    app.hwnd = 10
    app.visible = True
    app._apply = Mock()
    app.reader = FakeReader(claude, "missing" if claude is None else "")
    app.codex = FakeReader(codex, "" if codex else "no_rate_limits", available=True)
    app.pinger = FakePinger() if pinger else None
    return app


def click_refresh(app, now=APP_NOW):
    app.w32.track_menu.return_value = widget.MENU_REFRESH
    with patch.object(widget.time, "time", return_value=now):
        app.on_right_up()


def last_display(app):
    return app._apply.call_args_list[-1][0][0]


def _menu_swaps_in_newer_codex(app):
    """Menu loop stand-in: a poll during the menu replaces the Codex snapshot."""

    def menu(hwnd, x, y):
        app.codex.snapshot = data_at(OBS_NEW)
        return widget.MENU_REFRESH

    app.w32.track_menu.side_effect = menu


class NoRealProcessGuardTests(NoRealProcessTestCase):
    def test_guard_records_a_call_even_when_the_caller_swallows_the_error(self):
        try:
            subprocess.Popen(["ignored"])
        except Exception:
            pass
        self.assertTrue(self.popen_guard.called)
        self.popen_guard.reset_mock()


class PingerCreationTests(NoRealProcessTestCase):
    def _construct(self, options):
        with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
                patch.object(widget, "read_light_theme", return_value=False):
            return widget.WidgetApp(options, Mock(), Mock())

    def test_no_codex_builds_neither_reader_nor_pinger(self):
        app = self._construct(widget.Options(".", None, None, None, no_codex=True))
        self.assertIsNone(app.codex)
        self.assertIsNone(app.pinger)

    def test_no_codex_ping_keeps_the_reader_only(self):
        app = self._construct(widget.Options(".", None, None, None, no_codex_ping=True))
        self.assertIsInstance(app.codex, widget.CodexReader)
        self.assertIsNone(app.pinger)

    def test_default_options_build_a_pinger_with_the_default_model(self):
        app = self._construct(widget.Options(".", None, None, None))
        self.assertIsInstance(app.pinger, widget.CodexPinger)
        self.assertEqual(app.pinger.data_dir, ".")
        self.assertEqual(app.pinger.model, widget.CODEX_PING_MODEL)
        self.assertIsNone(app.pinger.exe_override)
        self.assertIsNone(app.pinger.codex_home)
        self.assertIsNone(app._ping_before)

    def test_explicit_home_binary_and_model_are_stored(self):
        home = os.path.abspath(os.path.join("nowhere", "codex-home"))
        binary = os.path.abspath(os.path.join("nowhere", "codex.exe"))
        app = self._construct(widget.Options(
            ".", None, None, None,
            codex_home=home, codex_bin=binary, codex_ping_model="gpt-x"))
        self.assertEqual(app.pinger.model, "gpt-x")
        self.assertEqual(app.pinger.exe_override, binary)
        self.assertEqual(app.pinger.codex_home, home)


class RefreshNowPingTests(NoRealProcessTestCase):
    def test_start_sees_the_codex_observation_from_menu_open(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        _menu_swaps_in_newer_codex(app)
        click_refresh(app)
        self.assertEqual(app.pinger.start_calls, 1)
        self.assertEqual(app._ping_before, OBS_OLD)

    def test_refresh_passes_the_menu_open_observation(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        _menu_swaps_in_newer_codex(app)
        with patch.object(app, "_start_codex_ping") as start:
            click_refresh(app)
        start.assert_called_once_with(OBS_OLD)

    def test_refresh_now_orders_poll_then_ping_then_flash(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        parent = Mock()
        app.poll_snapshot = parent.poll_snapshot
        app._start_codex_ping = parent._start_codex_ping
        app._begin_flash = parent._begin_flash
        app._refresh_now((OBS_OLD, OBS_OLD))
        self.assertEqual(
            [item[0] for item in parent.mock_calls],
            ["poll_snapshot", "_start_codex_ping", "_begin_flash"])

    def test_started_arms_the_ping_timer_and_shows_asking(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.reader.queued = (data_at(OBS_NEW), "")
        click_refresh(app)
        self.assertIn(widget.TIMER_PING, app.timers)
        self.assertIn(
            call(10, widget.TIMER_PING, widget.CODEX_PING_POLL_MS),
            app.w32.set_timer.call_args_list)
        self.assertEqual(last_display(app).captions, ("new 14:58", "asking"))

    def test_asking_stays_up_while_the_ping_is_running(self):
        app = make_app(claude=data_at(OBS_NEW), codex=data_at(OBS_NEW))
        app.pinger.running = True

        def redraw():
            with patch.object(widget.time, "time", return_value=APP_NOW):
                app._refresh_once(False, False)

        with self.subTest(name="no caption"):
            app._caption = None
            redraw()
            self.assertEqual(last_display(app).captions, ("", "asking"))
        with self.subTest(name="expired caption"):
            app._caption = (APP_NOW - 1.0, ("new 14:58", "same 14:00"))
            redraw()
            self.assertEqual(last_display(app).captions, ("", "asking"))
            self.assertIsNone(app._caption)
        with self.subTest(name="live caption"):
            app._caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
            redraw()
            self.assertEqual(last_display(app).captions, ("new 14:58", "asking"))
        with self.subTest(name="not running"):
            app.pinger.running = False
            app._caption = None
            redraw()
            self.assertEqual(last_display(app).captions, ())
        with self.subTest(name="caption timer"):
            app.pinger.running = True
            app._caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
            app.on_timer(widget.TIMER_CAPTION)
            self.assertIsNone(app._caption)
            self.assertEqual(last_display(app).captions, ("", "asking"))

        bare = make_app(claude=data_at(OBS_NEW), codex=data_at(OBS_NEW), pinger=False)
        with patch.object(widget.time, "time", return_value=APP_NOW):
            bare._refresh_once(False, False)
        self.assertNotIn("asking", last_display(bare).captions)

    def test_cooldown_rewrites_only_the_codex_text(self):
        deadline = APP_NOW + 100.0
        cases = (
            ("same 14:00", "cooldown"),
            ("no data", "cooldown"),
            ("", "cooldown"),
            ("new 14:59", "new 14:59"),
            ("read error", "read error"),
        )
        for original, expected in cases:
            with self.subTest(original=original):
                app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
                app.pinger.start_result = "cooldown"
                app._caption = (deadline, ("new 14:58", original))
                app.refresh_view = Mock()
                app._start_codex_ping(OBS_OLD)
                self.assertEqual(app._caption, (deadline, ("new 14:58", expected)))
                app.refresh_view.assert_called_once_with()
                ping_arms = [
                    item for item in app.w32.set_timer.call_args_list
                    if len(item.args) >= 2 and item.args[1] == widget.TIMER_PING]
                self.assertEqual(ping_arms, [])

    def test_cooldown_without_a_caption_still_redraws(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.start_result = "cooldown"
        app.refresh_view = Mock()
        app._start_codex_ping(OBS_OLD)
        self.assertIsNone(app._caption)
        app.refresh_view.assert_called_once_with()

    def test_no_exe_logs_and_leaves_the_caption(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.start_result = "no_exe"
        caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
        app._caption = caption
        app.refresh_view = Mock()
        app._start_codex_ping(OBS_OLD)
        self.assertEqual(app._caption, caption)
        app.log.log.assert_called_with("CodexPing", "codex.exe not found; ping skipped")
        app.refresh_view.assert_not_called()
        ping_arms = [
            item for item in app.w32.set_timer.call_args_list
            if len(item.args) >= 2 and item.args[1] == widget.TIMER_PING]
        self.assertEqual(ping_arms, [])
        self.assertIsNone(app._ping_before)

    def test_spawn_error_marks_ping_failed(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.start_result = "spawn_error:OSError"
        deadline = APP_NOW + 100.0
        app._caption = (deadline, ("new 14:58", "same 14:00"))
        app.refresh_view = Mock()
        app._start_codex_ping(OBS_OLD)
        app.log.log.assert_called_with("CodexPing", "spawn_error:OSError")
        self.assertEqual(app._caption, (deadline, ("new 14:58", "ping failed")))
        app.refresh_view.assert_called_once_with()

        bare = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        bare.pinger.start_result = "spawn_error:OSError"
        bare.refresh_view = Mock()
        bare._start_codex_ping(OBS_OLD)
        self.assertIsNone(bare._caption)

    def test_running_result_changes_nothing(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.start_result = "running"
        caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
        app._caption = caption
        app._ping_before = OBS_OLD
        app.refresh_view = Mock()
        app._start_codex_ping(OBS_OLD)
        self.assertEqual(app._caption, caption)
        app.refresh_view.assert_not_called()
        ping_arms = [
            item for item in app.w32.set_timer.call_args_list
            if len(item.args) >= 2 and item.args[1] == widget.TIMER_PING]
        self.assertEqual(ping_arms, [])
        self.assertEqual(app._ping_before, OBS_OLD)

    def test_start_is_skipped_when_ping_cannot_run(self):
        cases = ("unavailable", "no pinger", "no codex", "exiting", "no window")
        for name in cases:
            with self.subTest(name=name):
                app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
                app.refresh_view = Mock()
                app.w32.set_timer.reset_mock()
                if name == "unavailable":
                    app.codex.available = False
                elif name == "no pinger":
                    app.pinger = None
                elif name == "no codex":
                    app.codex = None
                elif name == "exiting":
                    app.exiting = True
                else:
                    app.hwnd = 0
                app._start_codex_ping(OBS_OLD)
                if app.pinger is not None:
                    self.assertEqual(app.pinger.start_calls, 0)
                app.refresh_view.assert_not_called()
                app.w32.set_timer.assert_not_called()

    def test_failed_ping_timer_stops_the_process(self):
        deadline = APP_NOW + 100.0
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.w32.set_timer.return_value = False
        app._caption = (deadline, ("new 14:58", "same 14:00"))
        app.refresh_view = Mock()
        app._start_codex_ping(OBS_OLD)
        self.assertEqual(app.pinger.stop_calls, 1)
        self.assertFalse(app.pinger.running)
        self.assertIsNone(app._ping_before)
        self.assertNotIn(widget.TIMER_PING, app.timers)
        self.assertEqual(app._caption, (deadline, ("new 14:58", "ping failed")))
        app.log.log.assert_any_call("CodexPing", "poll timer unavailable; ping abandoned")
        app.refresh_view.assert_called_once_with()

        bare = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        bare.w32.set_timer.return_value = False
        bare.refresh_view = Mock()
        bare._start_codex_ping(OBS_OLD)
        self.assertIsNone(bare._caption)
        self.assertEqual(bare.pinger.stop_calls, 1)
        self.assertIsNone(bare._ping_before)

    def test_start_error_still_flashes(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app._start_codex_ping = Mock(side_effect=RuntimeError("boom"))
        click_refresh(app)
        self.assertTrue(app._flashing)
        app.log.log_exception.assert_called()

    def test_refresh_without_a_pinger_keeps_the_original_timer_calls(self):
        app = make_app(claude=data_at(OBS_OLD), pinger=False)
        app.reader.queued = (data_at(OBS_NEW), "")
        click_refresh(app)
        self.assertEqual(app.w32.set_timer.call_args_list, [
            call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
            call(10, widget.TIMER_FLASH, widget.FLASH_MS),
        ])


class PollPathNeverPingsTests(NoRealProcessTestCase):
    def test_poll_paths_never_start_a_ping(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.poll_snapshot()
        app.poll_snapshot(force=True)
        app.on_timer(widget.TIMER_POLL)
        app.on_timer(widget.TIMER_CHECK)
        self.assertEqual(app.pinger.start_calls, 0)
        self.assertNotIn(widget.TIMER_PING, app.timers)


def _arm_ping_timer(app):
    """Put TIMER_PING where kill_timer will actually see it, then forget the arming call."""
    app.set_timer(widget.TIMER_PING, widget.CODEX_PING_POLL_MS)
    app.w32.set_timer.reset_mock()
    app.w32.kill_timer.reset_mock()


def _ping_timer_calls(app):
    return [
        item for item in app.w32.set_timer.call_args_list
        if len(item.args) >= 2 and item.args[1] == widget.TIMER_PING]


class PingTimerTests(NoRealProcessTestCase):
    def test_running_poll_keeps_the_timer(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.running = True
        app.pinger.poll_results = [None]
        _arm_ping_timer(app)
        caption = app._caption
        app.refresh_view = Mock()
        app._on_ping_timer()
        app.w32.kill_timer.assert_not_called()
        self.assertIn(widget.TIMER_PING, app.timers)
        self.assertEqual(app.codex.calls, [])
        self.assertIs(app._caption, caption)
        app.refresh_view.assert_not_called()

    def test_idle_poll_kills_the_timer(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.running = False
        app.pinger.poll_results = [None]
        _arm_ping_timer(app)
        app._on_ping_timer()
        app.w32.kill_timer.assert_called_with(10, widget.TIMER_PING)
        self.assertNotIn(widget.TIMER_PING, app.timers)

    def test_ok_reads_the_log_and_arms_a_caption(self):
        # (name, ping_before, Codex snapshot when the timer fires, queued read, expected text)
        cases = (
            ("new", OBS_OLD, data_at(OBS_OLD), (data_at(OBS_NEW), ""), "new 14:58"),
            ("same", OBS_OLD, data_at(OBS_OLD), (data_at(OBS_OLD), ""), "same 14:00"),
            ("no data", OBS_OLD, data_at(OBS_OLD), (None, "no_rate_limits"), "no data"),
            ("read error", OBS_OLD, data_at(OBS_OLD), (data_at(OBS_OLD), "bad_json"), "read error"),
            # A poll between the click and the ping timer may already have read the new log.
            # The comparison must use the observation recorded at click time, not the one at timer time.
            ("already read", OBS_OLD, data_at(OBS_NEW), None, "new 14:58"),
            ("already read, refresh repeats it", OBS_OLD, data_at(OBS_NEW),
             (data_at(OBS_NEW), ""), "new 14:58"),
            # Nothing was observed at click time: whatever is read afterwards is new.
            ("no earlier observation", None, data_at(OBS_OLD), (data_at(OBS_OLD), ""), "new 14:00"),
        )
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        for name, before, current, queued, text in cases:
            with self.subTest(name=name):
                app = make_app(claude=data_at(OBS_OLD), codex=current)
                app.pinger.poll_results = [("ok", "exit:0")]
                app._ping_before = before
                _arm_ping_timer(app)
                app.refresh_view = Mock()
                app.codex.queued = queued
                with patch.object(widget.time, "time", return_value=APP_NOW):
                    app._on_ping_timer()
                self.assertEqual(app.codex.calls, [True])
                app.w32.kill_timer.assert_called_with(10, widget.TIMER_PING)
                self.assertNotIn(widget.TIMER_PING, app.timers)
                self.assertEqual(app._caption, (deadline, ("", text)))
                self.assertIn(
                    call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
                    app.w32.set_timer.call_args_list)
                self.assertIn(widget.TIMER_CAPTION, app.timers)
                self.assertIsNone(app._ping_before)
                app.refresh_view.assert_called_once_with()
                ping_logs = [
                    item for item in app.log.log.call_args_list
                    if item.args and item.args[0] == "CodexPing"]
                self.assertEqual(ping_logs, [])

    def test_ok_redraws_the_new_caption_without_asking(self):
        app = make_app(claude=data_at(OBS_NEW), codex=data_at(OBS_OLD))
        app.pinger.poll_results = [("ok", "exit:0")]
        app._ping_before = OBS_OLD
        _arm_ping_timer(app)
        app.codex.queued = (data_at(OBS_NEW), "")
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._on_ping_timer()
        self.assertEqual(last_display(app).captions, ("", "new 14:58"))
        self.assertFalse(app.pinger.running)

    def test_failed_and_timeout_show_ping_failed(self):
        cases = (
            (("failed", "exit:3"), "ping failed (exit:3)"),
            (("timeout", "unreaped"), "ping timeout (unreaped)"),
        )
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        for result, message in cases:
            with self.subTest(result=result[0]):
                app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
                app.pinger.poll_results = [result]
                app._ping_before = OBS_OLD
                _arm_ping_timer(app)
                app.refresh_view = Mock()
                with patch.object(widget.time, "time", return_value=APP_NOW):
                    app._on_ping_timer()
                self.assertEqual(app.codex.calls, [True])
                self.assertEqual(app._caption, (deadline, ("", "ping failed")))
                self.assertIsNone(app._ping_before)
                app.log.log.assert_any_call("CodexPing", message)
                app.w32.kill_timer.assert_called_with(10, widget.TIMER_PING)
                self.assertNotIn(widget.TIMER_PING, app.timers)
                self.assertIn(
                    call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
                    app.w32.set_timer.call_args_list)
                self.assertIn(widget.TIMER_CAPTION, app.timers)
                app.refresh_view.assert_called_once_with()

    def test_poll_error_is_logged_and_not_raised(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        error = OSError("poll broke")
        app.pinger.poll_error = error
        caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
        app._caption = caption
        app._on_ping_timer()
        self.assertEqual(app._caption, caption)
        app.log.log_exception.assert_called_with(error)

    def test_missing_pinger_kills_the_timer(self):
        app = make_app(pinger=False)
        _arm_ping_timer(app)
        app._on_ping_timer()
        app.w32.kill_timer.assert_called_with(10, widget.TIMER_PING)

    def test_timer_message_reaches_poll_unless_exiting(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.running = True
        app.on_timer(widget.TIMER_PING)
        self.assertEqual(app.pinger.poll_calls, 1)
        self.assertEqual(app._dispatch(widget.WM_TIMER, widget.TIMER_PING), (True, 0))
        self.assertEqual(app.pinger.poll_calls, 2)
        app.exiting = True
        app.on_timer(widget.TIMER_PING)
        self.assertEqual(app.pinger.poll_calls, 2)

    def test_stop_ping_is_idempotent_and_swallows_errors(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app._stop_ping()
        app._stop_ping()
        self.assertEqual(app.pinger.stop_calls, 2)

        bare = make_app(pinger=False)
        bare._stop_ping()

        broken = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        broken.pinger.stop_error = RuntimeError("boom")
        broken._stop_ping()
        broken.log.log_exception.assert_called()


class ExitAndTimersTests(NoRealProcessTestCase):
    def test_request_exit_stops_the_ping_after_the_flag_is_set(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        seen = []
        app.pinger.stop = Mock(side_effect=lambda: seen.append(app.exiting))
        app.request_exit()
        app.pinger.stop.assert_called_once_with()
        self.assertEqual(seen, [True])
        self.assertTrue(app.exiting)
        app.w32.destroy_window.assert_called_with(10)
        app.request_exit()
        app.pinger.stop.assert_called_once_with()

    def test_request_exit_without_a_pinger(self):
        app = make_app(pinger=False)
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        app.request_exit()
        self.assertTrue(app.exiting)

    def test_stop_error_does_not_block_exit(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        app.pinger.stop_error = RuntimeError("boom")
        app.timers.update({widget.TIMER_PING, widget.TIMER_CHECK})
        app.request_exit()
        self.assertTrue(app.exiting)
        self.assertEqual(app.timers, set())
        app.w32.destroy_window.assert_called()
        app.log.log_exception.assert_called()

    def test_start_timers_rearms_a_running_ping_only_after_recreate(self):
        running = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        running.pinger.running = True
        running._start_timers(recreated=True)
        self.assertIn(widget.TIMER_PING, running.timers)
        self.assertIn(
            call(10, widget.TIMER_PING, widget.CODEX_PING_POLL_MS),
            running.w32.set_timer.call_args_list)

        idle = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        idle.pinger.running = False
        idle._start_timers(recreated=True)
        self.assertNotIn(widget.TIMER_PING, idle.timers)

        bare = make_app(pinger=False)
        bare._start_timers(recreated=True)
        self.assertEqual(bare.timers, {
            widget.TIMER_CHECK, widget.TIMER_POLL, widget.TIMER_THEME})

        first = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        first.pinger.running = True
        first._start_timers()
        self.assertNotIn(widget.TIMER_PING, first.timers)

    def test_recreate_rearms_a_running_ping(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.running = True
        app.timers.add(widget.TIMER_PING)
        app.w32.is_window.return_value = False
        app.w32.create_window.return_value = 11
        app.refresh_view = Mock()
        app._recreate_window("owner disappeared")
        self.assertIn(widget.TIMER_PING, app.timers)
        app.w32.set_timer.assert_any_call(11, widget.TIMER_PING, widget.CODEX_PING_POLL_MS)
        self.assertIsNone(app._caption)

        posted = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        posted.pinger.running = True
        posted.hwnd = 0
        posted.w32.create_window.return_value = 12
        posted.refresh_view = Mock()
        posted.on_thread_message(widget.WM_APP_RECREATE)
        posted.w32.set_timer.assert_any_call(12, widget.TIMER_PING, widget.CODEX_PING_POLL_MS)

        quiet = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        quiet.pinger.running = False
        quiet.w32.is_window.return_value = False
        quiet.w32.create_window.return_value = 11
        quiet.refresh_view = Mock()
        quiet._recreate_window("owner disappeared")
        self.assertEqual(_ping_timer_calls(quiet), [])

        quiet_post = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        quiet_post.pinger.running = False
        quiet_post.hwnd = 0
        quiet_post.w32.create_window.return_value = 12
        quiet_post.refresh_view = Mock()
        quiet_post.on_thread_message(widget.WM_APP_RECREATE)
        self.assertEqual(_ping_timer_calls(quiet_post), [])

    def test_failed_rearm_after_recreate_gives_up_the_ping(self):
        def refuse_ping_timer(hwnd, timer_id, interval_ms):
            return timer_id != widget.TIMER_PING

        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.pinger.running = True
        app._ping_before = OBS_OLD
        app.w32.set_timer.side_effect = refuse_ping_timer
        app._start_timers(recreated=True)
        self.assertEqual(app.pinger.stop_calls, 1)
        self.assertFalse(app.pinger.running)
        self.assertIsNone(app._ping_before)
        self.assertNotIn(widget.TIMER_PING, app.timers)
        app.log.log.assert_any_call("CodexPing", "poll timer unavailable; ping abandoned")

        rebuilt = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        rebuilt.pinger.running = True
        rebuilt.w32.is_window.return_value = False
        rebuilt.w32.create_window.return_value = 11
        rebuilt.w32.set_timer.side_effect = refuse_ping_timer
        rebuilt._recreate_window("owner disappeared")
        self.assertEqual(rebuilt.pinger.stop_calls, 1)
        self.assertNotIn("asking", last_display(rebuilt).captions)

        healthy = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        healthy.pinger.running = True
        healthy._start_timers(recreated=True)
        self.assertEqual(healthy.pinger.stop_calls, 0)
        self.assertTrue(healthy.pinger.running)
        self.assertIn(widget.TIMER_PING, healthy.timers)

    def test_run_stops_the_ping_on_the_way_out(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.hwnd = 0
        app.w32.run_message_loop.return_value = 0
        app.w32.create_window.return_value = 11
        app.poll_snapshot = Mock()
        app.refresh_view = Mock()
        app._start_timers = Mock()
        self.assertEqual(app.run(), 0)
        self.assertEqual(app.pinger.stop_calls, 1)
        self.assertIsNone(widget._APP)

        failed = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        failed.hwnd = 0
        failed.w32.run_message_loop.side_effect = RuntimeError("loop failed")
        failed.w32.create_window.return_value = 11
        failed.poll_snapshot = Mock()
        failed.refresh_view = Mock()
        failed._start_timers = Mock()
        with self.assertRaises(RuntimeError):
            failed.run()
        self.assertEqual(failed.pinger.stop_calls, 1)
        self.assertIsNone(widget._APP)

        bare = make_app(pinger=False)
        bare.hwnd = 0
        bare.w32.run_message_loop.return_value = 0
        bare.w32.create_window.return_value = 11
        bare.poll_snapshot = Mock()
        bare.refresh_view = Mock()
        bare._start_timers = Mock()
        self.assertEqual(bare.run(), 0)


class ValidPingModelTests(NoRealProcessTestCase):
    def test_accepts_conservative_names(self):
        for name in ("gpt-6-luna", "a.b_c:d-1", "A", "0abc", "a" * 64):
            with self.subTest(name=name):
                self.assertTrue(widget.valid_ping_model(name))

    def test_rejects_unsafe_names(self):
        rejected = (
            "",
            "has space",
            'quo"te',
            "semi;colon",
            "back\\slash",
            "-leading",
            "a" * 65,
            None,
            123,
            b"gpt",
            "tab\tx",
            "new\nline",
            "trailing\n",
            "gpt-é",
        )
        for name in rejected:
            with self.subTest(name=name):
                self.assertFalse(widget.valid_ping_model(name))


class BuildPingArgvTests(NoRealProcessTestCase):
    def test_argv_matches_the_fixed_command(self):
        exe = os.path.join("bin", "codex.exe")
        model = "gpt-6-luna"
        cwd = os.path.join("data", "codex-ping")
        self.assertEqual(widget.build_ping_argv(exe, model, cwd), [
            exe, "exec", "-m", model, "-c", 'model_reasoning_effort="low"',
            "--ignore-user-config", "--ignore-rules", "-s", "read-only", "-C", cwd,
            "--skip-git-repo-check", "--json", "ok",
        ])
        other = widget.build_ping_argv("other.exe", "other-model", "other-cwd")
        self.assertEqual(other[0], "other.exe")
        self.assertEqual(other[3], "other-model")
        self.assertEqual(other[11], "other-cwd")
        self.assertEqual(widget.CODEX_PING_PROMPT, "ok")

    def test_each_call_returns_a_fresh_list(self):
        exe = os.path.join("bin", "codex.exe")
        first = widget.build_ping_argv(exe, "gpt-6-luna", os.path.join("data", "codex-ping"))
        first.append("mutated")
        second = widget.build_ping_argv(exe, "gpt-6-luna", os.path.join("data", "codex-ping"))
        self.assertNotIn("mutated", second)


class CodexPingCaptionTests(NoRealProcessTestCase):
    def test_caption_constants_are_ascii(self):
        self.assertEqual(widget.CAPTION_ASKING, "asking")
        self.assertEqual(widget.CAPTION_COOLDOWN, "cooldown")
        self.assertEqual(widget.CAPTION_PING_FAILED, "ping failed")
        for text in (widget.CAPTION_ASKING, widget.CAPTION_COOLDOWN, widget.CAPTION_PING_FAILED):
            self.assertTrue(text.isascii())

    def test_asking_and_failed_replace_any_text(self):
        for read_caption in ("", "new 14:59", "same 14:29", "read error"):
            with self.subTest(read_caption=read_caption):
                self.assertEqual(widget.codex_ping_caption(read_caption, "asking"), "asking")
                self.assertEqual(widget.codex_ping_caption(read_caption, "failed"), "ping failed")

    def test_cooldown_replaces_only_empty_news(self):
        for read_caption in ("same 14:29", "no data", "", "same --:--"):
            with self.subTest(read_caption=read_caption):
                self.assertEqual(widget.codex_ping_caption(read_caption, "cooldown"), "cooldown")
        for read_caption in ("new 14:59", "read error"):
            with self.subTest(read_caption=read_caption):
                self.assertEqual(widget.codex_ping_caption(read_caption, "cooldown"), read_caption)

    def test_unknown_state_keeps_the_read_caption(self):
        self.assertEqual(widget.codex_ping_caption("new 14:59", "bogus"), "new 14:59")
        self.assertEqual(widget.codex_ping_caption("same 14:29", ""), "same 14:29")
        self.assertEqual(widget.codex_ping_caption("read error", None), "read error")


class FindCodexExeTests(NoRealProcessTestCase):
    NPM_REL = os.path.join(
        "npm", "node_modules", "@openai", "codex", "node_modules",
        "@openai", "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc",
        "bin", "codex.exe")

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name

    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb"):
            pass

    def _assert_exe(self, result):
        self.assertIsNotNone(result)
        self.assertTrue(result.lower().endswith(".exe"))

    def test_override_exe_wins_over_path(self):
        chosen = os.path.join(self.root, "chosen.exe")
        other_dir = os.path.join(self.root, "elsewhere")
        self._touch(chosen)
        self._touch(os.path.join(other_dir, "codex.exe"))
        upper = os.path.join(self.root, "UPPER.EXE")
        self._touch(upper)
        for override in (chosen, upper):
            with self.subTest(override=os.path.basename(override)):
                found = widget.find_codex_exe(override, other_dir, "")
                self._assert_exe(found)
                self.assertEqual(found, override)

    def test_override_cmd_does_not_fall_through(self):
        shim = os.path.join(self.root, "codex.cmd")
        path_dir = os.path.join(self.root, "on-path")
        self._touch(shim)
        self._touch(os.path.join(path_dir, "codex.exe"))
        self.assertIsNone(widget.find_codex_exe(shim, path_dir, ""))

    def test_missing_override_does_not_fall_through(self):
        path_dir = os.path.join(self.root, "on-path")
        self._touch(os.path.join(path_dir, "codex.exe"))
        missing = os.path.join(self.root, "missing.exe")
        self.assertIsNone(widget.find_codex_exe(missing, path_dir, ""))

    def test_override_directory_is_rejected(self):
        folder = os.path.join(self.root, "something.exe")
        os.makedirs(folder)
        self.assertIsNone(widget.find_codex_exe(folder, "", ""))

    def test_path_returns_the_first_real_exe(self):
        first = os.path.join(self.root, "first")
        second = os.path.join(self.root, "second")
        os.makedirs(first)
        self._touch(os.path.join(second, "codex.exe"))
        found = widget.find_codex_exe(None, os.pathsep.join((first, second)), "")
        self._assert_exe(found)
        self.assertEqual(found, os.path.join(second, "codex.exe"))

        self._touch(os.path.join(first, "codex.exe"))
        found = widget.find_codex_exe(None, os.pathsep.join((first, second)), "")
        self._assert_exe(found)
        self.assertEqual(found, os.path.join(first, "codex.exe"))

    def test_path_ignores_cmd_bat_and_ps1(self):
        path_dir = os.path.join(self.root, "shims")
        os.makedirs(path_dir)
        for name in ("codex.cmd", "codex.bat", "codex.ps1"):
            self._touch(os.path.join(path_dir, name))
        self.assertIsNone(widget.find_codex_exe(None, path_dir, ""))

    def test_path_ignores_a_directory_named_codex_exe(self):
        path_dir = os.path.join(self.root, "dir-exe")
        os.makedirs(os.path.join(path_dir, "codex.exe"))
        self.assertIsNone(widget.find_codex_exe(None, path_dir, ""))

    def test_blank_path_entries_are_skipped(self):
        here = os.path.join(self.root, "here")
        os.makedirs(here)
        self._touch(os.path.join(here, "codex.exe"))
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(here)
        blank = os.pathsep + "  " + os.pathsep
        self.assertIsNone(widget.find_codex_exe(None, blank, ""))

    def test_relative_entries_are_skipped(self):
        here = os.path.join(self.root, "here")
        self._touch(os.path.join(here, "codex.exe"))
        self._touch(os.path.join(here, "tools", "codex.exe"))
        self._touch(os.path.join(here, "rel-appdata", self.NPM_REL))
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(here)
        for entry in (".", "tools", os.path.join(".", "tools")):
            with self.subTest(entry=entry):
                self.assertIsNone(widget.find_codex_exe(None, entry, ""))
        self.assertIsNone(widget.find_codex_exe(None, "", "rel-appdata"))
        absolute = os.path.join(here, "tools")
        found = widget.find_codex_exe(None, os.pathsep.join((".", "tools", absolute)), "")
        self._assert_exe(found)
        self.assertEqual(found, os.path.join(absolute, "codex.exe"))

    def test_npm_layout_is_used_when_path_is_empty(self):
        appdata = os.path.join(self.root, "appdata")
        target = os.path.join(appdata, self.NPM_REL)
        self._touch(target)
        found = widget.find_codex_exe(None, "", appdata)
        self._assert_exe(found)
        self.assertEqual(found, target)

    def test_npm_picks_the_sorted_first_candidate(self):
        appdata = os.path.join(self.root, "appdata")
        arm = self.NPM_REL.replace("codex-win32-x64", "codex-win32-arm64")
        self._touch(os.path.join(appdata, self.NPM_REL))
        arm_path = os.path.join(appdata, arm)
        self._touch(arm_path)
        found = widget.find_codex_exe(None, "", appdata)
        self._assert_exe(found)
        self.assertEqual(found, arm_path)

    def test_npm_directory_named_codex_exe_is_rejected(self):
        appdata = os.path.join(self.root, "appdata")
        os.makedirs(os.path.join(appdata, self.NPM_REL))
        self.assertIsNone(widget.find_codex_exe(None, "", appdata))

    def test_appdata_brackets_are_escaped(self):
        appdata = os.path.join(self.root, "app[data]")
        target = os.path.join(appdata, self.NPM_REL)
        self._touch(target)
        found = widget.find_codex_exe(None, "", appdata)
        self._assert_exe(found)
        self.assertEqual(found, target)

    def test_path_beats_npm(self):
        path_dir = os.path.join(self.root, "on-path")
        path_exe = os.path.join(path_dir, "codex.exe")
        appdata = os.path.join(self.root, "appdata")
        self._touch(path_exe)
        self._touch(os.path.join(appdata, self.NPM_REL))
        found = widget.find_codex_exe(None, path_dir, appdata)
        self._assert_exe(found)
        self.assertEqual(found, path_exe)

    def test_nothing_found_returns_none(self):
        empty_path = os.path.join(self.root, "empty-path")
        empty_app = os.path.join(self.root, "empty-app")
        os.makedirs(empty_path)
        os.makedirs(empty_app)
        self.assertIsNone(widget.find_codex_exe(None, empty_path, empty_app))

    def test_defaults_come_from_the_environment(self):
        path_dir = os.path.join(self.root, "on-path")
        path_exe = os.path.join(path_dir, "codex.exe")
        empty_app = os.path.join(self.root, "empty-app")
        self._touch(path_exe)
        os.makedirs(empty_app)
        with patch.dict(os.environ, {"PATH": path_dir, "APPDATA": empty_app}):
            found = widget.find_codex_exe()
        self._assert_exe(found)
        self.assertEqual(found, path_exe)

        empty_path = os.path.join(self.root, "empty-path")
        os.makedirs(empty_path)
        with patch.dict(os.environ, {"PATH": empty_path}):
            os.environ.pop("APPDATA", None)
            self.assertIsNone(widget.find_codex_exe())


class ParsePingOptionsTests(NoRealProcessTestCase):
    def test_defaults_leave_the_ping_fields_unset(self):
        opts = widget.parse_args([])
        self.assertIsNone(opts.codex_bin)
        self.assertFalse(opts.no_codex_ping)
        self.assertIsNone(opts.codex_ping_model)

    def test_options_field_order_and_positional_defaults(self):
        self.assertEqual(
            widget.Options._fields[-3:],
            ("codex_bin", "no_codex_ping", "codex_ping_model"))
        plain = widget.Options(".", None, None, None)
        flagged = widget.Options(".", None, None, None, no_codex=True)
        self.assertFalse(plain.no_codex)
        self.assertTrue(flagged.no_codex)
        for opts in (plain, flagged):
            self.assertIsNone(opts.codex_bin)
            self.assertFalse(opts.no_codex_ping)
            self.assertIsNone(opts.codex_ping_model)

    def test_no_codex_ping_is_a_switch(self):
        home = os.path.join("rel", "codex-home")
        self.assertTrue(widget.parse_args(["--no-codex-ping"]).no_codex_ping)
        self.assertTrue(widget.parse_args(["--no-codex-ping=1"]).no_codex_ping)
        both = widget.parse_args(["--no-codex", "--no-codex-ping"])
        self.assertTrue(both.no_codex)
        self.assertTrue(both.no_codex_ping)
        after = widget.parse_args(["--no-codex-ping", "--codex-home", home])
        self.assertTrue(after.no_codex_ping)
        self.assertEqual(after.codex_home, os.path.abspath(home))

    def test_codex_bin_accepts_both_forms(self):
        relative = os.path.join("rel", "codex.exe")
        expected = os.path.abspath(relative)
        spaced = widget.parse_args(["--codex-bin", relative])
        inline = widget.parse_args(["--codex-bin=%s" % relative])
        self.assertEqual(spaced.codex_bin, expected)
        self.assertEqual(inline.codex_bin, expected)

    def test_codex_bin_missing_or_empty_is_ignored(self):
        self.assertIsNone(widget.parse_args(["--codex-bin"]).codex_bin)
        followed = widget.parse_args(["--codex-bin", "--no-codex"])
        self.assertIsNone(followed.codex_bin)
        self.assertTrue(followed.no_codex)
        self.assertIsNone(widget.parse_args(["--codex-bin="]).codex_bin)

    def test_codex_bin_is_stored_without_checking_the_file(self):
        relative = "whatever.cmd"
        opts = widget.parse_args(["--codex-bin=%s" % relative])
        self.assertEqual(opts.codex_bin, os.path.abspath(relative))

    def test_codex_ping_model_accepts_both_forms(self):
        spaced = widget.parse_args(["--codex-ping-model", "gpt-6-luna"])
        inline = widget.parse_args(["--codex-ping-model=gpt-6-luna"])
        self.assertEqual(spaced.codex_ping_model, "gpt-6-luna")
        self.assertEqual(inline.codex_ping_model, "gpt-6-luna")

    def test_illegal_model_names_are_ignored(self):
        cases = (
            ["--codex-ping-model=bad name"],
            ["--codex-ping-model=a;b"],
            ['--codex-ping-model=x"y'],
            ["--codex-ping-model=a\\b"],
            ["--codex-ping-model", "-x"],
            ["--codex-ping-model", "a" * 65],
            ["--codex-ping-model="],
            ["--codex-ping-model"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertIsNone(widget.parse_args(argv).codex_ping_model)

    def test_illegal_model_does_not_block_later_flags(self):
        opts = widget.parse_args(["--codex-ping-model=bad name", "--no-codex-ping"])
        self.assertTrue(opts.no_codex_ping)
        self.assertIsNone(opts.codex_ping_model)

    def test_unknown_options_are_ignored(self):
        opts = widget.parse_args(["--bogus", "x", "--no-codex-ping"])
        self.assertTrue(opts.no_codex_ping)
        self.assertIsNone(opts.codex_bin)
        self.assertIsNone(opts.codex_ping_model)
        self.assertFalse(opts.no_codex)
        self.assertIsNone(opts.codex_home)

    def test_existing_options_still_parse(self):
        data = os.path.join("rel", "data")
        render = os.path.join("rel", "out")
        home = os.path.join("rel", "home")
        opts = widget.parse_args([
            "--data-dir", data,
            "--exit-after=1.5",
            "--selftest-render", render,
            "--selftest-gdi", "4",
            "--codex-home=%s" % home,
        ])
        self.assertEqual(opts.data_dir, os.path.abspath(data))
        self.assertEqual(opts.exit_after, 1.5)
        self.assertEqual(opts.selftest_render, os.path.abspath(render))
        self.assertEqual(opts.selftest_gdi, 4)
        self.assertEqual(opts.codex_home, os.path.abspath(home))
        self.assertIsNone(opts.codex_bin)
        self.assertFalse(opts.no_codex_ping)
        self.assertIsNone(opts.codex_ping_model)


class FakeClock:
    """Manual monotonic clock. Tests move time by assigning now."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeProc:
    """Process stand-in. wait and communicate are absent on purpose."""

    def __init__(self, pid=4242):
        self.code = None
        self.pid = pid
        self.poll_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.poll_error = None
        self.terminate_error = None
        self.kill_error = None

    def poll(self):
        self.poll_calls += 1
        if self.poll_error is not None:
            raise self.poll_error
        return self.code

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self):
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error


class FakePopen:
    """Callable stand-in for subprocess.Popen."""

    def __init__(self):
        self.calls = []
        self.procs = []
        self.error = None

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        proc = FakeProc()
        self.procs.append(proc)
        return proc


class PingerTestCase(NoRealProcessTestCase):
    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name
        self.exe = os.path.join(self.root, "bin", "codex.exe")
        self.work = os.path.join(self.root, "codex-ping")

    def make_pinger(self, find_result="exe", **overrides):
        self.clock = FakeClock()
        self.popen = FakePopen()
        self.find_calls = []

        def find_exe(override):
            self.find_calls.append(override)
            if find_result == "exe":
                return self.exe
            return None

        options = {
            "data_dir": self.root,
            "popen": self.popen,
            "find_exe": find_exe,
            "clock": self.clock,
        }
        options.update(overrides)
        self.pinger = widget.CodexPinger(**options)
        return self.pinger


class CodexPingerStartTests(PingerTestCase):
    def test_constructor_stores_options_and_does_no_io(self):
        pinger = self.make_pinger(model="m-1", exe_override="x.exe", codex_home="h")
        self.assertEqual(pinger.data_dir, self.root)
        self.assertEqual(pinger.model, "m-1")
        self.assertEqual(pinger.exe_override, "x.exe")
        self.assertEqual(pinger.codex_home, "h")
        self.assertEqual(self.popen.calls, [])
        self.assertFalse(os.path.exists(self.work))
        self.assertFalse(pinger.running)
        self.assertEqual(pinger.cooldown_left(), 0.0)

    def test_default_popen_is_the_real_subprocess_popen(self):
        pinger = widget.CodexPinger(self.root, find_exe=lambda override: self.exe)
        with self.assertRaises(AssertionError):
            pinger.start()
        self.assertEqual(self.popen_guard.call_count, 1)
        self.popen_guard.reset_mock()

    def test_default_find_exe_is_find_codex_exe(self):
        seen = []

        def fake_find(override=None, path_env=None, appdata=None):
            seen.append(override)
            return None

        popen = FakePopen()
        with patch.object(widget, "find_codex_exe", fake_find):
            pinger = widget.CodexPinger(self.root, exe_override="x.exe", popen=popen)
            self.assertEqual(pinger.start(), "no_exe")
        self.assertEqual(seen, ["x.exe"])
        self.assertEqual(popen.calls, [])
        self.assertFalse(pinger.running)

    def test_start_passes_the_fixed_popen_arguments(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        self.assertEqual(len(self.popen.calls), 1)
        args, kwargs = self.popen.calls[0]
        argv = widget.build_ping_argv(self.exe, widget.CODEX_PING_MODEL, self.work)
        self.assertEqual(args, (argv,))
        self.assertEqual(argv[0], self.exe)
        self.assertEqual(argv[1], "exec")
        self.assertEqual(argv[3], "gpt-6-luna")
        self.assertEqual(argv[5], 'model_reasoning_effort="low"')
        self.assertEqual(argv[11], self.work)
        self.assertEqual(argv[-1], "ok")
        self.assertEqual(set(kwargs), {
            "stdin", "stdout", "stderr", "cwd", "env", "close_fds", "creationflags"})
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], self.work)
        self.assertTrue(os.path.isdir(self.work))
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertTrue(pinger.running)
        self.assertEqual(self.find_calls, [None])

        named = self.make_pinger(exe_override="x.exe")
        self.assertEqual(named.start(), "started")
        self.assertEqual(self.find_calls, ["x.exe"])

    def test_codex_home_overrides_the_copied_environment(self):
        bare = self.make_pinger()
        self.assertEqual(bare.start(), "started")
        self.assertIsNone(self.popen.calls[0][1]["env"])

        home = self.make_pinger(codex_home="h")
        with patch.dict(os.environ, {"PING_TEST_MARKER": "kept", "CODEX_HOME": "somewhere-else"}):
            self.assertEqual(home.start(), "started")
            env = self.popen.calls[0][1]["env"]
            self.assertEqual(env["CODEX_HOME"], "h")
            self.assertEqual(env["PING_TEST_MARKER"], "kept")
            self.assertIsNot(env, os.environ)
            self.assertEqual(os.environ["CODEX_HOME"], "somewhere-else")

    def test_second_start_while_running_does_not_spawn(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        self.assertEqual(pinger.start(), "running")
        self.assertEqual(len(self.popen.calls), 1)

    def test_reaped_success_enters_cooldown(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        self.popen.procs[0].code = 0
        self.assertEqual(pinger.poll(), ("ok", "exit:0"))
        self.clock.now = 1100.0
        self.assertEqual(pinger.start(), "cooldown")
        self.assertAlmostEqual(pinger.cooldown_left(), 200.0)
        self.clock.now = 1299.5
        self.assertAlmostEqual(pinger.cooldown_left(), 0.5)
        self.assertEqual(pinger.start(), "cooldown")
        self.clock.now = 1300.0
        self.assertEqual(pinger.cooldown_left(), 0.0)
        self.assertEqual(pinger.start(), "started")
        self.assertEqual(len(self.popen.calls), 2)

    def test_failure_and_timeout_still_cool_down_from_the_start(self):
        failed = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(failed.start(), "started")
        self.popen.procs[0].code = 3
        self.clock.now = 1010.0
        self.assertEqual(failed.poll(), ("failed", "exit:3"))
        self.clock.now = 1020.0
        self.assertEqual(failed.start(), "cooldown")
        self.assertAlmostEqual(failed.cooldown_left(), 280.0)

        timed = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(timed.start(), "started")
        self.clock.now = 1060.0
        self.assertIsNone(timed.poll())
        self.popen.procs[0].code = 1
        self.clock.now = 1061.0
        self.assertEqual(timed.poll(), ("timeout", "exit:1"))
        self.clock.now = 1100.0
        self.assertEqual(timed.start(), "cooldown")
        self.assertAlmostEqual(timed.cooldown_left(), 200.0)

    def test_clock_going_backwards_ends_the_cooldown(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        self.popen.procs[0].code = 0
        self.assertEqual(pinger.poll(), ("ok", "exit:0"))
        self.clock.now = 500.0
        self.assertEqual(pinger.cooldown_left(), 0.0)
        self.assertEqual(pinger.start(), "started")

    def test_missing_exe_does_not_create_the_work_directory(self):
        pinger = self.make_pinger(find_result="none")
        self.assertEqual(pinger.start(), "no_exe")
        self.assertEqual(self.popen.calls, [])
        self.assertFalse(os.path.exists(self.work))
        self.assertFalse(pinger.running)
        self.assertEqual(pinger.cooldown_left(), 0.0)
        self.assertEqual(pinger.start(), "no_exe")

    def test_spawn_errors_do_not_enter_cooldown(self):
        pinger = self.make_pinger()
        self.popen.error = OSError("denied")
        self.assertEqual(pinger.start(), "spawn_error:OSError")
        self.assertFalse(pinger.running)
        self.assertEqual(pinger.cooldown_left(), 0.0)
        self.assertEqual(pinger.start(), "spawn_error:OSError")
        self.assertEqual(len(self.popen.calls), 2)
        self.popen.error = ValueError("bad")
        self.assertEqual(pinger.start(), "spawn_error:ValueError")
        self.popen.error = None
        self.assertEqual(pinger.start(), "started")

    def test_directory_creation_failure_is_a_spawn_error(self):
        blocker = os.path.join(self.root, "blocker")
        with open(blocker, "wb"):
            pass
        pinger = self.make_pinger(data_dir=os.path.join(blocker, "sub"))
        result = pinger.start()
        self.assertTrue(result.startswith("spawn_error:"))
        self.assertEqual(self.popen.calls, [])
        self.assertFalse(pinger.running)

    def test_checks_running_then_cooldown_then_no_exe(self):
        self.make_pinger()
        mode = {"value": "exe"}

        def find_exe(override):
            self.find_calls.append(override)
            if mode["value"] == "exe":
                return self.exe
            return None

        self.find_calls.clear()
        pinger = widget.CodexPinger(
            self.root, popen=self.popen, find_exe=find_exe, clock=self.clock)
        self.assertEqual(pinger.start(), "started")
        self.assertEqual(self.find_calls, [None])
        mode["value"] = "none"
        self.assertEqual(pinger.start(), "running")
        self.assertEqual(self.find_calls, [None])
        self.popen.procs[0].code = 0
        self.assertEqual(pinger.poll(), ("ok", "exit:0"))
        self.assertEqual(pinger.start(), "cooldown")
        self.assertEqual(self.find_calls, [None])
        self.clock.now = 1000.0 + widget.CODEX_PING_COOLDOWN_SECONDS
        self.assertEqual(pinger.start(), "no_exe")
        self.assertEqual(self.find_calls, [None, None])


class CodexPingerPollTests(PingerTestCase):
    def test_poll_without_a_process_returns_none(self):
        pinger = self.make_pinger()
        self.assertIsNone(pinger.poll())
        self.assertFalse(pinger.running)

    def test_poll_while_running_does_not_terminate(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        for now in (1000.0, 1059.9):
            self.clock.now = now
            self.assertIsNone(pinger.poll())
        self.assertEqual(self.popen.procs[0].terminate_calls, 0)
        self.assertTrue(pinger.running)

    def test_exit_codes(self):
        cases = (
            (0, ("ok", "exit:0")),
            (3, ("failed", "exit:3")),
            (-1, ("failed", "exit:-1")),
            (3221225786, ("failed", "exit:3221225786")),
        )
        for code, expected in cases:
            with self.subTest(code=code):
                pinger = self.make_pinger()
                self.assertEqual(pinger.start(), "started")
                self.popen.procs[0].code = code
                self.assertEqual(pinger.poll(), expected)
                self.assertFalse(pinger.running)
                self.assertIsNone(pinger.poll())

    def test_timeout_terminates_then_kills(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        proc = self.popen.procs[0]
        self.clock.now = 1059.9
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.terminate_calls, 0)
        self.clock.now = 1060.0
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.terminate_calls, 1)
        self.clock.now = 1061.9
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.kill_calls, 0)
        self.clock.now = 1062.0
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.kill_calls, 1)
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.kill_calls, 1)
        proc.code = 1
        self.assertEqual(pinger.poll(), ("timeout", "exit:1"))
        self.assertFalse(pinger.running)
        self.assertIsNone(pinger.poll())

    def test_exit_right_after_terminate_skips_kill(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        self.clock.now = 1060.0
        self.assertIsNone(pinger.poll())
        self.popen.procs[0].code = 1
        self.clock.now = 1060.5
        self.assertEqual(pinger.poll(), ("timeout", "exit:1"))
        self.assertEqual(self.popen.procs[0].kill_calls, 0)

    def test_unreaped_process_is_abandoned_and_still_cools_down(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        proc = self.popen.procs[0]
        self.clock.now = 1060.0
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.terminate_calls, 1)
        self.clock.now = 1062.0
        self.assertIsNone(pinger.poll())
        self.assertEqual(proc.kill_calls, 1)
        self.clock.now = 1063.9
        self.assertIsNone(pinger.poll())
        self.assertTrue(pinger.running)
        self.clock.now = 1064.0
        self.assertEqual(pinger.poll(), ("timeout", "unreaped"))
        self.assertFalse(pinger.running)
        self.assertIsNone(pinger.poll())
        self.assertEqual(pinger.start(), "cooldown")

    def test_terminate_and_kill_errors_are_swallowed(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        proc = self.popen.procs[0]
        proc.terminate_error = OSError("gone")
        proc.kill_error = OSError("gone")
        self.clock.now = 1060.0
        self.assertIsNone(pinger.poll())
        self.clock.now = 1062.0
        self.assertIsNone(pinger.poll())
        self.clock.now = 1064.0
        self.assertEqual(pinger.poll(), ("timeout", "unreaped"))
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_poll_error_clears_the_process_and_keeps_cooldown(self):
        pinger = self.make_pinger()
        self.clock.now = 1000.0
        self.assertEqual(pinger.start(), "started")
        self.popen.procs[0].poll_error = OSError("broken")
        self.assertEqual(pinger.poll(), ("failed", "poll_error"))
        self.assertFalse(pinger.running)
        self.assertEqual(pinger.start(), "cooldown")


class CodexPingerStopTests(PingerTestCase):
    def test_stop_terminates_and_kills_without_waiting(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        pinger.stop()
        proc = self.popen.procs[0]
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)
        self.assertFalse(pinger.running)
        self.assertIsNone(pinger.poll())

    def test_stop_while_idle_is_a_no_op(self):
        pinger = self.make_pinger()
        pinger.stop()
        pinger.stop()
        self.assertEqual(self.popen.calls, [])
        self.assertFalse(pinger.running)

    def test_second_stop_does_not_signal_again(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        pinger.stop()
        pinger.stop()
        proc = self.popen.procs[0]
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_stop_after_exit_does_not_signal(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        self.popen.procs[0].code = 0
        pinger.stop()
        self.assertEqual(self.popen.procs[0].terminate_calls, 0)
        self.assertEqual(self.popen.procs[0].kill_calls, 0)
        self.assertFalse(pinger.running)

    def test_stop_swallows_signal_and_poll_errors(self):
        pinger = self.make_pinger()
        self.assertEqual(pinger.start(), "started")
        proc = self.popen.procs[0]
        proc.terminate_error = OSError("gone")
        proc.kill_error = OSError("gone")
        pinger.stop()
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)
        self.assertFalse(pinger.running)

        broken = self.make_pinger()
        self.assertEqual(broken.start(), "started")
        broken_proc = self.popen.procs[0]
        broken_proc.poll_error = OSError("broken")
        broken.stop()
        self.assertEqual(broken_proc.terminate_calls, 1)
        self.assertEqual(broken_proc.kill_calls, 1)
        self.assertFalse(broken.running)


class RealPingTests(unittest.TestCase):
    """Starts the user's real Codex command line and spends account quota.

    It runs only when CLAUDE_USAGE_CODEX_PING_TEST is explicitly set to 1.
    """

    def setUp(self):
        if os.environ.get("CLAUDE_USAGE_CODEX_PING_TEST") != "1":
            self.skipTest("set CLAUDE_USAGE_CODEX_PING_TEST=1 to run the real Codex ping")

    def test_real_ping_writes_a_session_the_reader_can_see(self):
        home = widget.codex_home()
        sessions = os.path.join(home, "sessions")
        pattern = os.path.join(sessions, "**", "rollout-*.jsonl")
        before = set(glob.glob(pattern, recursive=True))
        started_wall = time.time()
        # stop() does not wait for the process to exit, and the process runs inside this folder;
        # on Windows the removal can fail while it is still alive, which must not hide the real failure.
        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(holder.cleanup)
        pinger = widget.CodexPinger(holder.name)
        self.addCleanup(pinger.stop)
        self.assertEqual(pinger.start(), "started")
        deadline = time.time() + 90.0
        result = None
        while time.time() < deadline:
            time.sleep(0.5)
            result = pinger.poll()
            if result is not None:
                break
        if result is None:
            self.fail("ping did not finish within 90 s")
        self.assertEqual(result[0], "ok")
        after = set(glob.glob(pattern, recursive=True))
        new_files = after - before
        self.assertTrue(new_files)
        found_limits = False
        for path in new_files:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "token_count" in line and "rate_limits" in line:
                        found_limits = True
                        break
            if found_limits:
                break
        self.assertTrue(found_limits)
        reader = widget.CodexReader(home)
        reader.refresh(force=True)
        self.assertIsNotNone(reader.snapshot)
        self.assertEqual(reader.last_reason, "")
        self.assertGreaterEqual(
            widget._snapshot_observed_at(reader.snapshot), started_wall - 30.0)


FROZEN_CTYPES = {
    "gdi32.CreateCompatibleDC", "gdi32.CreateDIBSection", "gdi32.DeleteDC", "gdi32.DeleteObject",
    "gdi32.SelectObject", "kernel32.CloseHandle", "kernel32.CreateMutexW",
    "kernel32.GetCurrentProcess", "kernel32.GetCurrentProcessId", "kernel32.GetCurrentThreadId",
    "kernel32.GetModuleHandleW", "shcore.SetProcessDpiAwareness", "shell32.SHAppBarMessage",
    "shell32.SHQueryUserNotificationState", "user32.AppendMenuW", "user32.CreatePopupMenu",
    "user32.CreateWindowExW", "user32.DefWindowProcW", "user32.DestroyMenu", "user32.DestroyWindow",
    "user32.DispatchMessageW", "user32.FindWindowExW", "user32.FindWindowW", "user32.GetClassNameW",
    "user32.GetCursorPos", "user32.GetDC", "user32.GetDesktopWindow", "user32.GetDpiForSystem",
    "user32.GetDpiForWindow", "user32.GetForegroundWindow", "user32.GetGuiResources",
    "user32.GetMessageW", "user32.GetMonitorInfoW", "user32.GetWindow", "user32.GetWindowRect",
    "user32.GetWindowThreadProcessId", "user32.IsWindow", "user32.IsWindowVisible",
    "user32.KillTimer", "user32.LoadCursorW", "user32.MonitorFromRect", "user32.PostMessageW",
    "user32.PostQuitMessage", "user32.PostThreadMessageW", "user32.RegisterClassExW",
    "user32.RegisterWindowMessageW", "user32.ReleaseCapture", "user32.ReleaseDC",
    "user32.SetCapture", "user32.SetForegroundWindow", "user32.SetProcessDpiAwarenessContext",
    "user32.SetTimer", "user32.SetWinEventHook", "user32.SetWindowPos", "user32.ShowWindow",
    "user32.TrackPopupMenu", "user32.TranslateMessage", "user32.UnhookWinEvent",
    "user32.UnregisterClassW", "user32.UpdateLayeredWindow",
}


class SourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.source = pathlib.Path(widget.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.lines = self.source.splitlines()

    def _pinger_span(self):
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "CodexPinger":
                return node.lineno, node.end_lineno
        self.fail("CodexPinger is missing")

    def _pinger_source(self):
        start, end = self._pinger_span()
        return "\n".join(self.lines[start - 1:end])

    def test_popen_only_appears_inside_codex_pinger(self):
        start, end = self._pinger_span()
        hits = [index for index, line in enumerate(self.lines, 1) if "subprocess.Popen" in line]
        self.assertTrue(hits)
        for index in hits:
            self.assertGreaterEqual(index, start)
            self.assertLessEqual(index, end)
        attributes = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute) and node.attr == "Popen"
            and isinstance(node.value, ast.Name) and node.value.id == "subprocess"]
        self.assertEqual(len(attributes), 1)
        self.assertGreaterEqual(attributes[0].lineno, start)
        self.assertLessEqual(attributes[0].lineno, end)

    def test_subprocess_attributes_are_a_closed_set(self):
        attributes = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"]
        self.assertEqual({node.attr for node in attributes}, {"Popen", "DEVNULL", "CREATE_NO_WINDOW"})
        calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"]
        self.assertEqual(calls, [])

    def test_forbidden_calls_and_strings_are_absent(self):
        self.assertNotIn("shell=True", self.source)
        self.assertNotIn("os.system", self.source)
        self.assertNotIn("os.startfile", self.source)
        self.assertNotIn("os.popen(", self.source)
        self.assertNotIn("auth" + ".json", self.source)

    def test_codex_pinger_never_waits_or_reads_pipes(self):
        segment = self._pinger_source()
        for needle in ("wait(", "communicate(", ".read(", ".readline("):
            self.assertNotIn(needle, segment)

    def test_no_new_threads(self):
        self.assertNotIn("threading.Thread(", self.source)
        self.assertEqual(self.source.count("threading.Timer("), 1)

    def test_ctypes_surface_is_unchanged(self):
        names = {
            "%s.%s" % pair
            for pair in re.findall(r"_signature\(\s*self\.(\w+)\.(\w+)", self.source)}
        self.assertEqual(len(names), 60)
        self.assertEqual(names, FROZEN_CTYPES)
        structures = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if isinstance(base, ast.Attribute) and base.attr == "Structure":
                    structures.append((node.lineno, node.name))
        structures.sort()
        self.assertEqual(
            [name for _lineno, name in structures],
            ["APPBARDATA", "MONITORINFO", "BITMAPINFOHEADER", "BLENDFUNCTION", "WNDCLASSEXW"])
        self.assertEqual(
            re.findall(r'WinDLL\("(\w+)"', self.source),
            ["user32", "gdi32", "shell32", "kernel32", "shcore"])
        self.assertEqual(self.source.count("WINFUNCTYPE"), 2)

    def test_timer_ids_are_unique(self):
        values = []
        for name in dir(widget):
            if not name.startswith("TIMER_"):
                continue
            value = getattr(widget, name)
            if isinstance(value, int) and not isinstance(value, bool):
                values.append(value)
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(widget.TIMER_PING, 10)

    def test_ping_constants_match_the_design(self):
        self.assertEqual(widget.CODEX_PING_MODEL, "gpt-6-luna")
        self.assertEqual(widget.CODEX_PING_PROMPT, "ok")
        self.assertEqual(widget.CODEX_PING_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(widget.CODEX_PING_KILL_WAIT_SECONDS, 2.0)
        self.assertEqual(widget.CODEX_PING_COOLDOWN_SECONDS, 300.0)
        self.assertEqual(widget.CODEX_PING_POLL_MS, 500)
        self.assertEqual(widget.CODEX_PING_DIR_NAME, "codex-ping")


class PingSampleTests(unittest.TestCase):
    NAMES = ("dual_caption_asking", "dual_caption_cooldown", "dual_caption_ping_failed")

    def test_new_samples_keep_the_old_indexes(self):
        self.assertEqual(widget.ALL_SAMPLE_STATES[-3:], self.NAMES)
        self.assertEqual(widget.EXTRA_SAMPLE_STATES[-3:], self.NAMES)
        self.assertEqual(widget.EXTRA_DUAL_SAMPLE_STATES[-3:], self.NAMES)
        indexes = [widget.ALL_SAMPLE_STATES.index(name) + 1 for name in self.NAMES]
        self.assertEqual(indexes, [24, 25, 26])
        self.assertEqual(
            widget.sample_filename(24, "dual_caption_asking", "light", 1.0),
            "24_dual_caption_asking_light_1.00.png")

    def test_new_samples_reuse_dual_green_rows(self):
        expected = (
            ("", "asking"),
            ("new 14:59", "cooldown"),
            ("", "ping failed"),
        )
        green = widget.sample_display("dual_green", "light", 1.0)
        for name, captions in zip(self.NAMES, expected):
            with self.subTest(name=name):
                display = widget.sample_display(name, "light", 1.0)
                self.assertEqual(display.kind, "dual")
                self.assertEqual(display.captions, captions)
                self.assertEqual(display.rows, green.rows)

    def test_selftest_render_writes_every_sample(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        with patch.object(widget, "say"):
            self.assertEqual(widget.selftest_render(holder.name), 0)
        pngs = [name for name in os.listdir(holder.name) if name.lower().endswith(".png")]
        self.assertEqual(len(pngs), 6 * len(widget.ALL_SAMPLE_STATES) + 1)
        sheet = os.path.join(holder.name, "contact_sheet.png")
        self.assertGreater(os.path.getsize(sheet), 0)
        for index, name in enumerate(widget.ALL_SAMPLE_STATES, 1):
            for theme, scale in widget.SAMPLE_COMBOS:
                path = os.path.join(holder.name, widget.sample_filename(index, name, theme, scale))
                self.assertTrue(os.path.isfile(path), path)
        reference_index = widget.ALL_SAMPLE_STATES.index("dual_caption_new_same") + 1
        for theme, scale in widget.SAMPLE_COMBOS:
            reference_name = widget.sample_filename(
                reference_index, "dual_caption_new_same", theme, scale)
            with Image.open(os.path.join(holder.name, reference_name)) as reference:
                reference_size = reference.size
            for name in self.NAMES:
                filename = widget.sample_filename(
                    widget.ALL_SAMPLE_STATES.index(name) + 1, name, theme, scale)
                path = os.path.join(holder.name, filename)
                self.assertGreater(os.path.getsize(path), 0)
                with Image.open(path) as image:
                    self.assertEqual((image.width, image.height), reference_size)
