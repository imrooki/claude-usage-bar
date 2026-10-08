"""Error log bounds, dedupe and message rules; position file load and save rules, including atomic replace."""

import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (\w+): (.*)$")


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = holder.name
        self.path = os.path.join(self.dir, widget.POS_FILE_NAME)

    def write_bytes(self, raw):
        with open(self.path, "wb") as handle:
            handle.write(raw)


class ErrorLogTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.log_path = os.path.join(self.dir, widget.ERROR_LOG_NAME)

    def at(self, now):
        """Pin the dedupe clock. ErrorLog reads time.monotonic() once per call."""
        return patch.object(widget.time, "monotonic", return_value=now)

    def lines(self):
        with open(self.log_path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()

    def test_writes_one_timestamped_kind_and_message_line(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            self.assertTrue(log.log("Kind", "hello"))
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        match = LINE.match(lines[0])
        self.assertIsNotNone(match, lines[0])
        self.assertEqual(match.groups(), ("Kind", "hello"))

    def test_same_kind_and_message_are_written_once_per_dedupe_window(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            self.assertTrue(log.log("Kind", "again"))
            self.assertFalse(log.log("Kind", "again"))
        with self.at(1000.0 + widget.LOG_DEDUP_SECONDS - 1):
            self.assertFalse(log.log("Kind", "again"))
        with self.at(1000.0 + widget.LOG_DEDUP_SECONDS):
            self.assertTrue(log.log("Kind", "again"))
        self.assertEqual(len(self.lines()), 2)

    def test_same_message_under_another_kind_is_not_a_duplicate(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            self.assertTrue(log.log("First", "same"))
            self.assertTrue(log.log("Second", "same"))
        self.assertEqual(len(self.lines()), 2)

    def test_whitespace_runs_are_squashed_to_single_spaces(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            log.log("Kind", "first\n  second\t\tthird\r\nfourth")
        self.assertEqual(LINE.match(self.lines()[0]).group(2), "first second third fourth")

    def test_messages_are_capped_at_the_message_limit(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            log.log("Kind", "x" * 2000)
        message = LINE.match(self.lines()[0]).group(2)
        self.assertEqual(len(message), widget.LOG_MAX_MESSAGE)
        self.assertEqual(message, "x" * widget.LOG_MAX_MESSAGE)

    def test_key_table_stays_within_its_limit_when_every_key_is_fresh(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            for index in range(widget.LOG_MAX_KEYS):
                log.log("Kind", "key %d" % index)
            self.assertEqual(len(log._seen), widget.LOG_MAX_KEYS)
            log.log("Kind", "one more")
            # Nothing had expired, so the table is cleared and holds only the new key.
            self.assertEqual(len(log._seen), 1)
            for index in range(3 * widget.LOG_MAX_KEYS):
                log.log("Kind", "later %d" % index)
                self.assertLessEqual(len(log._seen), widget.LOG_MAX_KEYS)

    def test_expired_keys_are_pruned_while_fresh_keys_are_kept(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            for index in range(40):
                log.log("Kind", "old %d" % index)
        later = 1000.0 + widget.LOG_DEDUP_SECONDS + 1.0
        with self.at(later):
            for index in range(25):
                self.assertTrue(log.log("Kind", "new %d" % index))
            self.assertEqual(len(log._seen), 25)
            self.assertFalse(log.log("Kind", "new 0"))
            self.assertTrue(log.log("Kind", "old 0"))

    def test_no_data_directory_never_writes(self):
        for data_dir in (None, ""):
            with self.subTest(data_dir=data_dir):
                log = widget.ErrorLog(data_dir)
                with self.at(1000.0):
                    self.assertFalse(log.log("Kind", "msg"))

    def test_refused_write_does_not_use_up_the_dedupe_window(self):
        data_dir = os.path.join(self.dir, "not-yet")
        log = widget.ErrorLog(data_dir)
        # Both calls happen inside one dedupe window: only the write that actually lands may claim it.
        with self.at(1000.0):
            self.assertFalse(log.log("Kind", "again"))
            os.mkdir(data_dir)
            self.assertTrue(log.log("Kind", "again"))
        with open(os.path.join(data_dir, widget.ERROR_LOG_NAME), "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(LINE.match(lines[0]).groups(), ("Kind", "again"))

    def test_log_exception_records_the_exception_class_name(self):
        log = widget.ErrorLog(self.dir)
        with self.at(1000.0):
            self.assertTrue(log.log_exception(ValueError("bad value")))
        self.assertEqual(LINE.match(self.lines()[0]).groups(), ("ValueError", "bad value"))


class LoadPositionTests(TempDirTestCase):
    default = (widget.MODE_AUTO, widget.DEFAULT_OFFSET)

    def test_missing_file_defaults_to_auto_at_the_default_offset(self):
        self.assertEqual(widget.load_position(self.path), self.default)

    def test_unreadable_or_non_object_files_default_to_auto(self):
        for raw in (b"{not json", b"", b"[1, 2]", b'"manual"', b"\xff\xfe\x00"):
            with self.subTest(raw=raw):
                self.write_bytes(raw)
                self.assertEqual(widget.load_position(self.path), self.default)

    def test_only_schema_two_is_trusted(self):
        for schema in ("1", 1, True, "2", None):
            with self.subTest(schema=schema):
                self.write_bytes(json.dumps(
                    {"schema": schema, "mode": "manual", "offset_from_right": 50}).encode())
                self.assertEqual(widget.load_position(self.path), self.default)

    def test_manual_mode_keeps_its_offset(self):
        self.write_bytes(b'{"schema": 2, "mode": "manual", "offset_from_right": 260}')
        self.assertEqual(widget.load_position(self.path), (widget.MODE_MANUAL, 260))

    def test_manual_without_a_usable_offset_becomes_auto(self):
        cases = (
            b'{"schema": 2, "mode": "manual"}',
            b'{"schema": 2, "mode": "manual", "offset_from_right": true}',
            b'{"schema": 2, "mode": "manual", "offset_from_right": "260"}',
            b'{"schema": 2, "mode": "manual", "offset_from_right": NaN}',
            b'{"schema": 2, "mode": "manual", "offset_from_right": ' + b"1" * 400 + b"}",
        )
        for raw in cases:
            with self.subTest(raw=raw[-40:]):
                self.write_bytes(raw)
                self.assertEqual(widget.load_position(self.path), self.default)

    def test_auto_mode_keeps_the_stored_offset_for_the_next_drag(self):
        self.write_bytes(b'{"schema": 2, "mode": "auto", "offset_from_right": 250}')
        self.assertEqual(widget.load_position(self.path), (widget.MODE_AUTO, 250))
        self.write_bytes(b'{"schema": 2, "mode": "auto"}')
        self.assertEqual(widget.load_position(self.path), self.default)

    def test_unknown_mode_falls_back_to_auto_at_the_default_offset(self):
        self.write_bytes(b'{"schema": 2, "mode": "diagonal", "offset_from_right": 250}')
        self.assertEqual(widget.load_position(self.path), self.default)

    def test_offsets_are_clamped_to_the_offset_limit(self):
        for stored, expected in ((10 ** 9, widget.OFFSET_LIMIT), (-10 ** 9, -widget.OFFSET_LIMIT)):
            with self.subTest(stored=stored):
                self.write_bytes(json.dumps(
                    {"schema": 2, "mode": "manual", "offset_from_right": stored}).encode())
                self.assertEqual(widget.load_position(self.path), (widget.MODE_MANUAL, expected))
        self.write_bytes(b'{"schema": 2, "mode": "auto", "offset_from_right": 1000000000}')
        self.assertEqual(widget.load_position(self.path), (widget.MODE_AUTO, widget.OFFSET_LIMIT))

    def test_fractional_offsets_round_half_up(self):
        for stored, expected in ((259.5, 260), (259.4, 259)):
            with self.subTest(stored=stored):
                self.write_bytes(json.dumps(
                    {"schema": 2, "mode": "manual", "offset_from_right": stored}).encode())
                self.assertEqual(widget.load_position(self.path), (widget.MODE_MANUAL, expected))

    def test_byte_order_mark_is_accepted(self):
        self.write_bytes(b'\xef\xbb\xbf{"schema": 2, "mode": "manual", "offset_from_right": 260}')
        self.assertEqual(widget.load_position(self.path), (widget.MODE_MANUAL, 260))


class SavePositionTests(TempDirTestCase):
    def test_save_writes_schema_mode_and_offset_and_loads_back(self):
        self.assertTrue(widget.save_position(self.dir, widget.MODE_MANUAL, 260))
        with open(self.path, "rb") as handle:
            self.assertEqual(json.loads(handle.read().decode("utf-8")),
                             {"schema": 2, "mode": "manual", "offset_from_right": 260})
        self.assertEqual(widget.load_position(self.path), (widget.MODE_MANUAL, 260))

    def test_save_is_skipped_without_an_existing_data_directory(self):
        absent = os.path.join(self.dir, "absent")
        self.assertFalse(widget.save_position(absent, widget.MODE_MANUAL, 260))
        self.assertFalse(os.path.exists(absent))
        for data_dir in (None, ""):
            with self.subTest(data_dir=data_dir):
                self.assertFalse(widget.save_position(data_dir, widget.MODE_MANUAL, 260))

    def test_save_goes_through_a_temp_file_and_leaves_no_temp_behind(self):
        temp = self.path + ".tmp"
        with patch.object(widget.os, "replace", wraps=widget.os.replace) as replace:
            widget.save_position(self.dir, widget.MODE_AUTO, 330)
        replace.assert_called_once_with(temp, self.path)
        self.assertFalse(os.path.exists(temp))
        self.assertTrue(os.path.exists(self.path))

    def test_failed_replace_keeps_the_old_file_and_removes_the_temp_file(self):
        self.write_bytes(b'{"schema": 2, "mode": "auto", "offset_from_right": 330}\n')
        with open(self.path, "rb") as handle:
            before = handle.read()
        temp = self.path + ".tmp"
        with patch.object(widget.os, "replace", side_effect=OSError("file in use")):
            with self.assertRaises(OSError):
                widget.save_position(self.dir, widget.MODE_MANUAL, 260)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(temp))


if __name__ == "__main__":
    unittest.main()
