# -*- coding: utf-8 -*-
"""数据层：快照读取与校验、Codex 限流解析与 ping、应用刷新、错误日志、位置文件与主题读取。

从 usage_widget.py 拆出的模块之一，行为与原单文件实现一致。测试对 usage_widget
打的补丁经 _entry() 取回入口模块，所以这里不直接引用被测试替换的名字。
"""

import collections
import datetime
import glob
import json
import math
import os
import re
import subprocess
import sys
import time

# winreg 只在 Windows 上有；其他平台置为 None，read_light_theme 按浅色处理。
try:
    import winreg
except ImportError:
    winreg = None


def _entry():
    """入口模块（usage_widget 或 __main__）：测试对入口模块打的补丁必须在这里仍然可见。"""
    return sys.modules.get("usage_widget") or sys.modules["__main__"]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SNAPSHOT_NAME = "usage.json"
ERROR_LOG_NAME = "widget_error.log"
POS_FILE_NAME = "widget_pos.json"
POS_SCHEMA = 2
MODE_AUTO = "auto"
MODE_MANUAL = "manual"

SKIN_FILE_NAME = "widget_skin.json"
SKIN_SCHEMA = 1
SKIN_NEON = "neon"
SKIN_MECHA = "mecha"
SKINS = (SKIN_NEON, SKIN_MECHA)
DEFAULT_SKIN = SKIN_NEON
SKIN_LABELS = {SKIN_NEON: "Neon Minimal", SKIN_MECHA: "Mecha Tech"}

STYLE_AUTO = "auto"
STYLE_BAR = "bar"
STYLE_RINGS = "rings"
STYLES = (STYLE_AUTO, STYLE_BAR, STYLE_RINGS)
DEFAULT_STYLE = STYLE_AUTO
STYLE_FILE_NAME = "widget_style.json"
STYLE_SCHEMA = 1
STYLE_LABELS = {
    STYLE_AUTO: "Auto (fit taskbar)",
    STYLE_BAR: "Full bar",
    STYLE_RINGS: "Rings",
}

MAX_SNAPSHOT_BYTES = 1024 * 1024

LOG_MAX_BYTES = 64 * 1024
LOG_MAX_MESSAGE = 500
LOG_DEDUP_SECONDS = 600.0
LOG_MAX_KEYS = 64

# 取不到托盘区矩形时的退路：右边缘 = 任务栏右边缘 - 这个逻辑像素数
DEFAULT_OFFSET = 330
OFFSET_LIMIT = 100000

# 数据文件不存在时的反馈文字（只有 Claude 块的 usage.json 会报 missing）。
CAPTION_NO_FILE = "no file"

THEME_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
THEME_VALUE_NAME = "SystemUsesLightTheme"


# ---------------------------------------------------------------------------
# 纯函数层：快照读取与校验
# ---------------------------------------------------------------------------

Win = collections.namedtuple("Win", ["pct", "resets_at", "observed_at"])


def _finite(value):
    """int/float（bool 不算）且有限则返回 float，否则返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # 超大整数转不了 float，按无效处理
        return None
    return number if math.isfinite(number) else None


def _parse_window(raw, written_at):
    if not isinstance(raw, dict):
        return None
    pct = _finite(raw.get("used_percentage"))
    if pct is None or pct < 0:
        return None
    resets_at = _finite(raw.get("resets_at"))
    if resets_at is None or resets_at <= 0:
        return None
    observed_at = _finite(raw.get("observed_at"))
    if observed_at is None:
        observed_at = written_at
    if observed_at is None:
        return None
    return Win(pct, resets_at, observed_at)


def _schema_is(doc, n):
    """doc 的 schema 字段是不是数字 n。布尔值不算数字；字符串、缺失或其他类型都不相等。"""
    schema = doc.get("schema")
    return not isinstance(schema, bool) and isinstance(schema, (int, float)) and schema == n


def parse_snapshot(raw):
    """解析快照字节串，返回 (snapshot, reason)。

    snapshot 为 None 表示本次读取作废（调用方沿用上一次成功的数据），reason 给出
    简短原因；成功时 snapshot 是 {"five_hour": Win|None, "seven_day": Win|None}。
    写入方之一不是原子写，所以截断或半截内容只会落到 bad_json，不会抛异常。
    """
    if len(raw) > MAX_SNAPSHOT_BYTES:
        return None, "too_large"
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(doc, dict):
        return None, "not_object"
    if not _schema_is(doc, 1):
        return None, "bad_schema"
    windows = doc.get("windows")
    if not isinstance(windows, dict):
        return None, "bad_windows"
    written_at = _finite(doc.get("written_at"))
    snapshot = {}
    for key in ("five_hour", "seven_day"):
        snapshot[key] = _parse_window(windows.get(key), written_at)
    return snapshot, ""


def read_snapshot(path):
    """打开、一次读完、立即关闭（不长占文件，写入方才能 os.replace）。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, "read_error:" + type(exc).__name__
    return parse_snapshot(raw)


