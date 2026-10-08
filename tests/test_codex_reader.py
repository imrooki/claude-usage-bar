"""Codex rate_limits reader. Synthetic lines in a temp tree only, never real session logs."""

import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import usage_widget as widget

EXAMPLE = (
    '{"timestamp":"2026-10-03T01:46:27.212Z","type":"event_msg","payload":'
    '{"type":"token_count","info":{"ignored":true},"rate_limits":'
    '{"limit_id":"codex","limit_name":null,'
    '"primary":{"used_percent":3.0,"window_minutes":300,"resets_at":1791009691},'
    '"secondary":{"used_percent":36.0,"window_minutes":10080,"resets_at":1791512592},'
    '"credits":{"has_credits":false,"unlimited":false,"balance":null},'
    '"plan_type":null}}}'
)
OBSERVED = datetime.datetime.fromisoformat("2026-10-03T01:46:27.212+00:00").timestamp()
PROMPT_LINE = (
    '{"timestamp":"2026-10-03T01:40:00.000Z","type":"event_msg","payload":'
    '{"type":"user_message","text":"please show my api key sk-test"}}'
)


def line_with(primary, secondary, timestamp="2026-10-03T01:46:27.212Z"):
    payload = {"type": "token_count", "rate_limits": {}}
    if primary is not None:
        payload["rate_limits"]["primary"] = primary
    if secondary is not None:
        payload["rate_limits"]["secondary"] = secondary
    doc = {"type": "event_msg", "payload": payload}
    if timestamp is not None:
        doc["timestamp"] = timestamp
    return json.dumps(doc, separators=(",", ":"))


def write_rollout(folder, name, lines, mtime=None):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        if lines:
            handle.write("\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def day_folder(root, year, month, day):
    folder = os.path.join(root, "sessions", "%04d" % year, "%02d" % month, "%02d" % day)
    os.makedirs(folder)
    return folder


def append_line(path, text, mtime=None):
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def iso_unix(text):
    """期望值不经过被测代码：Z 结尾的 ISO 8601 转 Unix 秒。"""
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def record_line(pct, timestamp):
    """一行 token_count 记录：primary 用 pct，secondary 用 pct + 1。"""
    return line_with(
        {"used_percent": pct, "window_minutes": 300, "resets_at": 90},
        {"used_percent": pct + 1, "window_minutes": 10080, "resets_at": 91},
        timestamp=timestamp,
    )


STAMP_NEWER = "2026-10-03T03:00:00.000Z"
STAMP_OLDER = "2026-10-03T01:00:00.000Z"


class ParseCodexTests(unittest.TestCase):
    def test_real_example(self):
        snapshot = widget.parse_codex_rate_limits(EXAMPLE)
        self.assertEqual(snapshot["five_hour"], widget.Win(3.0, 1791009691, OBSERVED))
        self.assertEqual(snapshot["seven_day"], widget.Win(36.0, 1791512592, OBSERVED))

    def test_resets_in_seconds_fallback(self):
        text = line_with(
            {"used_percent": 12, "window_minutes": 300, "resets_in_seconds": 90},
            {"used_percent": 4, "window_minutes": 10080, "resets_in_seconds": 10},
        )
        snapshot = widget.parse_codex_rate_limits(text)
        self.assertEqual(snapshot["five_hour"], widget.Win(12.0, OBSERVED + 90, OBSERVED))
        self.assertEqual(snapshot["seven_day"], widget.Win(4.0, OBSERVED + 10, OBSERVED))
        present = line_with(
            {"used_percent": 1, "window_minutes": 300, "resets_at": 70, "resets_in_seconds": 5},
            None,
        )
        self.assertEqual(widget.parse_codex_rate_limits(present)["five_hour"].resets_at, 70)

    def test_unknown_window_minutes_uses_position(self):
        text = line_with(
            {"used_percent": 1, "window_minutes": 15, "resets_at": 100},
            {"used_percent": 2, "window_minutes": 99999, "resets_at": 200},
        )
        snapshot = widget.parse_codex_rate_limits(text)
        self.assertEqual(snapshot["five_hour"].pct, 1.0)
        self.assertEqual(snapshot["seven_day"].pct, 2.0)

    def test_known_minutes_override_position(self):
        text = line_with(
            {"used_percent": 8, "window_minutes": 10080, "resets_at": 300},
            {"used_percent": 9, "window_minutes": 300, "resets_at": 400},
        )
        snapshot = widget.parse_codex_rate_limits(text)
        self.assertEqual(snapshot["seven_day"].pct, 8.0)
        self.assertEqual(snapshot["five_hour"].pct, 9.0)

    def test_missing_or_invalid_windows(self):
        none_cases = (
            line_with({"used_percent": -1, "window_minutes": 300, "resets_at": 10}, None),
            line_with({"used_percent": 1, "window_minutes": 300, "resets_at": 0}, None),
            line_with({"used_percent": 1, "window_minutes": 300}, None),
            line_with({"used_percent": 1, "resets_in_seconds": 5}, None, timestamp=None),
            line_with("nope", {"used_percent": "x"}),
            line_with(None, None),
        )
        for text in none_cases:
            with self.subTest(text=text):
                self.assertIsNone(widget.parse_codex_rate_limits(text))

    def test_one_bad_window_keeps_the_other(self):
        text = line_with(
            {"used_percent": True, "window_minutes": 300, "resets_at": 10},
            {"used_percent": 7, "window_minutes": 10080, "resets_at": 20},
        )
        snapshot = widget.parse_codex_rate_limits(text)
        self.assertIsNone(snapshot["five_hour"])
        self.assertEqual(snapshot["seven_day"].pct, 7.0)

    def test_prompt_and_other_events_are_ignored(self):
        self.assertIsNone(widget.parse_codex_rate_limits(PROMPT_LINE))
        other = (
            '{"timestamp":"2026-10-03T01:46:27.212Z","type":"event_msg","payload":'
            '{"type":"agent_message","rate_limits":{"primary":'
            '{"used_percent":1,"window_minutes":300,"resets_at":10}}}}'
        )
        self.assertIsNone(widget.parse_codex_rate_limits(other))
        self.assertIsNone(widget.parse_codex_rate_limits("{not json"))
        self.assertIsNone(widget.parse_codex_rate_limits("[]"))

    def test_limit_id_other_than_codex_is_not_a_record(self):
        for limit_id in (None, "codex"):
            with self.subTest(limit_id=limit_id):
                doc = json.loads(EXAMPLE)
                doc["payload"]["rate_limits"]["limit_id"] = limit_id
                snapshot = widget.parse_codex_rate_limits(json.dumps(doc))
                self.assertEqual(snapshot["five_hour"].pct, 3.0)
        missing = json.loads(EXAMPLE)
        del missing["payload"]["rate_limits"]["limit_id"]
        self.assertEqual(widget.parse_codex_rate_limits(json.dumps(missing))["five_hour"].pct, 3.0)
        for limit_id in ("chatgpt", "other", ""):
            with self.subTest(limit_id=limit_id):
                doc = json.loads(EXAMPLE)
                doc["payload"]["rate_limits"]["limit_id"] = limit_id
                self.assertIsNone(widget.parse_codex_rate_limits(json.dumps(doc)))

    def test_iso_timestamp_forms(self):
        primary = {"used_percent": 3, "window_minutes": 300, "resets_at": 1791009691}
        secondary = {"used_percent": 36, "window_minutes": 10080, "resets_at": 1791512592}
        offset = widget.parse_codex_rate_limits(
            line_with(primary, secondary, timestamp="2026-10-03T09:46:27.212+08:00"))
        self.assertEqual(offset["five_hour"].observed_at, OBSERVED)
        whole = widget.parse_codex_rate_limits(
            line_with(primary, secondary, timestamp="2026-10-03T01:46:27Z"))
        expected = datetime.datetime.fromisoformat("2026-10-03T01:46:27+00:00").timestamp()
        self.assertEqual(whole["five_hour"].observed_at, expected)
        naive = widget.parse_codex_rate_limits(
            line_with(primary, secondary, timestamp="2026-10-03T01:46:27"))
        self.assertEqual(naive["five_hour"].observed_at, 0.0)
        garbage = widget.parse_codex_rate_limits(
            line_with(primary, secondary, timestamp="not-a-time"))
        self.assertEqual(garbage["five_hour"].observed_at, 0.0)
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            naive_line = line_with(primary, secondary, timestamp="2026-10-03T01:46:27")
            path = write_rollout(folder, "rollout-naive.jsonl", [naive_line], mtime=5151)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].observed_at, os.stat(path).st_mtime)
            garbage_line = line_with(primary, secondary, timestamp="not-a-time")
            write_rollout(folder, "rollout-naive.jsonl", [garbage_line], mtime=6161)
            self.assertTrue(reader.refresh(force=True))
            self.assertEqual(reader.snapshot["five_hour"].observed_at, os.stat(path).st_mtime)

    def test_bool_and_nan_rejected(self):
        text = line_with(
            {"used_percent": True, "window_minutes": 300, "resets_at": 10},
            {"used_percent": float("nan"), "window_minutes": 10080, "resets_at": 20},
        )
        self.assertIsNone(widget.parse_codex_rate_limits(text))
        bad_reset = line_with(
            {"used_percent": 1, "window_minutes": True, "resets_at": False},
            None,
        )
        self.assertIsNone(widget.parse_codex_rate_limits(bad_reset))


