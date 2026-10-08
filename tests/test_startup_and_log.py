"""Startup and error log: the data folder, parse_args warnings, log rotation and the no-file caption.

Everything runs on temporary folders with the window (run_app) and the console (say) patched out.
"""

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget


NOW = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))


def at(hour, minute):
    return time.mktime((2026, 10, 2, hour, minute, 0, 0, 0, -1))


class TempFolderTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name

    def write_bytes(self, path, raw):
        with open(path, "wb") as handle:
            handle.write(raw)

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def read_text(self, path):
        return self.read_bytes(path).decode("utf-8")


# ---------------------------------------------------------------------------
# Data folder: main creates it, and says so once when it cannot
# ---------------------------------------------------------------------------

class DataFolderStartupTests(TempFolderTestCase):
    def run_main(self, argv):
        """main with the window stubbed out. Returns (exit code, say mock, run_app mock)."""
        with patch.object(widget, "run_app", return_value=0) as run_app, \
                patch.object(widget, "say") as say:
            code = widget.main(argv)
        return code, say, run_app

    def test_missing_data_folder_is_created_before_the_app_starts(self):
        target = os.path.join(self.root, "nested", "data")
        code, say, run_app = self.run_main(["--data-dir", target])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(target))
        say.assert_not_called()
        run_app.assert_called_once()
        self.assertEqual(run_app.call_args.args[0].data_dir, os.path.abspath(target))

    def test_existing_data_folder_is_left_alone(self):
        target = os.path.join(self.root, "data")
        os.mkdir(target)
        marker = os.path.join(target, "usage.json")
        self.write_bytes(marker, b"{}")
        code, say, run_app = self.run_main(["--data-dir", target])
        self.assertEqual(code, 0)
        say.assert_not_called()
        self.assertEqual(self.read_bytes(marker), b"{}")

    def test_uncreatable_data_folder_is_reported_once_and_startup_continues(self):
        blocker = os.path.join(self.root, "blocker")
        self.write_bytes(blocker, b"a file, not a folder")
        target = os.path.join(blocker, "data")
        code, say, run_app = self.run_main(["--data-dir", target])
        self.assertEqual(code, 0)
        run_app.assert_called_once()
        self.assertEqual(say.call_count, 1)
        message = say.call_args.args[0]
        self.assertIn(os.path.abspath(target), message)
        self.assertIn("could not be created", message)

    def test_selftest_render_does_not_create_the_data_folder(self):
        target = os.path.join(self.root, "data")
        with patch.object(widget, "selftest_render", return_value=0) as render, \
                patch.object(widget, "run_app") as run_app:
            code = widget.main(
                ["--data-dir", target, "--selftest-render", os.path.join(self.root, "out")])
        self.assertEqual(code, 0)
        render.assert_called_once()
        run_app.assert_not_called()
        self.assertFalse(os.path.exists(target))


class WarningOutputTests(TempFolderTestCase):
    def test_each_warning_is_logged_and_printed_once(self):
        target = os.path.join(self.root, "data")
        with patch.object(widget, "run_app", return_value=0), \
                patch.object(widget, "say") as say:
            widget.main(["--data-dir", target, "--bogus", "--exit-after=abc"])
        printed = [item.args[0] for item in say.call_args_list]
        self.assertEqual(len(printed), 2)
        lines = self.read_text(os.path.join(target, widget.ERROR_LOG_NAME)).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("Argument: " + printed[0]))
        self.assertTrue(lines[1].endswith("Argument: " + printed[1]))

    def test_clean_arguments_write_no_log_and_print_nothing(self):
        target = os.path.join(self.root, "data")
        with patch.object(widget, "run_app", return_value=0), \
                patch.object(widget, "say") as say:
            widget.main(["--data-dir", target, "--no-codex-ping"])
        say.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(target, widget.ERROR_LOG_NAME)))


# ---------------------------------------------------------------------------
# parse_args: lenient as before, but every ignored piece is reported
# ---------------------------------------------------------------------------

