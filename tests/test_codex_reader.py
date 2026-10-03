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

    def test_older_observed_at_does_not_replace_snapshot_unless_forced(self):
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
            self.assertTrue(reader.refresh(force=True))
            self.assertEqual(reader.snapshot["five_hour"].pct, 1.0)

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


if __name__ == "__main__":
    unittest.main()