class SnapshotReader:
    """按 mtime/size 变化才重读；坏内容作废并沿用上一次成功的数据。"""

    def __init__(self, path):
        self.path = path
        self.snapshot = None
        self.last_reason = ""
        self._signature = None

    def refresh(self, force=False):
        """返回快照内容是否变化。签名只在读取成功后更新，失败的下次自动重读。"""
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            self.last_reason = "missing"
            return False
        except OSError as exc:
            self.last_reason = "stat_error:" + type(exc).__name__
            return False
        signature = (stat.st_mtime_ns, stat.st_size)
        if not force and signature == self._signature:
            return False
        snapshot, reason = _entry().read_snapshot(self.path)
        self.last_reason = reason
        if snapshot is None:
            return False
        self._signature = signature
        changed = snapshot != self.snapshot
        self.snapshot = snapshot
        return changed


# ---------------------------------------------------------------------------
# Codex rate limits (read-only)
# 只读本地会话日志尾部的 rate_limits。用户提示行不解析、不保存、不写入原因。
# ---------------------------------------------------------------------------

CODEX_SCAN_DAYS = 14
CODEX_RESCAN_SECONDS = 60.0
CODEX_TAIL_BYTES = 1024 * 1024
CODEX_MAX_CANDIDATES = 5
CODEX_WINDOW_KEYS = {300: "five_hour", 10080: "seven_day"}
# 当前快照的观测时间超前时钟超过这个秒数时，视为时钟曾拨快后又拨回，不再用“更旧”挡住新记录。
CODEX_FUTURE_TOLERANCE_SECONDS = 300.0
# 这三个读取原因只表示没有这个数据源，或数据源里还没有限额记录，不算读取失败
# （no_rate_limits：装了 Codex 但还没跑过带限额记录的对话）。其它非空原因
# （bad_json、read_error:... 等）才算失败。
CAPTION_BENIGN_REASONS = frozenset({"missing", "no_codex", "no_rate_limits"})
CAPTION_READ_ERROR = "read error"


def codex_home(override=None):
    """override，否则环境变量 CODEX_HOME，否则 ~/.codex。"""
    if override is not None:
        return override
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return env_home
    return os.path.join(os.path.expanduser("~"), ".codex")


