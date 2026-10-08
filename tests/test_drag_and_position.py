"""Dragging the widget: move threshold, clamping, capture handling, and the manual position saved on drop."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

HWND = 10
TASKBAR = ((0, 1032, 1920, 1080), widget.ABE_BOTTOM)
TASKBAR_RIGHT = 1920
WIDGET_Y = 1036          # 40 px window centred in the 48 px taskbar
START_X = 600
HEIGHT = 40
RESTING_CURSOR = (500, 1050)


def real_width():
    """The width _refresh_once will compute for the nodata display, so drop positions line up."""
    return widget.build_display(None, 0.0, "dark", 1.0, HEIGHT).width


def make_app(data_dir):
    w32 = Mock()
    w32.taskbar_hwnd.return_value = 0
    w32.taskbar_pos.return_value = TASKBAR
    w32.taskbar_autohide.return_value = False
    w32.notification_state.return_value = 5
    w32.foreground_covers_taskbar.return_value = False
    w32.scale_for.return_value = 1.0
    w32.tray_notify_rect.return_value = None
    w32.topmost_allowed.return_value = True
    w32.set_timer.return_value = True
    w32.cursor_pos.return_value = RESTING_CURSOR
    options = widget.Options(data_dir, None, None, None, no_codex=True, no_app_refresh=True)
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, widget.DEFAULT_OFFSET)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(options, w32, Mock())
    app.hwnd = HWND
    app.visible = True
    app._apply = Mock()
    app.taskbar = TASKBAR
    app.scale = 1.0
    app.geom = (START_X, WIDGET_Y, real_width(), HEIGHT)
    return app


def offset_for(left, width):
    """The saved offset that puts a window with this left edge at the taskbar's right end."""
    return TASKBAR_RIGHT - (left + width)


class DragTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.data_dir = holder.name
        patcher = patch.object(widget, "save_position")
        self.save = patcher.start()
        self.addCleanup(patcher.stop)
        self.app = make_app(self.data_dir)
        self.width = self.app.geom[2]

    def cursor(self, x):
        self.app.w32.cursor_pos.return_value = (x, RESTING_CURSOR[1])

    def press(self, x):
        self.cursor(x)
        self.app.on_left_down()

    def drag_to(self, x):
        self.cursor(x)
        self.app.on_mouse_move(widget.MK_LBUTTON)

    def test_drag_moves_the_window_and_switches_to_manual_mode(self):
        app = self.app
        self.press(500)
        self.drag_to(540)
        app.w32.move_window.assert_called_once_with(HWND, START_X + 40, WIDGET_Y)
        app.on_left_up()
        self.save.assert_called_once_with(
            self.data_dir, widget.MODE_MANUAL, offset_for(START_X + 40, self.width))
        self.assertEqual(app.mode, widget.MODE_MANUAL)
        self.assertEqual(app.offset, offset_for(START_X + 40, self.width))
        self.assertIsNone(app.drag)
        app.w32.release_capture.assert_called_once_with()

    def test_dropped_window_stays_where_the_manual_layout_puts_it(self):
        """After the drop the layout is recomputed from the saved offset and agrees with the drop point."""
        self.press(500)
        self.drag_to(540)
        self.app.on_left_up()
        drop_geom = self.app._apply.call_args.args[1]
        self.assertEqual(drop_geom[:2], (START_X + 40, WIDGET_Y))

    def test_movement_is_clamped_to_both_ends_of_the_taskbar(self):
        for cursor, expected_x in ((-2000, 0), (9000, TASKBAR_RIGHT - self.width)):
            with self.subTest(cursor=cursor):
                app = make_app(self.data_dir)
                width = app.geom[2]
                app.w32.cursor_pos.return_value = RESTING_CURSOR
                app.on_left_down()
                app.w32.cursor_pos.return_value = (cursor, RESTING_CURSOR[1])
                app.on_mouse_move(widget.MK_LBUTTON)
                app.w32.move_window.assert_called_once_with(HWND, expected_x, WIDGET_Y)
                self.assertEqual(app.geom[0], expected_x)
                self.assertEqual(app.geom[2], width)

    def test_capture_lost_mid_drag_ends_the_drag_and_saves_where_it_was(self):
        self.press(500)
        self.drag_to(560)
        self.app.on_capture_lost()
        self.assertIsNone(self.app.drag)
        self.app.w32.release_capture.assert_not_called()
        self.save.assert_called_once_with(
            self.data_dir, widget.MODE_MANUAL, offset_for(START_X + 60, self.width))
        self.app.on_left_up()
        self.save.assert_called_once()

    def test_capture_lost_without_movement_saves_nothing(self):
        self.press(500)
        self.app.on_capture_lost()
        self.assertIsNone(self.app.drag)
        self.save.assert_not_called()
        self.assertEqual(self.app.mode, widget.MODE_AUTO)

    def test_release_capture_re_entering_the_capture_handler_finishes_once(self):
        """ReleaseCapture sends WM_CAPTURECHANGED synchronously; the drop must not be saved twice."""
        self.app.w32.release_capture.side_effect = self.app.on_capture_lost
        self.press(500)
        self.drag_to(540)
        self.app.on_left_up()
        self.save.assert_called_once()
        self.app.w32.release_capture.assert_called_once_with()

    def test_capture_message_raised_by_set_capture_does_not_end_the_new_drag(self):
        """on_left_down takes capture before it creates the drag state, so the echo is harmless."""
        self.app.w32.set_capture.side_effect = lambda _hwnd: self.app.on_capture_lost()
        self.press(500)
        self.assertIsNotNone(self.app.drag)
        self.save.assert_not_called()
        self.app.w32.release_capture.assert_not_called()

    def test_click_without_movement_keeps_auto_mode(self):
        self.press(500)
        self.drag_to(503)
        self.app.on_left_up()
        self.app.w32.move_window.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.app.mode, widget.MODE_AUTO)
        self.assertEqual(self.app.offset, widget.DEFAULT_OFFSET)

    def test_movement_must_be_more_than_the_drag_threshold(self):
        """Three pixels either way is still a click; four is a drag."""
        for cursor, moved in ((497, False), (503, False), (496, True), (504, True)):
            with self.subTest(cursor=cursor):
                self.save.reset_mock()
                app = make_app(self.data_dir)
                app.w32.cursor_pos.return_value = RESTING_CURSOR
                app.on_left_down()
                app.w32.cursor_pos.return_value = (cursor, RESTING_CURSOR[1])
                app.on_mouse_move(widget.MK_LBUTTON)
                app.on_left_up()
                self.assertEqual(app.mode == widget.MODE_MANUAL, moved)
                self.assertEqual(self.save.called, moved)

    def test_once_past_the_threshold_returning_to_the_start_still_counts_as_a_drag(self):
        self.press(500)
        self.drag_to(510)
        self.drag_to(500)
        self.app.on_left_up()
        self.assertEqual(self.app.mode, widget.MODE_MANUAL)
        self.save.assert_called_once_with(
            self.data_dir, widget.MODE_MANUAL, offset_for(START_X, self.width))

    def test_move_without_the_button_held_finishes_the_drag_at_the_last_position(self):
        self.press(500)
        self.drag_to(540)
        self.cursor(560)
        self.app.on_mouse_move(0)
        self.assertIsNone(self.app.drag)
        self.app.w32.release_capture.assert_called_once_with()
        self.assertEqual(self.app.w32.move_window.call_count, 1)
        self.save.assert_called_once_with(
            self.data_dir, widget.MODE_MANUAL, offset_for(START_X + 40, self.width))
        self.app.on_left_up()
        self.save.assert_called_once()

    def test_press_without_a_placed_window_or_with_a_menu_open_does_nothing(self):
        self.app.geom = None
        self.app.on_left_down()
        self.assertIsNone(self.app.drag)
        self.app.geom = (START_X, WIDGET_Y, self.width, HEIGHT)
        self.app.menu_open = True
        self.app.on_left_down()
        self.assertIsNone(self.app.drag)
        self.app.w32.set_capture.assert_not_called()

    def test_right_click_during_a_drag_opens_no_menu(self):
        self.press(500)
        self.app.on_right_up()
        self.app.w32.track_menu.assert_not_called()

    def test_snap_to_tray_returns_to_auto_and_keeps_the_stored_offset(self):
        self.app.mode = widget.MODE_MANUAL
        self.app.offset = 260
        self.app.w32.track_menu.return_value = widget.MENU_SNAP
        self.app.on_right_up()
        self.assertEqual((self.app.mode, self.app.offset), (widget.MODE_AUTO, 260))
        self.save.assert_called_once_with(self.data_dir, widget.MODE_AUTO, 260)


class DropPersistenceTests(unittest.TestCase):
    """No save_position patch: the drop goes to a real position file and a new app reads it back."""

    def test_dropped_position_is_written_and_restored_by_the_next_app(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        app = make_app(holder.name)
        width = app.geom[2]
        expected = offset_for(START_X + 40, width)
        app.w32.cursor_pos.return_value = RESTING_CURSOR
        app.on_left_down()
        app.w32.cursor_pos.return_value = (540, RESTING_CURSOR[1])
        app.on_mouse_move(widget.MK_LBUTTON)
        app.on_left_up()

        path = os.path.join(holder.name, widget.POS_FILE_NAME)
        with open(path, "rb") as handle:
            self.assertEqual(json.loads(handle.read().decode("utf-8")),
                             {"schema": 2, "mode": "manual", "offset_from_right": expected})

        options = widget.Options(holder.name, None, None, None, no_codex=True, no_app_refresh=True)
        with patch.object(widget, "read_light_theme", return_value=False):
            restarted = widget.WidgetApp(options, Mock(), Mock())
        self.assertEqual((restarted.mode, restarted.offset), (widget.MODE_MANUAL, expected))


if __name__ == "__main__":
    unittest.main()
