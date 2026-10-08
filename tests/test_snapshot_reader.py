"""Snapshot parsing rules, read_snapshot on real files, and SnapshotReader's (mtime, size) gate."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

WRITTEN = 1791000000.0
RESETS = WRITTEN + 3600.0
OBSERVED = WRITTEN - 60.0
MTIME_A = 1_700_000_000_000_000_000
MTIME_B = MTIME_A + 5_000_000_000
MTIME_C = MTIME_B + 5_000_000_000


def window(pct, resets_at=RESETS, observed_at=None):
    raw = {"used_percentage": pct, "resets_at": resets_at}
    if observed_at is not None:
        raw["observed_at"] = observed_at
    return raw


def document(five=None, seven=None, schema=1, written_at=WRITTEN):
    windows = {}
    if five is not None:
        windows["five_hour"] = five
    if seven is not None:
        windows["seven_day"] = seven
    return {"schema": schema, "written_at": written_at, "windows": windows}


def encode(doc):
    return json.dumps(doc).encode("utf-8")


class ParseSnapshotTests(unittest.TestCase):
    def test_valid_document_yields_both_windows(self):
        raw = encode(document(
            five=window(30, RESETS, OBSERVED),
            seven=window(12.5, RESETS + 86400.0, OBSERVED)))
        snapshot, reason = widget.parse_snapshot(raw)
        self.assertEqual(reason, "")
        self.assertEqual(snapshot, {
            "five_hour": widget.Win(30.0, RESETS, OBSERVED),
            "seven_day": widget.Win(12.5, RESETS + 86400.0, OBSERVED),
        })

    def test_observed_at_falls_back_to_written_at(self):
        """A missing or unusable observed_at takes the document's written_at."""
        for observed in (None, True, 10 ** 400, "soon", float("-inf")):
            with self.subTest(observed_at=observed):
                snapshot, reason = widget.parse_snapshot(
                    encode(document(five=window(30, observed_at=observed))))
                self.assertEqual(reason, "")
                self.assertEqual(snapshot["five_hour"].observed_at, WRITTEN)

    def test_window_without_any_observation_time_is_dropped(self):
        raw = encode({"schema": 1, "windows": {"five_hour": window(30)}})
        snapshot, reason = widget.parse_snapshot(raw)
        self.assertEqual(reason, "")
        self.assertIsNone(snapshot["five_hour"])

    def test_window_without_a_usable_resets_at_is_dropped(self):
        for bad in (None, 0, -5, "later", True, float("nan")):
            with self.subTest(resets_at=bad):
                raw = {"used_percentage": 30, "resets_at": bad, "observed_at": OBSERVED}
                snapshot, _reason = widget.parse_snapshot(encode(document(five=raw)))
                self.assertIsNone(snapshot["five_hour"])
        missing = {"used_percentage": 30, "observed_at": OBSERVED}
        snapshot, _reason = widget.parse_snapshot(encode(document(five=missing)))
        self.assertIsNone(snapshot["five_hour"])

    def test_negative_percentage_is_dropped_and_zero_is_kept(self):
        snapshot, _reason = widget.parse_snapshot(encode(document(five=window(-0.5, observed_at=OBSERVED))))
        self.assertIsNone(snapshot["five_hour"])
        snapshot, _reason = widget.parse_snapshot(encode(document(five=window(0, observed_at=OBSERVED))))
        self.assertEqual(snapshot["five_hour"], widget.Win(0.0, RESETS, OBSERVED))

    def test_bool_huge_and_non_finite_percentages_are_dropped(self):
        for bad in (True, False, "30", None, 10 ** 400, float("nan"), float("inf")):
            with self.subTest(used_percentage=bad):
                raw = {"used_percentage": bad, "resets_at": RESETS, "observed_at": OBSERVED}
                snapshot, _reason = widget.parse_snapshot(encode(document(five=raw)))
                self.assertIsNone(snapshot["five_hour"])

    def test_json_nan_and_infinity_tokens_are_not_numbers(self):
        raw = (b'{"schema":1,"written_at":1791000000.0,"windows":{'
               b'"five_hour":{"used_percentage":NaN,"resets_at":1791003600.0},'
               b'"seven_day":{"used_percentage":-Infinity,"resets_at":1791003600.0}}}')
        snapshot, reason = widget.parse_snapshot(raw)
        self.assertEqual(reason, "")
        self.assertEqual(snapshot, {"five_hour": None, "seven_day": None})

    def test_schema_must_be_the_number_one(self):
        for schema in (True, False, 2, "1", None):
            with self.subTest(schema=schema):
                self.assertEqual(
                    widget.parse_snapshot(encode(document(five=window(30), schema=schema))),
                    (None, "bad_schema"))
        self.assertEqual(
            widget.parse_snapshot(encode({"windows": {}})), (None, "bad_schema"))

    def test_malformed_documents_name_their_reason(self):
        cases = (
            (b"", "bad_json"),
            (b"{not json", "bad_json"),
            (b"\xff\xfe\x00", "bad_json"),
            (b"[" * 100000, "bad_json"),
            (b"[1, 2]", "not_object"),
            (b'"text"', "not_object"),
            (encode({"schema": 1, "windows": [1]}), "bad_windows"),
            (encode({"schema": 1}), "bad_windows"),
        )
        for raw, reason in cases:
            with self.subTest(raw=raw[:12], reason=reason):
                self.assertEqual(widget.parse_snapshot(raw), (None, reason))

    def test_utf8_bom_is_accepted(self):
        raw = b"\xef\xbb\xbf" + encode(document(five=window(30, observed_at=OBSERVED)))
        snapshot, reason = widget.parse_snapshot(raw)
        self.assertEqual(reason, "")
        self.assertEqual(snapshot["five_hour"].pct, 30.0)

    def test_size_limit_is_inclusive(self):
        """A document of exactly MAX_SNAPSHOT_BYTES is parsed; one byte more is too large."""
        base = encode(document(five=window(30, observed_at=OBSERVED)))
        padded = base + b" " * (widget.MAX_SNAPSHOT_BYTES - len(base))
        self.assertEqual(len(padded), widget.MAX_SNAPSHOT_BYTES)
        self.assertEqual(widget.parse_snapshot(padded)[1], "")
        self.assertEqual(widget.parse_snapshot(padded + b" "), (None, "too_large"))


class ReadSnapshotTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.path = os.path.join(holder.name, widget.SNAPSHOT_NAME)

    def test_missing_file_reports_missing(self):
        self.assertEqual(widget.read_snapshot(self.path), (None, "missing"))

    def test_reads_and_parses_the_file(self):
        with open(self.path, "wb") as handle:
            handle.write(encode(document(five=window(30, observed_at=OBSERVED))))
        snapshot, reason = widget.read_snapshot(self.path)
        self.assertEqual(reason, "")
        self.assertEqual(snapshot["five_hour"], widget.Win(30.0, RESETS, OBSERVED))

    def test_file_one_byte_over_the_limit_is_too_large(self):
        with open(self.path, "wb") as handle:
            handle.write(b" " * (widget.MAX_SNAPSHOT_BYTES + 1))
        self.assertEqual(widget.read_snapshot(self.path), (None, "too_large"))

    def test_unreadable_path_reports_read_error(self):
        # A directory cannot be opened for reading on either platform.
        snapshot, reason = widget.read_snapshot(os.path.dirname(self.path))
        self.assertIsNone(snapshot)
        self.assertTrue(reason.startswith("read_error:"), reason)


class SnapshotReaderTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.path = os.path.join(holder.name, widget.SNAPSHOT_NAME)
        self.reader = widget.SnapshotReader(self.path)

    def put(self, raw, mtime_ns):
        """Write the file and pin its mtime so the signature changes (or does not) on purpose."""
        with open(self.path, "wb") as handle:
            handle.write(raw)
        os.utime(self.path, ns=(mtime_ns, mtime_ns))

    def read_spy(self):
        return patch.object(widget, "read_snapshot", wraps=widget.read_snapshot)

    def test_first_read_parses_and_reports_a_change(self):
        self.put(encode(document(five=window(30, observed_at=OBSERVED))), MTIME_A)
        self.assertTrue(self.reader.refresh())
        self.assertEqual(self.reader.snapshot["five_hour"], widget.Win(30.0, RESETS, OBSERVED))
        self.assertEqual(self.reader.last_reason, "")

    def test_unchanged_mtime_and_size_skips_the_read(self):
        self.put(encode(document(five=window(30, observed_at=OBSERVED))), MTIME_A)
        self.reader.refresh()
        with self.read_spy() as spy:
            self.assertFalse(self.reader.refresh())
            self.assertFalse(self.reader.refresh())
        spy.assert_not_called()

    def test_new_mtime_rereads_but_reports_no_change_for_identical_content(self):
        raw = encode(document(five=window(30, observed_at=OBSERVED)))
        self.put(raw, MTIME_A)
        self.reader.refresh()
        self.put(raw, MTIME_B)
        with self.read_spy() as spy:
            self.assertFalse(self.reader.refresh())
        self.assertEqual(spy.call_count, 1)

    def test_new_content_reports_a_change(self):
        self.put(encode(document(five=window(30, observed_at=OBSERVED))), MTIME_A)
        self.reader.refresh()
        self.put(encode(document(five=window(31, observed_at=OBSERVED))), MTIME_B)
        self.assertTrue(self.reader.refresh())
        self.assertEqual(self.reader.snapshot["five_hour"].pct, 31.0)

    def test_force_rereads_even_when_the_signature_is_unchanged(self):
        first = encode(document(five=window(30, observed_at=OBSERVED)))
        second = encode(document(five=window(31, observed_at=OBSERVED)))
        self.assertEqual(len(first), len(second))
        self.put(first, MTIME_A)
        self.reader.refresh()
        # Same size and mtime as before: the gate skips it, only force sees the new bytes.
        self.put(second, MTIME_A)
        self.assertFalse(self.reader.refresh())
        self.assertEqual(self.reader.snapshot["five_hour"].pct, 30.0)
        self.assertTrue(self.reader.refresh(force=True))
        self.assertEqual(self.reader.snapshot["five_hour"].pct, 31.0)

    def test_bad_json_keeps_the_last_good_snapshot(self):
        good = encode(document(five=window(30, observed_at=OBSERVED)))
        self.put(good, MTIME_A)
        self.reader.refresh()
        good_snapshot = self.reader.snapshot
        self.put(b'{"schema": 1, "windows": {', MTIME_B)
        self.assertFalse(self.reader.refresh())
        self.assertEqual(self.reader.snapshot, good_snapshot)
        self.assertEqual(self.reader.last_reason, "bad_json")

    def test_failed_read_is_not_cached_and_recovers_when_the_file_is_fixed(self):
        """The signature is stored only after a successful read, so a bad file is read again."""
        self.put(b"{truncated", MTIME_A)
        with self.read_spy() as spy:
            self.reader.refresh()
            self.reader.refresh()
        self.assertEqual(spy.call_count, 2)
        self.assertIsNone(self.reader.snapshot)
        self.assertEqual(self.reader.last_reason, "bad_json")
        self.put(encode(document(five=window(30, observed_at=OBSERVED))), MTIME_C)
        self.assertTrue(self.reader.refresh())
        self.assertEqual(self.reader.snapshot["five_hour"].pct, 30.0)
        self.assertEqual(self.reader.last_reason, "")

    def test_missing_file_on_first_read_reports_missing(self):
        self.assertFalse(self.reader.refresh())
        self.assertEqual(self.reader.last_reason, "missing")
        self.assertIsNone(self.reader.snapshot)


if __name__ == "__main__":
    unittest.main()