def _unix_from_iso(value):
    """ISO 8601 转 Unix 秒。尾部 Z 当作 UTC；无效或缺失返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _codex_window(raw, observed_at, default_key):
    """解析 primary 或 secondary。无效返回 (None, None)。"""
    if not isinstance(raw, dict):
        return None, None
    pct = _finite(raw.get("used_percent"))
    if pct is None or pct < 0:
        return None, None
    resets = _finite(raw.get("resets_at"))
    if resets is None:
        # 没有 resets_at 时，才用观测时刻加 resets_in_seconds
        delta = _finite(raw.get("resets_in_seconds"))
        if delta is None or observed_at is None:
            return None, None
        resets = observed_at + delta
    if resets <= 0:
        return None, None
    key = default_key
    minutes = _finite(raw.get("window_minutes"))
    if minutes is not None:
        try:
            mapped = CODEX_WINDOW_KEYS.get(int(minutes))
        except (OverflowError, ValueError):
            mapped = None
        if mapped is not None:
            key = mapped
    stamped = observed_at if observed_at is not None else 0.0
    return key, Win(pct, resets, stamped)


def parse_codex_rate_limits(line_text):
    """解析一行日志。不是 token_count，或两个窗口都无效时返回 None。"""
    try:
        doc = json.loads(line_text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    payload = doc.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    # 只认缺省、null 或 codex。别的 limit_id 是以后的另一个桶，这里当作没有这条记录。
    if "limit_id" in limits and limits.get("limit_id") not in (None, "codex"):
        return None
    observed_at = _unix_from_iso(doc.get("timestamp"))
    snapshot = {"five_hour": None, "seven_day": None}
    for name, default_key in (("primary", "five_hour"), ("secondary", "seven_day")):
        key, win = _codex_window(limits.get(name), observed_at, default_key)
        if key in snapshot and win is not None:
            snapshot[key] = win
    if snapshot["five_hour"] is None and snapshot["seven_day"] is None:
        return None
    return snapshot


def _numeric_child_dirs(path):
    """子目录里纯数字的 (整数值, 路径)。读失败或名称不是数字则跳过。"""
    found = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if not name.isascii() or not name.isdigit():
                    continue
                try:
                    number = int(name)
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except (OSError, ValueError):
                    continue
                found.append((number, entry.path))
    except OSError:
        return []
    return found


def find_codex_rollouts(sessions_dir, days=CODEX_SCAN_DAYS, limit=CODEX_MAX_CANDIDATES):
    """最新 days 个日期目录里的 rollout-*.jsonl，按 mtime 从新到旧，最多 limit 个。"""
    if days <= 0 or limit <= 0:
        return []
    day_folders = []
    for year, year_path in _numeric_child_dirs(sessions_dir):
        for month, month_path in _numeric_child_dirs(year_path):
            for day, day_path in _numeric_child_dirs(month_path):
                day_folders.append((year, month, day, day_path))
    day_folders.sort(reverse=True)
    ranked = []
    for _year, _month, _day, folder in day_folders[:days]:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        # 目录项时间在文件被别的进程追加打开时可能滞后。
                        # os.stat 会打开文件，拿到当前的 mtime。
                        mtime = os.stat(entry.path).st_mtime
                    except OSError:
                        continue
                    ranked.append((mtime, entry.path))
        except OSError:
            continue
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in ranked[:limit]]


def _snapshot_observed_at(snapshot):
    """各窗口观测时间的最大值。没有窗口时返回 None。"""
    if not snapshot:
        return None
    latest = None
    for key in ("five_hour", "seven_day"):
        win = snapshot.get(key)
        if win is None:
            continue
        if latest is None or win.observed_at > latest:
            latest = win.observed_at
    return latest


def _ahead_of_clock(observed_at, now):
    """观测时间是否比 now 超前超过 CODEX_FUTURE_TOLERANCE_SECONDS（时钟曾拨快后又校正的迹象）。

    恰好超前容差那么多不算。_snapshot_is_older 与 CodexReader._latest_record 用同一个判断。
    """
    return observed_at - now > CODEX_FUTURE_TOLERANCE_SECONDS


def _snapshot_is_older(found, current, now=None):
    """found 的观测时间是否早于已有快照。缺一边的时间时不算更旧。

    当前快照的最新观测时间比 now 超前超过 CODEX_FUTURE_TOLERANCE_SECONDS 时，
    视为时钟曾拨快后又校正，不再把文件里较新的记录当成更旧而拒绝。
    """
    if not current:
        return False
    current_at = _snapshot_observed_at(current)
    found_at = _snapshot_observed_at(found)
    if current_at is None or found_at is None:
        return False
    if now is not None and _ahead_of_clock(current_at, now):
        return False
    return found_at < current_at


def _stamp_observed(snapshot, mtime):
    """时间戳缺失时占位 0.0，读到文件后换成该文件的 mtime。"""
    stamped = {}
    for key in ("five_hour", "seven_day"):
        win = snapshot[key]
        if win is not None and win.observed_at == 0.0:
            win = win._replace(observed_at=mtime)
        stamped[key] = win
    return stamped


def _read_codex_tail(path):
    """只读文件尾部。返回 (snapshot 或 None, mtime)。调用方负责捕获 OSError。

    JSONL 只以 \\n 分行。str.splitlines 还会在 U+2028、U+2029、NEL、FF、0x1C 到 0x1E 处断行，
    会把含这些字符的一行拆碎，所以先在原始字节上按 b"\\n" 切，只对通过预过滤的行解码。
    """
    file_stat = os.stat(path)
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        tail_bytes = _entry().CODEX_TAIL_BYTES
        start = size - tail_bytes if size > tail_bytes else 0
        handle.seek(start)
        raw = handle.read(tail_bytes)
    lines = raw.split(b"\n")
    # 不是从文件头开始时，首行多半被截断，丢掉
    if start != 0:
        lines = lines[1:]
    for line in reversed(lines):
        if b'"rate_limits"' not in line or b'"token_count"' not in line:
            continue
        parsed = _entry().parse_codex_rate_limits(line.decode("utf-8", errors="replace"))
        if parsed is not None:
            return _stamp_observed(parsed, file_stat.st_mtime), file_stat.st_mtime
    return None, file_stat.st_mtime


class CodexReader:
    """按日期目录找最近的几个 rollout，取其中观测时间最晚的限额记录。

    只在候选文件的签名（每个候选的路径、mtime_ns、size）有变化时重新评估；各文件尾部的
    解析结果按 (mtime_ns, size) 缓存，没变的文件不重读。
    """

    def __init__(self, home=None, clock=time.time):
        self.home = codex_home(home)
        self.clock = clock
        self.available = False
        self.snapshot = None
        self.last_reason = ""
        self.source_path = None
        self._candidates = None
        self._last_scan = None
        self._signature = None
        # 路径 -> ((mtime_ns, size), 该文件尾部解析出的快照或 None)。只保留当前候选，不会增长。
        self._tail_cache = {}

    def refresh(self, force=False):
        """返回快照是否变化。任何 OSError / ValueError 都吞掉，不把文件内容写进原因。"""
        try:
            return self._refresh(force)
        except (OSError, ValueError):
            return False

    def _refresh(self, force):
        sessions = os.path.join(self.home, "sessions")
        self.available = os.path.isdir(sessions)
        if not self.available:
            changed = self.snapshot is not None
            self.snapshot = None
            self.last_reason = "no_codex"
            self.source_path = None
            self._candidates = None
            self._last_scan = None
            self._signature = None
            self._tail_cache = {}
            return changed

        now = self.clock()
        # 时钟回拨时上次扫描时间在未来，也要重新扫，否则候选列表会一直过期。
        if (force or self._candidates is None or self._last_scan is None
                or now < self._last_scan
                or now - self._last_scan >= CODEX_RESCAN_SECONDS):
            self._candidates = _entry().find_codex_rollouts(sessions)
            self._last_scan = now
            # 缓存只留当前候选：掉出候选的路径立即丢掉。
            kept = set(self._candidates)
            self._tail_cache = {
                path: entry for path, entry in self._tail_cache.items() if path in kept}

        if not self._candidates:
            self.last_reason = "no_rate_limits"
            self._signature = None
            self._tail_cache = {}
            return False

        signature = self._candidates_signature()
        if not force and signature is not None and signature == self._signature:
            return False

        found, found_path, read_error = self._latest_record(force, now)
        if read_error:
            # 有候选读失败：不记签名，下次轮询再试（SnapshotReader 也只在读成功后更新签名）。
            self._signature = None
        else:
            # signature 为 None（有候选 stat 失败）时同样不记，下次轮询照常重新评估。
            self._signature = signature
        if found is None:
            # 一条记录都没有：读不了（read error）与还没有记录（no data）要分开。
            self.last_reason = read_error or "no_rate_limits"
            return False
        # 观测时间只增不减，强制刷新也一样：找到的记录比已有快照更旧就不采用。
        # 从前 force 会放行更旧的记录，为的是时钟曾拨快后又校正的情形；那种情形现在由两处
        # 自己处理：当前快照的观测时间比 now 超前超过 CODEX_FUTURE_TOLERANCE_SECONDS 时，
        # _snapshot_is_older 不再拒绝较新的记录；_latest_record 又把这样超前的记录排在正常
        # 记录之后，别的日志里的正常记录不会被它压住。所以不必再靠 force 放行。
        if _snapshot_is_older(found, self.snapshot, now):
            self.last_reason = ""
            return False
        changed = found != self.snapshot
        self.snapshot = found
        self.source_path = found_path
        self.last_reason = ""
        return changed

    def _candidates_signature(self):
        """所有候选文件的 (路径, mtime_ns, size)，按候选顺序排成元组；有一个 stat 失败就返回 None。

        只看 mtime 最新的那个文件会漏掉别的日志里的新记录：它在上一次扫描时不是最新的，
        追加之后候选顺序要等下一次扫描（最多 60 秒）才会变，期间每次轮询都看不见。
        候选最多 CODEX_MAX_CANDIDATES 个，每次轮询多几次 stat；没变的文件的解析结果仍
        按 (mtime_ns, size) 缓存，所以多的只是 stat，不会多读文件。
        返回 None 表示这一轮无法确认“没变”：调用方照常重新评估，也不记签名。
        """
        parts = []
        for path in self._candidates:
            try:
                file_stat = os.stat(path)
            except OSError:
                return None
            parts.append((path, file_stat.st_mtime_ns, file_stat.st_size))
        return tuple(parts)

    def _latest_record(self, force, now):
        """解析每个候选文件的尾部，返回 (快照或 None, 路径或 None, 读取失败的原因或 "")。

        快照取观测时间最晚的那条。文件的 mtime 只说明谁最后被写过：在某个会话里敲提示词
        也会抬高它日志的 mtime，却不带新的限额记录；应用加命令行同时开着两个会话很常见，
        所以 mtime 最新的文件未必有最新的记录。候选都看，按记录自己的观测时间取最大；
        观测时间相同取排在前面的，即 mtime 较新的文件。

        例外：观测时间比 now 超前超过 CODEX_FUTURE_TOLERANCE_SECONDS 的记录（写它的时候
        时钟拨快了）排在所有没超前的记录之后。否则它的观测时间比之后写下的正常记录大，
        要等时钟追上才会让位；排在后面，另一个日志里的正常记录就能马上顶替它。所有记录
        都超前时没有可比的正常记录，照旧取观测时间最晚的。

        没变的文件不重读：按 (mtime_ns, size) 缓存各路径的解析结果，缓存只留当前候选。
        签名在读取之前取，读到的内容至少和签名一样新；读的当中文件又被追加，下次签名
        不同，会重读。force 绕过缓存，Refresh now 总是真读一遍。
        读失败的路径不进缓存，下次再试。扫描之后才被删掉的文件（FileNotFoundError）只说明
        候选列表过期，不算读取失败。
        """
        fresh = {}
        best = None
        best_path = None
        best_rank = None
        read_error = ""
        for path in self._candidates:
            try:
                file_stat = os.stat(path)
                token = (file_stat.st_mtime_ns, file_stat.st_size)
                cached = None if force else self._tail_cache.get(path)
                if cached is not None and cached[0] == token:
                    parsed = cached[1]
                else:
                    parsed, _mtime = _entry()._read_codex_tail(path)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                if not read_error:
                    read_error = "read_error:" + type(exc).__name__
                continue
            fresh[path] = (token, parsed)
            if parsed is None:
                continue
            observed_at = _snapshot_observed_at(parsed)
            # 没超前的排在超前的前面；同一类里观测时间大的在前，相同则先遇到的（mtime 较新的文件）保住。
            rank = (not _ahead_of_clock(observed_at, now), observed_at)
            if best is None or rank > best_rank:
                best = parsed
                best_path = path
                best_rank = rank
        self._tail_cache = fresh
        return best, best_path, read_error


# ---------------------------------------------------------------------------
# Codex ping：Refresh now 时向 Codex 发一个很小的请求
# 让 Codex 把最新额度写进它的会话日志。小窗仍然只读这份日志，不解析命令行输出。
# ---------------------------------------------------------------------------

# 发往 Codex 的模型名。最终进命令行，须再过 valid_ping_model。
CODEX_PING_MODEL = "gpt-6-luna"
# 最短提示词。只要 Codex 写出带限额的会话日志即可。
CODEX_PING_PROMPT = "ok"
# 从成功启动起算，超过这个秒数仍未退出就开始终止。
CODEX_PING_TIMEOUT_SECONDS = 60.0
# terminate 之后再等这么久仍未退出才 kill；kill 之后再等这么久仍未回收就放弃跟踪。
CODEX_PING_KILL_WAIT_SECONDS = 2.0
# 成功启动之后的冷却。失败或超时的那次也占冷却，避免连点反复拉起进程。
CODEX_PING_COOLDOWN_SECONDS = 300.0
# 主线程轮询子进程的间隔。本模块不设计时器，只给出毫秒数。
CODEX_PING_POLL_MS = 500
# 子进程的工作目录名，建在 data_dir 下，不借用用户正在用的目录。
CODEX_PING_DIR_NAME = "codex-ping"
# 子进程不弹控制台窗口。Windows 上就是 subprocess.CREATE_NO_WINDOW；非 Windows 上没有这个属性，
# 退回同一个值 0x08000000，让离线测试也能构造 Popen 参数。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
# 反馈文字。小窗上的字必须是纯 ASCII。
CAPTION_ASKING = "asking"
CAPTION_COOLDOWN = "cooldown"
CAPTION_PING_FAILED = "ping failed"


def valid_ping_model(name):
    """模型名会进命令行，只放行保守字符集。"""
    if not isinstance(name, str):
        return False
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", name) is not None


def find_codex_exe(override=None, path_env=None, appdata=None):
    """返回原生 codex.exe 的路径，找不到返回 None。任何 OSError 都当没找到。

    不接受 .cmd / .bat / .ps1：npm 垫片经 node 再起原生程序，杀掉垫片留不住孙进程。
    PATH 与 APPDATA 里只认绝对路径：空项和相对项会按小窗当前的工作目录解析，
    那个目录取决于小窗怎么被启动，不能让里面的 codex.exe 被点一下就跑起来。
    override（--codex-bin）是显式指定的文件，不受此限。
    """
    try:
        if override is not None:
            if os.path.isfile(override) and override.lower().endswith(".exe"):
                return override
            return None
    except OSError:
        return None

    if path_env is None:
        path_env = os.environ.get("PATH", "")
    for directory in path_env.split(os.pathsep):
        if not directory.strip() or not os.path.isabs(directory):
            continue
        candidate = os.path.join(directory, "codex.exe")
        try:
            if os.path.isfile(candidate):
                return candidate
        except OSError:
            continue

    if appdata is None:
        appdata = os.environ.get("APPDATA")
    if appdata and os.path.isabs(appdata):
        pattern = os.path.join(
            glob.escape(appdata),
            "npm", "node_modules", "@openai", "codex", "node_modules",
            "@openai", "codex-win32-*", "vendor", "*", "bin", "codex.exe")
        try:
            matches = sorted(glob.glob(pattern))
        except OSError:
            matches = []
        for match in matches:
            try:
                if os.path.isfile(match):
                    return match
            except OSError:
                continue
    return None


def build_ping_argv(exe, model, cwd):
    """固定参数。第 6 项是带双引号的一整段配置，不能拆开。"""
    return [
        exe, "exec", "-m", model, "-c", 'model_reasoning_effort="low"',
        "--ignore-user-config", "--ignore-rules", "-s", "read-only", "-C", cwd,
        "--skip-git-repo-check", "--json", CODEX_PING_PROMPT,
    ]


def cooldown_caption(read_caption):
    """冷却期里 Refresh now 的反馈文字，两个提供方共用这一处定义。

    “没有新东西可报”的读数文字（没有文字、no data、no file、same HH:MM，也就是 caption_text
    的输出里除 new HH:MM 与 read error 之外的全部）换成 cooldown，其余保留。
    """
    if read_caption in ("", "no data", CAPTION_NO_FILE) or read_caption.startswith("same"):
        return CAPTION_COOLDOWN
    return read_caption


def codex_ping_caption(read_caption, state):
    """Codex 块的反馈文字。冷却期里哪些读数文字换成 cooldown，见 cooldown_caption。"""
    if state == "asking":
        return CAPTION_ASKING
    if state == "failed":
        return CAPTION_PING_FAILED
    if state == "cooldown":
        return cooldown_caption(read_caption)
    return read_caption


def cooldown_remaining(clock, since, seconds):
    """从 since 起算的 seconds 秒冷却还剩几秒，已结束返回 0.0。两个驱动对象共用。

    since 为 None 表示没设过冷却，此时不读时钟。时钟回拨（现在早于 since）按已结束处理，
    否则冷却会被拉长。
    """
    if since is None:
        return 0.0
    now = clock()
    if now < since:
        return 0.0
    left = seconds - (now - since)
    return left if left > 0 else 0.0


class CodexPinger:
    """管理一次 Codex 刷新子进程。主线程只轮询，不阻塞、不读管道。

    冷却从成功启动那一刻算起：随后失败或超时的那次也占冷却，免得连点反复拉起。
    no_exe 与创建失败不算启动。超时先 terminate，仍不退出再 kill；两步都不等待。
    """

    def __init__(self, data_dir, model=CODEX_PING_MODEL, exe_override=None,
                 codex_home=None, popen=None, find_exe=None, clock=time.monotonic):
        self.data_dir = data_dir
        self.model = model
        self.exe_override = exe_override
        self.codex_home = codex_home
        self._popen = subprocess.Popen if popen is None else popen
        self._find_exe = _entry().find_codex_exe if find_exe is None else find_exe
        self._clock = clock
        self._proc = None
        self._started_at = None
        self._last_started_at = None
        self._terminated_at = None
        self._killed_at = None

    @property
    def running(self):
        """已启动且尚未回收。"""
        return self._proc is not None

    def cooldown_left(self):
        """距上次成功启动不足冷却时长时返回剩余秒数，否则 0.0。时钟回拨视为已结束。"""
        return cooldown_remaining(
            self._clock, self._last_started_at, CODEX_PING_COOLDOWN_SECONDS)

    def _clear_proc(self):
        """丢掉这次子进程的跟踪。上次启动时刻留给冷却，不清。"""
        self._proc = None
        self._started_at = None
        self._terminated_at = None
        self._killed_at = None

    def start(self):
        """尝试启动。返回原因字符串，绝不抛异常。"""
        if self.running:
            return "running"
        if self.cooldown_left() > 0:
            return "cooldown"
        exe = self._find_exe(self.exe_override)
        if exe is None:
            return "no_exe"
        work = os.path.join(self.data_dir, CODEX_PING_DIR_NAME)
        argv = build_ping_argv(exe, self.model, work)
        if self.codex_home is None:
            env = None
        else:
            env = dict(os.environ)
            env["CODEX_HOME"] = self.codex_home
        try:
            os.makedirs(work, exist_ok=True)
            self._proc = self._popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=work,
                env=env,
                close_fds=True,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, ValueError) as exc:
            self._proc = None
            return "spawn_error:%s" % type(exc).__name__
        started = self._clock()
        self._started_at = started
        self._last_started_at = started
        self._terminated_at = None
        self._killed_at = None
        return "started"

    def poll(self):
        """没有子进程或仍在跑时返回 None，结束时返回 (outcome, detail)。"""
        proc = self._proc
        if proc is None:
            return None
        try:
            code = proc.poll()
        except OSError:
            self._clear_proc()
            return ("failed", "poll_error")
        if code is None:
            now = self._clock()
            if (self._terminated_at is None
                    and self._started_at is not None
                    and now - self._started_at >= CODEX_PING_TIMEOUT_SECONDS):
                try:
                    proc.terminate()
                except OSError:
                    pass
                self._terminated_at = now
                return None
            if (self._terminated_at is not None and self._killed_at is None
                    and now - self._terminated_at >= CODEX_PING_KILL_WAIT_SECONDS):
                try:
                    proc.kill()
                except OSError:
                    pass
                self._killed_at = now
                return None
            if (self._killed_at is not None
                    and now - self._killed_at >= CODEX_PING_KILL_WAIT_SECONDS):
                self._clear_proc()
                return ("timeout", "unreaped")
            return None
        timed_out = self._terminated_at is not None
        self._clear_proc()
        if timed_out:
            return ("timeout", "exit:%d" % code)
        if code == 0:
            return ("ok", "exit:0")
        return ("failed", "exit:%d" % code)

    def stop(self):
        """有子进程就终止再杀，然后清掉跟踪。不等待，重复调用无效果。"""
        proc = self._proc
        if proc is None:
            return
        still = True
        try:
            still = proc.poll() is None
        except OSError:
            still = True
        if still:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
        self._clear_proc()


# ---------------------------------------------------------------------------
# App refresh：Refresh now 时请桌面会话里的 usage-feed 插件报一次账号级用量
# 小窗只往数据目录写一个很小的请求文件并读确认文件，联网的是插件所在的
# Claude Code 会话，不是本程序。
# ---------------------------------------------------------------------------

# 小窗写、插件读的请求文件名。
APP_REFRESH_REQUEST_NAME = "refresh-request.json"
# 插件写、小窗读的确认文件名。插件不是原子写，半截内容要当还没有确认。
APP_REFRESH_ACK_NAME = "refresh-ack.json"
# 从写出请求起算，最多等这么多秒。插件最迟 3 秒发现请求，一次调用最多 5 秒，再加几次写文件，端到端约 8 秒；这里多给约 1 秒余量。
APP_REFRESH_WAIT_SECONDS = 9.0
# 主线程轮询确认文件的间隔。本模块不设计时器，只给出毫秒数。
APP_REFRESH_POLL_MS = 500
# 收到 ok 或 unavailable 之后的冷却。这段时间里再点不再写请求。
APP_REFRESH_COOLDOWN_SECONDS = 60.0
# 等到超时之后的冷却，比成功回应短，方便再试一次。
APP_REFRESH_RETRY_SECONDS = 10.0
# 确认文件超过这个字节数就当还没有确认。恰好这么多字节仍然有效。
APP_ACK_MAX_BYTES = 4096
# 反馈文字。小窗上的字必须是纯 ASCII。asking 与 cooldown 复用 Codex ping 一节。
CAPTION_NO_SESSION = "no session"
CAPTION_NO_LIMITS = "no limits"
CAPTION_NOT_SENT = "not sent"


def read_app_ack(path, expected_id):
    """读确认文件，返回 "ok"、"unavailable" 或 None。

    缺失、读失败、超长、坏 JSON、schema 不是 1、id 对不上、status 不是这两个值，
    一律当还没有确认。插件不是原子写，半截内容下一轮再读。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(APP_ACK_MAX_BYTES + 1)
        if len(raw) > APP_ACK_MAX_BYTES:
            return None
        doc = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    if not _schema_is(doc, 1):
        return None
    if doc.get("id") != expected_id:
        return None
    status = doc.get("status")
    if status == "ok" or status == "unavailable":
        return status
    return None