class ParseArgsWarningTests(unittest.TestCase):
    def test_valid_arguments_produce_no_warnings(self):
        opts = widget.parse_args([
            "--data-dir", os.path.join("rel", "data"),
            "--exit-after=1.5",
            "--selftest-gdi", "4",
            "--selftest-render", os.path.join("rel", "out"),
            "--codex-home=%s" % os.path.join("rel", "home"),
            "--codex-bin", os.path.join("rel", "codex.exe"),
            "--codex-ping-model", "gpt-6-luna",
            "--no-codex", "--no-codex-ping", "--no-app-refresh",
        ])
        self.assertEqual(list(opts.warnings), [])
        self.assertEqual(opts.exit_after, 1.5)
        self.assertEqual(opts.selftest_gdi, 4)
        self.assertEqual(opts.codex_ping_model, "gpt-6-luna")

    def test_defaults_are_unchanged_and_silent(self):
        opts = widget.parse_args([])
        self.assertEqual(
            opts.data_dir,
            os.path.join(os.path.dirname(os.path.abspath(widget.__file__)), "data"))
        self.assertIsNone(opts.exit_after)
        self.assertIsNone(opts.selftest_render)
        self.assertIsNone(opts.selftest_gdi)
        self.assertFalse(opts.no_codex)
        self.assertIsNone(opts.codex_home)
        self.assertIsNone(opts.codex_bin)
        self.assertFalse(opts.no_codex_ping)
        self.assertIsNone(opts.codex_ping_model)
        self.assertFalse(opts.no_app_refresh)
        self.assertEqual(list(opts.warnings), [])

    def test_unknown_token_gives_one_warning(self):
        opts = widget.parse_args(["--bogus=1", "--no-codex"])
        self.assertEqual(len(opts.warnings), 1)
        self.assertIn("--bogus=1", opts.warnings[0])
        self.assertTrue(opts.no_codex)

    def test_bad_model_gives_one_warning_and_keeps_the_default(self):
        opts = widget.parse_args(["--codex-ping-model=bad name"])
        self.assertIsNone(opts.codex_ping_model)
        self.assertEqual(len(opts.warnings), 1)
        self.assertIn("--codex-ping-model", opts.warnings[0])

    def test_bad_number_gives_one_warning_each(self):
        cases = (
            ["--exit-after=abc"],
            ["--exit-after", "nan"],
            ["--exit-after=-1"],
            ["--selftest-gdi=0"],
            ["--selftest-gdi", "two"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                opts = widget.parse_args(argv)
                self.assertEqual(len(opts.warnings), 1)
                self.assertIsNone(opts.exit_after)
                self.assertIsNone(opts.selftest_gdi)

    def test_missing_value_gives_one_warning_and_the_next_flag_still_works(self):
        for argv in (["--data-dir"], ["--data-dir="], ["--codex-bin", "--no-codex"]):
            with self.subTest(argv=argv):
                opts = widget.parse_args(argv)
                self.assertEqual(len(opts.warnings), 1)
                self.assertIn("no value given", opts.warnings[0])
        opts = widget.parse_args(["--codex-bin", "--no-codex"])
        self.assertIsNone(opts.codex_bin)
        self.assertTrue(opts.no_codex)

    def test_each_warning_is_short_ascii(self):
        opts = widget.parse_args([
            "--bogus=" + "é" * 300,
            "--codex-ping-model=" + "中" * 80,
        ])
        self.assertEqual(len(opts.warnings), 2)
        for text in opts.warnings:
            with self.subTest(text=text[:30]):
                self.assertTrue(text.isascii())
                self.assertLessEqual(len(text), 100)

    def test_parse_args_does_no_io(self):
        with patch("builtins.open", side_effect=AssertionError("open")), \
                patch.object(os, "makedirs", side_effect=AssertionError("makedirs")), \
                patch.object(os, "replace", side_effect=AssertionError("replace")):
            opts = widget.parse_args(
                ["--data-dir", "x", "--bogus", "--exit-after=abc", "--codex-ping-model=a b"])
        self.assertEqual(len(opts.warnings), 3)

    def test_warnings_is_the_last_field_and_defaults_to_empty(self):
        self.assertEqual(widget.Options._fields[-1], "warnings")
        self.assertEqual(widget.Options(".", None, None, None).warnings, ())


# ---------------------------------------------------------------------------
# ErrorLog: a full log moves aside to .1 instead of being wiped
# ---------------------------------------------------------------------------

class ErrorLogRotationTests(TempFolderTestCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, widget.ERROR_LOG_NAME)
        self.aside = self.path + ".1"

    @staticmethod
    def oversized(marker):
        """More than LOG_MAX_BYTES of text, starting with marker."""
        return (marker + "\n" + "x" * widget.LOG_MAX_BYTES + "\n").encode("utf-8")

    def test_full_log_moves_aside_and_the_new_file_starts_small(self):
        old = self.oversized("first run: startup error")
        self.write_bytes(self.path, old)
        log = widget.ErrorLog(self.root)
        self.assertTrue(log.log("Startup", "fresh message"))
        self.assertEqual(self.read_bytes(self.aside), old)
        current = self.read_text(self.path)
        self.assertEqual(current.count("\n"), 1)
        self.assertIn("Startup: fresh message", current)
        self.assertLess(os.path.getsize(self.path), 200)

    def test_a_second_rotation_replaces_the_older_copy(self):
        self.write_bytes(self.path, self.oversized("first"))
        log = widget.ErrorLog(self.root)
        self.assertTrue(log.log("A", "one"))
        self.write_bytes(self.path, self.oversized("second"))
        self.assertTrue(log.log("B", "two"))
        aside = self.read_text(self.aside)
        self.assertTrue(aside.startswith("second\n"))
        self.assertNotIn("first", aside)
        self.assertNotIn("B: two", aside)
        current = self.read_text(self.path)
        self.assertEqual(current.count("\n"), 1)
        self.assertIn("B: two", current)

    def test_failed_rename_falls_back_to_truncation_without_raising(self):
        self.write_bytes(self.path, self.oversized("old"))
        log = widget.ErrorLog(self.root)
        with patch.object(widget.os, "replace", side_effect=PermissionError("locked")):
            self.assertTrue(log.log("Kind", "new"))
        self.assertFalse(os.path.exists(self.aside))
        current = self.read_text(self.path)
        self.assertEqual(current.count("\n"), 1)
        self.assertIn("Kind: new", current)

    def test_a_small_log_is_appended_without_rotation(self):
        self.write_bytes(self.path, b"earlier line\n")
        log = widget.ErrorLog(self.root)
        self.assertTrue(log.log("Kind", "later"))
        self.assertFalse(os.path.exists(self.aside))
        lines = self.read_text(self.path).splitlines()
        self.assertEqual(lines[0], "earlier line")
        self.assertIn("Kind: later", lines[1])

    def test_exactly_the_limit_is_not_rotated(self):
        self.write_bytes(self.path, b"x" * widget.LOG_MAX_BYTES)
        log = widget.ErrorLog(self.root)
        self.assertTrue(log.log("Kind", "at the limit"))
        self.assertFalse(os.path.exists(self.aside))

    def test_log_does_not_create_a_missing_folder(self):
        missing = os.path.join(self.root, "absent")
        self.assertFalse(widget.ErrorLog(missing).log("Kind", "lost"))
        self.assertFalse(os.path.exists(missing))


# ---------------------------------------------------------------------------
# Caption: a missing file is "no file", not "same HH:MM"
# ---------------------------------------------------------------------------

class NoFileCaptionTests(unittest.TestCase):
    def test_missing_with_a_retained_snapshot_is_no_file(self):
        self.assertEqual(widget.caption_text(at(9, 5), at(9, 5), "missing"), "no file")
        self.assertEqual(widget.caption_text(at(9, 5), at(14, 58), "missing"), "no file")
        self.assertEqual(widget.caption_text(None, at(9, 5), "missing"), "no file")

    def test_missing_without_a_snapshot_is_no_file(self):
        self.assertEqual(widget.caption_text(None, None, "missing"), "no file")
        self.assertEqual(widget.caption_text(at(9, 5), None, "missing"), "no file")

    def test_other_reasons_are_unchanged(self):
        self.assertEqual(widget.caption_text(None, at(9, 5), ""), "new 09:05")
        self.assertEqual(widget.caption_text(at(9, 5), at(9, 5), ""), "same 09:05")
        self.assertEqual(widget.caption_text(None, None, ""), "no data")
        self.assertEqual(widget.caption_text(None, None, "no_codex"), "no data")
        self.assertEqual(widget.caption_text(at(9, 5), at(9, 5), "no_rate_limits"), "same 09:05")
        self.assertEqual(widget.caption_text(None, None, "bad_json"), "read error")
        self.assertEqual(
            widget.caption_text(at(9, 5), at(9, 5), "read_error:PermissionError"), "read error")

    def test_no_file_is_short_ascii(self):
        self.assertEqual(widget.CAPTION_NO_FILE, "no file")
        self.assertLessEqual(len(widget.CAPTION_NO_FILE), 8)
        self.assertTrue(widget.CAPTION_NO_FILE.isascii())

    def test_no_file_fits_inside_each_dual_block_header(self):
        five = widget.Win(30.0, NOW + 3600.0, NOW - 60.0)
        seven = widget.Win(10.0, NOW + 2 * 86400.0, NOW - 60.0)
        for scale in (1.0, 1.25, 1.5, 2.0):
            with self.subTest(scale=scale):
                height = widget.scaled(widget.LOGICAL_H, scale)
                display = widget.build_display(
                    {"five_hour": five, "seven_day": seven}, NOW, "light", scale, height,
                    codex={"five_hour": five, "seven_day": seven}, codex_available=True,
                    captions=("no file", "no file"))
                self.assertEqual(display.kind, "dual")
                header_font = widget.get_font(
                    widget.dual_render_metrics(scale, height)["header_font_px"])
                text_width = header_font.getlength("no file")
                for edges in widget.dual_pixel_columns(scale):
                    center = (edges["bar"][0] + edges["bar"][1]) / 2.0
                    self.assertGreaterEqual(center - text_width / 2.0, edges["label"][0])
                    self.assertLessEqual(center + text_width / 2.0, edges["reset"][1])


class ReaderMissingTests(TempFolderTestCase):
    @staticmethod
    def usage_doc():
        return {
            "schema": 1,
            "written_at": NOW,
            "windows": {
                "five_hour": {"used_percentage": 30.0, "resets_at": NOW + 3600.0,
                              "observed_at": NOW - 60.0},
                "seven_day": {"used_percentage": 10.0, "resets_at": NOW + 2 * 86400.0,
                              "observed_at": NOW - 60.0},
            },
        }

    def test_deleted_usage_file_keeps_the_numbers_and_says_no_file(self):
        path = os.path.join(self.root, widget.SNAPSHOT_NAME)
        self.write_bytes(path, json.dumps(self.usage_doc()).encode("utf-8"))
        reader = widget.SnapshotReader(path)
        self.assertTrue(reader.refresh())
        before = widget._snapshot_observed_at(reader.snapshot)
        os.remove(path)
        self.assertFalse(reader.refresh())
        self.assertEqual(reader.last_reason, "missing")
        self.assertIsNotNone(reader.snapshot)
        after = widget._snapshot_observed_at(reader.snapshot)
        self.assertEqual(before, after)
        self.assertEqual(widget.caption_text(before, after, reader.last_reason), "no file")

    def test_codex_without_a_sessions_folder_reports_no_codex_not_missing(self):
        reader = widget.CodexReader(home=os.path.join(self.root, "no-codex-here"))
        reader.refresh()
        self.assertEqual(reader.last_reason, "no_codex")
        self.assertIsNone(reader.snapshot)
        caption = widget.caption_text(
            None, widget._snapshot_observed_at(reader.snapshot), reader.last_reason)
        self.assertEqual(caption, "no data")


if __name__ == "__main__":
    unittest.main()