class FindRolloutTests(unittest.TestCase):
    def test_ordering_uses_os_stat_not_dir_entry_time(self):
        """目录项时间可能滞后。排序必须用 os.stat 打开文件后的 mtime。"""
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            held = write_rollout(folder, "rollout-held.jsonl", ["{}"], mtime=1000)
            other = write_rollout(folder, "rollout-other.jsonl", ["{}"], mtime=5000)
            real_stat = os.stat
            stamped = {"rollout-held.jsonl": 9000.0, "rollout-other.jsonl": 5000.0}

            def stat_open(path, *args, **kwargs):
                real_stat(path, *args, **kwargs)
                name = os.path.basename(path)

                class _Stamped(object):
                    st_mtime = stamped.get(name, 0.0)

                return _Stamped()

            # 写句柄一直开着。目录项时间停在 utime 的旧值，os.stat 被改成更新的值。
            with open(held, "ab") as handle:
                handle.write(b"")
                os.utime(held, (1000, 1000))
                with patch("os.stat", side_effect=stat_open):
                    found = widget.find_codex_rollouts(os.path.join(root, "sessions"))
            self.assertEqual(found[0], held)
            self.assertEqual(found, [held, other])

    def test_non_numeric_folder_with_newest_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            numeric = day_folder(root, 2026, 10, 3)
            stray = os.path.join(root, "sessions", "notes", "10", "03")
            os.makedirs(stray)
            kept = write_rollout(numeric, "rollout-kept.jsonl", ["{}"], mtime=1000)
            write_rollout(stray, "rollout-stray.jsonl", ["{}"], mtime=9000)
            found = widget.find_codex_rollouts(os.path.join(root, "sessions"))
            self.assertEqual(found, [kept])

    def test_newest_mtime_can_live_in_an_older_date_folder(self):
        with tempfile.TemporaryDirectory() as root:
            older = day_folder(root, 2026, 9, 1)
            newer = day_folder(root, 2026, 10, 3)
            os.makedirs(os.path.join(root, "sessions", "notes"))
            os.makedirs(os.path.join(root, "sessions", "2026", "10", "extra"))
            old_path = write_rollout(older, "rollout-old.jsonl", ["{}"], mtime=5000)
            new_path = write_rollout(newer, "rollout-new.jsonl", ["{}"], mtime=1000)
            write_rollout(newer, "other.txt", ["{}"], mtime=9000)
            found = widget.find_codex_rollouts(os.path.join(root, "sessions"))
            self.assertEqual(found, [old_path, new_path])

    def test_limit_and_only_newest_days(self):
        with tempfile.TemporaryDirectory() as root:
            paths = []
            for index in range(3):
                folder = day_folder(root, 2026, 1, index + 1)
                paths.append(write_rollout(
                    folder, "rollout-%d.jsonl" % index, ["{}"], mtime=1000 + index))
            found = widget.find_codex_rollouts(
                os.path.join(root, "sessions"), days=2, limit=1)
            self.assertEqual(found, [paths[2]])

    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(widget.find_codex_rollouts(os.path.join(root, "missing")), [])