def app_refresh_caption(read_caption, state):
    """Claude 块的反馈文字。冷却期里哪些读数文字换成 cooldown，见 cooldown_caption。"""
    if state == "asking":
        return CAPTION_ASKING
    if state == "cooldown":
        return cooldown_caption(read_caption)
    if state == "no_session":
        return CAPTION_NO_SESSION
    if state == "no_limits":
        return CAPTION_NO_LIMITS
    if state == "not_sent":
        return CAPTION_NOT_SENT
    return read_caption


class AppRefresher:
    """管理一次账号级用量刷新请求。主线程只轮询确认文件，不阻塞、不联网。

    冷却在 poll() 得出结果那一刻设定：回应用长冷却，超时用短冷却。
    冷却记的是设定那一刻的单调钟和时长，钟回退按已结束处理。
    """

    def __init__(self, data_dir, clock=time.time, monotonic=time.monotonic, token=None):
        self.data_dir = data_dir
        self._clock = clock
        self._monotonic = monotonic
        self._token = (lambda: os.urandom(6).hex()) if token is None else token
        self._pending_id = None
        self._started_at = None
        self._cooldown_at = None
        self._cooldown_for = 0.0

    @property
    def waiting(self):
        """已发出、尚未得到回应或超时的请求。"""
        return self._pending_id is not None

    def cooldown_left(self):
        """剩余冷却秒数；未设冷却或单调钟回退都返回 0.0。"""
        return cooldown_remaining(self._monotonic, self._cooldown_at, self._cooldown_for)

    def _begin_cooldown(self, seconds):
        self._cooldown_at = self._monotonic()
        self._cooldown_for = seconds

    def _clear_wait(self):
        """只清掉这次等待。冷却与请求文件都不动。"""
        self._pending_id = None
        self._started_at = None

    def start(self):
        """尝试写请求文件。返回原因字符串，绝不抛异常。"""
        if self.waiting:
            return "waiting"
        if self.cooldown_left() > 0:
            return "cooldown"
        if not self.data_dir or not os.path.isdir(self.data_dir):
            return "no_dir"
        path = os.path.join(self.data_dir, APP_REFRESH_REQUEST_NAME)
        temp = path + ".tmp"
        try:
            new_id = self._token()
            payload = json.dumps(
                {"schema": 1, "id": new_id, "requested_at": self._clock()},
                allow_nan=False) + "\n"
            with open(temp, "wb") as handle:
                handle.write(payload.encode("utf-8"))
            os.replace(temp, path)
        except (OSError, ValueError) as exc:
            try:
                os.remove(temp)
            except OSError:
                pass
            return "write_error:%s" % type(exc).__name__
        self._pending_id = new_id
        self._started_at = self._monotonic()
        return "started"

    def poll(self):
        """没有在等的请求返回 None，结束时返回 (outcome, detail)。detail 恒为空串。"""
        if not self.waiting:
            return None
        status = _entry().read_app_ack(
            os.path.join(self.data_dir, APP_REFRESH_ACK_NAME), self._pending_id)
        if status == "ok" or status == "unavailable":
            self._clear_wait()
            self._begin_cooldown(APP_REFRESH_COOLDOWN_SECONDS)
            return (status, "")
        now = self._monotonic()
        elapsed = 0.0
        if self._started_at is not None and now >= self._started_at:
            elapsed = now - self._started_at
        if elapsed >= APP_REFRESH_WAIT_SECONDS:
            self._clear_wait()
            self._begin_cooldown(APP_REFRESH_RETRY_SECONDS)
            return ("timeout", "")
        return None

    def stop(self):
        """清掉等待状态。不删请求文件，不改冷却，重复调用无效果。"""
        self._clear_wait()



