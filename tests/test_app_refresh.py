"""App refresh: ack reader, caption helper, AppRefresher, WidgetApp wiring, options, source guards and samples."""

import ast
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, mock_open, patch

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget
from test_codex_ping import FROZEN_CTYPES


# Injected ids, one per start() that gets as far as building the request.
REFRESH_TOKENS = (
    "aaaaaaaaaaaa",
    "bbbbbbbbbbbb",
    "cccccccccccc",
    "dddddddddddd",
)
ACK_ID = "3fa91c07b2de"


class FakeClock:
    """Manual clock. Tests move time by assigning now."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class AppRefreshTestCase(unittest.TestCase):
    """Private data directory, plus a guard against request files in the work tree.

    AppRefresher writes as soon as start() succeeds. A test that points it at "."
    would drop a request file into the working tree, so each test compares the
    refresh- names in the process directory and the module directory.
    """

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = holder.name
        self._watched_dirs = (
            os.getcwd(),
            os.path.dirname(os.path.abspath(widget.__file__)),
        )
        self._refresh_names_before = tuple(
            self.refresh_file_names(path) for path in self._watched_dirs)
        self.addCleanup(self._assert_no_new_refresh_files)

    def refresh_file_names(self, directory):
        """Names in one directory that start with refresh-. Missing directories are empty."""
        if not os.path.isdir(directory):
            return set()
        return {name for name in os.listdir(directory) if name.startswith("refresh-")}

    def _assert_no_new_refresh_files(self):
        after = tuple(self.refresh_file_names(path) for path in self._watched_dirs)
        self.assertEqual(after, self._refresh_names_before)

    def request_path(self):
        return os.path.join(self.root, widget.APP_REFRESH_REQUEST_NAME)

    def ack_path(self):
        return os.path.join(self.root, widget.APP_REFRESH_ACK_NAME)

    def read_request(self):
        with open(self.request_path(), "rb") as handle:
            return handle.read()

    def write_ack(self, request_id, status="ok", **overrides):
        """Write one ack. An override of None removes that key."""
        doc = {
            "schema": 1,
            "id": request_id,
            "status": status,
            "at": 1791279533.912,
            "windows": 2,
        }
        for key, value in overrides.items():
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
        self.write_ack_raw((json.dumps(doc) + "\n").encode("utf-8"))

    def write_ack_raw(self, raw_bytes):
        with open(self.ack_path(), "wb") as handle:
            handle.write(raw_bytes)

    def make_refresher(self, **overrides):
        self.wall = FakeClock(1791279531.301)
        self.mono = FakeClock(1000.0)
        self.tokens = iter(REFRESH_TOKENS)
        options = {
            "data_dir": self.root,
            "clock": self.wall,
            "monotonic": self.mono,
            "token": lambda: next(self.tokens),
        }
        options.update(overrides)
        self.refresher = widget.AppRefresher(**options)
        return self.refresher

    def parsed_request(self):
        return json.loads(self.read_request().decode("utf-8"))


class StrayFileGuardTests(AppRefreshTestCase):
    def test_guard_notices_a_stray_request_file(self):
        # Only proves the name collector is not hard-coded to return an empty set.
        path = os.path.join(self.root, "refresh-guard-probe.json")
        with open(path, "wb") as handle:
            handle.write(b"{}\n")
        self.assertIn("refresh-guard-probe.json", self.refresh_file_names(self.root))
        self.assertEqual(self.refresh_file_names(os.path.join(self.root, "missing")), set())


class ReadAppAckTests(AppRefreshTestCase):
    def test_ok_and_unavailable_ignore_extra_or_missing_fields(self):
        self.write_ack(ACK_ID, status="ok", extra="kept")
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "ok")
        self.write_ack(ACK_ID, status="unavailable", at=None, windows=None)
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "unavailable")

    def test_wrong_id_returns_none(self):
        cases = (
            ("other id", {"id": "0123456789ab"}),
            ("different case", {"id": "3FA91C07B2DE"}),
            ("not a string", {"id": 12345}),
            ("missing", {"id": None}),
        )
        for name, overrides in cases:
            with self.subTest(name=name):
                self.write_ack(ACK_ID, **overrides)
                self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))

    def test_schema_must_be_numeric_one(self):
        rejected = (0, 2, "1", True, None)
        for schema in rejected:
            with self.subTest(schema=schema):
                self.write_ack(ACK_ID, schema=schema)
                self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))
        self.write_ack(ACK_ID, schema=1.0)
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "ok")

    def test_status_must_be_ok_or_unavailable(self):
        rejected = ("", "failed", "OK", 1, ["ok"], {"x": 1})
        for status in rejected:
            with self.subTest(status=status):
                self.write_ack(ACK_ID, status=status)
                self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))
        self.write_ack(ACK_ID, status=None)
        self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))
        self.write_ack_raw(
            b'{"schema": 1, "id": "3fa91c07b2de", "status": null}\n')
        self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))

    def test_broken_json_returns_none(self):
        samples = (
            ("truncated", b'{"schema": 1, "id": "3fa91c07b2de", "sta'),
            ("empty", b""),
            ("random", os.urandom(24)),
            ("bad utf-8", b"\xff\xfe{"),
            ("nan", b"NaN"),
            ("nested", b"[" * 100000),
        )
        for name, raw in samples:
            with self.subTest(name=name):
                self.write_ack_raw(raw)
                self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))

    def test_non_object_returns_none(self):
        samples = (
            ("array", b"[1, 2]"),
            ("number", b"4"),
            ("string", b'"ok"'),
            ("null", b"null"),
            ("true", b"true"),
        )
        for name, raw in samples:
            with self.subTest(name=name):
                self.write_ack_raw(raw)
                self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))

    def test_missing_file_and_directory_return_none(self):
        self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))
        os.mkdir(self.ack_path())
        self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))

    def test_size_limit_accepts_4096_and_rejects_longer(self):
        base = (json.dumps({
            "schema": 1,
            "id": ACK_ID,
            "status": "ok",
        }) + "\n").encode("utf-8")
        self.assertLess(len(base), widget.APP_ACK_MAX_BYTES)
        exact = base + b" " * (widget.APP_ACK_MAX_BYTES - len(base))
        self.write_ack_raw(exact)
        self.assertEqual(os.path.getsize(self.ack_path()), widget.APP_ACK_MAX_BYTES)
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "ok")
        over = exact + b" "
        self.write_ack_raw(over)
        self.assertEqual(os.path.getsize(self.ack_path()), widget.APP_ACK_MAX_BYTES + 1)
        self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))
        huge = base + b" " * (1024 * 1024 - len(base))
        self.write_ack_raw(huge)
        self.assertEqual(os.path.getsize(self.ack_path()), 1024 * 1024)
        self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))

    def test_leading_bom_is_accepted(self):
        body = (json.dumps({
            "schema": 1,
            "id": ACK_ID,
            "status": "ok",
        }) + "\n").encode("utf-8")
        self.write_ack_raw(b"\xef\xbb\xbf" + body)
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "ok")

    def test_oserror_and_nul_path_return_none(self):
        with patch("builtins.open", side_effect=PermissionError("denied")) as opened:
            self.assertIsNone(widget.read_app_ack(self.ack_path(), ACK_ID))
        self.assertTrue(opened.called)
        self.assertIsNone(widget.read_app_ack("bad\0path", ACK_ID))

    def test_reads_one_bounded_binary_chunk_and_closes(self):
        payload = (json.dumps({
            "schema": 1,
            "id": ACK_ID,
            "status": "ok",
        }) + "\n").encode("utf-8")
        with patch("builtins.open", mock_open(read_data=payload)) as opened:
            self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "ok")
        opened.assert_called_once_with(self.ack_path(), "rb")
        handle = opened.return_value
        handle.read.assert_called_once_with(widget.APP_ACK_MAX_BYTES + 1)
        handle.__exit__.assert_called()

    def test_handle_is_closed_so_the_ack_can_be_replaced(self):
        # On Windows os.replace fails while another handle still has the file open.
        self.write_ack(ACK_ID, status="ok")
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "ok")
        replacement = os.path.join(self.root, "replacement.json")
        body = (json.dumps({
            "schema": 1,
            "id": ACK_ID,
            "status": "unavailable",
        }) + "\n").encode("utf-8")
        with open(replacement, "wb") as handle:
            handle.write(body)
        os.replace(replacement, self.ack_path())
        self.assertEqual(widget.read_app_ack(self.ack_path(), ACK_ID), "unavailable")


class AppRefreshCaptionTests(AppRefreshTestCase):
    def test_fixed_states_ignore_the_read_caption(self):
        expected = (
            ("asking", "asking"),
            ("no_session", "no session"),
            ("no_limits", "no limits"),
            ("not_sent", "not sent"),
        )
        for read_caption in ("", "new 14:59", "read error"):
            for state, text in expected:
                with self.subTest(state=state, read_caption=read_caption):
                    self.assertEqual(widget.app_refresh_caption(read_caption, state), text)

    def test_cooldown_keeps_a_real_read_result(self):
        for read_caption in ("same 14:29", "no data", "", "same --:--"):
            with self.subTest(read_caption=read_caption):
                self.assertEqual(
                    widget.app_refresh_caption(read_caption, "cooldown"), "cooldown")
        for read_caption in ("new 14:59", "read error"):
            with self.subTest(read_caption=read_caption):
                self.assertEqual(
                    widget.app_refresh_caption(read_caption, "cooldown"), read_caption)

    def test_unknown_state_returns_the_read_caption(self):
        for state in ("bogus", "", None, "failed", "ping failed"):
            with self.subTest(state=state):
                self.assertEqual(
                    widget.app_refresh_caption("new 14:59", state), "new 14:59")

    def test_caption_constants_are_the_shared_ascii_words(self):
        self.assertEqual(widget.CAPTION_NO_SESSION, "no session")
        self.assertEqual(widget.CAPTION_NO_LIMITS, "no limits")
        self.assertEqual(widget.CAPTION_NOT_SENT, "not sent")
        self.assertEqual(widget.CAPTION_ASKING, "asking")
        self.assertEqual(widget.CAPTION_COOLDOWN, "cooldown")
        for text in (
            widget.CAPTION_NO_SESSION,
            widget.CAPTION_NO_LIMITS,
            widget.CAPTION_NOT_SENT,
            widget.CAPTION_ASKING,
            widget.CAPTION_COOLDOWN,
        ):
            self.assertTrue(text.isascii())

    def test_protocol_constants(self):
        self.assertEqual(widget.TIMER_APP, 11)
        self.assertEqual(widget.APP_REFRESH_REQUEST_NAME, "refresh-request.json")
        self.assertEqual(widget.APP_REFRESH_ACK_NAME, "refresh-ack.json")
        self.assertEqual(widget.APP_REFRESH_WAIT_SECONDS, 9.0)
        self.assertEqual(widget.APP_REFRESH_POLL_MS, 500)
        self.assertEqual(widget.APP_REFRESH_COOLDOWN_SECONDS, 60.0)
        self.assertEqual(widget.APP_REFRESH_RETRY_SECONDS, 10.0)
        self.assertEqual(widget.APP_ACK_MAX_BYTES, 4096)


class AppRefresherStartTests(AppRefreshTestCase):
    def _assert_idle_after_write_error(self, refresher, result, expected):
        self.assertEqual(result, expected)
        self.assertFalse(refresher.waiting)
        self.assertEqual(refresher.cooldown_left(), 0.0)
        names = set(os.listdir(self.root))
        self.assertNotIn(widget.APP_REFRESH_REQUEST_NAME + ".tmp", names)
        self.assertNotIn(widget.APP_REFRESH_REQUEST_NAME, names)
        self.assertEqual(refresher.start(), "started")

    def test_constructor_stores_the_directory_and_does_no_io(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.data_dir, self.root)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse(refresher.waiting)
        self.assertEqual(refresher.cooldown_left(), 0.0)

    def test_start_writes_one_request_file(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        raw = self.read_request()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        doc = json.loads(raw.decode("utf-8"))
        self.assertEqual(list(doc), ["schema", "id", "requested_at"])
        self.assertIs(type(doc["schema"]), int)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["id"], REFRESH_TOKENS[0])
        self.assertIs(type(doc["requested_at"]), float)
        self.assertEqual(doc["requested_at"], self.wall())
        self.assertEqual(sorted(os.listdir(self.root)), [widget.APP_REFRESH_REQUEST_NAME])
        self.assertTrue(refresher.waiting)

    def test_default_token_is_twelve_lowercase_hex_digits(self):
        before = time.time()
        refresher = widget.AppRefresher(self.root)
        self.assertEqual(refresher.start(), "started")
        after = time.time()
        doc = self.parsed_request()
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{12}", doc["id"]))
        self.assertGreaterEqual(doc["requested_at"], before)
        self.assertLessEqual(doc["requested_at"], after)

    def test_replace_sees_a_complete_temp_file(self):
        # The spy reads the temp file at the moment os.replace is called, then performs
        # the real replace. That is the "write the whole temp file, then rename" order.
        refresher = self.make_refresher()
        real_replace = os.replace
        seen = {}

        def spy(src, dst):
            self.assertTrue(os.path.isfile(src))
            with open(src, "rb") as handle:
                seen["body"] = handle.read()
            return real_replace(src, dst)

        with patch.object(os, "replace", side_effect=spy) as replace_mock:
            self.assertEqual(refresher.start(), "started")
        replace_mock.assert_called_once_with(
            self.request_path() + ".tmp", self.request_path())
        doc = json.loads(seen["body"].decode("utf-8"))
        self.assertEqual(doc["id"], REFRESH_TOKENS[0])
        self.assertEqual(list(doc), ["schema", "id", "requested_at"])

    def test_waiting_start_does_not_rewrite_or_take_a_token(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        raw = self.read_request()
        modified = os.stat(self.request_path()).st_mtime_ns
        with patch.object(os, "replace", wraps=os.replace) as replace_mock:
            self.assertEqual(refresher.start(), "waiting")
        replace_mock.assert_not_called()
        self.assertEqual(self.read_request(), raw)
        self.assertEqual(os.stat(self.request_path()).st_mtime_ns, modified)
        self.assertEqual(next(self.tokens), REFRESH_TOKENS[1])

    def test_answer_cools_down_for_sixty_seconds(self):
        for status in ("ok", "unavailable"):
            with self.subTest(status=status):
                refresher = self.make_refresher()
                self.mono.now = 1000.0
                self.assertEqual(refresher.start(), "started")
                raw = self.read_request()
                request_id = self.parsed_request()["id"]
                self.mono.now = 1002.0
                self.write_ack(request_id, status=status)
                self.assertEqual(refresher.poll(), (status, ""))
                self.assertFalse(refresher.waiting)
                self.assertAlmostEqual(refresher.cooldown_left(), 60.0, places=6)
                self.mono.now = 1002.0 + 59.999
                self.assertEqual(refresher.start(), "cooldown")
                self.assertAlmostEqual(refresher.cooldown_left(), 0.001, places=3)
                self.assertEqual(self.read_request(), raw)
                self.mono.now = 1062.0
                self.assertEqual(refresher.cooldown_left(), 0.0)
                self.assertEqual(refresher.start(), "started")
                self.assertEqual(self.parsed_request()["id"], REFRESH_TOKENS[1])
                self.assertTrue(refresher.waiting)

    def test_timeout_cools_down_for_ten_seconds(self):
        refresher = self.make_refresher()
        self.mono.now = 2000.0
        self.assertEqual(refresher.start(), "started")
        self.mono.now = 2009.0
        self.assertEqual(refresher.poll(), ("timeout", ""))
        self.assertAlmostEqual(refresher.cooldown_left(), 10.0, places=6)
        self.mono.now = 2009.0 + 9.999
        self.assertEqual(refresher.start(), "cooldown")
        self.mono.now = 2019.0
        self.assertEqual(refresher.cooldown_left(), 0.0)
        self.assertEqual(refresher.start(), "started")
        self.assertEqual(self.parsed_request()["id"], REFRESH_TOKENS[1])

    def test_waiting_and_cooldown_are_checked_before_the_directory(self):
        with self.subTest(name="waiting"):
            refresher = self.make_refresher()
            self.assertEqual(refresher.start(), "started")
            shutil.rmtree(self.root)
            try:
                self.assertEqual(refresher.start(), "waiting")
            finally:
                os.makedirs(self.root, exist_ok=True)
        with self.subTest(name="cooldown"):
            refresher = self.make_refresher()
            self.mono.now = 1000.0
            self.assertEqual(refresher.start(), "started")
            self.mono.now = 1001.0
            self.write_ack(self.parsed_request()["id"], status="ok")
            self.assertEqual(refresher.poll(), ("ok", ""))
            shutil.rmtree(self.root)
            try:
                self.assertEqual(refresher.start(), "cooldown")
            finally:
                os.makedirs(self.root, exist_ok=True)
        with self.subTest(name="missing"):
            missing = os.path.join(self.root, "gone")
            refresher = widget.AppRefresher(missing, clock=self.wall, monotonic=self.mono)
            self.assertEqual(refresher.start(), "no_dir")
            self.assertFalse(refresher.waiting)
            self.assertEqual(refresher.cooldown_left(), 0.0)

    def test_unusable_directory_is_no_dir_and_stays_idle(self):
        file_path = os.path.join(self.root, "not-a-dir")
        with open(file_path, "wb") as handle:
            handle.write(b"x")
        cases = (
            ("missing", os.path.join(self.root, "missing")),
            ("file", file_path),
            ("none", None),
            ("empty", ""),
            ("nul", "bad\0path"),
        )
        for name, data_dir in cases:
            with self.subTest(name=name):
                refresher = widget.AppRefresher(data_dir)
                self.assertEqual(refresher.start(), "no_dir")
                self.assertFalse(refresher.waiting)
                self.assertEqual(refresher.cooldown_left(), 0.0)

    def test_replace_error_does_not_arm_a_cooldown(self):
        refresher = self.make_refresher()
        with patch.object(os, "replace", side_effect=PermissionError("locked")) as replace_mock:
            result = refresher.start()
        self.assertEqual(replace_mock.call_count, 1)
        self._assert_idle_after_write_error(refresher, result, "write_error:PermissionError")

    def test_open_error_does_not_arm_a_cooldown(self):
        refresher = self.make_refresher()
        with patch("builtins.open", side_effect=OSError("disk")):
            result = refresher.start()
        self._assert_idle_after_write_error(refresher, result, "write_error:OSError")

    def test_non_finite_clock_is_a_value_error_without_a_temp_file(self):
        # Each value gets its own directory: a later case must not see the request
        # file written by an earlier recovery. The clock box is the injected fault;
        # a finite value is written back before the recovery start.
        for name, value in (("nan", float("nan")), ("inf", float("inf"))):
            with self.subTest(name=name):
                sub = os.path.join(self.root, name)
                os.mkdir(sub)
                box = {"value": value}
                refresher = widget.AppRefresher(
                    sub, clock=lambda: box["value"], monotonic=FakeClock(1000.0))
                result = refresher.start()
                self.assertEqual(result, "write_error:ValueError")
                self.assertFalse(refresher.waiting)
                self.assertEqual(refresher.cooldown_left(), 0.0)
                self.assertEqual(os.listdir(sub), [])
                box["value"] = 1791279531.301
                self.assertEqual(refresher.start(), "started")

    def test_temp_cleanup_error_is_swallowed(self):
        # remove failing on purpose leaves the temp file. The start itself must not raise.
        refresher = self.make_refresher()
        with patch.object(os, "replace", side_effect=OSError("replace")), \
                patch.object(os, "remove", side_effect=PermissionError("busy")):
            result = refresher.start()
        self.assertEqual(result, "write_error:OSError")
        self.assertFalse(refresher.waiting)
        self.assertEqual(refresher.cooldown_left(), 0.0)
        self.assertFalse(os.path.exists(self.request_path()))
        self.assertTrue(os.path.isfile(self.request_path() + ".tmp"))
        os.remove(self.request_path() + ".tmp")
        self.assertEqual(refresher.start(), "started")

    def test_monotonic_rollback_ends_the_cooldown(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        self.mono.now = 1002.0
        self.write_ack(self.parsed_request()["id"], status="ok")
        self.assertEqual(refresher.poll(), ("ok", ""))
        self.mono.now = 500.0
        self.assertEqual(refresher.cooldown_left(), 0.0)
        self.assertEqual(refresher.start(), "started")

    def test_each_click_uses_the_next_token(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        self.assertEqual(self.parsed_request()["id"], REFRESH_TOKENS[0])
        self.mono.now = 1001.0
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        self.assertEqual(refresher.poll(), ("ok", ""))
        self.mono.now = 1001.0 + widget.APP_REFRESH_COOLDOWN_SECONDS
        self.assertEqual(refresher.start(), "started")
        self.assertEqual(self.parsed_request()["id"], REFRESH_TOKENS[1])


class AppRefresherPollTests(AppRefreshTestCase):
    def test_idle_poll_does_not_read_an_ack(self):
        refresher = self.make_refresher()
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        with patch.object(widget, "read_app_ack") as reader:
            self.assertIsNone(refresher.poll())
        reader.assert_not_called()
        self.assertFalse(refresher.waiting)

    def test_missing_ack_keeps_waiting(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        self.assertIsNone(refresher.poll())
        self.assertTrue(refresher.waiting)
        self.assertIsNone(refresher.poll())
        self.assertTrue(refresher.waiting)

    def test_unusable_ack_keeps_waiting_without_a_cooldown(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        cases = (
            ("truncated", None, b'{"schema": 1, "id": "aaaaaaaaaaaa", "sta'),
            ("old id", {"id": "0123456789ab"}, None),
            ("unknown status", {"status": "failed"}, None),
            ("bad schema", {"schema": 2}, None),
            ("array", None, b"[1, 2]"),
        )
        for name, overrides, raw in cases:
            with self.subTest(name=name):
                if raw is None:
                    self.write_ack(REFRESH_TOKENS[0], **overrides)
                else:
                    self.write_ack_raw(raw)
                self.assertIsNone(refresher.poll())
                self.assertTrue(refresher.waiting)
                self.assertEqual(refresher.cooldown_left(), 0.0)

    def test_matching_ack_ends_the_wait_and_starts_the_long_cooldown(self):
        for status in ("ok", "unavailable"):
            with self.subTest(status=status):
                refresher = self.make_refresher()
                self.mono.now = 1000.0
                self.assertEqual(refresher.start(), "started")
                self.mono.now = 1003.0
                self.write_ack(REFRESH_TOKENS[0], status=status)
                self.assertEqual(refresher.poll(), (status, ""))
                self.assertFalse(refresher.waiting)
                self.assertAlmostEqual(refresher.cooldown_left(), 60.0, places=6)
                self.mono.now = 1033.0
                self.assertAlmostEqual(refresher.cooldown_left(), 30.0, places=6)
                self.assertIsNone(refresher.poll())

    def test_an_old_ack_is_ignored_until_the_matching_one_arrives(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        self.write_ack("0123456789ab", status="ok")
        self.assertIsNone(refresher.poll())
        self.assertTrue(refresher.waiting)
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        self.assertEqual(refresher.poll(), ("ok", ""))

    def test_a_previous_rounds_ack_does_not_answer_the_next_request(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        self.assertEqual(refresher.poll(), ("ok", ""))
        self.mono.now = 1000.0 + widget.APP_REFRESH_COOLDOWN_SECONDS
        self.assertEqual(refresher.start(), "started")
        self.assertEqual(self.parsed_request()["id"], REFRESH_TOKENS[1])
        self.assertIsNone(refresher.poll())
        self.assertTrue(refresher.waiting)
        self.write_ack(REFRESH_TOKENS[1], status="ok")
        self.assertEqual(refresher.poll(), ("ok", ""))

    def test_timeout_boundary(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        self.mono.now = 1008.999
        self.assertIsNone(refresher.poll())
        self.assertTrue(refresher.waiting)
        self.mono.now = 1009.0
        self.assertEqual(refresher.poll(), ("timeout", ""))
        self.assertFalse(refresher.waiting)
        self.assertAlmostEqual(refresher.cooldown_left(), 10.0, places=6)

    def test_a_matching_ack_wins_over_the_timeout(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        self.mono.now = 1009.0
        self.assertEqual(refresher.poll(), ("ok", ""))
        self.assertAlmostEqual(refresher.cooldown_left(), 60.0, places=6)

    def test_an_ack_for_the_timed_out_request_does_not_answer_the_retry(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        self.mono.now = 1009.0
        self.assertEqual(refresher.poll(), ("timeout", ""))
        self.mono.now = 1019.0
        self.assertEqual(refresher.start(), "started")
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        self.assertIsNone(refresher.poll())
        self.assertTrue(refresher.waiting)
        self.write_ack(REFRESH_TOKENS[1], status="unavailable")
        self.assertEqual(refresher.poll(), ("unavailable", ""))

    def test_monotonic_rollback_does_not_time_out_early(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        for now in (900.0, -5.0, 1008.9):
            with self.subTest(now=now):
                self.mono.now = now
                self.assertIsNone(refresher.poll())
                self.assertTrue(refresher.waiting)
        self.mono.now = 1009.0
        self.assertEqual(refresher.poll(), ("timeout", ""))

    def test_a_read_error_keeps_waiting_until_the_timeout(self):
        refresher = self.make_refresher()
        self.mono.now = 1000.0
        self.assertEqual(refresher.start(), "started")
        with patch("builtins.open", side_effect=PermissionError("denied")) as opened:
            self.assertIsNone(refresher.poll())
            self.assertTrue(refresher.waiting)
            self.assertTrue(opened.called)
            self.mono.now = 1009.0
            self.assertEqual(refresher.poll(), ("timeout", ""))

    def test_poll_after_a_result_stays_idle(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        self.assertEqual(refresher.poll(), ("ok", ""))
        self.assertIsNone(refresher.poll())
        self.assertFalse(refresher.waiting)


class AppRefresherStopTests(AppRefreshTestCase):
    def test_stop_drops_a_pending_request_without_reading_the_ack(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        self.write_ack(REFRESH_TOKENS[0], status="ok")
        refresher.stop()
        self.assertFalse(refresher.waiting)
        self.assertIsNone(refresher.poll())

    def test_stop_leaves_the_request_file_in_place(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        raw = self.read_request()
        refresher.stop()
        self.assertTrue(os.path.isfile(self.request_path()))
        self.assertEqual(self.read_request(), raw)

    def test_stop_is_idempotent_before_and_after_a_start(self):
        refresher = self.make_refresher()
        refresher.stop()
        self.assertEqual(refresher.start(), "started")
        refresher.stop()
        refresher.stop()
        refresher.stop()
        self.assertFalse(refresher.waiting)

    def test_stop_does_not_change_the_cooldown(self):
        with self.subTest(name="answer"):
            refresher = self.make_refresher()
            self.mono.now = 1000.0
            self.assertEqual(refresher.start(), "started")
            self.mono.now = 1002.0
            self.write_ack(REFRESH_TOKENS[0], status="ok")
            self.assertEqual(refresher.poll(), ("ok", ""))
            before = refresher.cooldown_left()
            refresher.stop()
            self.assertEqual(refresher.cooldown_left(), before)
            self.assertAlmostEqual(refresher.cooldown_left(), 60.0, places=6)
        with self.subTest(name="timeout"):
            # The answer case above left an ack for the first token. A new refresher
            # starts that sequence again, so the leftover ack would look like a reply.
            try:
                os.remove(self.ack_path())
            except OSError:
                pass
            refresher = self.make_refresher()
            self.mono.now = 2000.0
            self.assertEqual(refresher.start(), "started")
            self.mono.now = 2009.0
            self.assertEqual(refresher.poll(), ("timeout", ""))
            before = refresher.cooldown_left()
            refresher.stop()
            self.assertEqual(refresher.cooldown_left(), before)
            self.assertAlmostEqual(refresher.cooldown_left(), 10.0, places=6)
        with self.subTest(name="no cooldown"):
            refresher = self.make_refresher()
            self.assertEqual(refresher.start(), "started")
            refresher.stop()
            self.assertEqual(refresher.cooldown_left(), 0.0)
            self.assertEqual(refresher.start(), "started")
            self.assertEqual(self.parsed_request()["id"], REFRESH_TOKENS[1])

    def test_stop_survives_a_missing_data_directory(self):
        refresher = self.make_refresher()
        self.assertEqual(refresher.start(), "started")
        shutil.rmtree(self.root)
        try:
            refresher.stop()
            self.assertFalse(refresher.waiting)
        finally:
            os.makedirs(self.root, exist_ok=True)


# WidgetApp wiring -----------------------------------------------------------

def at(hour, minute):
    return time.mktime((2026, 10, 2, hour, minute, 0, 0, 0, -1))


APP_NOW = at(15, 0)
OBS_OLD = at(14, 0)
OBS_NEW = at(14, 58)

# Sentinel: this app has no Codex data source. None would mean an empty snapshot.
NO_CODEX = object()


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


class FakeAppRefresher:
    """Stands in for AppRefresher. Only the four members WidgetApp calls, and no files."""

    def __init__(self):
        self.waiting = False
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
            self.waiting = True
        return self.start_result

    def poll(self):
        self.poll_calls += 1
        if self.poll_error is not None:
            raise self.poll_error
        if self.poll_results:
            result = self.poll_results.pop(0)
            if result is not None:
                self.waiting = False
            return result
        return None

    def stop(self):
        self.stop_calls += 1
        self.waiting = False
        if self.stop_error is not None:
            raise self.stop_error


def make_app(claude=None, codex=NO_CODEX, pinger=False, refresher=True, options=None):
    """Widget with fake readers. refresher='real' keeps the AppRefresher from Options."""
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
            os.path.join("nowhere", "data"), None, None, None,
            codex_home=os.path.join("nowhere", "codex-home"))
    with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
            patch.object(widget, "read_light_theme", return_value=False):
        app = widget.WidgetApp(options, w32, Mock())
    app.hwnd = 10
    app.visible = True
    app._apply = Mock()
    app.reader = FakeReader(claude, "missing" if claude is None else "")
    if codex is NO_CODEX:
        app.codex = None
    else:
        app.codex = FakeReader(codex, "" if codex else "no_rate_limits", available=True)
    app.pinger = FakePinger() if pinger else None
    if refresher is True:
        app.app_refresher = FakeAppRefresher()
    elif refresher is False:
        app.app_refresher = None
    return app


def click_refresh(app, now=APP_NOW):
    app.w32.track_menu.return_value = widget.MENU_REFRESH
    with patch.object(widget.time, "time", return_value=now):
        app.on_right_up()


def last_display(app):
    return app._apply.call_args_list[-1][0][0]


def menu_swaps_in_newer_claude(app):
    """Menu loop stand-in: a poll during the menu replaces the Claude snapshot."""

    def menu(hwnd, x, y):
        app.reader.snapshot = data_at(OBS_NEW)
        return widget.MENU_REFRESH

    app.w32.track_menu.side_effect = menu


class AppRefresherCreationTests(AppRefreshTestCase):
    def _construct(self, options):
        with patch.object(widget, "load_position", return_value=(widget.MODE_AUTO, 330)), \
                patch.object(widget, "read_light_theme", return_value=False):
            return widget.WidgetApp(options, Mock(), Mock())

    def test_default_options_build_an_app_refresher(self):
        app = self._construct(widget.Options(self.root, None, None, None))
        self.assertIsInstance(app.app_refresher, widget.AppRefresher)
        self.assertEqual(app.app_refresher.data_dir, self.root)
        self.assertIsNone(app._app_before)
        self.assertFalse(app.app_refresher.waiting)
        self.assertEqual(os.listdir(self.root), [])

    def test_no_codex_does_not_switch_it_off(self):
        app = self._construct(widget.Options(self.root, None, None, None, no_codex=True))
        self.assertIsNone(app.codex)
        self.assertIsNone(app.pinger)
        self.assertIsInstance(app.app_refresher, widget.AppRefresher)

    def test_no_codex_ping_does_not_switch_it_off(self):
        app = self._construct(widget.Options(self.root, None, None, None, no_codex_ping=True))
        self.assertIsNone(app.pinger)
        self.assertIsInstance(app.app_refresher, widget.AppRefresher)
        self.assertIsInstance(app.codex, widget.CodexReader)

    def test_no_app_refresh_switches_off_only_the_app_refresher(self):
        app = self._construct(widget.Options(self.root, None, None, None, no_app_refresh=True))
        self.assertIsNone(app.app_refresher)
        self.assertIsInstance(app.codex, widget.CodexReader)
        self.assertIsInstance(app.pinger, widget.CodexPinger)

    def test_click_writes_a_request_only_when_enabled(self):
        # The disabled case is empty only because the enabled case writes a file.
        for flag in (False, True):
            with self.subTest(flag=flag):
                data_dir = os.path.join(self.root, "off" if flag else "on")
                os.mkdir(data_dir)
                options = widget.Options(
                    data_dir, None, None, None, no_codex=True, no_app_refresh=flag)
                app = make_app(
                    claude=data_at(OBS_OLD), refresher="real", options=options)
                click_refresh(app)
                names = self.refresh_file_names(data_dir)
                if flag:
                    self.assertEqual(names, set())
                    self.assertIsNone(app.app_refresher)
                    self.assertNotIn(widget.TIMER_APP, app.timers)
                else:
                    self.assertIn(widget.APP_REFRESH_REQUEST_NAME, names)


def _app_timer_calls(app):
    return [
        item for item in app.w32.set_timer.call_args_list
        if len(item.args) >= 2 and item.args[1] == widget.TIMER_APP]


# WidgetApp: Refresh now -----------------------------------------------------

class RefreshNowAppTests(AppRefreshTestCase):
    def test_start_sees_the_claude_observation_from_menu_open(self):
        app = make_app(claude=data_at(OBS_OLD))
        menu_swaps_in_newer_claude(app)
        click_refresh(app)
        self.assertEqual(app.app_refresher.start_calls, 1)
        self.assertEqual(app._app_before, OBS_OLD)

    def test_refresh_passes_the_menu_open_observation(self):
        app = make_app(claude=data_at(OBS_OLD))
        menu_swaps_in_newer_claude(app)
        with patch.object(app, "_start_app_refresh") as start:
            click_refresh(app)
        # The second argument is "redraw": the flash redraw that follows shows the asking text.
        start.assert_called_once_with(OBS_OLD, False)

    def test_refresh_now_orders_poll_then_codex_then_app_then_flash(self):
        # The two observations differ on purpose, so a swapped index fails here.
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        parent = Mock()
        app.poll_snapshot = parent.poll_snapshot
        app._start_codex_ping = parent._start_codex_ping
        app._start_app_refresh = parent._start_app_refresh
        app._begin_flash = parent._begin_flash
        app._refresh_now((OBS_OLD, OBS_NEW))
        self.assertEqual(
            [item[0] for item in parent.mock_calls],
            ["poll_snapshot", "_start_codex_ping", "_start_app_refresh", "_begin_flash"])
        # Not flashing yet, so the flash redraw is still to come and neither request redraws.
        parent._start_codex_ping.assert_called_once_with(OBS_NEW, False)
        parent._start_app_refresh.assert_called_once_with(OBS_OLD, False)

    def test_refresh_now_without_before_observes_at_call_time(self):
        app = make_app(claude=data_at(OBS_OLD))
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._refresh_now()
        self.assertEqual(app._app_before, OBS_OLD)
        self.assertEqual(app.app_refresher.start_calls, 1)

    def test_started_arms_the_app_timer_and_shows_asking(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.reader.queued = (data_at(OBS_NEW), "")
        click_refresh(app)
        self.assertIn(widget.TIMER_APP, app.timers)
        self.assertIn(
            call(10, widget.TIMER_APP, widget.APP_REFRESH_POLL_MS),
            app.w32.set_timer.call_args_list)
        self.assertEqual(last_display(app).captions, ("asking",))
        self.assertEqual(app._app_before, OBS_OLD)

    def test_started_keeps_the_codex_text_from_the_same_click(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app.reader.queued = (data_at(OBS_NEW), "")
        app.codex.queued = (data_at(OBS_NEW), "")
        click_refresh(app)
        self.assertEqual(last_display(app).captions, ("asking", "new 14:58"))

    def test_asking_stays_up_while_waiting(self):
        app = make_app(claude=data_at(OBS_NEW))
        app.app_refresher.waiting = True

        def redraw():
            with patch.object(widget.time, "time", return_value=APP_NOW):
                app._refresh_once(False, False)

        with self.subTest(name="no caption"):
            app._caption = None
            redraw()
            self.assertEqual(last_display(app).captions, ("asking",))
        with self.subTest(name="expired caption"):
            app._caption = (APP_NOW - 1.0, ("new 14:58", ""))
            redraw()
            self.assertEqual(last_display(app).captions, ("asking",))
            self.assertIsNone(app._caption)
        with self.subTest(name="live caption"):
            app._caption = (APP_NOW + 100.0, ("new 14:58", ""))
            redraw()
            self.assertEqual(last_display(app).captions, ("asking",))
        with self.subTest(name="not waiting"):
            app.app_refresher.waiting = False
            app._caption = None
            redraw()
            self.assertEqual(last_display(app).captions, ())
        with self.subTest(name="caption timer"):
            app.app_refresher.waiting = True
            app._caption = (APP_NOW + 100.0, ("new 14:58", ""))
            with patch.object(widget.time, "time", return_value=APP_NOW):
                app.on_timer(widget.TIMER_CAPTION)
            self.assertIsNone(app._caption)
            self.assertEqual(last_display(app).captions, ("asking",))

        bare = make_app(claude=data_at(OBS_NEW), refresher=False)
        with patch.object(widget.time, "time", return_value=APP_NOW):
            bare._refresh_once(False, False)
        self.assertNotIn("asking", last_display(bare).captions)

    def test_claude_and_codex_asking_are_independent(self):
        app = make_app(claude=data_at(OBS_NEW), codex=data_at(OBS_NEW), pinger=True)
        idle = {
            (False, False): (),
            (True, False): ("asking", ""),
            (False, True): ("", "asking"),
            (True, True): ("asking", "asking"),
        }
        live = {
            (False, False): ("new 14:58", "same 14:00"),
            (True, False): ("asking", "same 14:00"),
            (False, True): ("new 14:58", "asking"),
            (True, True): ("asking", "asking"),
        }
        stored = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))

        def redraw():
            with patch.object(widget.time, "time", return_value=APP_NOW):
                app._refresh_once(False, False)

        for waiting, running in idle:
            for caption, expected in ((None, idle), (stored, live)):
                with self.subTest(waiting=waiting, running=running, caption=caption):
                    app.app_refresher.waiting = waiting
                    app.pinger.running = running
                    app._caption = caption
                    redraw()
                    self.assertEqual(
                        last_display(app).captions, expected[(waiting, running)])

    def test_the_requests_leave_the_redraw_to_the_flash(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD), pinger=True)
        click_refresh(app)
        self.assertTrue(app.pinger.running)
        self.assertTrue(app.app_refresher.waiting)
        # Two pictures: the read result the poll draws, then the flash with both blocks asking.
        # The two requests do not draw one picture each in between.
        self.assertEqual(app._apply.call_count, 2)
        self.assertEqual(
            app._apply.call_args_list[0][0][0].captions, ("same 14:00", "same 14:00"))
        self.assertEqual(last_display(app).captions, ("asking", "asking"))
        self.assertTrue(app._flashing)

    def test_what_the_requests_change_shows_in_the_flash_picture(self):
        # (driver, what start() says, caption index, text the picture must carry)
        cases = (
            ("pinger", "cooldown", 1, "cooldown"),
            ("pinger", "spawn_error:OSError", 1, "ping failed"),
            ("app_refresher", "cooldown", 0, "cooldown"),
            ("app_refresher", "write_error:PermissionError", 0, "not sent"),
        )
        for driver, start_result, index, text in cases:
            with self.subTest(driver=driver, start_result=start_result):
                app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD), pinger=True)
                getattr(app, driver).start_result = start_result
                click_refresh(app)
                self.assertEqual(app._apply.call_count, 2)
                self.assertEqual(last_display(app).captions[index], text)
                self.assertTrue(app._flashing)

    def test_a_click_during_a_flash_lets_the_requests_redraw_for_themselves(self):
        # _begin_flash draws nothing when the widget is already dim, so a text the requests
        # change (cooldown on the Codex block, asking on the Claude block) would otherwise
        # only show when that flash ends.
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD), pinger=True)
        app.pinger.start_result = "cooldown"
        app._flashing = True
        click_refresh(app)
        self.assertEqual(last_display(app).captions, ("asking", "cooldown"))
        self.assertTrue(app._flashing)

    def test_cooldown_rewrites_only_the_claude_text(self):
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
                app = make_app(claude=data_at(OBS_OLD))
                app.app_refresher.start_result = "cooldown"
                app._caption = (deadline, (original, "same 14:00"))
                app.refresh_view = Mock()
                app._start_app_refresh(OBS_OLD)
                self.assertEqual(app._caption, (deadline, (expected, "same 14:00")))
                app.refresh_view.assert_called_once_with()
                self.assertEqual(_app_timer_calls(app), [])
                self.assertIsNone(app._app_before)
                self.assertEqual(app.app_refresher.start_calls, 1)

    def test_cooldown_without_a_caption_still_redraws(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.start_result = "cooldown"
        app.refresh_view = Mock()
        app._start_app_refresh(OBS_OLD)
        self.assertIsNone(app._caption)
        app.refresh_view.assert_called_once_with()

    def test_no_dir_changes_nothing(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.start_result = "no_dir"
        caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
        app._caption = caption
        app.refresh_view = Mock()
        app._start_app_refresh(OBS_OLD)
        self.assertEqual(app._caption, caption)
        app.refresh_view.assert_not_called()
        self.assertEqual(_app_timer_calls(app), [])
        self.assertIsNone(app._app_before)
        app_logs = [
            item for item in app.log.log.call_args_list
            if item.args and item.args[0] == "AppRefresh"]
        self.assertEqual(app_logs, [])

    def test_write_error_marks_not_sent(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.start_result = "write_error:PermissionError"
        deadline = APP_NOW + 100.0
        app._caption = (deadline, ("new 14:58", "same 14:00"))
        app.refresh_view = Mock()
        app._start_app_refresh(OBS_OLD)
        app.log.log.assert_called_with("AppRefresh", "write_error:PermissionError")
        self.assertEqual(app._caption, (deadline, ("not sent", "same 14:00")))
        app.refresh_view.assert_called_once_with()
        self.assertEqual(_app_timer_calls(app), [])
        self.assertIsNone(app._app_before)

        bare = make_app(claude=data_at(OBS_OLD))
        bare.app_refresher.start_result = "write_error:PermissionError"
        bare.refresh_view = Mock()
        bare._start_app_refresh(OBS_OLD)
        self.assertIsNone(bare._caption)
        bare.refresh_view.assert_called_once_with()
        bare.log.log.assert_called_with("AppRefresh", "write_error:PermissionError")

    def test_waiting_result_changes_nothing(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.start_result = "waiting"
        app._app_before = OBS_OLD
        caption = (APP_NOW + 100.0, ("new 14:58", "same 14:00"))
        app._caption = caption
        app.refresh_view = Mock()
        app._start_app_refresh(OBS_NEW)
        self.assertEqual(app._caption, caption)
        self.assertEqual(app._app_before, OBS_OLD)
        app.refresh_view.assert_not_called()
        self.assertEqual(_app_timer_calls(app), [])
        self.assertEqual(app.app_refresher.start_calls, 1)

    def test_start_is_skipped_when_the_request_cannot_be_made(self):
        for name in ("no refresher", "exiting", "no window"):
            with self.subTest(name=name):
                app = make_app(claude=data_at(OBS_OLD))
                app.refresh_view = Mock()
                app.w32.set_timer.reset_mock()
                if name == "no refresher":
                    app.app_refresher = None
                elif name == "exiting":
                    app.exiting = True
                else:
                    app.hwnd = 0
                app._start_app_refresh(OBS_OLD)
                app.refresh_view.assert_not_called()
                app.w32.set_timer.assert_not_called()
                if app.app_refresher is not None:
                    self.assertEqual(app.app_refresher.start_calls, 0)

    def test_failed_timer_abandons_the_request(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.w32.set_timer.return_value = False
        deadline = APP_NOW + 100.0
        app._caption = (deadline, ("new 14:58", "same 14:00"))
        app.refresh_view = Mock()
        app._start_app_refresh(OBS_OLD)
        self.assertEqual(app.app_refresher.stop_calls, 1)
        self.assertFalse(app.app_refresher.waiting)
        self.assertIsNone(app._app_before)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        self.assertEqual(app._caption, (deadline, ("no session", "same 14:00")))
        app.log.log.assert_any_call(
            "AppRefresh", "poll timer unavailable; request abandoned")
        app.refresh_view.assert_called_once_with()

        bare = make_app(claude=data_at(OBS_OLD))
        bare.w32.set_timer.return_value = False
        bare.refresh_view = Mock()
        bare._start_app_refresh(OBS_OLD)
        self.assertIsNone(bare._caption)
        self.assertEqual(bare.app_refresher.stop_calls, 1)
        self.assertIsNone(bare._app_before)

    def test_start_error_still_flashes(self):
        app = make_app(claude=data_at(OBS_OLD))
        app._start_app_refresh = Mock(side_effect=RuntimeError("boom"))
        click_refresh(app)
        self.assertTrue(app._flashing)
        app.log.log_exception.assert_called()

    def test_without_a_refresher_the_timer_calls_are_unchanged(self):
        app = make_app(claude=data_at(OBS_OLD), refresher=False)
        app.reader.queued = (data_at(OBS_NEW), "")
        click_refresh(app)
        self.assertEqual(app.w32.set_timer.call_args_list, [
            call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
            call(10, widget.TIMER_FLASH, widget.FLASH_MS),
        ])

    def test_the_request_does_not_depend_on_codex(self):
        with self.subTest(name="no codex"):
            app = make_app(claude=data_at(OBS_OLD))
            click_refresh(app)
            self.assertEqual(app.app_refresher.start_calls, 1)
        with self.subTest(name="codex unavailable"):
            app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
            app.codex.available = False
            click_refresh(app)
            self.assertEqual(app.app_refresher.start_calls, 1)


class PollPathNeverStartsAppRefreshTests(AppRefreshTestCase):
    def test_poll_paths_never_start_a_request(self):
        # The stand-in raises, and WidgetApp would swallow that. The call count is
        # what makes a hidden start fail the test.
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD), pinger=True)
        app.app_refresher.start = Mock(side_effect=AssertionError("must not start from a poll path"))
        app.poll_snapshot()
        app.poll_snapshot(force=True)
        for timer_id in (
            widget.TIMER_CHECK,
            widget.TIMER_POLL,
            widget.TIMER_THEME,
            widget.TIMER_FLASH,
            widget.TIMER_CAPTION,
            widget.TIMER_PING,
            widget.TIMER_APP,
        ):
            app.on_timer(timer_id)
        self.assertFalse(app.app_refresher.start.called)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        self.assertIsNone(app._app_before)


# WidgetApp: app-refresh timer, captions, exit and recreate -------------------

def _arm_app_timer(app):
    """Put TIMER_APP where kill_timer will actually see it, then forget the arming call."""
    app.set_timer(widget.TIMER_APP, widget.APP_REFRESH_POLL_MS)
    app.w32.set_timer.reset_mock()
    app.w32.kill_timer.reset_mock()


def _arm_ping_timer(app):
    """Put TIMER_PING where kill_timer will actually see it, then forget the arming call."""
    app.set_timer(widget.TIMER_PING, widget.CODEX_PING_POLL_MS)
    app.w32.set_timer.reset_mock()
    app.w32.kill_timer.reset_mock()


class AppTimerTests(AppRefreshTestCase):
    def test_pending_poll_keeps_the_timer(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.waiting = True
        app.app_refresher.poll_results = [None]
        _arm_app_timer(app)
        caption = app._caption
        app.refresh_view = Mock()
        app._on_app_timer()
        app.w32.kill_timer.assert_not_called()
        self.assertIn(widget.TIMER_APP, app.timers)
        self.assertEqual(app.reader.calls, [])
        self.assertIs(app._caption, caption)
        app.refresh_view.assert_not_called()
        self.assertEqual(app.app_refresher.poll_calls, 1)

    def test_idle_poll_kills_the_timer(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.waiting = False
        app.app_refresher.poll_results = [None]
        _arm_app_timer(app)
        app._on_app_timer()
        app.w32.kill_timer.assert_called_with(10, widget.TIMER_APP)
        self.assertNotIn(widget.TIMER_APP, app.timers)

    def test_missing_refresher_kills_the_timer(self):
        app = make_app(refresher=False)
        _arm_app_timer(app)
        app._on_app_timer()
        app.w32.kill_timer.assert_called_with(10, widget.TIMER_APP)

    def test_ok_rereads_the_snapshot_and_arms_a_caption(self):
        # (name, observation at click time, Claude snapshot when the timer fires,
        # queued read, expected text). "already read" has no queued read: TIMER_POLL
        # already stored the new snapshot, so the comparison must use the click-time before.
        cases = (
            ("new", OBS_OLD, data_at(OBS_OLD), (data_at(OBS_NEW), ""), "new 14:58"),
            ("same", OBS_OLD, data_at(OBS_OLD), (data_at(OBS_OLD), ""), "same 14:00"),
            ("no file", OBS_OLD, data_at(OBS_OLD), (None, "missing"), "no file"),
            ("no data", OBS_OLD, data_at(OBS_OLD), (None, ""), "no data"),
            ("read error", OBS_OLD, data_at(OBS_OLD), (data_at(OBS_OLD), "bad_json"), "read error"),
            ("already read", OBS_OLD, data_at(OBS_NEW), None, "new 14:58"),
            ("already read, refresh repeats it", OBS_OLD, data_at(OBS_NEW),
             (data_at(OBS_NEW), ""), "new 14:58"),
            ("no earlier observation", None, data_at(OBS_OLD), (data_at(OBS_OLD), ""), "new 14:00"),
            ("no earlier observation, no file", None, data_at(OBS_OLD), (None, "missing"), "no file"),
        )
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        for name, before, current, queued, text in cases:
            with self.subTest(name=name):
                app = make_app(claude=current)
                app.app_refresher.poll_results = [("ok", "")]
                app._app_before = before
                _arm_app_timer(app)
                app.refresh_view = Mock()
                app.reader.queued = queued
                with patch.object(widget.time, "time", return_value=APP_NOW):
                    app._on_app_timer()
                self.assertEqual(app.reader.calls, [True])
                app.w32.kill_timer.assert_called_with(10, widget.TIMER_APP)
                self.assertNotIn(widget.TIMER_APP, app.timers)
                self.assertEqual(app._caption, (deadline, (text, "")))
                self.assertIn(
                    call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
                    app.w32.set_timer.call_args_list)
                self.assertIn(widget.TIMER_CAPTION, app.timers)
                self.assertIsNone(app._app_before)
                app.refresh_view.assert_called_once_with()
                app_logs = [
                    item for item in app.log.log.call_args_list
                    if item.args and item.args[0] == "AppRefresh"]
                self.assertEqual(app_logs, [])
                invalid = [
                    item.args for item in app.log.log.call_args_list
                    if item.args and item.args[0] == "SnapshotInvalid"]
                if name == "read error":
                    self.assertEqual(invalid, [("SnapshotInvalid", "bad_json")])
                else:
                    self.assertEqual(invalid, [])

    def test_ok_redraws_the_new_caption_without_asking(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.poll_results = [("ok", "")]
        app._app_before = OBS_OLD
        _arm_app_timer(app)
        app.reader.queued = (data_at(OBS_NEW), "")
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._on_app_timer()
        self.assertEqual(last_display(app).captions, ("new 14:58",))
        self.assertFalse(app.app_refresher.waiting)

    def test_unavailable_shows_no_limits(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.poll_results = [("unavailable", "")]
        app._app_before = OBS_OLD
        _arm_app_timer(app)
        app.refresh_view = Mock()
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._on_app_timer()
        self.assertEqual(app.reader.calls, [])
        self.assertEqual(app._caption, (deadline, ("no limits", "")))
        app.w32.kill_timer.assert_called_with(10, widget.TIMER_APP)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        self.assertIn(
            call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
            app.w32.set_timer.call_args_list)
        self.assertIn(widget.TIMER_CAPTION, app.timers)
        self.assertIsNone(app._app_before)
        app.refresh_view.assert_called_once_with()
        app_logs = [
            item for item in app.log.log.call_args_list
            if item.args and item.args[0] == "AppRefresh"]
        self.assertEqual(app_logs, [])

    def test_timeout_shows_no_session_and_logs(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.poll_results = [("timeout", "")]
        app._app_before = OBS_OLD
        _arm_app_timer(app)
        app.refresh_view = Mock()
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._on_app_timer()
        self.assertEqual(app.reader.calls, [])
        self.assertEqual(app._caption, (deadline, ("no session", "")))
        app.log.log.assert_any_call("AppRefresh", "no answer within 9 s")
        app.w32.kill_timer.assert_called_with(10, widget.TIMER_APP)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        self.assertIn(
            call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
            app.w32.set_timer.call_args_list)
        self.assertIn(widget.TIMER_CAPTION, app.timers)
        self.assertIsNone(app._app_before)
        app.refresh_view.assert_called_once_with()

    def timed_out_text(self, claude, last_reason=None):
        """The Claude caption after an unanswered request, with the reader in the given state."""
        app = make_app(claude=claude)
        if last_reason is not None:
            app.reader.last_reason = last_reason
        app.app_refresher.poll_results = [("timeout", "")]
        app._app_before = OBS_OLD
        _arm_app_timer(app)
        app.refresh_view = Mock()
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._on_app_timer()
        self.assertEqual(app._caption[0], APP_NOW + widget.CAPTION_MS / 1000.0)
        # The reader is not read again, and the timeout is logged whatever the caption says.
        self.assertEqual(app.reader.calls, [])
        app.log.log.assert_any_call("AppRefresh", "no answer within 9 s")
        return app._caption[1]

    def test_timeout_with_the_snapshot_file_missing_shows_no_file(self):
        # A missing usage.json usually means the widget and the plugin use different folders;
        # no session would hide that.
        self.assertEqual(self.timed_out_text(None), ("no file", ""))
        self.assertEqual(self.timed_out_text(data_at(OBS_OLD), "missing"), ("no file", ""))

    def test_timeout_with_any_other_reader_state_keeps_no_session(self):
        reasons = ("", "bad_json", "bad_schema", "bad_windows", "not_object", "too_large",
                   "read_error:OSError", "stat_error:OSError")
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.timed_out_text(data_at(OBS_OLD), reason), ("no session", ""))

    def test_an_answer_never_turns_into_no_file(self):
        # The plugin got the request, so the folders match: no limits is the truth even
        # when usage.json does not exist yet.
        app = make_app()
        self.assertEqual(app.reader.last_reason, "missing")
        app.app_refresher.poll_results = [("unavailable", "")]
        app._app_before = None
        _arm_app_timer(app)
        app.refresh_view = Mock()
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._on_app_timer()
        self.assertEqual(app._caption[1], ("no limits", ""))

    def test_result_keeps_a_live_codex_text_and_drops_an_expired_one(self):
        cases = (
            ("live", (APP_NOW + 100.0, ("", "new 14:59")), ("no limits", "new 14:59")),
            ("expired", (APP_NOW - 1.0, ("", "new 14:59")), ("no limits", "")),
        )
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        for name, caption, expected in cases:
            with self.subTest(name=name):
                app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
                app.app_refresher.poll_results = [("unavailable", "")]
                app._caption = caption
                _arm_app_timer(app)
                app.refresh_view = Mock()
                with patch.object(widget.time, "time", return_value=APP_NOW):
                    app._on_app_timer()
                self.assertEqual(app._caption, (deadline, expected))

    def test_poll_error_is_logged_and_not_raised(self):
        app = make_app(claude=data_at(OBS_OLD))
        error = OSError("poll broke")
        app.app_refresher.poll_error = error
        caption = (APP_NOW + 100.0, ("new 14:58", ""))
        app._caption = caption
        _arm_app_timer(app)
        app._on_app_timer()
        self.assertEqual(app._caption, caption)
        app.log.log_exception.assert_called_with(error)

    def test_a_failed_reread_is_logged_and_not_raised(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.poll_results = [("ok", "")]
        app.reader.refresh = Mock(side_effect=RuntimeError("boom"))
        _arm_app_timer(app)
        app._on_app_timer()
        app.log.log_exception.assert_called()
        self.assertNotIn(widget.TIMER_APP, app.timers)

    def test_timer_message_reaches_the_poll_unless_exiting(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.waiting = True
        app.on_timer(widget.TIMER_APP)
        self.assertEqual(app.app_refresher.poll_calls, 1)
        self.assertEqual(app._dispatch(widget.WM_TIMER, widget.TIMER_APP), (True, 0))
        self.assertEqual(app.app_refresher.poll_calls, 2)
        app.exiting = True
        app.on_timer(widget.TIMER_APP)
        self.assertEqual(app.app_refresher.poll_calls, 2)

    def test_stop_and_drop_helpers(self):
        app = make_app(claude=data_at(OBS_OLD))
        app._stop_slot(widget.APP_SLOT)
        app._stop_slot(widget.APP_SLOT)
        self.assertEqual(app.app_refresher.stop_calls, 2)

        bare = make_app(refresher=False)
        bare._stop_slot(widget.APP_SLOT)

        broken = make_app(claude=data_at(OBS_OLD))
        broken.app_refresher.stop_error = RuntimeError("boom")
        broken._stop_slot(widget.APP_SLOT)
        broken.log.log_exception.assert_called()

        dropped = make_app(claude=data_at(OBS_OLD))
        dropped._app_before = OBS_OLD
        dropped._drop_slot(widget.APP_SLOT)
        dropped.log.log.assert_called_with(
            "AppRefresh", "poll timer unavailable; request abandoned")
        self.assertEqual(dropped.app_refresher.stop_calls, 1)
        self.assertIsNone(dropped._app_before)


class SetSlotCaptionTests(AppRefreshTestCase):
    def test_slots_are_set_and_the_other_one_follows_the_expiry_rule(self):
        origins = (
            ("none", None),
            ("live", (APP_NOW + 100.0, ("a", "b"))),
            ("expired", (APP_NOW - 1.0, ("a", "b"))),
            # A deadline equal to now has already expired.
            ("deadline is now", (APP_NOW, ("a", "b"))),
            ("short tuple", (APP_NOW + 100.0, ("a",))),
        )
        expected = {
            "none": {0: ("X", ""), 1: ("", "X")},
            "live": {0: ("X", "b"), 1: ("a", "X")},
            "expired": {0: ("X", ""), 1: ("", "X")},
            "deadline is now": {0: ("X", ""), 1: ("", "X")},
            "short tuple": {0: ("X", ""), 1: ("a", "X")},
        }
        deadline = APP_NOW + widget.CAPTION_MS / 1000.0
        for name, caption in origins:
            for index in (0, 1):
                with self.subTest(name=name, index=index):
                    app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
                    app.refresh_view = Mock()
                    app._caption = caption
                    with patch.object(widget.time, "time", return_value=APP_NOW):
                        app._set_slot_caption(index, "X")
                    self.assertEqual(app._caption, (deadline, expected[name][index]))
                    self.assertIn(
                        call(10, widget.TIMER_CAPTION, widget.CAPTION_MS),
                        app.w32.set_timer.call_args_list)
                    self.assertIn(widget.TIMER_CAPTION, app.timers)
                    app.refresh_view.assert_called_once_with()

    def test_a_live_claude_text_is_extended_with_a_fresh_deadline(self):
        app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD))
        app._caption = (APP_NOW + 1.0, ("a", "b"))
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app._set_slot_caption(1, "X")
        # The whole caption gets a new deadline; the leftover Claude text is not left on the old one.
        self.assertEqual(app._caption[0], APP_NOW + 2.5)
        self.assertNotEqual(app._caption[0], APP_NOW + 1.0)
        self.assertEqual(app._caption[1][0], "a")


class PingKeepsTheClaudeTextTests(AppRefreshTestCase):
    def test_ping_end_keeps_a_live_claude_text(self):
        cases = (
            ("live", (APP_NOW + 100.0, ("no limits", "")),
             [("ok", "exit:0")], (data_at(OBS_NEW), ""),
             (APP_NOW + widget.CAPTION_MS / 1000.0, ("no limits", "new 14:58"))),
            ("expired", (APP_NOW - 1.0, ("no limits", "")),
             [("ok", "exit:0")], (data_at(OBS_NEW), ""),
             (APP_NOW + widget.CAPTION_MS / 1000.0, ("", "new 14:58"))),
            ("none", None,
             [("ok", "exit:0")], (data_at(OBS_NEW), ""),
             (APP_NOW + widget.CAPTION_MS / 1000.0, ("", "new 14:58"))),
            ("failed", (APP_NOW + 100.0, ("no limits", "")),
             [("failed", "exit:3")], None,
             (APP_NOW + widget.CAPTION_MS / 1000.0, ("no limits", "ping failed"))),
        )
        for name, caption, results, queued, expected in cases:
            with self.subTest(name=name):
                app = make_app(claude=data_at(OBS_OLD), codex=data_at(OBS_OLD), pinger=True)
                app.pinger.poll_results = results
                app._ping_before = OBS_OLD
                app.codex.queued = queued
                _arm_ping_timer(app)
                app.refresh_view = Mock()
                app._caption = caption
                with patch.object(widget.time, "time", return_value=APP_NOW):
                    app._on_ping_timer()
                self.assertEqual(app._caption, expected)
                app.refresh_view.assert_called_once_with()
                self.assertNotIn(widget.TIMER_PING, app.timers)


class AppExitAndTimersTests(AppRefreshTestCase):
    def test_request_exit_stops_the_request_after_the_flag_is_set(self):
        app = make_app(claude=data_at(OBS_OLD))
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        seen = []
        app.app_refresher.stop = Mock(side_effect=lambda: seen.append(app.exiting))
        app.request_exit()
        app.app_refresher.stop.assert_called_once_with()
        self.assertEqual(seen, [True])
        self.assertTrue(app.exiting)
        app.w32.destroy_window.assert_called_with(10)
        app.request_exit()
        app.app_refresher.stop.assert_called_once_with()

    def test_request_exit_without_a_refresher(self):
        app = make_app(refresher=False)
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        app.request_exit()
        self.assertTrue(app.exiting)

    def test_stop_error_does_not_block_exit(self):
        app = make_app(claude=data_at(OBS_OLD))
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        app.app_refresher.stop_error = RuntimeError("boom")
        app.timers.update({widget.TIMER_APP, widget.TIMER_CHECK})
        app.request_exit()
        self.assertTrue(app.exiting)
        self.assertEqual(app.timers, set())
        app.w32.destroy_window.assert_called()
        app.log.log_exception.assert_called()

    def test_exit_stops_a_pending_request_and_kills_its_timer(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.waiting = True
        _arm_app_timer(app)
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        app.request_exit()
        self.assertFalse(app.app_refresher.waiting)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        app.w32.kill_timer.assert_any_call(10, widget.TIMER_APP)

    def test_start_timers_rearms_a_waiting_request_only_after_recreate(self):
        waiting = make_app(claude=data_at(OBS_OLD))
        waiting.app_refresher.waiting = True
        waiting._start_timers(recreated=True)
        self.assertIn(widget.TIMER_APP, waiting.timers)
        self.assertIn(
            call(10, widget.TIMER_APP, widget.APP_REFRESH_POLL_MS),
            waiting.w32.set_timer.call_args_list)

        idle = make_app(claude=data_at(OBS_OLD))
        idle.app_refresher.waiting = False
        idle._start_timers(recreated=True)
        self.assertNotIn(widget.TIMER_APP, idle.timers)

        bare = make_app(refresher=False)
        bare._start_timers(recreated=True)
        self.assertEqual(bare.timers, {
            widget.TIMER_CHECK, widget.TIMER_POLL, widget.TIMER_THEME})

        first = make_app(claude=data_at(OBS_OLD))
        first.app_refresher.waiting = True
        first._start_timers()
        self.assertNotIn(widget.TIMER_APP, first.timers)

    def test_recreate_rearms_a_waiting_request(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.waiting = True
        app.timers.add(widget.TIMER_APP)
        app.w32.is_window.return_value = False
        app.w32.create_window.return_value = 11
        app._recreate_window("owner disappeared")
        self.assertIn(widget.TIMER_APP, app.timers)
        app.w32.set_timer.assert_any_call(11, widget.TIMER_APP, widget.APP_REFRESH_POLL_MS)
        self.assertIsNone(app._caption)
        self.assertEqual(last_display(app).captions, ("asking",))

        posted = make_app(claude=data_at(OBS_OLD))
        posted.app_refresher.waiting = True
        posted.hwnd = 0
        posted.w32.create_window.return_value = 12
        posted.refresh_view = Mock()
        posted.on_thread_message(widget.WM_APP_RECREATE)
        posted.w32.set_timer.assert_any_call(12, widget.TIMER_APP, widget.APP_REFRESH_POLL_MS)

        quiet = make_app(claude=data_at(OBS_OLD))
        quiet.app_refresher.waiting = False
        quiet.w32.is_window.return_value = False
        quiet.w32.create_window.return_value = 11
        quiet._recreate_window("owner disappeared")
        self.assertEqual(_app_timer_calls(quiet), [])

        quiet_post = make_app(claude=data_at(OBS_OLD))
        quiet_post.app_refresher.waiting = False
        quiet_post.hwnd = 0
        quiet_post.w32.create_window.return_value = 12
        quiet_post.refresh_view = Mock()
        quiet_post.on_thread_message(widget.WM_APP_RECREATE)
        self.assertEqual(_app_timer_calls(quiet_post), [])

    def test_failed_rearm_after_recreate_gives_up_the_request(self):
        def refuse_app_timer(hwnd, timer_id, interval_ms):
            return timer_id != widget.TIMER_APP

        app = make_app(claude=data_at(OBS_OLD))
        app.app_refresher.waiting = True
        app._app_before = OBS_OLD
        app.w32.set_timer.side_effect = refuse_app_timer
        app._start_timers(recreated=True)
        self.assertEqual(app.app_refresher.stop_calls, 1)
        self.assertFalse(app.app_refresher.waiting)
        self.assertIsNone(app._app_before)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        app.log.log.assert_any_call(
            "AppRefresh", "poll timer unavailable; request abandoned")

        rebuilt = make_app(claude=data_at(OBS_OLD))
        rebuilt.app_refresher.waiting = True
        rebuilt.w32.is_window.return_value = False
        rebuilt.w32.create_window.return_value = 11
        rebuilt.w32.set_timer.side_effect = refuse_app_timer
        rebuilt._recreate_window("owner disappeared")
        self.assertEqual(rebuilt.app_refresher.stop_calls, 1)
        self.assertNotIn("asking", last_display(rebuilt).captions)

        healthy = make_app(claude=data_at(OBS_OLD))
        healthy.app_refresher.waiting = True
        healthy._start_timers(recreated=True)
        self.assertEqual(healthy.app_refresher.stop_calls, 0)
        self.assertTrue(healthy.app_refresher.waiting)
        self.assertIn(widget.TIMER_APP, healthy.timers)

    def test_run_stops_the_request_on_the_way_out(self):
        app = make_app(claude=data_at(OBS_OLD))
        app.hwnd = 0
        app.w32.run_message_loop.return_value = 0
        app.w32.create_window.return_value = 11
        app.poll_snapshot = Mock()
        app.refresh_view = Mock()
        app._start_timers = Mock()
        self.assertEqual(app.run(), 0)
        self.assertEqual(app.app_refresher.stop_calls, 1)
        self.assertIsNone(widget._APP)

        failed = make_app(claude=data_at(OBS_OLD))
        failed.hwnd = 0
        failed.w32.run_message_loop.side_effect = RuntimeError("loop failed")
        failed.w32.create_window.return_value = 11
        failed.poll_snapshot = Mock()
        failed.refresh_view = Mock()
        failed._start_timers = Mock()
        with self.assertRaises(RuntimeError):
            failed.run()
        self.assertEqual(failed.app_refresher.stop_calls, 1)
        self.assertIsNone(widget._APP)

        bare = make_app(refresher=False)
        bare.hwnd = 0
        bare.w32.run_message_loop.return_value = 0
        bare.w32.create_window.return_value = 11
        bare.poll_snapshot = Mock()
        bare.refresh_view = Mock()
        bare._start_timers = Mock()
        self.assertEqual(bare.run(), 0)


# Options, source guards, samples, end to end --------------------------------

class ParseAppOptionsTests(AppRefreshTestCase):
    def test_default_leaves_it_enabled(self):
        self.assertFalse(widget.parse_args([]).no_app_refresh)
        self.assertFalse(widget.Options(".", None, None, None).no_app_refresh)

    def test_options_field_order_and_positional_defaults(self):
        self.assertEqual(
            widget.Options._fields,
            ("data_dir", "exit_after", "selftest_render", "selftest_gdi", "no_codex",
             "codex_home", "codex_bin", "no_codex_ping", "codex_ping_model", "no_app_refresh",
             "warnings"))
        self.assertEqual(
            widget.Options._field_defaults,
            {"no_codex": False, "codex_home": None, "codex_bin": None, "no_codex_ping": False,
             "codex_ping_model": None, "no_app_refresh": False, "warnings": ()})
        plain = widget.Options(".", None, None, None)
        flagged = widget.Options(".", None, None, None, no_codex=True)
        self.assertFalse(plain.no_codex)
        self.assertTrue(flagged.no_codex)
        for opts in (plain, flagged):
            self.assertIsNone(opts.codex_home)
            self.assertIsNone(opts.codex_bin)
            self.assertFalse(opts.no_codex_ping)
            self.assertIsNone(opts.codex_ping_model)
            self.assertFalse(opts.no_app_refresh)
        full = widget.Options("d", 1.5, "r", 2, True, "h", "b", True, "m", True)
        self.assertEqual(full.data_dir, "d")
        self.assertEqual(full.exit_after, 1.5)
        self.assertEqual(full.selftest_render, "r")
        self.assertEqual(full.selftest_gdi, 2)
        self.assertTrue(full.no_codex)
        self.assertEqual(full.codex_home, "h")
        self.assertEqual(full.codex_bin, "b")
        self.assertTrue(full.no_codex_ping)
        self.assertEqual(full.codex_ping_model, "m")
        self.assertTrue(full.no_app_refresh)

    def test_switch_forms(self):
        home = os.path.join("rel", "home")
        self.assertTrue(widget.parse_args(["--no-app-refresh"]).no_app_refresh)
        self.assertTrue(widget.parse_args(["--no-app-refresh=1"]).no_app_refresh)
        both = widget.parse_args(["--no-codex", "--no-app-refresh"])
        self.assertTrue(both.no_codex)
        self.assertTrue(both.no_app_refresh)
        after = widget.parse_args(["--no-app-refresh", "--codex-home", home])
        self.assertTrue(after.no_app_refresh)
        self.assertEqual(after.codex_home, os.path.abspath(home))
        # A following option is not consumed as the switch's value.
        followed = widget.parse_args(["--no-app-refresh", "--data-dir", "rel"])
        self.assertEqual(followed.data_dir, os.path.abspath("rel"))

    def test_the_switches_are_independent(self):
        self.assertFalse(widget.parse_args(["--no-codex-ping"]).no_app_refresh)
        off = widget.parse_args(["--no-app-refresh"])
        self.assertFalse(off.no_codex_ping)
        self.assertFalse(off.no_codex)

    def test_unknown_options_are_still_ignored(self):
        opts = widget.parse_args(["--bogus", "x", "--no-app-refresh"])
        self.assertTrue(opts.no_app_refresh)
        self.assertFalse(opts.no_codex)
        self.assertFalse(opts.no_codex_ping)
        self.assertIsNone(opts.codex_bin)
        self.assertIsNone(opts.codex_ping_model)
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
        self.assertFalse(opts.no_app_refresh)


def imported_roots(source_text):
    """First segment of every imported module name. 'http.client' counts as 'http'."""
    roots = set()
    for node in ast.walk(ast.parse(source_text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


class SourceGuardAppTests(unittest.TestCase):
    def setUp(self):
        self.source = pathlib.Path(widget.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.lines = self.source.splitlines()

    def _class_span(self, name):
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return node.lineno, node.end_lineno
        self.fail("%s is missing" % name)

    def test_no_network_modules_are_imported(self):
        sample = "import socket\nimport http.client\nfrom urllib import request\nimport requests\n"
        self.assertEqual(
            imported_roots(sample), {"socket", "http", "urllib", "requests"})
        self.assertEqual(
            imported_roots(self.source) & {"socket", "urllib", "http", "requests"}, set())

    def test_popen_only_appears_inside_codex_pinger(self):
        start, end = self._class_span("CodexPinger")
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

    def test_forbidden_calls_and_strings_are_absent(self):
        self.assertNotIn("shell=True", self.source)
        self.assertNotIn("os.system", self.source)
        self.assertNotIn("os.startfile", self.source)
        self.assertNotIn("os.popen(", self.source)
        self.assertNotIn("auth" + ".json", self.source)

    def test_plugin_only_strings_are_absent(self):
        # The pieces are joined so this file does not contain the plugin-side spellings.
        needles = (".m" + "cp", "get_" + "usage", "ccd_session" + "_mgmt")
        self.assertIn("get_" + "usage", "call get_" + "usage now")
        for needle in needles:
            self.assertNotIn(needle, self.source)

    def test_app_refresher_stays_pure_python(self):
        start, end = self._class_span("AppRefresher")
        segment = "\n".join(self.lines[start - 1:end])
        for needle in (
            "subprocess", "threading", "socket", "Popen", "ctypes", "winreg",
            "set_timer", "SetTimer",
        ):
            self.assertNotIn(needle, segment)
        self.assertEqual(segment.count("os.replace("), 1)

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
        # Expectation updated on purpose. The callback factory is WINFUNCTYPE on Windows and
        # CFUNCTYPE elsewhere (so the offline tests can import the module), and the literal name
        # now appears once in the fallback line instead of once per callback. Pin the fallback
        # line and the two callback definitions by name, and count every spelling of the factory
        # in the whole file: a callback built indented (inside a method) or through a second
        # getattr fallback at module level must fail too.
        self.assertIn(
            '_CALLBACK_TYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)', self.source)
        self.assertEqual(
            re.findall(r"^(\w+) = _CALLBACK_TYPE\(", self.source, re.M),
            ["WNDPROC", "WINEVENTPROC"])
        self.assertEqual(self.source.count("_CALLBACK_TYPE("), 2)
        self.assertEqual(self.source.count("WINFUNCTYPE"), 1)
        self.assertEqual(self.source.count("ctypes.CFUNCTYPE"), 1)

    def test_timer_ids_are_unique_and_app_is_eleven(self):
        values = []
        for name in dir(widget):
            if not name.startswith("TIMER_"):
                continue
            value = getattr(widget, name)
            if isinstance(value, int) and not isinstance(value, bool):
                values.append(value)
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(widget.TIMER_APP, 11)
        self.assertEqual(widget.TIMER_PING, 10)


class AppSampleTests(unittest.TestCase):
    NAMES = ("caption_asking", "caption_no_session", "dual_caption_app_no_limits")

    def test_new_samples_are_appended_and_old_indexes_stay(self):
        self.assertEqual(widget.ALL_SAMPLE_STATES[-3:], self.NAMES)
        self.assertEqual(widget.EXTRA_SAMPLE_STATES[-3:], self.NAMES)
        indexes = [widget.ALL_SAMPLE_STATES.index(name) + 1 for name in self.NAMES]
        self.assertEqual(indexes, [27, 28, 29])
        self.assertEqual(len(widget.ALL_SAMPLE_STATES), 29)
        previous = (
            "dual_caption_asking", "dual_caption_cooldown", "dual_caption_ping_failed")
        previous_indexes = [widget.ALL_SAMPLE_STATES.index(name) + 1 for name in previous]
        self.assertEqual(previous_indexes, [24, 25, 26])
        self.assertEqual(widget.EXTRA_DUAL_SAMPLE_STATES[-1], "dual_caption_app_no_limits")
        self.assertNotIn("caption_asking", widget.EXTRA_DUAL_SAMPLE_STATES)
        self.assertNotIn("caption_no_session", widget.EXTRA_DUAL_SAMPLE_STATES)
        self.assertEqual(
            widget.sample_filename(27, "caption_asking", "light", 1.0),
            "27_caption_asking_light_1.00.png")

    def test_text_and_layout_at_every_combo(self):
        for theme, scale in widget.SAMPLE_COMBOS:
            with self.subTest(theme=theme, scale=scale):
                asking = widget.sample_display("caption_asking", theme, scale)
                self.assertEqual(asking.kind, "data")
                self.assertEqual(asking.captions, ("asking",))
                self.assertEqual(
                    asking.rows, widget.sample_display("green", theme, scale).rows)
                no_session = widget.sample_display("caption_no_session", theme, scale)
                self.assertEqual(no_session.kind, "data")
                self.assertEqual(no_session.captions, ("no session",))
                self.assertEqual(
                    no_session.rows, widget.sample_display("green", theme, scale).rows)
                no_limits = widget.sample_display("dual_caption_app_no_limits", theme, scale)
                self.assertEqual(no_limits.kind, "dual")
                self.assertEqual(no_limits.captions, ("no limits", "cooldown"))
                self.assertEqual(
                    no_limits.rows, widget.sample_display("dual_green", theme, scale).rows)

    def test_selftest_render_writes_the_new_samples(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        with patch.object(widget, "say"):
            self.assertEqual(widget.selftest_render(holder.name), 0)
        pngs = [name for name in os.listdir(holder.name) if name.lower().endswith(".png")]
        self.assertEqual(len(pngs), 6 * len(widget.ALL_SAMPLE_STATES) + 1)
        caption_new_index = widget.ALL_SAMPLE_STATES.index("caption_new") + 1
        dual_index = widget.ALL_SAMPLE_STATES.index("dual_caption_new_same") + 1
        for theme, scale in widget.SAMPLE_COMBOS:
            dual_name = widget.sample_filename(
                dual_index, "dual_caption_new_same", theme, scale)
            with Image.open(os.path.join(holder.name, dual_name)) as reference:
                dual_size = reference.size
            caption_name = widget.sample_filename(
                caption_new_index, "caption_new", theme, scale)
            with Image.open(os.path.join(holder.name, caption_name)) as reference:
                caption_size = reference.size
            widths = {}
            for name in self.NAMES:
                filename = widget.sample_filename(
                    widget.ALL_SAMPLE_STATES.index(name) + 1, name, theme, scale)
                path = os.path.join(holder.name, filename)
                self.assertTrue(os.path.isfile(path), path)
                self.assertGreater(os.path.getsize(path), 0)
                with Image.open(path) as image:
                    if name == "dual_caption_app_no_limits":
                        self.assertEqual((image.width, image.height), dual_size)
                    else:
                        self.assertEqual(image.height, caption_size[1])
                        ratio = image.width / caption_size[0]
                        self.assertGreaterEqual(ratio, 0.8)
                        self.assertLessEqual(ratio, 1.25)
                        widths[name] = image.width
            # "no session" is a longer caption than "asking", so its window is wider.
            self.assertGreater(widths["caption_no_session"], widths["caption_asking"])


class AppRefreshEndToEndTests(AppRefreshTestCase):
    def _make(self):
        self.wall = FakeClock(1791279531.301)
        self.mono = FakeClock(1000.0)
        ids = iter(["a1b2c3d4e5f6", "0f9e8d7c6b5a", "112233445566"])
        app = make_app(claude=data_at(OBS_OLD), refresher=False)
        app.app_refresher = widget.AppRefresher(
            self.root, clock=self.wall, monotonic=self.mono, token=lambda: next(ids))
        return app

    def _request_id(self):
        return json.loads(self.read_request().decode("utf-8"))["id"]

    def test_ok_flow(self):
        app = self._make()
        click_refresh(app)
        doc = json.loads(self.read_request().decode("utf-8"))
        self.assertEqual(doc["id"], "a1b2c3d4e5f6")
        self.assertEqual(doc["requested_at"], 1791279531.301)
        self.assertEqual(doc["schema"], 1)
        self.assertIn(widget.TIMER_APP, app.timers)
        self.assertEqual(last_display(app).captions, ("asking",))
        self.assertEqual(app._app_before, OBS_OLD)

        self.mono.now = 1000.5
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertTrue(app.app_refresher.waiting)
        self.assertIn(widget.TIMER_APP, app.timers)
        self.assertEqual(last_display(app).captions, ("asking",))

        self.write_ack("0123456789ab")
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertTrue(app.app_refresher.waiting)

        self.write_ack_raw(b'{"schema": 1, "id": "a1b2c3d4e5f6", "sta')
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertTrue(app.app_refresher.waiting)

        self.write_ack("a1b2c3d4e5f6", status="ok")
        app.reader.queued = (data_at(OBS_NEW), "")
        self.mono.now = 1001.0
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertFalse(app.app_refresher.waiting)
        self.assertNotIn(widget.TIMER_APP, app.timers)
        self.assertEqual(
            app._caption, (APP_NOW + widget.CAPTION_MS / 1000.0, ("new 14:58", "")))
        self.assertEqual(last_display(app).captions, ("new 14:58",))

        self.mono.now = 1030.0
        before_bytes = self.read_request()
        click_refresh(app)
        self.assertEqual(self.read_request(), before_bytes)
        self.assertEqual(last_display(app).captions, ("cooldown",))
        self.assertNotIn(widget.TIMER_APP, app.timers)

        self.mono.now = 1001.0 + 60.0
        click_refresh(app)
        self.assertEqual(self._request_id(), "0f9e8d7c6b5a")
        self.assertIn(widget.TIMER_APP, app.timers)
        self.assertEqual(last_display(app).captions, ("asking",))

    def test_unavailable_flow(self):
        app = self._make()
        click_refresh(app)
        self.write_ack("a1b2c3d4e5f6", status="unavailable")
        reads = list(app.reader.calls)
        self.mono.now = 1001.0
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertEqual(app._caption[1][0], "no limits")
        self.assertEqual(last_display(app).captions, ("no limits",))
        self.assertEqual(app.reader.calls, reads)
        self.assertNotIn(widget.TIMER_APP, app.timers)

        self.mono.now = 1060.9
        click_refresh(app)
        self.assertEqual(self._request_id(), "a1b2c3d4e5f6")
        self.mono.now = 1061.0
        click_refresh(app)
        self.assertEqual(self._request_id(), "0f9e8d7c6b5a")

    def test_timeout_flow(self):
        app = self._make()
        click_refresh(app)
        self.mono.now = 1008.9
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertTrue(app.app_refresher.waiting)

        self.mono.now = 1009.0
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertEqual(app._caption[1][0], "no session")
        self.assertEqual(last_display(app).captions, ("no session",))
        app.log.log.assert_any_call("AppRefresh", "no answer within 9 s")
        self.assertNotIn(widget.TIMER_APP, app.timers)

        self.mono.now = 1018.9
        click_refresh(app)
        self.assertEqual(self._request_id(), "a1b2c3d4e5f6")
        self.mono.now = 1019.0
        click_refresh(app)
        self.assertEqual(self._request_id(), "0f9e8d7c6b5a")

    def test_timeout_flow_follows_the_real_reader(self):
        # The real reader on the data folder the request goes to: no usage.json there means no
        # file, a readable one means the request really went unanswered.
        for present in (False, True):
            with self.subTest(snapshot_file=present):
                app = self._make()
                app.reader = widget.SnapshotReader(
                    os.path.join(self.root, widget.SNAPSHOT_NAME))
                if present:
                    window = {"used_percentage": 30.0, "resets_at": APP_NOW + 7200.0,
                              "observed_at": OBS_OLD}
                    with open(app.reader.path, "w", encoding="utf-8") as handle:
                        json.dump({"schema": 1, "written_at": OBS_OLD,
                                   "windows": {"five_hour": window, "seven_day": window}},
                                  handle)
                click_refresh(app)
                self.assertEqual(app.reader.last_reason, "" if present else "missing")
                self.assertEqual(last_display(app).captions, ("asking",))
                self.mono.now = 1009.0
                with patch.object(widget.time, "time", return_value=APP_NOW):
                    app.on_timer(widget.TIMER_APP)
                self.assertFalse(app.app_refresher.waiting)
                expected = "no session" if present else "no file"
                self.assertEqual(app._caption[1][0], expected)
                self.assertEqual(last_display(app).captions, (expected,))
                app.log.log.assert_any_call("AppRefresh", "no answer within 9 s")

    def test_request_exit_leaves_the_request_file(self):
        app = self._make()
        click_refresh(app)
        before_bytes = self.read_request()
        app._arm_watchdog = Mock()
        app.w32.destroy_window.return_value = True
        app.request_exit()
        self.assertTrue(os.path.isfile(self.request_path()))
        self.assertEqual(self.read_request(), before_bytes)
        self.assertFalse(app.app_refresher.waiting)
        self.assertNotIn(widget.TIMER_APP, app.timers)

    def test_no_stray_files_besides_the_request(self):
        app = self._make()
        click_refresh(app)
        self.write_ack("a1b2c3d4e5f6", status="ok")
        app.reader.queued = (data_at(OBS_NEW), "")
        self.mono.now = 1001.0
        with patch.object(widget.time, "time", return_value=APP_NOW):
            app.on_timer(widget.TIMER_APP)
        self.assertEqual(
            set(os.listdir(self.root)),
            {"refresh-request.json", "refresh-ack.json"})
