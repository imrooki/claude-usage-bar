"""Theme polling: a light/dark flip repaints the widget once; an unchanged theme repaints nothing."""

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget


def make_app(data_dir, light):
    w32 = Mock()
    w32.taskbar_hwnd.return_value = 0
    w32.taskbar_pos.return_value = ((0, 1032, 1920, 1080), widget.ABE_BOTTOM)
    w32.taskbar_autohide.return_value = False
    w32.notification_state.return_value = 5
    w32.foreground_covers_taskbar.return_value = False
    w32.scale_for.return_value = 1.0
    w32.tray_notify_rect.return_value = None
    w32.topmost_allowed.return_value = True
    w32.set_timer.return_value = True
    options = widget.Options(data_dir, None, None, None, no_codex=True, no_app_refresh=True)
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, widget.DEFAULT_OFFSET)), \
            patch.object(widget, "read_light_theme", return_value=light):
        app = widget.WidgetApp(options, w32, Mock())
    app.hwnd = 10
    app.visible = True
    app._apply = widget.WidgetApp._apply.__get__(app, widget.WidgetApp)
    return app


class PollThemeTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.app = make_app(holder.name, light=False)
        self.app.refresh_view()
        self.assertEqual(self.app.w32.update_layered.call_count, 1)

    def poll_with(self, light):
        with patch.object(widget, "read_light_theme", return_value=light):
            self.app.poll_theme()

    def test_theme_flip_repaints_once_in_the_new_theme(self):
        self.poll_with(True)
        self.assertTrue(self.app.light)
        self.assertEqual(self.app.w32.update_layered.call_count, 2)
        self.assertEqual(self.app.shown_display.theme, "light")

    def test_unchanged_theme_does_not_refresh_or_repaint(self):
        with patch.object(self.app, "refresh_view") as refresh:
            self.poll_with(False)
        refresh.assert_not_called()
        self.assertFalse(self.app.light)
        self.assertEqual(self.app.w32.update_layered.call_count, 1)

    def test_each_flip_repaints_and_the_pixels_change_with_the_theme(self):
        self.poll_with(True)
        light_pixels = self.app.w32.update_layered.call_args.args[5]
        self.poll_with(False)
        dark_pixels = self.app.w32.update_layered.call_args.args[5]
        self.assertEqual(self.app.w32.update_layered.call_count, 3)
        self.assertNotEqual(light_pixels, dark_pixels)

    def test_repeated_polls_in_one_theme_repaint_only_on_the_change(self):
        # The first paint from setUp, then one repaint for each of the two flips.
        for light in (False, False, True, True, True, False):
            self.poll_with(light)
        self.assertEqual(self.app.w32.update_layered.call_count, 3)


if __name__ == "__main__":
    unittest.main()