# ---------------------------------------------------------------------------
# 文件：错误日志与位置文件（只写 data-dir 里的这两个文件）
# ---------------------------------------------------------------------------

class ErrorLog:
    """错误日志：data-dir 不存在就不写；超过 64 KB 时换成 widget_error.log.1（覆盖更旧的那份）后从空文件写起；
    同一种异常 10 分钟只记一次。换名失败时才退回直接截断。"""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, ERROR_LOG_NAME) if data_dir else None
        self._dir = data_dir
        self._seen = {}

    def log_exception(self, exc):
        return self.log(type(exc).__name__, str(exc))

    def log(self, kind, message):
        """返回是否真的写入。日志自身的 I/O 失败只能吞掉，没有地方再报。

        去重只针对真正写进去的记录：写失败（比如 data-dir 还没建好）不占去重名额，同样的消息下次照常再试。
        """
        if not self.path:
            return False
        message = " ".join(str(message).split())[:LOG_MAX_MESSAGE]
        key = (kind, message)
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < LOG_DEDUP_SECONDS:
            return False
        try:
            if not os.path.isdir(self._dir):
                return False
            line = "%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), kind, message)
            try:
                oversized = os.path.getsize(self.path) > LOG_MAX_BYTES
            except OSError:
                oversized = False
            mode = "ab"
            if oversized:
                # 最早的记录往往正好解释问题，所以先换到 .1 留着；换不成才截断。
                mode = "wb"
                try:
                    os.replace(self.path, self.path + ".1")
                    mode = "ab"
                except OSError:
                    pass
            with open(self.path, mode) as handle:
                handle.write(line.encode("utf-8"))
        except (OSError, ValueError):
            return False
        # 只有真的写进去了才记下这个键；写失败时去重表保持原样。
        if len(self._seen) >= LOG_MAX_KEYS:
            # 去重表有上限，内存不随时间增长
            self._seen = {k: t for k, t in self._seen.items() if now - t < LOG_DEDUP_SECONDS}
            if len(self._seen) >= LOG_MAX_KEYS:
                self._seen.clear()
        self._seen[key] = now
        return True


