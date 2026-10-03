"""Opt-in native checks in the ordinary desktop band, using our off-screen windows.

These checks do not simulate Windows shell band promotion when system panels open.
"""

import ctypes
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget


def run_driver():
    widget.WINDOW_CLASS = "ClaudeUsageEventTestTaskbar"
    w32 = widget.Win32()
    wndproc = widget.WNDPROC(w32.def_window_proc)
    w32.register_class(wndproc)
    hwnd = w32.create_window(-32000, -32000, 8, 8)
    popup32 = widget.Win32()
    widget.WINDOW_CLASS = "XamlExplorerHostIslandWindow"
    popup_proc = widget.WNDPROC(popup32.def_window_proc)
    popup32.register_class(popup_proc)
    popup = popup32.create_window(-32000, -31980, 8, 8)
    try:
        w32.show_window(hwnd, widget.SW_SHOWNOACTIVATE)
        popup32.show_window(popup, widget.SW_SHOWNOACTIVATE)
        print(json.dumps([hwnd, popup]), flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "quit":
                break
            if command in ("raise", "overlap", "separate"):
                if command != "raise":
                    popup32.move_window(popup, -32000, -32000 if command == "overlap" else -31980)
                result = w32.keep_topmost(hwnd) and popup32.keep_topmost(popup)
                print(int(result), flush=True)
    finally:
        popup32.destroy_window(popup)
        popup32.unregister_class()
        widget.WINDOW_CLASS = "ClaudeUsageEventTestTaskbar"
        w32.destroy_window(hwnd)
        w32.unregister_class()


class TaskbarProxy:
    """Substitute our test window for the taskbar in the production Z-order query."""

    def __init__(self, user32, hwnd):
        self.user32 = user32
        self.hwnd = hwnd

    def FindWindowW(self, class_name, title):
        if class_name == "Shell_TrayWnd":
            return self.hwnd
        return self.user32.FindWindowW(class_name, title)

    def __getattr__(self, name):
        return getattr(self.user32, name)


@unittest.skipUnless(os.environ.get("CLAUDE_USAGE_NATIVE_TEST") == "1",
                     "Set CLAUDE_USAGE_NATIVE_TEST=1 to run off-screen native checks")
class NativeWindowEventTests(unittest.TestCase):
    def test_external_z_order_events_restore_without_polling_or_focus(self):
        from test_window_events import make_app

        driver = subprocess.Popen(
            [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "--driver"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        responses = queue.Queue()

        def read_responses():
            try:
                for response in driver.stdout:
                    responses.put(response)
            finally:
                responses.put(None)

        reader = threading.Thread(target=read_responses, daemon=True)
        reader.start()

        def response():
            try:
                line = responses.get(timeout=3)
            except queue.Empty:
                self.fail("Native driver response timed out")
            self.assertIsNotNone(line, "Native driver closed its output unexpectedly")
            return line.strip()

        w32 = None
        hwnd = 0
        original_class = widget.WINDOW_CLASS
        try:
            w32 = widget.Win32()
            child_hwnd, popup_hwnd = json.loads(response())
            w32.user32 = TaskbarProxy(w32.user32, child_hwnd)
            widget.WINDOW_CLASS = "ClaudeUsageEventTestWidget"
            app = make_app()
            app.w32 = w32
            # 此处只测外进程层级事件；所有权由独立的隐藏窗口用例验证。
            w32.taskbar_hwnd = lambda: 0
            w32.taskbar_pos = lambda: ((-32000, -32000, -31000, -31960), widget.ABE_BOTTOM)
            w32.taskbar_autohide = lambda: False
            w32.notification_state = lambda: 5
            w32.tray_notify_rect = lambda: None
            w32.foreground_hwnd = lambda: 0
            restored = []
            keep_topmost = w32.keep_topmost

            def restore(own_hwnd):
                restored.append(time.monotonic())
                return keep_topmost(own_hwnd)

            wndproc = widget.WNDPROC(app.window_proc)
            w32.register_class(wndproc)
            hwnd = w32.create_window(-32000, -32000, 8, 8)
            app.hwnd = hwnd
            w32.show_window(hwnd, widget.SW_SHOWNOACTIVATE)
            keep_topmost(hwnd)
            w32.keep_topmost = restore
            observed = []

            def event_handler(event, event_hwnd, object_id, child_id):
                if event_hwnd in (child_hwnd, w32.desktop_hwnd()):
                    observed.append((event, object_id, child_id))
                app.on_window_event(event, event_hwnd, object_id, child_id)

            w32.watch_window_events(event_handler)
            peek = w32.user32.PeekMessageW
            peek.argtypes = [ctypes.POINTER(widget.wintypes.MSG), ctypes.c_void_p,
                             ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
            peek.restype = ctypes.c_int

            def pump():
                message = widget.wintypes.MSG()
                while peek(ctypes.byref(message), None, 0, 0, 1):
                    w32.user32.TranslateMessage(ctypes.byref(message))
                    w32.user32.DispatchMessageW(ctypes.byref(message))

            foreground_before = w32.user32.GetForegroundWindow()
            latencies = []
            for index in range(3):
                started = time.monotonic()
                driver.stdin.write("raise\n")
                driver.stdin.flush()
                self.assertEqual(response(), "1")
                self.assertTrue(w32.taskbar_above(hwnd), "Test window must first cover widget")
                while len(restored) <= index and time.monotonic() - started < 1.5:
                    pump()
                    time.sleep(0.002)
                self.assertEqual(len(restored), index + 1, observed)
                self.assertFalse(w32.taskbar_above(hwnd))
                latencies.append(restored[-1] - started)
            driver.stdin.write("overlap\n")
            driver.stdin.flush()
            self.assertEqual(response(), "1")
            self.assertFalse(w32.topmost_allowed(hwnd))
            pump()
            app.on_timer(widget.TIMER_CHECK)
            self.assertEqual(len(restored), 3, "An overlapping panel must retain its window order")
            driver.stdin.write("separate\n")
            driver.stdin.flush()
            self.assertEqual(response(), "1")
            deadline = time.monotonic() + 1.5
            while len(restored) < 4 and time.monotonic() < deadline:
                pump()
                time.sleep(0.002)
            self.assertEqual(len(restored), 4, "Restore when panel no longer covers widget")
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                pump()
                time.sleep(0.002)
            self.assertEqual(len(restored), 4, "Own reorder must not cause a feedback loop")
            self.assertEqual(w32.user32.GetForegroundWindow(), foreground_before)
            self.assertTrue(any(event[0] == widget.EVENT_OBJECT_REORDER for event in observed))
            print("Native restore latency (ms):", [round(value * 1000, 1) for value in latencies])
            print("Native taskbar/desktop event tuples:", sorted(set(observed)))
        finally:
            if w32:
                w32.unwatch_window_events()
                if hwnd:
                    w32.destroy_window(hwnd)
                w32.unregister_class()
            widget.WINDOW_CLASS = original_class
            try:
                if driver.poll() is None:
                    driver.stdin.write("quit\n")
                    driver.stdin.flush()
                driver.wait(timeout=3)
            except (BrokenPipeError, OSError):
                driver.kill()
                driver.wait(timeout=3)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.wait(timeout=3)
            reader.join(timeout=1)
            driver.stdin.close()
            driver.stdout.close()
            driver.stderr.close()


if __name__ == "__main__":
    if "--driver" in sys.argv:
        run_driver()
    else:
        unittest.main()