class CodexReaderTests(unittest.TestCase):
    def test_missing_home_is_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            reader = widget.CodexReader(home=os.path.join(root, "absent"), clock=lambda: 0)
            self.assertFalse(reader.refresh())
            self.assertFalse(reader.available)
            self.assertIsNone(reader.snapshot)
            self.assertEqual(reader.last_reason, "no_codex")
            self.assertFalse(reader.refresh())

    def test_reads_only_the_tail_and_drops_a_cut_first_line(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            filler = "x" * (widget.CODEX_TAIL_BYTES + 50)
            good = line_with(
                {"used_percent": 3, "window_minutes": 300, "resets_at": 1791009691},
                {"used_percent": 36, "window_minutes": 10080, "resets_at": 1791512592},
            )
            path = write_rollout(folder, "rollout-big.jsonl", [filler, good])
            reader = widget.CodexReader(home=root, clock=lambda: 1000.0)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)
            self.assertEqual(reader.last_reason, "")

    def test_newest_without_limits_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            newest = write_rollout(folder, "rollout-new.jsonl", [PROMPT_LINE], mtime=2000)
            older = write_rollout(folder, "rollout-old.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, older)
            self.assertNotEqual(reader.source_path, newest)
            self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)

    def test_unchanged_signature_does_not_reread(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(folder, "rollout-a.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            reader.refresh()
            with patch("builtins.open", side_effect=AssertionError("reread")):
                self.assertFalse(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)

    def test_rescan_waits_for_interval(self):
        clock = {"now": 1000.0}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 2)
            write_rollout(folder, "rollout-old.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            reader.refresh()
            later = day_folder(root, 2026, 10, 3)
            fresh = line_with(
                {"used_percent": 11, "window_minutes": 300, "resets_at": 50},
                {"used_percent": 12, "window_minutes": 10080, "resets_at": 60},
            )
            path = write_rollout(later, "rollout-new.jsonl", [fresh], mtime=5000)
            clock["now"] = 1000.0 + widget.CODEX_RESCAN_SECONDS - 1
            self.assertFalse(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)
            clock["now"] = 1000.0 + widget.CODEX_RESCAN_SECONDS
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.snapshot["five_hour"].pct, 11.0)

    def test_malformed_newest_keeps_last_good_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            path = write_rollout(folder, "rollout-a.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            reader.refresh()
            kept = reader.snapshot
            broken = '{"timestamp":"2026-10-03T02:00:00.000Z","type":"event_msg","payload":{"type":"token_count","rate_limits":{'
            write_rollout(folder, "rollout-a.jsonl", [broken], mtime=2000)
            self.assertFalse(reader.refresh(force=True))
            self.assertEqual(reader.snapshot, kept)
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.last_reason, "no_rate_limits")

    def test_missing_timestamp_uses_file_mtime(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            text = line_with(
                {"used_percent": 5, "window_minutes": 300, "resets_at": 80},
                None,
                timestamp=None,
            )
            path = write_rollout(folder, "rollout-a.jsonl", [text], mtime=4242)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.snapshot["five_hour"].observed_at, os.stat(path).st_mtime)
            self.assertIsNone(reader.snapshot["seven_day"])

    def test_force_rereads_same_signature(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(folder, "rollout-a.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            reader.refresh()
            opens = {"count": 0}
            real_open = open

            def counting_open(*args, **kwargs):
                opens["count"] += 1
                return real_open(*args, **kwargs)

            with patch("builtins.open", counting_open):
                self.assertFalse(reader.refresh(force=True))
            self.assertGreaterEqual(opens["count"], 1)

    def test_older_observed_at_does_not_replace_snapshot_even_when_forced(self):
        """观测时间只增不减：Refresh now（force）也不能退回更旧的记录。"""
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            newer = line_with(
                {"used_percent": 20, "window_minutes": 300, "resets_at": 90},
                {"used_percent": 21, "window_minutes": 10080, "resets_at": 91},
                timestamp="2026-10-03T03:00:00.000Z",
            )
            older = line_with(
                {"used_percent": 1, "window_minutes": 300, "resets_at": 90},
                {"used_percent": 2, "window_minutes": 10080, "resets_at": 91},
                timestamp="2026-10-03T01:00:00.000Z",
            )
            path = write_rollout(folder, "rollout-a.jsonl", [newer], mtime=1000)
            # 时钟贴近当前快照，超前量不超过容差，更旧记录仍应被挡住。
            now = datetime.datetime.fromisoformat(
                "2026-10-03T03:00:00.000+00:00").timestamp()
            reader = widget.CodexReader(home=root, clock=lambda: now)
            self.assertTrue(reader.refresh())
            kept = reader.snapshot
            write_rollout(folder, "rollout-a.jsonl", [older], mtime=2000)
            self.assertFalse(reader.refresh())
            self.assertEqual(reader.snapshot, kept)
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.last_reason, "")
            # 强制刷新也不退回：时钟没有超前，更旧的记录就是更旧，快照、来源与原因都不变。
            self.assertFalse(reader.refresh(force=True))
            self.assertEqual(reader.snapshot, kept)
            self.assertEqual(reader.snapshot["five_hour"].pct, 20.0)
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.last_reason, "")

    def test_future_snapshot_does_not_freeze_a_later_normal_record(self):
        """当前快照的观测时间远在未来时，时钟校正后的正常记录必须替换它。"""
        future_at = "2099-01-01T00:00:00.000Z"
        future_unix = datetime.datetime.fromisoformat(
            "2099-01-01T00:00:00.000+00:00").timestamp()
        normal_at = "2026-10-03T03:00:00.000Z"
        normal_unix = datetime.datetime.fromisoformat(
            "2026-10-03T03:00:00.000+00:00").timestamp()
        self.assertGreater(future_unix - normal_unix, widget.CODEX_FUTURE_TOLERANCE_SECONDS)
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            future = line_with(
                {"used_percent": 90, "window_minutes": 300, "resets_at": 90},
                {"used_percent": 91, "window_minutes": 10080, "resets_at": 91},
                timestamp=future_at,
            )
            normal = line_with(
                {"used_percent": 4, "window_minutes": 300, "resets_at": 90},
                {"used_percent": 5, "window_minutes": 10080, "resets_at": 91},
                timestamp=normal_at,
            )
            path = write_rollout(folder, "rollout-a.jsonl", [future], mtime=1000)
            # 时钟已校正：now 比当前快照的观测时间早超过容差。
            reader = widget.CodexReader(home=root, clock=lambda: normal_unix)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].observed_at, future_unix)
            self.assertGreater(
                future_unix - normal_unix, widget.CODEX_FUTURE_TOLERANCE_SECONDS)
            write_rollout(folder, "rollout-a.jsonl", [normal], mtime=2000)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 4.0)
            self.assertEqual(reader.snapshot["five_hour"].observed_at, normal_unix)
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.last_reason, "")

    def test_clock_backwards_rescans(self):
        clock = {"now": 1000.0}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 2)
            write_rollout(folder, "rollout-old.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            reader.refresh()
            later = day_folder(root, 2026, 10, 3)
            fresh = line_with(
                {"used_percent": 15, "window_minutes": 300, "resets_at": 50},
                {"used_percent": 16, "window_minutes": 10080, "resets_at": 60},
            )
            path = write_rollout(later, "rollout-new.jsonl", [fresh], mtime=5000)
            clock["now"] = 999.0
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path)
            self.assertEqual(reader.snapshot["five_hour"].pct, 15.0)

    def test_tail_only_misses_a_record_before_the_tail(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            good = line_with(
                {"used_percent": 3, "window_minutes": 300, "resets_at": 1791009691},
                {"used_percent": 36, "window_minutes": 10080, "resets_at": 1791512592},
            )
            prompts = [PROMPT_LINE] * 8
            write_rollout(folder, "rollout-head.jsonl", [good] + prompts)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            with patch.object(widget, "CODEX_TAIL_BYTES", 40):
                self.assertFalse(reader.refresh())
            self.assertIsNone(reader.snapshot)
            self.assertEqual(reader.last_reason, "no_rate_limits")

    def test_tail_that_starts_on_a_record_line_drops_that_line(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            good = line_with(
                {"used_percent": 3, "window_minutes": 300, "resets_at": 1791009691},
                {"used_percent": 36, "window_minutes": 10080, "resets_at": 1791512592},
            )
            prompts = [PROMPT_LINE] * 4
            # 记录行前面留一个字节，尾部窗口正好从这条记录的行首开始，首行必须丢掉。
            path = write_rollout(folder, "rollout-aligned.jsonl", [good] + prompts)
            with open(path, "rb") as handle:
                raw = handle.read()
            prefixed = b"\n" + raw
            with open(path, "wb") as handle:
                handle.write(prefixed)
            tail = len(prefixed) - 1
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            with patch.object(widget, "CODEX_TAIL_BYTES", tail):
                self.assertFalse(reader.refresh())
            self.assertIsNone(reader.snapshot)
            self.assertEqual(reader.last_reason, "no_rate_limits")

    def test_prefilter_skips_lines_without_both_markers(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            rate_only = '{"payload":{"type":"other","rate_limits":{"primary":{}}}}'
            token_only = '{"payload":{"type":"token_count"},"text":"no limits here"}'
            # 从文件尾向前扫，不合格行必须排在 EXAMPLE 之后，否则合格行先被解析，预过滤删了也测不出来。
            write_rollout(folder, "rollout-a.jsonl", [EXAMPLE, PROMPT_LINE, rate_only, token_only])
            seen = []
            real_parse = widget.parse_codex_rate_limits

            def wrapped(line):
                seen.append(line)
                return real_parse(line)

            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            with patch.object(widget, "parse_codex_rate_limits", side_effect=wrapped):
                self.assertTrue(reader.refresh())
            self.assertEqual(seen, [EXAMPLE])
            for line in seen:
                self.assertIn('"rate_limits"', line)
                self.assertIn('"token_count"', line)

    def test_days_cutoff_excludes_the_newest_mtime_file(self):
        with tempfile.TemporaryDirectory() as root:
            old_day = day_folder(root, 2026, 9, 1)
            new_day = day_folder(root, 2026, 10, 3)
            excluded = write_rollout(old_day, "rollout-old.jsonl", [EXAMPLE], mtime=9000)
            kept = write_rollout(new_day, "rollout-new.jsonl", ["{}"], mtime=1000)
            found = widget.find_codex_rollouts(os.path.join(root, "sessions"), days=1)
            self.assertEqual(found, [kept])
            self.assertNotIn(excluded, found)

    def test_removing_sessions_clears_an_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(folder, "rollout-a.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            self.assertTrue(reader.refresh())
            self.assertIsNotNone(reader.snapshot)
            os.remove(os.path.join(folder, "rollout-a.jsonl"))
            os.rmdir(folder)
            os.rmdir(os.path.join(root, "sessions", "2026", "10"))
            os.rmdir(os.path.join(root, "sessions", "2026"))
            os.rmdir(os.path.join(root, "sessions"))
            self.assertTrue(reader.refresh())
            self.assertIsNone(reader.snapshot)
            self.assertFalse(reader.available)
            self.assertEqual(reader.last_reason, "no_codex")


class MultiSessionTests(unittest.TestCase):
    """同时开着多个 Codex 会话（应用加命令行很常见）：取观测时间最晚的记录，不是最近被写过的文件。

    会话 A 的最后一条限额记录是 03:00 的。会话 B 的是 01:00 的，但之后用户在 B 里敲了提示词，
    B 的 mtime 因此更新。mtime 只说明谁最后被写过，不说明谁有最新的限额记录。
    """

    # 晚于所有记录：当前快照不会被当成“来自未来”，更旧记录的拒绝逻辑照常生效。
    NOW = iso_unix("2026-10-03T06:00:00.000Z")

    def make_pair(self, root):
        folder = day_folder(root, 2026, 10, 3)
        path_a = write_rollout(
            folder, "rollout-a.jsonl", [record_line(40, STAMP_NEWER)], mtime=1000)
        path_b = write_rollout(
            folder, "rollout-b.jsonl",
            [record_line(10, STAMP_OLDER), PROMPT_LINE], mtime=2000)
        # 前提：B 才是 mtime 最新的文件。
        found = widget.find_codex_rollouts(os.path.join(root, "sessions"))
        self.assertEqual(found, [path_b, path_a])
        return folder, path_a, path_b

    def test_newer_record_wins_over_newer_file_on_first_read(self):
        with tempfile.TemporaryDirectory() as root:
            _folder, path_a, _path_b = self.make_pair(root)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path_a)
            self.assertEqual(reader.snapshot["five_hour"].pct, 40.0)
            self.assertEqual(reader.snapshot["seven_day"].pct, 41.0)
            observed = widget._snapshot_observed_at(reader.snapshot)
            self.assertEqual(observed, iso_unix(STAMP_NEWER))
            self.assertEqual(reader.last_reason, "")
            # 启动后第一次 Refresh now 的反馈文字也是 03:00，不是 B 的 01:00。
            self.assertEqual(
                widget.caption_text(None, observed, reader.last_reason),
                "new " + widget._clock_text(iso_unix(STAMP_NEWER)))

    def test_equal_observed_time_prefers_the_newer_file(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(
                folder, "rollout-a.jsonl", [record_line(40, STAMP_NEWER)], mtime=1000)
            path_b = write_rollout(
                folder, "rollout-b.jsonl", [record_line(10, STAMP_NEWER)], mtime=2000)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path_b)
            self.assertEqual(reader.snapshot["five_hour"].pct, 10.0)

    def test_a_later_record_in_either_file_takes_over(self):
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            _folder, path_a, path_b = self.make_pair(root)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            reader.refresh()
            self.assertEqual(reader.source_path, path_a)
            # B 的回复写完，它的新记录最晚。B 是 mtime 最新的文件，签名变了，马上读到。
            append_line(path_b, record_line(12, "2026-10-03T04:00:00.000Z"), mtime=3000)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path_b)
            self.assertEqual(reader.snapshot["five_hour"].pct, 12.0)
            # 之后 A 写了更新的记录。A 不是候选里的第一个（B 才是），候选顺序到下一次扫描才变，
            # 但签名覆盖全部候选，所以不用等扫描：这一次轮询就读到。
            append_line(path_a, record_line(45, "2026-10-03T05:00:00.000Z"), mtime=4000)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path_a)
            self.assertEqual(reader.snapshot["five_hour"].pct, 45.0)

    def test_a_record_in_a_log_that_was_not_the_newest_is_seen_on_the_next_poll(self):
        """候选顺序只在扫描时更新；门槛的签名若只看第一个候选，别的日志里的新记录要等下一次扫描。"""
        poll = widget.SNAPSHOT_POLL_MS / 1000.0
        self.assertLess(poll, widget.CODEX_RESCAN_SECONDS)
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            _folder, path_a, path_b = self.make_pair(root)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, path_a)
            self.assertEqual(reader.snapshot["five_hour"].pct, 40.0)
            # A 的会话答完一轮，追加了更晚的记录；它的 mtime 现在最新，但候选里 B 仍排第一。
            append_line(path_a, record_line(45, "2026-10-03T05:00:00.000Z"), mtime=4000)
            reads = []
            real_read = widget._read_codex_tail

            def counting(path):
                reads.append(path)
                return real_read(path)

            with patch.object(widget, "_read_codex_tail", side_effect=counting), \
                    patch.object(widget, "find_codex_rollouts",
                                 wraps=widget.find_codex_rollouts) as scan:
                clock["now"] += poll
                self.assertTrue(reader.refresh())
                self.assertEqual(reader.source_path, path_a)
                self.assertEqual(reader.snapshot["five_hour"].pct, 45.0)
                # 这一次不是靠重新扫描看到的，也只重读了变了的那个文件。
                scan.assert_not_called()
                self.assertEqual(reads, [path_a])
                # 之后什么都没变：连评估都不做。
                del reads[:]
                clock["now"] += poll
                self.assertFalse(reader.refresh())
                self.assertEqual(reads, [])
                self.assertEqual(reader.snapshot["five_hour"].pct, 45.0)
            # 前提：B 确实一直排在候选的第一个。
            self.assertEqual(reader._candidates, [path_b, path_a])

    def test_a_log_that_vanishes_after_the_scan_is_noticed_on_the_next_poll(self):
        """签名里有一个候选 stat 不了，就不能当作“没变”：照常评估，读不到的文件直接跳过。"""
        with tempfile.TemporaryDirectory() as root:
            _folder, path_a, path_b = self.make_pair(root)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            self.assertTrue(reader.refresh())
            self.assertEqual(set(reader._tail_cache), {path_a, path_b})
            os.remove(path_a)
            self.assertFalse(reader.refresh())
            # 重新评估过了：消失的那个文件的缓存项没了。已显示的读数不退回 B 里更旧的那条。
            self.assertEqual(set(reader._tail_cache), {path_b})
            self.assertEqual(reader.snapshot["five_hour"].pct, 40.0)
            self.assertEqual(reader.last_reason, "")

    def test_forced_refresh_never_lowers_the_observed_time(self):
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            path_a = write_rollout(
                folder, "rollout-a.jsonl", [record_line(40, STAMP_NEWER)], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            self.assertTrue(reader.refresh())
            kept = reader.snapshot
            observed = [widget._snapshot_observed_at(kept)]
            # 之后用户在会话 B 里敲了提示词：B 成了 mtime 最新的文件，它最后一条记录是 01:00 的。
            write_rollout(
                folder, "rollout-b.jsonl",
                [record_line(10, STAMP_OLDER), PROMPT_LINE], mtime=2000)
            clock["now"] += widget.CODEX_RESCAN_SECONDS
            for force in (False, True, True, False):
                reader.refresh(force=force)
                self.assertEqual(reader.snapshot, kept)
                self.assertEqual(reader.source_path, path_a)
                self.assertEqual(reader.last_reason, "")
                observed.append(widget._snapshot_observed_at(reader.snapshot))
            # A 里的记录读不到了（日志尾部只剩提示行），只剩 B 的旧记录：强制刷新也不退回。
            write_rollout(folder, "rollout-a.jsonl", [PROMPT_LINE] * 3, mtime=3000)
            self.assertFalse(reader.refresh(force=True))
            self.assertEqual(reader.snapshot, kept)
            self.assertEqual(reader.source_path, path_a)
            self.assertEqual(reader.last_reason, "")
            observed.append(widget._snapshot_observed_at(reader.snapshot))
            self.assertEqual(observed, [iso_unix(STAMP_NEWER)] * len(observed))

    def test_caption_after_forced_refresh_reports_the_newest_time(self):
        """README 里 same 的定义是数据里最新一条读数的时间，点 Refresh now 不能让它倒退。"""
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(
                folder, "rollout-a.jsonl", [record_line(40, STAMP_NEWER)], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            reader.refresh()
            path_b = write_rollout(
                folder, "rollout-b.jsonl",
                [record_line(10, STAMP_OLDER), PROMPT_LINE], mtime=2000)
            clock["now"] += widget.CODEX_RESCAN_SECONDS
            before = widget._snapshot_observed_at(reader.snapshot)
            reader.refresh(force=True)
            after = widget._snapshot_observed_at(reader.snapshot)
            text = widget.caption_text(before, after, reader.last_reason)
            self.assertEqual(text, "same " + widget._clock_text(iso_unix(STAMP_NEWER)))
            self.assertNotEqual(text, "same " + widget._clock_text(iso_unix(STAMP_OLDER)))
            self.assertGreaterEqual(after, before)
            # B 之后真的写出更新的记录：这一次是 new，时间是那条记录的。
            stamp = "2026-10-03T04:00:00.000Z"
            append_line(path_b, record_line(12, stamp), mtime=3000)
            reader.refresh(force=True)
            text = widget.caption_text(
                after, widget._snapshot_observed_at(reader.snapshot), reader.last_reason)
            self.assertEqual(text, "new " + widget._clock_text(iso_unix(stamp)))

    def test_forced_refresh_still_accepts_a_normal_record_after_a_clock_correction(self):
        """force 不再放行更旧的记录之后，时钟曾拨快的情形仍由容差分支处理。"""
        future_stamp = "2099-01-01T00:00:00.000Z"
        normal_unix = iso_unix(STAMP_NEWER)
        self.assertGreater(
            iso_unix(future_stamp) - normal_unix, widget.CODEX_FUTURE_TOLERANCE_SECONDS)
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(
                folder, "rollout-a.jsonl", [record_line(90, future_stamp)], mtime=1000)
            # 时钟已校正：now 比当前快照的观测时间早很多。
            reader = widget.CodexReader(home=root, clock=lambda: normal_unix)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 90.0)
            write_rollout(
                folder, "rollout-a.jsonl", [record_line(4, STAMP_NEWER)], mtime=2000)
            self.assertTrue(reader.refresh(force=True))
            self.assertEqual(reader.snapshot["five_hour"].pct, 4.0)
            self.assertEqual(widget._snapshot_observed_at(reader.snapshot), normal_unix)


class ClockAheadTests(unittest.TestCase):
    """写记录时时钟拨快了：观测时间超前 now 的记录，排在所有没超前的记录之后。

    只取观测时间最大的，这样的记录会一直压着之后写下的正常记录，直到时钟追上它。
    """

    NOW = iso_unix("2026-10-03T06:00:00.000Z")
    # 超前一天，远超容差；STAMP_NEWER / STAMP_OLDER 都早于 NOW，是正常记录。
    AHEAD = "2026-10-04T06:00:00.000Z"
    AHEAD_MORE = "2026-10-05T06:00:00.000Z"

    @staticmethod
    def stamp(unix_seconds):
        return datetime.datetime.fromtimestamp(
            unix_seconds, datetime.timezone.utc).isoformat()

    def test_the_stamps_mean_what_the_tests_assume(self):
        for text in (self.AHEAD, self.AHEAD_MORE):
            self.assertGreater(iso_unix(text) - self.NOW, widget.CODEX_FUTURE_TOLERANCE_SECONDS)
        for text in (STAMP_NEWER, STAMP_OLDER):
            self.assertLess(iso_unix(text), self.NOW)
        self.assertEqual(iso_unix(self.stamp(self.NOW + 300.0)), self.NOW + 300.0)

    def test_a_normal_record_in_another_log_outranks_a_future_stamped_one(self):
        # 正常记录的文件 mtime 较新或较旧都一样：结果不取决于候选顺序。
        for normal_mtime, ahead_mtime in ((2000, 1000), (1000, 2000)):
            with self.subTest(normal_mtime=normal_mtime), tempfile.TemporaryDirectory() as root:
                folder = day_folder(root, 2026, 10, 3)
                write_rollout(
                    folder, "rollout-ahead.jsonl",
                    [record_line(90, self.AHEAD)], mtime=ahead_mtime)
                normal = write_rollout(
                    folder, "rollout-normal.jsonl",
                    [record_line(4, STAMP_NEWER)], mtime=normal_mtime)
                reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
                self.assertTrue(reader.refresh())
                self.assertEqual(reader.source_path, normal)
                self.assertEqual(reader.snapshot["five_hour"].pct, 4.0)
                self.assertEqual(
                    widget._snapshot_observed_at(reader.snapshot), iso_unix(STAMP_NEWER))
                self.assertEqual(reader.last_reason, "")

    def test_refresh_now_replaces_a_future_stamped_reading_with_a_normal_one(self):
        """时钟拨快时写下的读数在屏幕上；时钟校正后 ping 在另一个日志里写下正常记录。"""
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(
                folder, "rollout-ahead.jsonl", [record_line(90, self.AHEAD)], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            # 只有超前的记录：没有可比的正常记录，照旧显示它。
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 90.0)
            normal = write_rollout(
                folder, "rollout-ping.jsonl", [record_line(4, STAMP_NEWER)], mtime=2000)
            clock["now"] += 5.0
            self.assertTrue(reader.refresh(force=True))
            self.assertEqual(reader.source_path, normal)
            self.assertEqual(reader.snapshot["five_hour"].pct, 4.0)
            # 再评估一次（Refresh now 每次都评估）也不会被压回超前的那条。
            clock["now"] += widget.CODEX_RESCAN_SECONDS
            self.assertFalse(reader.refresh(force=True))
            self.assertEqual(reader.snapshot["five_hour"].pct, 4.0)
            self.assertEqual(reader.source_path, normal)

    def test_when_every_record_is_ahead_the_latest_observed_time_still_wins(self):
        for a_mtime, b_mtime in ((2000, 1000), (1000, 2000)):
            with self.subTest(a_mtime=a_mtime), tempfile.TemporaryDirectory() as root:
                folder = day_folder(root, 2026, 10, 3)
                write_rollout(
                    folder, "rollout-a.jsonl", [record_line(10, self.AHEAD)], mtime=a_mtime)
                later = write_rollout(
                    folder, "rollout-b.jsonl", [record_line(20, self.AHEAD_MORE)], mtime=b_mtime)
                reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
                self.assertTrue(reader.refresh())
                self.assertEqual(reader.source_path, later)
                self.assertEqual(reader.snapshot["five_hour"].pct, 20.0)

    def test_equal_observed_time_among_ahead_records_prefers_the_newer_file(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(
                folder, "rollout-a.jsonl", [record_line(10, self.AHEAD)], mtime=1000)
            newer = write_rollout(
                folder, "rollout-b.jsonl", [record_line(20, self.AHEAD)], mtime=2000)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.source_path, newer)
            self.assertEqual(reader.snapshot["five_hour"].pct, 20.0)

    def test_a_record_ahead_by_exactly_the_tolerance_is_not_ahead(self):
        tolerance = widget.CODEX_FUTURE_TOLERANCE_SECONDS
        for extra, expected_pct in ((0.0, 10.0), (1.0, 4.0)):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as root:
                folder = day_folder(root, 2026, 10, 3)
                write_rollout(
                    folder, "rollout-edge.jsonl",
                    [record_line(10, self.stamp(self.NOW + tolerance + extra))], mtime=1000)
                write_rollout(
                    folder, "rollout-normal.jsonl",
                    [record_line(4, STAMP_NEWER)], mtime=2000)
                reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
                self.assertTrue(reader.refresh())
                # 容差之内算正常记录，观测时间更大的赢；多一秒就是超前，让位给正常记录。
                self.assertEqual(reader.snapshot["five_hour"].pct, expected_pct)

    def test_a_record_stops_being_ahead_when_the_clock_catches_up(self):
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            ahead = write_rollout(
                folder, "rollout-ahead.jsonl", [record_line(90, self.AHEAD)], mtime=1000)
            write_rollout(
                folder, "rollout-normal.jsonl", [record_line(4, STAMP_NEWER)], mtime=2000)
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 4.0)
            # 时钟走到了那条记录的时刻：它不再超前，观测时间更大，重新评估时它赢。
            clock["now"] = iso_unix(self.AHEAD)
            self.assertTrue(reader.refresh(force=True))
            self.assertEqual(reader.source_path, ahead)
            self.assertEqual(reader.snapshot["five_hour"].pct, 90.0)