def load_position(path):
    """读位置文件，返回 (mode, offset)。

    读不到、坏了、schema 不是 2、mode 不认识、手动模式却没有可用的 offset，都当作
    自动模式并取缺省 offset；只有 mode 为 auto 的文件会保留它存着的 offset（上一次
    手动拖动的位置，下次再拖动前不起作用）。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
        doc = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return MODE_AUTO, DEFAULT_OFFSET
    if not isinstance(doc, dict):
        return MODE_AUTO, DEFAULT_OFFSET
    if not _schema_is(doc, POS_SCHEMA):
        return MODE_AUTO, DEFAULT_OFFSET
    value = _finite(doc.get("offset_from_right"))
    offset = DEFAULT_OFFSET
    if value is not None:
        offset = max(-OFFSET_LIMIT, min(OFFSET_LIMIT, int(math.floor(value + 0.5))))
    mode = doc.get("mode")
    if mode == MODE_MANUAL and value is not None:
        return MODE_MANUAL, offset
    if mode == MODE_AUTO:
        return MODE_AUTO, offset
    return MODE_AUTO, DEFAULT_OFFSET


def save_position(data_dir, mode, offset):
    """临时文件 + os.replace 原子写位置文件；data-dir 不存在时不写，返回是否写入。"""
    if not data_dir or not os.path.isdir(data_dir):
        return False
    path = os.path.join(data_dir, POS_FILE_NAME)
    temp = path + ".tmp"
    payload = json.dumps({"schema": POS_SCHEMA, "mode": mode, "offset_from_right": int(offset)}) + "\n"
    try:
        with open(temp, "wb") as handle:
            handle.write(payload.encode("utf-8"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return True


def load_skin(path):
    """读皮肤文件，返回皮肤名；读不到、坏了、schema 不认识、皮肤名不认识都退回默认皮肤。

    这里从不抛错：皮肤只是外观偏好，任何异常都不该挡住小窗启动。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
        doc = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return DEFAULT_SKIN
    if not isinstance(doc, dict):
        return DEFAULT_SKIN
    if not _schema_is(doc, SKIN_SCHEMA):
        return DEFAULT_SKIN
    skin = doc.get("skin")
    if skin in SKINS:
        return skin
    return DEFAULT_SKIN


def save_skin(data_dir, skin):
    """临时文件 + os.replace 原子写皮肤文件；data-dir 不存在或皮肤名不认识时不写，返回是否写入。"""
    if not data_dir or not os.path.isdir(data_dir):
        return False
    if skin not in SKINS:
        return False
    path = os.path.join(data_dir, SKIN_FILE_NAME)
    temp = path + ".tmp"
    payload = json.dumps({"schema": SKIN_SCHEMA, "skin": skin}) + "\n"
    try:
        with open(temp, "wb") as handle:
            handle.write(payload.encode("utf-8"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return True


def load_style(path):
    """读样式文件；缺失、损坏或不认识的 schema/样式均退回 Auto。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
        doc = json.loads(raw.decode("utf-8-sig"))
    except (OSError, TypeError, ValueError, RecursionError):
        return DEFAULT_STYLE
    if not isinstance(doc, dict) or not _schema_is(doc, STYLE_SCHEMA):
        return DEFAULT_STYLE
    style = doc.get("style")
    return style if style in STYLES else DEFAULT_STYLE


def save_style(data_dir, style):
    """原子写样式文件；目录不存在或样式无效时不写，I/O 异常交给调用方。"""
    if not data_dir or not os.path.isdir(data_dir):
        return False
    if style not in STYLES:
        return False
    path = os.path.join(data_dir, STYLE_FILE_NAME)
    temp = path + ".tmp"
    payload = json.dumps({"schema": STYLE_SCHEMA, "style": style}) + "\n"
    try:
        with open(temp, "wb") as handle:
            handle.write(payload.encode("utf-8"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return True


def read_taskbar_centered():
    """只读 TaskbarAl；1 为居中、0 为左对齐，其他情况返回 None。"""
    registry = getattr(_entry(), "winreg", winreg)
    if registry is None:
        return None
    key = None
    result = None
    try:
        key = registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            0,
            registry.KEY_READ,
        )
        value, _kind = registry.QueryValueEx(key, "TaskbarAl")
        if value == 1:
            result = True
        elif value == 0:
            result = False
    except Exception:
        result = None
    finally:
        if key is not None:
            try:
                registry.CloseKey(key)
            except Exception:
                result = None
    return result


def read_light_theme():
    """只读注册表的 SystemUsesLightTheme；读不到按浅色。"""
    winreg = _entry().winreg
    if winreg is None:
        # 没有注册表模块（非 Windows，离线测试）时同样按浅色。
        return True
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY_PATH, 0, winreg.KEY_READ)
        value, _kind = winreg.QueryValueEx(key, THEME_VALUE_NAME)
        return value != 0
    except OSError:
        return True
    finally:
        if key is not None:
            winreg.CloseKey(key)