class TailCacheTests(unittest.TestCase):
    """各候选文件尾部的解析结果按 (mtime_ns, size) 缓存，没变的文件不重读。"""

    NOW = iso_unix("2026-10-03T06:00:00.000Z")

    def counting_reads(self):
        """补丁 _read_codex_tail：照常读，同时记下读过的路径。"""
        reads = []
        real_read = widget._read_codex_tail

        def counting(path):
            reads.append(path)
            return real_read(path)

        return reads, patch.object(widget, "_read_codex_tail", side_effect=counting)

    def test_unchanged_file_is_not_read_again(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            path_a = write_rollout(
                folder, "rollout-a.jsonl", [record_line(40, STAMP_NEWER)], mtime=1000)
            path_b = write_rollout(
                folder, "rollout-b.jsonl", [record_line(10, STAMP_OLDER)], mtime=2000)
            # 没有任何记录的文件也只读一次：读过的结果（没有记录）同样进缓存。
            path_c = write_rollout(folder, "rollout-c.jsonl", [PROMPT_LINE], mtime=500)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            reads, patched = self.counting_reads()
            with patched:
                self.assertTrue(reader.refresh())
                self.assertCountEqual(reads, [path_a, path_b, path_c])
                # 最新文件 B 被追加一行提示词：它的签名变了，要重新评估，但 A、C 没变，不重读。
                del reads[:]
                append_line(path_b, PROMPT_LINE, mtime=3000)
                self.assertFalse(reader.refresh())
                self.assertEqual(reads, [path_b])
                # 什么都没变：连评估都不做。
                del reads[:]
                self.assertFalse(reader.refresh())
                self.assertEqual(reads, [])
                # Refresh now 绕过缓存，每个文件真读一遍。
                self.assertFalse(reader.refresh(force=True))
                self.assertCountEqual(reads, [path_a, path_b, path_c])
            self.assertEqual(reader.snapshot["five_hour"].pct, 40.0)
            self.assertEqual(reader.source_path, path_a)

    def test_changed_file_is_read_again(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(
                folder, "rollout-a.jsonl", [record_line(40, STAMP_NEWER)], mtime=1000)
            path_b = write_rollout(
                folder, "rollout-b.jsonl", [record_line(10, STAMP_OLDER)], mtime=2000)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            reads, patched = self.counting_reads()
            with patched:
                reader.refresh()
                del reads[:]
                # mtime 保持不变，只是大小变了：缓存的结果作废，新记录马上被看到。
                append_line(path_b, record_line(12, "2026-10-03T04:00:00.000Z"), mtime=2000)
                self.assertTrue(reader.refresh())
                self.assertEqual(reads, [path_b])
            self.assertEqual(reader.source_path, path_b)
            self.assertEqual(reader.snapshot["five_hour"].pct, 12.0)

    def test_cache_is_bounded_to_the_candidates(self):
        clock = {"now": self.NOW}
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            for index in range(widget.CODEX_MAX_CANDIDATES + 2):
                write_rollout(
                    folder, "rollout-%d.jsonl" % index,
                    [record_line(index, STAMP_OLDER)], mtime=1000 + index)
            sessions = os.path.join(root, "sessions")
            reader = widget.CodexReader(home=root, clock=lambda: clock["now"])
            reader.refresh()
            first = widget.find_codex_rollouts(sessions)
            self.assertEqual(len(first), widget.CODEX_MAX_CANDIDATES)
            self.assertEqual(set(reader._tail_cache), set(first))
            # 候选里的一个文件被删。到下一次扫描，它的缓存项就跟着丢掉。
            os.remove(first[2])
            clock["now"] += widget.CODEX_RESCAN_SECONDS
            reader.refresh()
            second = widget.find_codex_rollouts(sessions)
            self.assertNotIn(first[2], second)
            self.assertNotIn(first[2], reader._tail_cache)
            self.assertTrue(set(reader._tail_cache) <= set(second))
            # 重新评估之后，缓存正好是新的候选集合，不会多出来。
            reader.refresh(force=True)
            self.assertEqual(set(reader._tail_cache), set(second))
            self.assertLessEqual(len(reader._tail_cache), widget.CODEX_MAX_CANDIDATES)

    def test_cache_is_cleared_when_sessions_disappear(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            write_rollout(folder, "rollout-a.jsonl", [EXAMPLE], mtime=1000)
            reader = widget.CodexReader(home=root, clock=lambda: self.NOW)
            reader.refresh()
            self.assertEqual(len(reader._tail_cache), 1)
            os.remove(os.path.join(folder, "rollout-a.jsonl"))
            os.rmdir(folder)
            os.rmdir(os.path.join(root, "sessions", "2026", "10"))
            os.rmdir(os.path.join(root, "sessions", "2026"))
            os.rmdir(os.path.join(root, "sessions"))
            reader.refresh()
            self.assertEqual(reader._tail_cache, {})


class TailSplitTests(unittest.TestCase):
    """JSONL 只以 \\n 分行：U+2028 等字符既不能把一行拆开，别的行里有它们也挡不住记录。"""

    def write_tail(self, root, *chunks):
        folder = day_folder(root, 2026, 10, 3)
        path = os.path.join(folder, "rollout-split.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"".join(chunks))
        return path

    def prompt_with_separators(self):
        doc = {
            "timestamp": "2026-10-03T01:40:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "text": "a b c\u0085d"},
        }
        # ensure_ascii=False：U+2028、U+2029、NEL 原样写进这一行，JSON 允许。
        return json.dumps(doc, ensure_ascii=False).encode("utf-8") + b"\n"

    def test_record_after_a_line_with_separator_characters_is_parsed(self):
        with tempfile.TemporaryDirectory() as root:
            junk = b"junk \x0b \x0c \x1c \x1d \x1e junk\n"
            path = self.write_tail(
                root, self.prompt_with_separators(), junk, EXAMPLE.encode("utf-8") + b"\n",
                self.prompt_with_separators(), junk)
            snapshot, _mtime = widget._read_codex_tail(path)
            self.assertEqual(snapshot["five_hour"].pct, 3.0)
            self.assertEqual(snapshot["seven_day"].pct, 36.0)
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)

    def test_record_line_that_contains_separator_characters_stays_one_line(self):
        doc = json.loads(EXAMPLE)
        doc["payload"]["rate_limits"]["plan_type"] = "pro plus x\u0085y"
        text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        # str.splitlines 会把这一行拆成四段，没有一段是合法 JSON。
        self.assertEqual(len(text.splitlines()), 4)
        with tempfile.TemporaryDirectory() as root:
            path = self.write_tail(
                root, PROMPT_LINE.encode("utf-8") + b"\n", text.encode("utf-8") + b"\n",
                PROMPT_LINE.encode("utf-8") + b"\n")
            snapshot, _mtime = widget._read_codex_tail(path)
            self.assertEqual(snapshot["five_hour"].pct, 3.0)
            self.assertEqual(snapshot["seven_day"].pct, 36.0)

    def test_crlf_line_endings_parse_the_same(self):
        with tempfile.TemporaryDirectory() as root:
            raw = "\r\n".join([PROMPT_LINE, EXAMPLE, PROMPT_LINE]) + "\r\n"
            path = self.write_tail(root, raw.encode("utf-8"))
            snapshot, _mtime = widget._read_codex_tail(path)
            self.assertEqual(snapshot["five_hour"], widget.Win(3.0, 1791009691, OBSERVED))
            self.assertEqual(snapshot["seven_day"], widget.Win(36.0, 1791512592, OBSERVED))

    def test_invalid_utf8_is_replaced_not_fatal(self):
        record = EXAMPLE.encode("utf-8").replace(b'"plan_type":null', b'"plan_type":"\xff\xfe"')
        self.assertIn(b"\xff\xfe", record)
        with tempfile.TemporaryDirectory() as root:
            path = self.write_tail(root, b"\xff\xfe junk\n", record + b"\n", b"\xc3\n")
            snapshot, _mtime = widget._read_codex_tail(path)
            self.assertEqual(snapshot["five_hour"].pct, 3.0)

    def test_tail_cut_inside_a_multibyte_character_drops_only_the_cut_line(self):
        record = EXAMPLE.encode("utf-8")
        filler = ("界" * 20).encode("utf-8")
        with tempfile.TemporaryDirectory() as root:
            path = self.write_tail(root, filler + b"\n", record + b"\n")
            # 尾部从填充行最后一个汉字的最后一个字节开始，后面是换行和记录行。
            with patch.object(widget, "CODEX_TAIL_BYTES", len(record) + 3):
                snapshot, _mtime = widget._read_codex_tail(path)
            self.assertEqual(snapshot["five_hour"].pct, 3.0)


class ReadErrorTests(unittest.TestCase):
    """候选文件读不了（ACL 问题等）要报 read error，不能当成还没有数据。"""

    def make_reader(self, root, count=2):
        folder = day_folder(root, 2026, 10, 3)
        paths = [
            write_rollout(folder, "rollout-%d.jsonl" % index, [EXAMPLE], mtime=1000 + index)
            for index in range(count)]
        return paths, widget.CodexReader(home=root, clock=lambda: 10.0)

    def test_every_candidate_unreadable_reports_read_error(self):
        with tempfile.TemporaryDirectory() as root:
            paths, reader = self.make_reader(root)
            denied = PermissionError(13, "Permission denied", paths[0])
            with patch.object(widget, "_read_codex_tail", side_effect=denied):
                self.assertFalse(reader.refresh())
            self.assertIsNone(reader.snapshot)
            self.assertEqual(reader.last_reason, "read_error:PermissionError")
            # 原因里只有异常类名：没有路径，也没有文件内容。
            self.assertNotIn(root, reader.last_reason)
            self.assertNotIn(os.path.basename(paths[0]), reader.last_reason)
            self.assertNotIn(reader.last_reason, widget.CAPTION_BENIGN_REASONS)
            self.assertEqual(
                widget.caption_text(None, None, reader.last_reason), widget.CAPTION_READ_ERROR)

    def test_read_error_is_retried_by_the_next_ordinary_poll(self):
        with tempfile.TemporaryDirectory() as root:
            _paths, reader = self.make_reader(root)
            with patch.object(
                    widget, "_read_codex_tail", side_effect=PermissionError(13, "denied")):
                reader.refresh()
            self.assertEqual(reader.last_reason, "read_error:PermissionError")
            # 文件没变，也没点 Refresh now：下一次轮询就重试，不能卡在“没变化”的捷径上。
            self.assertTrue(reader.refresh())
            self.assertEqual(reader.last_reason, "")
            self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)

    def test_read_error_keeps_the_last_good_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            _paths, reader = self.make_reader(root)
            reader.refresh()
            kept = reader.snapshot
            before = widget._snapshot_observed_at(kept)
            with patch.object(widget, "_read_codex_tail", side_effect=OSError(5, "I/O error")):
                self.assertFalse(reader.refresh(force=True))
            self.assertEqual(reader.snapshot, kept)
            self.assertEqual(reader.last_reason, "read_error:OSError")
            after = widget._snapshot_observed_at(reader.snapshot)
            self.assertEqual(
                widget.caption_text(before, after, reader.last_reason),
                widget.CAPTION_READ_ERROR)

    def test_a_file_deleted_after_the_scan_is_not_a_read_error(self):
        with tempfile.TemporaryDirectory() as root:
            folder = day_folder(root, 2026, 10, 3)
            gone = os.path.join(folder, "rollout-gone.jsonl")
            reader = widget.CodexReader(home=root, clock=lambda: 10.0)
            # 候选列表里的文件在读取前不在了：stat 就失败。
            with patch.object(widget, "find_codex_rollouts", return_value=[gone]):
                self.assertFalse(reader.refresh())
            self.assertEqual(reader.last_reason, "no_rate_limits")
            self.assertEqual(widget.caption_text(None, None, reader.last_reason), "no data")
            # stat 之后、open 之前才被删：同样不是读取失败。
            write_rollout(folder, "rollout-a.jsonl", [EXAMPLE], mtime=1000)
            other = widget.CodexReader(home=root, clock=lambda: 10.0)
            with patch.object(
                    widget, "_read_codex_tail", side_effect=FileNotFoundError(2, "gone")):
                self.assertFalse(other.refresh())
            self.assertEqual(other.last_reason, "no_rate_limits")

    def test_one_unreadable_candidate_does_not_hide_a_record_in_another(self):
        with tempfile.TemporaryDirectory() as root:
            paths, reader = self.make_reader(root)
            real_read = widget._read_codex_tail
            calls = []

            def flaky(path):
                calls.append(path)
                if path == paths[1]:
                    raise PermissionError(13, "denied")
                return real_read(path)

            with patch.object(widget, "_read_codex_tail", side_effect=flaky):
                self.assertTrue(reader.refresh())
                self.assertEqual(reader.source_path, paths[0])
                self.assertEqual(reader.snapshot["five_hour"].pct, 3.0)
                self.assertEqual(reader.last_reason, "")
                # 读失败的那个下一次轮询还要再试；读成功的那个已经缓存，不重读。
                del calls[:]
                self.assertFalse(reader.refresh())
                self.assertEqual(calls, [paths[1]])


if __name__ == "__main__":
    unittest.main()
