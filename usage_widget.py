# -*- coding: utf-8 -*-
"""Claude 用量小窗：贴在 Windows 任务栏通知区域左边的两行横向进度条，没有背景色。

上一行是 5 小时窗口，下一行是 7 天窗口，每行带百分比与重置时间。数据只来自
<data-dir>\\usage.json（由别的程序写入）。读数超过 30 分钟视为陈旧：进度条变成
灰色，并显示读数的年龄（单栏写在重置时间右边，双栏写在页眉里）。右键 Refresh now
之后，页眉或附加列（没有任何数据时是第二行）会短暂（约 2.5 秒）改写成刷新结果，
如 new 14:58、same 14:29、no data、read error。本程序自己不联网、不读任何凭证文件；
唯一例外是用户点 Refresh now 且 Codex 已启用时，会启动用户自己的 Codex 命令行
（原生 codex.exe，固定参数，不经 shell，隐藏窗口）发一个很小的请求，让 Codex
把最新额度写进它的会话日志，随后小窗照常只读这份日志；--no-codex-ping 可关闭。
用户点 Refresh now 时，小窗还会往数据目录写一个很小的请求文件 refresh-request.json，
并读 usage-feed 插件写的确认文件 refresh-ack.json，让装了插件的桌面会话报一次账号级
用量；本程序仍然不联网（联网的是插件所在的 Claude Code 会话，不是本程序）；
--no-app-refresh 可关闭。
窗口是无父窗口的顶层分层窗口（逐像素 alpha），以主任务栏为所有者，
随任务栏一起抬升层级。对资源管理器窗口只读查询；跨线程所有权会连接输入队列，主线程
不能阻塞。

运行：pythonw usage_widget.py [--data-dir DIR] [--no-codex] [--codex-home DIR]
        [--codex-bin FILE] [--no-codex-ping] [--codex-ping-model NAME]
        [--no-app-refresh]
测试用参数：--exit-after SEC、--selftest-render OUTDIR、--selftest-gdi N

代码分三层：
  1. 纯函数（快照读取与校验、显示状态、显示元组、布局、文字、渲染、预乘转换），可离线测试；
  2. Win32 薄封装（ctypes，只用白名单里的函数）；
  3. 窗口与周期任务（WidgetApp，全部在主线程，周期任务用 SetTimer 与 WM_TIMER）。
"""

import collections
import ctypes
import ctypes.wintypes as wintypes
import datetime
import functools
import glob
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

# winreg 只在 Windows 上有；其他平台（离线测试）置为 None，read_light_theme 按浅色处理。
try:
    import winreg
except ImportError:
    winreg = None

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SNAPSHOT_NAME = "usage.json"
ERROR_LOG_NAME = "widget_error.log"
POS_FILE_NAME = "widget_pos.json"
POS_SCHEMA = 2
MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MUTEX_NAME = "Local\\ClaudeUsageWidget"
WINDOW_CLASS = "ClaudeUsageWidgetWnd"
WINDOW_TITLE = "Claude usage"
ERROR_ALREADY_EXISTS = 183

MAX_SNAPSHOT_BYTES = 1024 * 1024
STALE_SECONDS = 1800
# 年龄文字的天数上限，更大的一律写 999d。畸形的 observed_at（例如 -1e300）经 _finite 检查后仍是
# 有限数，不封顶会得到几百位的天数，把小窗宽度撑到几千像素，右端锚定也随之失效。
MAX_AGE_DAYS = 999
SNAPSHOT_POLL_MS = 15000
WINDOW_CHECK_MS = 2000
OWNER_RECREATE_SECONDS = 30.0
THEME_POLL_MS = 60000
GDI_TICK_MS = 20
GDI_SETTLE_MS = 1000
WATCHDOG_SECONDS = 10.0
WATCHDOG_EXIT_CODE = 3
MAX_TIMER_MS = 0x7FFFFFFF

LOG_MAX_BYTES = 64 * 1024
LOG_MAX_MESSAGE = 500
LOG_DEDUP_SECONDS = 600.0
LOG_MAX_KEYS = 64

# 取不到托盘区矩形时的退路：右边缘 = 任务栏右边缘 - 这个逻辑像素数
DEFAULT_OFFSET = 330
OFFSET_LIMIT = 100000
DRAG_THRESHOLD_PX = 3
MOVE_THRESHOLD_PX = 1
MAX_REFRESH_PASSES = 3

# 逻辑尺寸（scale = 1.0，单位像素）
LOGICAL_H = 40
FONT_PX = 12
MARGIN_L = 2
LABEL_W = 18
GAP_LABEL_BAR = 4
BAR_W = 100
BAR_H = 7
BAR_R = 3
GAP_BAR_PCT = 6
PCT_W = 34
GAP_PCT_RESET = 6
RESET_W = 40
MARGIN_R = 2
BLOCK_GAP = 12
# 单栏在重置时间列右边再挂一列（年龄或反馈文字），与重置列隔开这么多逻辑像素。
ASIDE_GAP = 4
# 双栏：名称在进度条上方。行字号、槽高与页眉都比单栏略小，腾出页眉那一条。
DUAL_HEADER_H = 12
DUAL_HEADER_FONT_PX = 10
DUAL_FONT_PX = 11
DUAL_BAR_H = 6
PROVIDER_TAGS = ("Claude", "Codex")
TRAY_GAP = 2
MIN_HEIGHT_PX = 12
MIN_FONT_PX = 6
SUPERSAMPLE = 4
SCALE_MIN = 0.5
SCALE_MAX = 6.0
INITIAL_WINDOW_PX = 8

# 颜色与 alpha（直通 alpha 的 RGBA；提交前才转成预乘 alpha）
BACKGROUND_ALPHA = 1
TEXT_RGB = {"light": (0x1B, 0x1B, 0x1B), "dark": (0xF2, 0xF2, 0xF2)}
TRACK_RGBA = {"light": (0, 0, 0, 48), "dark": (255, 255, 255, 56)}
TIER_RGB = {"green": (0x2E, 0xA0, 0x43), "amber": (0xD2, 0x99, 0x22), "red": (0xDA, 0x36, 0x33)}
TEXT_ALPHA = 255
TEXT_ALPHA_STALE = 150
FILL_ALPHA = 255
# 陈旧行的进度条填充用固定中性灰，alpha 与正常行相同。透明任务栏上看不出变淡，
# 颜色被抽干才在任何壁纸上都站得住。
STALE_FILL_RGB = (0x8A, 0x8A, 0x8A)

NODATA_LINES = ("Claude usage", "no data yet")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Win32 常量
ABM_GETSTATE = 4
ABM_GETTASKBARPOS = 5
ABS_AUTOHIDE = 1
ABE_TOP = 1
ABE_BOTTOM = 3
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
GW_HWNDPREV = 3
GW_OWNER = 4
MAX_Z_ORDER_WINDOWS = 1024
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_REORDER = 0x8004
OBJID_WINDOW = 0
OBJID_CLIENT = -4
CHILDID_SELF = 0
WINEVENT_OUTOFCONTEXT = 0
WINEVENT_SKIPOWNPROCESS = 2
SWP_NOSIZE = 0x1
SWP_NOMOVE = 0x2
SWP_NOZORDER = 0x4
SWP_NOACTIVATE = 0x10
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
DIB_RGB_COLORS = 0
BI_RGB = 0
IDC_ARROW = 32512
MF_STRING = 0
MF_SEPARATOR = 0x800
TPM_RIGHTBUTTON = 0x2
TPM_NONOTIFY = 0x80
TPM_RETURNCMD = 0x100
DPI_CONTEXT_PER_MONITOR_V2 = -4
MONITOR_DEFAULTTONULL = 0
BUSY_NOTIFICATION_STATE = 2
HIDE_NOTIFICATION_STATES = frozenset({3, 4})
SUPPORTED_EDGES = frozenset({ABE_TOP, ABE_BOTTOM})
TOPMOST_SKIP_CLASSES = frozenset({
    "Windows.UI.Core.CoreWindow",
    "Xaml_WindowedPopupClass",
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
    "XamlExplorerHostIslandWindow",
    "#32768",
})
# 前台是这些窗口时用户点的是任务栏或桌面本身，不是全屏应用；它们的矩形恰好等于
# 任务栏或铺满显示器，只按矩形判断会把它们误当成全屏。
SHELL_SURFACE_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
})

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_SETTINGCHANGE = 0x001A
WM_MOUSEACTIVATE = 0x0021
WM_DISPLAYCHANGE = 0x007E
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CAPTURECHANGED = 0x0215
WM_DPICHANGED = 0x02E0
WM_APP_Z_ORDER = 0x8001
WM_APP_RECREATE = 0x8002
MA_NOACTIVATE = 3
MK_LBUTTON = 0x0001
SYSTEM_CHANGE_MESSAGES = frozenset({WM_DPICHANGED, WM_DISPLAYCHANGE, WM_SETTINGCHANGE})

MENU_REFRESH = 1
MENU_SNAP = 2
MENU_QUIT = 3
MENU_ITEMS = (
    (MENU_REFRESH, "Refresh now"),
    (MENU_SNAP, "Snap to tray"),
    (0, None),
    (MENU_QUIT, "Quit"),
)

TIMER_CHECK = 1
TIMER_POLL = 2
TIMER_THEME = 3
TIMER_EXIT = 4
TIMER_GDI_BEGIN = 5
TIMER_GDI_TICK = 6
TIMER_GDI_END = 7
TIMER_FLASH = 8
TIMER_CAPTION = 9
TIMER_PING = 10
TIMER_APP = 11
FLASH_MS = 300
FLASH_ALPHA_SCALE = 0.35
# Refresh now 之后反馈文字显示的毫秒数。
CAPTION_MS = 2500

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
    schema = doc.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, (int, float)) or schema != 1:
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
        snapshot, reason = read_snapshot(self.path)
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
    if (now is not None
            and current_at - now > CODEX_FUTURE_TOLERANCE_SECONDS):
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
    """只读文件尾部。返回 (snapshot 或 None, mtime)。调用方负责捕获 OSError。"""
    file_stat = os.stat(path)
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = size - CODEX_TAIL_BYTES if size > CODEX_TAIL_BYTES else 0
        handle.seek(start)
        raw = handle.read(CODEX_TAIL_BYTES)
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # 不是从文件头开始时，首行多半被截断，丢掉
    if start != 0 and lines:
        lines = lines[1:]
    for line in reversed(lines):
        if '"rate_limits"' not in line or '"token_count"' not in line:
            continue
        parsed = parse_codex_rate_limits(line)
        if parsed is not None:
            return _stamp_observed(parsed, file_stat.st_mtime), file_stat.st_mtime
    return None, file_stat.st_mtime


class CodexReader:
    """按日期目录找 rollout，只在最新文件签名变化时重读尾部。"""

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
            return changed

        now = self.clock()
        # 时钟回拨时上次扫描时间在未来，也要重新扫，否则候选列表会一直过期。
        if (force or self._candidates is None or self._last_scan is None
                or now < self._last_scan
                or now - self._last_scan >= CODEX_RESCAN_SECONDS):
            self._candidates = find_codex_rollouts(sessions)
            self._last_scan = now

        if not self._candidates:
            self.last_reason = "no_rate_limits"
            self._signature = None
            return False

        newest = self._candidates[0]
        signature = None
        try:
            newest_stat = os.stat(newest)
            signature = (newest, newest_stat.st_mtime_ns, newest_stat.st_size)
        except OSError:
            signature = None
        if not force and signature is not None and signature == self._signature:
            return False

        found = None
        found_path = None
        for path in self._candidates:
            try:
                parsed, _mtime = _read_codex_tail(path)
            except (OSError, ValueError):
                continue
            if parsed is None:
                continue
            found = parsed
            found_path = path
            break

        if signature is not None:
            self._signature = signature
        # 不拿更旧的观测时间盖掉已有快照。强制刷新时允许退回旧记录。
        # 当前快照的观测时间远在未来时，时钟校正后的新记录也要接受。
        if (found is not None and not force
                and _snapshot_is_older(found, self.snapshot, now)):
            self.last_reason = ""
            return False
        if found is None:
            self.last_reason = "no_rate_limits"
            return False
        changed = found != self.snapshot
        self.snapshot = found
        self.source_path = found_path
        self.last_reason = ""
        return changed


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


def codex_ping_caption(read_caption, state):
    """Codex 块的反馈文字。

    冷却期里“没有新东西可报”的读数文字才换成 cooldown，已经有新数据或读取失败的文字保留。
    """
    if state == "asking":
        return CAPTION_ASKING
    if state == "failed":
        return CAPTION_PING_FAILED
    if state == "cooldown":
        if (read_caption == "" or read_caption == "no data"
                or read_caption.startswith("same")):
            return CAPTION_COOLDOWN
        return read_caption
    return read_caption


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
        self._find_exe = find_codex_exe if find_exe is None else find_exe
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
        if self._last_started_at is None:
            return 0.0
        now = self._clock()
        if now < self._last_started_at:
            return 0.0
        left = CODEX_PING_COOLDOWN_SECONDS - (now - self._last_started_at)
        if left > 0:
            return left
        return 0.0

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
# 从写出请求起算，最多等这么多秒。
APP_REFRESH_WAIT_SECONDS = 8.0
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
    schema = doc.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, (int, float)) or schema != 1:
        return None
    if doc.get("id") != expected_id:
        return None
    status = doc.get("status")
    if status == "ok" or status == "unavailable":
        return status
    return None


def app_refresh_caption(read_caption, state):
    """Claude 块的反馈文字。

    冷却期里“没有新东西可报”的读数文字才换成 cooldown，已经有新数据或读取失败的文字保留。
    """
    if state == "asking":
        return CAPTION_ASKING
    if state == "cooldown":
        if (read_caption == "" or read_caption == "no data"
                or read_caption.startswith("same")):
            return CAPTION_COOLDOWN
        return read_caption
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
        if self._cooldown_at is None:
            return 0.0
        now = self._monotonic()
        if now < self._cooldown_at:
            return 0.0
        left = self._cooldown_for - (now - self._cooldown_at)
        if left > 0:
            return left
        return 0.0

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
        status = read_app_ack(
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
# 纯函数层：显示状态、文字、显示元组
# ---------------------------------------------------------------------------

RowView = collections.namedtuple(
    "RowView", ["label", "state", "fill", "tier", "pct_text", "reset_text"])
BlockView = collections.namedtuple("BlockView", ["tag", "rows"])
# ages、captions 按提供方各一条（下标 0 是 Claude，1 是 Codex）：陈旧读数的年龄文字，
# 以及点 Refresh now 之后的反馈文字。它们属于显示元组，元组相等才仍然意味着画面
# 相同；两条都是空串时存成 ()，所以没有这些文字的显示元组与原来的六字段写法相等。
# nodata 布局没有进度条行，ages 恒为 ()，captions 至多一项（下标 0），是顶替第二行的那条反馈文字。
Display = collections.namedtuple(
    "Display", ["kind", "rows", "theme", "scale", "width", "height", "ages", "captions"],
    defaults=((), ()))


def scaled(value, scale):
    """逻辑像素乘 scale 后四舍五入（半数向上）取整。"""
    return int(math.floor(value * scale + 0.5))


def row_state(win, now):
    """unknown / reset / normal / stale。"""
    if win is None:
        return "unknown"
    if now >= win.resets_at:
        return "reset"
    if now - win.observed_at > STALE_SECONDS:
        return "stale"
    return "normal"


def percent_int(pct):
    """百分比四舍五入为整数，半数向上。"""
    return int(math.floor(pct + 0.5))


def display_percent(pct):
    return max(0, min(999, percent_int(pct)))


def fill_percent(pct):
    return max(0, min(100, percent_int(pct)))


def color_tier(pct):
    """按浮点百分比分档：<50 绿，50 到 <80 琥珀，>=80 红。"""
    if pct < 50:
        return "green"
    if pct < 80:
        return "amber"
    return "red"


def reset_text(resets_at, seven_day):
    """5h 行写本地 %H:%M，7d 行写固定英文星期缩写（不用 %a，避免受区域设置影响）。"""
    try:
        local = time.localtime(resets_at)
    except (OverflowError, OSError, ValueError):
        # 极端的 resets_at 超出平台时间范围
        return "--"
    if seven_day:
        return WEEKDAYS[local.tm_wday]
    return "%02d:%02d" % (local.tm_hour, local.tm_min)


def build_row(label, win, now, seven_day):
    state = row_state(win, now)
    if state == "unknown":
        return RowView(label, state, 0, "none", "--", "")
    if state == "reset":
        return RowView(label, state, 0, "none", "--", "reset")
    return RowView(
        label, state, fill_percent(win.pct), color_tier(win.pct),
        "%d%%" % display_percent(win.pct), reset_text(win.resets_at, seven_day))


def format_age(seconds):
    """年龄文字：向下取整；不足 120 分钟写 "<分钟>m"（至少 1m），不足 48 小时写 "<小时>h"，
    否则写 "<天>d"。天数最多 MAX_AGE_DAYS（999），更大一律写 "999d"，所以文字宽度有界。"""
    # 整数不转 float：超大整数转 float 会溢出，所以天数按整数除法算，最后再封顶。
    # 负数、bool、非有限数一律按 0 处理。
    total = 0
    if isinstance(seconds, int) and not isinstance(seconds, bool):
        total = max(seconds, 0)
    elif isinstance(seconds, float) and math.isfinite(seconds):
        total = max(int(math.floor(seconds)), 0)
    if total < 7200:
        return "%dm" % max(1, total // 60)
    if total < 172800:
        return "%dh" % (total // 3600)
    return "%dd" % min(total // 86400, MAX_AGE_DAYS)


def stale_age_seconds(five, seven, now):
    """提供方陈旧时返回最旧的陈旧读数的年龄（秒），否则 None。"""
    oldest = None
    for win in (five, seven):
        if row_state(win, now) != "stale":
            continue
        if oldest is None or win.observed_at < oldest:
            oldest = win.observed_at
    if oldest is None:
        return None
    return now - oldest


def _clock_text(unix_seconds):
    """本地时间 HH:MM（两位补零）；超出平台时间范围时返回 "--:--"。"""
    try:
        local = time.localtime(unix_seconds)
    except (OverflowError, OSError, ValueError):
        return "--:--"
    return "%02d:%02d" % (local.tm_hour, local.tm_min)


# 数据文件不存在时的反馈文字（只有 Claude 块的 usage.json 会报 missing）。
CAPTION_NO_FILE = "no file"


def caption_text(before, after, reason):
    """点 Refresh now 之后某个提供方的反馈文字，纯 ASCII。

    文件已经不在时一律是 no file，哪怕小窗还留着上一次的读数，也不能写成 same HH:MM。
    """
    if reason and reason not in CAPTION_BENIGN_REASONS:
        return CAPTION_READ_ERROR
    if reason == "missing":
        return CAPTION_NO_FILE
    if after is None:
        return "no data"
    if before is None or after > before:
        return "new " + _clock_text(after)
    return "same " + _clock_text(after)


# ---------------------------------------------------------------------------
# 纯函数层：布局（右端锚定）
# ---------------------------------------------------------------------------

def logical_columns():
    """按逻辑像素排出各列的 (左, 右)，返回 (字典, 总宽)。"""
    columns = {}
    x = MARGIN_L
    for name, width, gap_after in (
        ("label", LABEL_W, GAP_LABEL_BAR),
        ("bar", BAR_W, GAP_BAR_PCT),
        ("pct", PCT_W, GAP_PCT_RESET),
        ("reset", RESET_W, 0),
    ):
        columns[name] = (x, x + width)
        x += width + gap_after
    return columns, x + MARGIN_R


def pixel_columns(scale):
    """各列的像素 (左, 右)。边界各自取整，所以相邻列之间不会因累计取整而错位。"""
    columns, _total = logical_columns()
    return {name: (scaled(lo, scale), scaled(hi, scale)) for name, (lo, hi) in columns.items()}


def aside_left(scale):
    """单栏附加列文字的左边缘（像素）：重置列右边缘再右移 ASIDE_GAP 个逻辑像素。"""
    return pixel_columns(scale)["reset"][1] + scaled(ASIDE_GAP, scale)


def dual_block_columns():
    """一个提供方块内各列相对块起点的逻辑像素 (左, 右)，以及块宽。

    列与单栏相同，名称画在进度条上方，不再占左侧一列。
    """
    columns = {}
    x = 0
    for name, width, gap_after in (
        ("label", LABEL_W, GAP_LABEL_BAR),
        ("bar", BAR_W, GAP_BAR_PCT),
        ("pct", PCT_W, GAP_PCT_RESET),
        ("reset", RESET_W, 0),
    ):
        columns[name] = (x, x + width)
        x += width + gap_after
    return columns, x


def dual_logical_width():
    """双栏总宽（逻辑像素）：左边距 + 块 + 块间隙 + 块 + 右边距。"""
    _columns, block_width = dual_block_columns()
    return MARGIN_L + block_width + BLOCK_GAP + block_width + MARGIN_R


def dual_pixel_columns(scale):
    """两个块各自的像素列 (左, 右)。每条边界单独取整。"""
    columns, block_width = dual_block_columns()
    blocks = []
    origin = MARGIN_L
    for _index in range(len(PROVIDER_TAGS)):
        blocks.append({
            name: (scaled(origin + lo, scale), scaled(origin + hi, scale))
            for name, (lo, hi) in columns.items()
        })
        origin += block_width + BLOCK_GAP
    return blocks


def window_height(taskbar_rect, scale):
    """小窗高度：标称 40 逻辑像素，任务栏放不下时取 任务栏高度 - 4。"""
    _left, top, _right, bottom = taskbar_rect
    return max(MIN_HEIGHT_PX, min(scaled(LOGICAL_H, scale), (bottom - top) - 4))


def render_metrics(scale, height):
    """窗口高度对应的字号、进度槽高度与圆角（像素）。

    任务栏太矮时窗口高度小于标称高度，字号、行高、进度槽按比例缩小；宽度由列布局
    决定，不随之变化。
    """
    nominal_height = max(1, scaled(LOGICAL_H, scale))
    vscale = min(1.0, height / float(nominal_height))
    return {
        "vscale": vscale,
        "font_px": max(MIN_FONT_PX, scaled(FONT_PX * vscale, scale)),
        "bar_height": max(2, scaled(BAR_H * vscale, scale)),
        "bar_radius": max(1, scaled(BAR_R * vscale, scale)),
    }


def dual_render_metrics(scale, height):
    """双栏的页眉高度、行高、字号与进度槽。单栏的 render_metrics 不走这里。"""
    nominal_height = max(1, scaled(LOGICAL_H, scale))
    vscale = min(1.0, height / float(nominal_height))
    header_height = scaled(DUAL_HEADER_H * vscale, scale)
    return {
        "vscale": vscale,
        "header_height": header_height,
        "row_height": (height - header_height) / 2.0,
        "font_px": max(MIN_FONT_PX, scaled(DUAL_FONT_PX * vscale, scale)),
        "header_font_px": max(MIN_FONT_PX, scaled(DUAL_HEADER_FONT_PX * vscale, scale)),
        "bar_height": max(2, scaled(DUAL_BAR_H * vscale, scale)),
        "bar_radius": max(1, scaled(BAR_R * vscale, scale)),
    }


def display_width(kind, scale, height, aside=""):
    """小窗宽度（像素）：数据行按列布局算，双栏按两块加间隙算，无数据时按两行文字的实际宽度算。

    单栏有附加文字时，宽度收到 aside_left 再加文字宽度和右边距；没有附加文字时与原来相同。
    nodata 的 aside 是顶替第二行的反馈文字，非空时一并计入宽度，避免窗口被这条文字撑出边界。
    """
    if kind == "nodata":
        font = get_font(render_metrics(scale, height)["font_px"])
        lines = NODATA_LINES + ((aside,) if aside else ())
        text_width = int(math.ceil(max(font.getlength(line) for line in lines)))
        return scaled(MARGIN_L, scale) + text_width + scaled(MARGIN_R, scale)
    if kind == "dual":
        return scaled(dual_logical_width(), scale)
    if aside:
        font = get_font(render_metrics(scale, height)["font_px"])
        return aside_left(scale) + int(math.ceil(font.getlength(aside))) + scaled(MARGIN_R, scale)
    return scaled(logical_columns()[1], scale)


def _window_pair(snapshot):
    """快照里的 (5h, 7d)；没有快照或窗口缺失时是 None。"""
    five = snapshot.get("five_hour") if snapshot else None
    seven = snapshot.get("seven_day") if snapshot else None
    return five, seven


def _metric_rows(five, seven, now):
    return (build_row("5h", five, now, False), build_row("7d", seven, now, True))


def _age_text(five, seven, now):
    """该提供方陈旧时的年龄文字；不陈旧时是空串。"""
    seconds = stale_age_seconds(five, seven, now)
    if seconds is None:
        return ""
    return format_age(seconds)


def _stored_strings(values):
    """全是空串时存成 ()，这样没有附加文字的显示元组在新字段上仍是默认值。"""
    if any(values):
        return tuple(values)
    return ()


def _caption_at(captions, index):
    """调用方给的反馈文字；缺项或 captions 为空时是空串。"""
    if not captions or index >= len(captions) or captions[index] is None:
        return ""
    return captions[index]


def _nodata_caption(texts):
    """无数据布局第二行的反馈文字。

    无数据布局只有第二行能写反馈文字，双栏两侧都没有窗口时 read error 优先；
    否则取第一条非空文字；都没有则返回空串。
    """
    for text in texts:
        if text == CAPTION_READ_ERROR:
            return CAPTION_READ_ERROR
    for text in texts:
        if text:
            return text
    return ""


def _nodata_display(theme, scale, height, caption):
    """无数据布局：没有进度条行，ages 恒为 ()，反馈文字至多一项。"""
    return Display(
        "nodata", NODATA_LINES, theme, scale,
        display_width("nodata", scale, height, caption), height,
        (), _stored_strings((caption,)))


def build_display(snapshot, now, theme, scale, height, codex=None, codex_available=False, captions=None):
    """把快照与当前时间整理成不可变的显示元组；元组相等就不用重绘。

    填充长度由取整后的百分比决定，这样元组相等一定意味着画面相同。宽度由内容、
    scale 与高度推出，放进元组是为了让尺寸变化也触发重绘。
    codex_available 为假时只画 Claude，结果与原来相同。为真时左 Claude、右 Codex；
    两边都没有任何窗口时仍是无数据。
    ages、captions 按提供方各一条（下标 0 是 Claude，1 是 Codex）；全空时存成 ()。
    有反馈文字时优先显示它，否则才显示年龄文字。nodata 没有进度条行可挂年龄文字，
    只保留反馈文字（单栏取 Claude 的；双栏两侧都没有窗口时 read error 优先，否则取
    第一条非空的），存成只有一项的 captions，渲染时顶替第二行 "no data yet"。
    """
    five, seven = _window_pair(snapshot)
    if not codex_available:
        if five is None and seven is None:
            return _nodata_display(theme, scale, height, _caption_at(captions, 0))
        age = _age_text(five, seven, now)
        caption = _caption_at(captions, 0)
        aside = caption or age
        rows = _metric_rows(five, seven, now)
        width = display_width("data", scale, height, aside)
        return Display(
            "data", rows, theme, scale, width, height,
            _stored_strings((age,)), _stored_strings((caption,)))
    codex_five, codex_seven = _window_pair(codex)
    if five is None and seven is None and codex_five is None and codex_seven is None:
        return _nodata_display(
            theme, scale, height,
            _nodata_caption((_caption_at(captions, 0), _caption_at(captions, 1))))
    blocks = (
        BlockView(PROVIDER_TAGS[0], _metric_rows(five, seven, now)),
        BlockView(PROVIDER_TAGS[1], _metric_rows(codex_five, codex_seven, now)),
    )
    ages = (
        _age_text(five, seven, now),
        _age_text(codex_five, codex_seven, now),
    )
    caption_pair = (_caption_at(captions, 0), _caption_at(captions, 1))
    width = display_width("dual", scale, height)
    return Display(
        "dual", blocks, theme, scale, width, height,
        _stored_strings(ages), _stored_strings(caption_pair))


def rect_inside(inner, outer):
    """inner 非空且完全落在 outer 之内。"""
    return (inner[0] < inner[2] and inner[1] < inner[3]
            and outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def rects_overlap(first, second):
    """屏幕矩形是否有面积交集；边缘接触不算遮挡。"""
    return (max(first[0], second[0]) < min(first[2], second[2])
            and max(first[1], second[1]) < min(first[3], second[3]))


def right_edge_x(taskbar_rect, scale, mode, offset, tray_rect):
    """小窗右边缘的 x。

    手动模式：任务栏右边缘 - offset * scale。自动模式：托盘区左边缘 - 2 * scale；
    托盘区矩形缺失或不在任务栏之内时退回 任务栏右边缘 - 330 * scale。
    """
    right = taskbar_rect[2]
    if mode == MODE_MANUAL:
        return right - scaled(offset, scale)
    if tray_rect is not None and rect_inside(tray_rect, taskbar_rect):
        return tray_rect[0] - scaled(TRAY_GAP, scale)
    return right - scaled(DEFAULT_OFFSET, scale)


def compute_layout(taskbar_rect, edge, scale, width, mode, offset, tray_rect=None):
    """返回小窗 (x, y, w, h)；任务栏不在底部或顶部时返回 None（调用方隐藏小窗）。

    taskbar_rect = (left, top, right, bottom)，offset 是逻辑像素。右边缘是锚点，
    宽度变化只向左展开；最后把 x 夹在任务栏矩形之内。
    """
    if edge not in SUPPORTED_EDGES:
        return None
    left, top, right, bottom = taskbar_rect
    height = window_height(taskbar_rect, scale)
    x = right_edge_x(taskbar_rect, scale, mode, offset, tray_rect) - width
    x = max(left, min(x, right - width))
    y = top + ((bottom - top) - height) // 2
    return x, y, width, height


def offset_from_right_edge(x, width, taskbar_right, scale):
    """小窗左边缘 x 换算回逻辑像素的 offset（整数）：(任务栏右边缘 - 小窗右边缘) / scale。"""
    return int(math.floor((taskbar_right - (x + width)) / scale + 0.5))


# ---------------------------------------------------------------------------
# 纯函数层：渲染（PIL RGBA 图，无窗口）与预乘转换
# ---------------------------------------------------------------------------

_font_state = {"fallback": False}
_ALPHA_FLOOR = [max(value, BACKGROUND_ALPHA) for value in range(256)]


@functools.lru_cache(maxsize=32)
def get_font(px):
    """Segoe UI，像素高度 px；加载失败才退回 Pillow 自带字体。"""
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", "segoeui.ttf")
    try:
        return ImageFont.truetype(path, px)
    except OSError:
        _font_state["fallback"] = True
        return ImageFont.load_default(px)


@functools.lru_cache(maxsize=512)
def _pill_mask(width, height, radius):
    """圆角矩形遮罩：先放大 SUPERSAMPLE 倍再用 BOX 缩回，边缘才平滑。结果只读。"""
    big = Image.new("L", (width * SUPERSAMPLE, height * SUPERSAMPLE), 0)
    corner = min(radius, width / 2.0, height / 2.0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, width * SUPERSAMPLE - 1, height * SUPERSAMPLE - 1),
        radius=int(math.floor(corner * SUPERSAMPLE + 0.5)), fill=255)
    return big.resize((width, height), Image.Resampling.BOX)


@functools.lru_cache(maxsize=256)
def _compose_bar(width, height, radius, fill_width, track_rgba, fill_rgba):
    """进度条（空槽加填充）的直通 alpha RGBA 图，像素在整数域里精确合成。

    填充覆盖的地方是"替换"空槽而不是叠在空槽上：叠加会让 alpha 120 的填充变成
    145，并被空槽的黑色染暗。逐像素公式（f、t 为填充与空槽的覆盖度 0..255，
    Af、At 为两者的 alpha）：
        权重 wf = f * Af * 255，wt = (255 - f) * t * At
        颜色 = (Cf * wf + Ct * wt) / (wf + wt)，alpha = (wf + wt) / 255²
    内部像素（f = 255）恰好得到填充色与 Af，空槽内部（f = 0，t = 255）恰好得到
    空槽色与 At；圆角边缘按覆盖度在预乘空间里过渡，不会出现染暗的毛边。

    逐像素的 Python 循环，一条约 1 毫秒；入参的取值范围很小（尺寸随 scale 变化，填充宽度
    最多 bar 宽 + 1 种，颜色只有几种），所以结果按入参缓存，同一个画面里的各条进度条和
    相邻两帧都直接复用。缓存返回的是同一个图对象：调用方只能读它（paste 不改源图），不得
    原地修改。
    """
    track_mask = _pill_mask(width, height, radius).tobytes()
    if fill_rgba is not None and fill_width > 0:
        fill_width = min(fill_width, width)
        fill_small = _pill_mask(fill_width, height, radius)
        fill_full = Image.new("L", (width, height), 0)
        fill_full.paste(fill_small, (0, 0))
        fill_mask = fill_full.tobytes()
        fr, fg, fb, f_alpha = fill_rgba
    else:
        fill_mask = bytes(width * height)
        fr = fg = fb = f_alpha = 0
    tr, tg, tb, t_alpha = track_rgba
    out = bytearray(width * height * 4)
    for index in range(width * height):
        t = track_mask[index]
        f = fill_mask[index]
        if t == 0 and f == 0:
            continue
        weight_fill = f * f_alpha * 255
        weight_track = (255 - f) * t * t_alpha
        total = weight_fill + weight_track
        if total == 0:
            continue
        half = total // 2
        base = index * 4
        out[base] = (fr * weight_fill + tr * weight_track + half) // total
        out[base + 1] = (fg * weight_fill + tg * weight_track + half) // total
        out[base + 2] = (fb * weight_fill + tb * weight_track + half) // total
        out[base + 3] = (total + 32512) // 65025
    return Image.frombytes("RGBA", (width, height), bytes(out))


def _draw_text(draw, font, x, center_y, text, ink, align):
    """按数字字形高度垂直居中（以基线定位），align 为 "l"、"r" 或 "c"。"""
    digit_top = font.getbbox("0", anchor="ls")[1]
    baseline = int(math.floor(center_y - digit_top / 2.0 + 0.5))
    if align == "l":
        anchor = "ls"
    elif align == "c":
        anchor = "ms"
    else:
        anchor = "rs"
    draw.text((x, baseline), text, font=font, fill=ink, anchor=anchor)


def _provider_item(items, index):
    """按提供方下标取字符串；元组比下标短时按空串处理。"""
    if index < len(items):
        return items[index]
    return ""


def single_aside(display):
    """单栏附加列的 (文字, alpha)。

    反馈文字优先，alpha 用 TEXT_ALPHA；否则是年龄文字，alpha 用 TEXT_ALPHA_STALE；
    都没有时文字为空串。
    """
    caption = _provider_item(display.captions, 0)
    if caption:
        return caption, TEXT_ALPHA
    age = _provider_item(display.ages, 0)
    if age:
        return age, TEXT_ALPHA_STALE
    return "", TEXT_ALPHA


def nodata_lines(display):
    """无数据布局实际画出的两行。

    有反馈文字时顶替第二行 "no data yet"；没有时原样返回 display.rows，画面与改动前相同。
    """
    caption = _provider_item(display.captions, 0)
    if caption:
        return (display.rows[0], caption)
    return display.rows


def dual_header(display, index):
    """双栏第 index 个块的页眉 (文字, alpha)。

    有反馈文字：文字是它，alpha 用 TEXT_ALPHA（替换原来的名称）。
    否则文字是名称 block.tag；有年龄文字时写成 "<tag> <年龄>"，例如 "Claude 41m"。
    alpha 沿用现有规则：该块有任何 state == "normal" 的行用 TEXT_ALPHA，否则用 TEXT_ALPHA_STALE。
    """
    block = display.rows[index]
    caption = _provider_item(display.captions, index)
    if caption:
        text = caption
        alpha = TEXT_ALPHA
    else:
        age = _provider_item(display.ages, index)
        text = ("%s %s" % (block.tag, age)) if age else block.tag
        alpha = TEXT_ALPHA if any(row.state == "normal" for row in block.rows) else TEXT_ALPHA_STALE
    return text, alpha


def _draw_rows(canvas, text_draw, font, rows, edges, metrics, text_rgb, track_rgba, row_height, origin=0.0):
    """画一个块里的两行：标签、进度条、百分比、重置时间。

    origin 为 0 时行中心与原来的单栏相同。双栏传入页眉高度，两行排在页眉下面。
    陈旧行的填充是中性灰、alpha 与正常行相同；文字仍用 TEXT_ALPHA_STALE 变淡。
    """
    bar_left, bar_right = edges["bar"]
    bar_width = bar_right - bar_left
    bar_height = metrics["bar_height"]
    bar_radius = metrics["bar_radius"]
    for index, row in enumerate(rows):
        center = origin + (index + 0.5) * row_height
        stale = row.state == "stale"
        ink = text_rgb + (TEXT_ALPHA_STALE if stale else TEXT_ALPHA,)
        _draw_text(text_draw, font, edges["label"][0], center, row.label, ink, "l")
        fill_rgba = None
        fill_width = 0
        if row.state in ("normal", "stale"):
            fill_width = int(math.floor(row.fill * bar_width / 100.0 + 0.5))
            if stale:
                fill_rgba = STALE_FILL_RGB + (FILL_ALPHA,)
            else:
                fill_rgba = TIER_RGB[row.tier] + (FILL_ALPHA,)
        top = int(math.floor(center - bar_height / 2.0 + 0.5))
        canvas.paste(
            _compose_bar(bar_width, bar_height, bar_radius, fill_width, track_rgba, fill_rgba),
            (bar_left, top))
        _draw_text(text_draw, font, edges["pct"][1], center, row.pct_text, ink, "r")
        if row.reset_text:
            _draw_text(text_draw, font, edges["reset"][0], center, row.reset_text, ink, "l")


def render_display(display):
    """把显示元组画成直通 alpha 的 RGBA 图（纯函数：同输入同输出）。

    没有任何可见背景：先在全透明画布上画进度条和文字，最后把所有 alpha 为 0 的像素
    抬到 alpha = 1（颜色 0），肉眼不可见，但整个小窗矩形都能接收鼠标，拖动和右键才
    不会落空。画法上的两个约束：
      - 文字画在一张"预先填好文字颜色、alpha 为 0"的图层上，抗锯齿边缘只改变 alpha，
        颜色保持不变；直接画在透明底上会把颜色朝黑色混合，预乘后边缘就偏暗偏细。
      - 进度条由 _compose_bar 精确合成，保证内部像素恰为规定的颜色与 alpha。
    元组相等画面就相同，所以 WidgetApp 会把最近一次的结果按显示元组缓存起来复用；
    返回的图因此不得被原地修改，要变暗请用 dim_image 另取新图。
    """
    theme = display.theme
    text_rgb = TEXT_RGB[theme]
    track_rgba = TRACK_RGBA[theme]
    scale = display.scale
    width, height = display.width, display.height
    metrics = render_metrics(scale, height)
    font = get_font(metrics["font_px"])
    row_height = height / 2.0
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    text_layer = Image.new("RGBA", (width, height), text_rgb + (0,))
    text_draw = ImageDraw.Draw(text_layer)

    if display.kind == "nodata":
        left = scaled(MARGIN_L, scale)
        for index, line in enumerate(nodata_lines(display)):
            _draw_text(text_draw, font, left, (index + 0.5) * row_height, line,
                       text_rgb + (TEXT_ALPHA,), "l")
    elif display.kind == "dual":
        dual = dual_render_metrics(scale, height)
        row_font = get_font(dual["font_px"])
        header_font = get_font(dual["header_font_px"])
        header_height = dual["header_height"]
        for index, (block, edges) in enumerate(zip(display.rows, dual_pixel_columns(scale))):
            # 名称水平落在该块进度条列的正中，垂直落在页眉带的正中。
            bar_left, bar_right = edges["bar"]
            header_text, tag_alpha = dual_header(display, index)
            _draw_text(text_draw, header_font, (bar_left + bar_right) / 2.0, header_height / 2.0,
                       header_text, text_rgb + (tag_alpha,), "c")
            _draw_rows(canvas, text_draw, row_font, block.rows, edges, dual, text_rgb, track_rgba,
                       dual["row_height"], header_height)
    else:
        edges = pixel_columns(scale)
        _draw_rows(canvas, text_draw, font, display.rows, edges, metrics, text_rgb, track_rgba, row_height)
        aside_text, aside_alpha = single_aside(display)
        if aside_text:
            _draw_text(text_draw, font, aside_left(scale), 0.5 * row_height, aside_text,
                       text_rgb + (aside_alpha,), "l")
    canvas.alpha_composite(text_layer)
    red, green, blue, alpha = canvas.split()
    return Image.merge("RGBA", (red, green, blue, alpha.point(_ALPHA_FLOOR)))


def dim_image(image, scale):
    """返回一张新的 RGBA 图：alpha 乘 scale 后向下取整，且不低于 BACKGROUND_ALPHA。

    RGB 不变。整窗 alpha 不能降到 0，否则点不中小窗。传入的图不改。
    """
    if image.mode != "RGBA":
        raise ValueError("expected an RGBA image, got %s" % image.mode)
    red, green, blue, alpha = image.split()
    floor = [max(int(math.floor(value * scale)), BACKGROUND_ALPHA) for value in range(256)]
    return Image.merge("RGBA", (red, green, blue, alpha.point(floor)))


def premultiply_bgra(image):
    """RGBA 图转成预乘 alpha 的 BGRA 字节串（每个像素 B、G、R 乘 a/255），供 UpdateLayeredWindow 提交。

    交给 Pillow 的原生转换：先对调红、蓝两个通道，再转成预乘的 RGBa 模式，取出的字节顺序
    就是 B、G、R、A。以前是逐像素的 Python 生成器，高 DPI 下每帧 3 到 12 毫秒，现在不到
    1 毫秒。Pillow 把 c * a / 255 四舍五入，原来的实现向下取整，所以每个字节最多差 1。
    UpdateLayeredWindow 要求的不变式不受影响：每个像素的 B、G、R 都不超过 alpha（c 不大于
    255，c * a / 255 取整后不会大于 a）。传入的图不改。
    """
    if image.mode != "RGBA":
        raise ValueError("expected an RGBA image, got %s" % image.mode)
    red, green, blue, alpha = image.split()
    return Image.merge("RGBA", (blue, green, red, alpha)).convert("RGBa").tobytes()


# ---------------------------------------------------------------------------
# 样张（--selftest-render）
# ---------------------------------------------------------------------------

SAMPLE_SCALES = (1.0, 1.25, 1.5)
SAMPLE_COMBOS = tuple((theme, scale) for theme in ("light", "dark") for scale in SAMPLE_SCALES)
DUAL_SAMPLE_STATES = (
    "dual_green", "dual_mixed", "dual_codex_reset", "dual_claude_none",
)
SAMPLE_STATES = (
    "nodata", "green", "amber", "red", "full", "stale", "reset",
) + DUAL_SAMPLE_STATES
# 另放一份：SAMPLE_STATES 的尾部被测试钉住，原有样张的文件名序号不能变。
EXTRA_SAMPLE_STATES = (
    "stale_hours", "stale_one_row",
    "dual_stale_both", "dual_stale_claude",
    "caption_new", "caption_same", "caption_error",
    "dual_caption_new_same", "dual_caption_nodata_error",
    "caption_nodata", "caption_nodata_error", "dual_caption_all_nodata",
    "dual_caption_asking", "dual_caption_cooldown", "dual_caption_ping_failed",
    "caption_asking", "caption_no_session", "dual_caption_app_no_limits",
)
EXTRA_DUAL_SAMPLE_STATES = (
    "dual_stale_both", "dual_stale_claude",
    "dual_caption_new_same", "dual_caption_nodata_error",
    "dual_caption_all_nodata",
    "dual_caption_asking", "dual_caption_cooldown", "dual_caption_ping_failed",
    "dual_caption_app_no_limits",
)
ALL_SAMPLE_STATES = SAMPLE_STATES + EXTRA_SAMPLE_STATES
SHEET_BACKGROUNDS = (
    ("light grey #F3F3F3", (0xF3, 0xF3, 0xF3)),
    ("dark grey #202020", (0x20, 0x20, 0x20)),
    ("wallpaper #D9B8A8", (0xD9, 0xB8, 0xA8)),
)


def _sample_snapshot(name, now):
    resets_5h = time.mktime((2026, 10, 2, 16, 50, 0, 0, 0, -1))
    resets_7d = time.mktime((2026, 10, 4, 12, 0, 0, 0, 0, -1))
    fresh = now - 60.0

    def win(pct, resets=resets_5h, observed=fresh):
        return Win(float(pct), resets, observed)

    if name in ("nodata", "caption_nodata", "caption_nodata_error", "dual_caption_all_nodata"):
        return None
    if name in ("green", "caption_asking", "caption_no_session"):
        return {"five_hour": win(30), "seven_day": win(10, resets_7d)}
    if name == "amber":
        return {"five_hour": win(65), "seven_day": win(33, resets_7d)}
    if name == "red":
        return {"five_hour": win(92), "seven_day": win(70, resets_7d)}
    if name == "full":
        return {"five_hour": win(100), "seven_day": win(100, resets_7d)}
    if name == "stale":
        old = now - 31 * 60.0
        return {"five_hour": win(65, observed=old), "seven_day": win(33, resets_7d, old)}
    if name == "reset":
        return {"five_hour": win(80, resets=now - 10.0, observed=now - 70.0),
                "seven_day": win(55, resets_7d)}
    if name in ("dual_green", "dual_codex_reset",
                "dual_caption_asking", "dual_caption_cooldown",
                "dual_caption_ping_failed", "dual_caption_app_no_limits"):
        # 左侧 Claude：与 green 样张相同
        return {"five_hour": win(30), "seven_day": win(10, resets_7d)}
    if name == "dual_mixed":
        # 左侧 Claude：与 amber 样张相同，观测时间仍是新鲜的
        return {"five_hour": win(65), "seven_day": win(33, resets_7d)}
    if name == "dual_claude_none":
        return None
    if name == "stale_hours":
        old = now - 3 * 3600.0
        return {"five_hour": win(65, observed=old), "seven_day": win(33, resets_7d, old)}
    if name == "stale_one_row":
        # 一行陈旧一行正常
        return {"five_hour": win(65, observed=now - 41 * 60.0), "seven_day": win(33, resets_7d)}
    if name in ("dual_stale_both", "dual_stale_claude"):
        old = now - 41 * 60.0
        return {"five_hour": win(65, observed=old), "seven_day": win(33, resets_7d, old)}
    if name == "caption_new":
        return {"five_hour": win(30), "seven_day": win(10, resets_7d)}
    if name == "caption_same":
        old = now - 31 * 60.0
        return {"five_hour": win(65, observed=old), "seven_day": win(33, resets_7d, old)}
    if name == "caption_error":
        return {"five_hour": win(65), "seven_day": win(33, resets_7d)}
    if name == "dual_caption_new_same":
        return {"five_hour": win(30), "seven_day": win(10, resets_7d)}
    if name == "dual_caption_nodata_error":
        return None
    raise ValueError("unknown sample state: %s" % name)


def _sample_codex_snapshot(name, now):
    """双栏样张的 Codex 侧。时间基准与 _sample_snapshot 相同。"""
    resets_5h = time.mktime((2026, 10, 2, 16, 50, 0, 0, 0, -1))
    resets_7d = time.mktime((2026, 10, 4, 12, 0, 0, 0, 0, -1))
    fresh = now - 60.0

    def win(pct, resets=resets_5h, observed=fresh):
        return Win(float(pct), resets, observed)

    if name in ("dual_green", "dual_caption_asking", "dual_caption_cooldown",
                "dual_caption_ping_failed", "dual_caption_app_no_limits"):
        return {"five_hour": win(3), "seven_day": win(36, resets_7d)}
    if name == "dual_mixed":
        # 右侧 Codex：与 stale 样张相同
        old = now - 31 * 60.0
        return {"five_hour": win(65, observed=old), "seven_day": win(33, resets_7d, old)}
    if name == "dual_codex_reset":
        # 5h 已过重置点，7d 仍有效（与 reset 样张的两行对应）
        return {"five_hour": win(80, resets=now - 10.0, observed=now - 70.0),
                "seven_day": win(55, resets_7d)}
    if name == "dual_claude_none":
        return {"five_hour": win(30), "seven_day": win(10, resets_7d)}
    if name == "dual_stale_both":
        old = now - 3 * 3600.0
        return {"five_hour": win(3, observed=old), "seven_day": win(36, resets_7d, old)}
    if name == "dual_stale_claude":
        return {"five_hour": win(3), "seven_day": win(36, resets_7d)}
    if name == "dual_caption_new_same":
        old = now - 31 * 60.0
        return {"five_hour": win(65, observed=old), "seven_day": win(33, resets_7d, old)}
    if name == "dual_caption_nodata_error":
        return {"five_hour": win(30), "seven_day": win(10, resets_7d)}
    if name == "dual_caption_all_nodata":
        return None
    raise ValueError("unknown sample state: %s" % name)


def _sample_captions(name, now):
    """样张的反馈文字，与应用层一样用 caption_text 算。其它名字返回 None。

    没有任何窗口时整个小窗是无数据布局，反馈文字顶替第二行；caption_nodata、
    caption_nodata_error、dual_caption_all_nodata 这三个样张演示它。
    dual_caption_asking、dual_caption_cooldown、dual_caption_ping_failed
    是 Codex 刷新时的三条反馈：asking、cooldown、ping failed。
    caption_asking、caption_no_session 是单栏 Claude 的 asking 与 no session；
    dual_caption_app_no_limits 是双栏里 Claude 的 no limits 与 Codex 的 cooldown。
    """
    if name == "caption_new":
        return (caption_text(now - 3600.0, now - 60.0, ""), "")
    if name == "caption_same":
        return (caption_text(now - 31 * 60.0, now - 31 * 60.0, ""), "")
    if name == "caption_error":
        return (caption_text(now - 60.0, now - 60.0, "bad_json"), "")
    if name == "dual_caption_new_same":
        return (
            caption_text(now - 3600.0, now - 60.0, ""),
            caption_text(now - 31 * 60.0, now - 31 * 60.0, ""),
        )
    if name == "dual_caption_nodata_error":
        return (
            caption_text(None, None, "missing"),
            caption_text(now - 60.0, now - 60.0, "bad_json"),
        )
    if name == "caption_nodata":
        return (caption_text(None, None, "missing"), "")
    if name == "caption_nodata_error":
        return (caption_text(None, None, "bad_json"), "")
    if name == "dual_caption_all_nodata":
        return (
            caption_text(None, None, "missing"),
            caption_text(None, None, "bad_json"),
        )
    if name == "dual_caption_asking":
        return ("", codex_ping_caption("", "asking"))
    if name == "dual_caption_cooldown":
        return (
            caption_text(now - 3600.0, now - 60.0, ""),
            codex_ping_caption(caption_text(now - 31 * 60.0, now - 31 * 60.0, ""), "cooldown"),
        )
    if name == "dual_caption_ping_failed":
        return ("", codex_ping_caption("", "failed"))
    if name == "caption_asking":
        return (app_refresh_caption("", "asking"), "")
    if name == "caption_no_session":
        return (app_refresh_caption("", "no_session"), "")
    if name == "dual_caption_app_no_limits":
        return (
            app_refresh_caption("", "no_limits"),
            codex_ping_caption(caption_text(now - 31 * 60.0, now - 31 * 60.0, ""), "cooldown"),
        )
    return None


def sample_display(name, theme, scale):
    """样张用的显示元组。时间固定，样张在任何时区、任何日期下都一样。"""
    now = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))
    height = scaled(LOGICAL_H, scale)
    snapshot = _sample_snapshot(name, now)
    captions = _sample_captions(name, now)
    if name not in DUAL_SAMPLE_STATES and name not in EXTRA_DUAL_SAMPLE_STATES:
        return build_display(snapshot, now, theme, scale, height, captions=captions)
    return build_display(
        snapshot, now, theme, scale, height,
        codex=_sample_codex_snapshot(name, now), codex_available=True, captions=captions)


def sample_filename(index, name, theme, scale):
    return "%02d_%s_%s_%.2f.png" % (index, name, theme, scale)


def composite_on(image, background_rgb):
    """RGBA 图合成到纯色背景上，返回 RGB 图（模拟任务栏背景，样张用）。"""
    base = Image.new("RGBA", image.size, tuple(background_rgb) + (255,))
    base.alpha_composite(image)
    return base.convert("RGB")


def contact_sheet_label_width(names, pad=10, gap=10, minimum=170):
    """标签栏宽度：最长状态名与最长底色名并排，中间留 gap，两侧各留 pad，且不窄于 minimum。"""
    font = get_font(13)
    name_width = max(font.getlength(name) for name in names)
    background_width = max(font.getlength(background_name) for background_name, _rgb in SHEET_BACKGROUNDS)
    return max(minimum, int(math.ceil(pad + name_width + gap + background_width + pad)))


def compose_contact_sheet(rows):
    """rows: [(状态名, [每个组合一张 RGBA 图])]。

    每个状态占三行，分别把六种组合合成到三种底色上；左侧写状态名与底色名。
    标签栏宽度随最长状态名而定，避免长状态名与底色名叠在同一行上。
    """
    pad = 10
    pad_v = 6
    label_width = contact_sheet_label_width([name for name, _ in rows], pad)
    header_height = 26
    state_gap = 10
    columns = len(SAMPLE_COMBOS)
    column_widths = [max(images[c].width for _, images in rows) for c in range(columns)]
    cell_height = max(img.height for _, images in rows for img in images)
    row_height = cell_height + 2 * pad_v
    stripe_width = sum(column_widths) + pad * (columns + 1)
    block_height = row_height * len(SHEET_BACKGROUNDS) + state_gap
    sheet = Image.new("RGB", (label_width + stripe_width, header_height + block_height * len(rows)),
                      (127, 127, 127))
    draw = ImageDraw.Draw(sheet)
    font = get_font(13)
    x = label_width + pad
    for c, (theme, scale) in enumerate(SAMPLE_COMBOS):
        draw.text((x, header_height // 2), "%s %.2f" % (theme, scale), font=font,
                  fill=(255, 255, 255), anchor="lm")
        x += column_widths[c] + pad
    for r, (name, images) in enumerate(rows):
        block_top = header_height + r * block_height
        for b, (background_name, background) in enumerate(SHEET_BACKGROUNDS):
            top = block_top + b * row_height
            stripe = Image.new("RGB", (stripe_width, row_height), background)
            x = pad
            for c, img in enumerate(images):
                flat = composite_on(img, background)
                stripe.paste(flat, (x, (row_height - flat.height) // 2))
                x += column_widths[c] + pad
            sheet.paste(stripe, (label_width, top))
            if b == 0:
                draw.text((pad, top + row_height // 2), name, font=font, fill=(255, 255, 255), anchor="lm")
            draw.text((label_width - pad, top + row_height // 2), background_name, font=font,
                      fill=(230, 230, 230), anchor="rm")
    return sheet


def say(text):
    """打印一行；控制台编码放不下的字符退成 ascii() 转义，不让打印本身把程序弄崩。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(ascii(text), flush=True)
    except (OSError, ValueError):
        # 标准输出已被关闭或不可用（例如脱离控制台后）
        pass


def selftest_render(out_dir):
    """每个样张状态在两种主题与三种 scale 下各出一张 RGBA PNG，再拼 contact_sheet.png。"""
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for index, name in enumerate(ALL_SAMPLE_STATES, 1):
        images = []
        for theme, scale in SAMPLE_COMBOS:
            image = render_display(sample_display(name, theme, scale))
            path = os.path.join(out_dir, sample_filename(index, name, theme, scale))
            image.save(path)
            say(path)
            images.append(image)
        rows.append((name, images))
    sheet_path = os.path.join(out_dir, "contact_sheet.png")
    compose_contact_sheet(rows).save(sheet_path)
    say(sheet_path)
    return 0


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
        """返回是否真的写入。日志自身的 I/O 失败只能吞掉，没有地方再报。"""
        if not self.path:
            return False
        message = " ".join(str(message).split())[:LOG_MAX_MESSAGE]
        key = (kind, message)
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < LOG_DEDUP_SECONDS:
            return False
        if len(self._seen) >= LOG_MAX_KEYS:
            # 去重表有上限，内存不随时间增长
            self._seen = {k: t for k, t in self._seen.items() if now - t < LOG_DEDUP_SECONDS}
            if len(self._seen) >= LOG_MAX_KEYS:
                self._seen.clear()
        self._seen[key] = now
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
            return True
        except (OSError, ValueError):
            return False


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
    schema = doc.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, (int, float)) or schema != POS_SCHEMA:
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


def read_light_theme():
    """只读注册表的 SystemUsesLightTheme；读不到按浅色。"""
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


# ---------------------------------------------------------------------------
# Win32 薄封装（ctypes，只用白名单里的函数）
# ---------------------------------------------------------------------------

class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", ctypes.c_void_p),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", ctypes.c_ssize_t),
    ]


# rcMonitor 是整块显示器（含任务栏占用的部分），rcWork 已扣掉任务栏；
# 调用 GetMonitorInfoW 前必须先把 cbSize 设为结构体大小。
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


# 64 位下 LRESULT 与 LPARAM 是指针宽度的有符号数，WPARAM 是指针宽度的无符号数，
# 句柄一律 c_void_p；位宽写错会在 64 位上悄悄截断。
# 回调用 Windows 的 stdcall 约定；非 Windows（离线测试）没有它，退回 CFUNCTYPE，参数表相同。
_CALLBACK_TYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDPROC = _CALLBACK_TYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
WINEVENTPROC = _CALLBACK_TYPE(
    None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
    ctypes.c_long, ctypes.c_long, ctypes.c_uint, ctypes.c_uint)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


def _signature(func, argtypes, restype):
    """给 ctypes 函数设好 argtypes 与 restype（64 位下不设会按 32 位 int 截断指针）。"""
    func.argtypes = argtypes
    func.restype = restype


class Win32:
    """所有 ctypes 调用集中在这里；方法名都是动作，便于测试时替换。

    对其它进程窗口的操作只有只读查询：前台窗口的类名、矩形与所属进程号，主任务栏
    及其通知区域子窗口的矩形与可见性，任务栏所在显示器的矩形。小窗以主任务栏为
    所有者；跨线程所有权会连接输入队列，主线程不能阻塞。显示、移动、置前台只针对
    本进程窗口，消息只发给本进程窗口或消息循环线程。
    """

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32")
        self.shell32 = ctypes.WinDLL("shell32")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.shcore = ctypes.WinDLL("shcore")
        except OSError:
            self.shcore = None
        self.hinstance = None
        self._wndclass = None
        self._class_registered = False
        self._class_buffer = ctypes.create_unicode_buffer(256)
        self._event_callback = None
        self._event_hooks = []
        self._thread_id = 0
        self._declare()

    def _declare(self):
        ptr, i32, u32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
        wstr = ctypes.c_wchar_p

        _signature(self.user32.RegisterClassExW, [ctypes.POINTER(WNDCLASSEXW)], ctypes.c_ushort)
        _signature(self.user32.UnregisterClassW, [wstr, ptr], i32)
        _signature(self.user32.CreateWindowExW, [u32, wstr, wstr, u32, i32, i32, i32, i32, ptr, ptr, ptr, ptr], ptr)
        _signature(self.user32.DestroyWindow, [ptr], i32)
        _signature(self.user32.DefWindowProcW, [ptr, u32, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t)
        # GetMessageW 出错时返回 -1，所以 restype 必须是有符号 int，循环条件写成 > 0
        _signature(self.user32.GetMessageW, [ctypes.POINTER(wintypes.MSG), ptr, u32, u32], i32)
        _signature(self.user32.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], i32)
        _signature(self.user32.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t)
        _signature(self.user32.PostQuitMessage, [i32], None)
        _signature(self.user32.PostMessageW, [ptr, u32, ctypes.c_size_t, ctypes.c_ssize_t], i32)
        _signature(self.user32.PostThreadMessageW, [wintypes.DWORD, u32, ctypes.c_size_t, ctypes.c_ssize_t], i32)
        _signature(self.user32.SetTimer, [ptr, ctypes.c_size_t, u32, ptr], ctypes.c_size_t)
        _signature(self.user32.KillTimer, [ptr, ctypes.c_size_t], i32)
        _signature(self.user32.ShowWindow, [ptr, i32], i32)
        _signature(self.user32.SetWindowPos, [ptr, ctypes.c_ssize_t, i32, i32, i32, i32, u32], i32)
        _signature(self.user32.UpdateLayeredWindow,
            [ptr, ptr, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), ptr,
             ctypes.POINTER(wintypes.POINT), u32, ctypes.POINTER(BLENDFUNCTION), u32], i32)
        _signature(self.user32.GetDC, [ptr], ptr)
        _signature(self.user32.ReleaseDC, [ptr, ptr], i32)
        _signature(self.user32.LoadCursorW, [ptr, ptr], ptr)
        _signature(self.user32.SetCapture, [ptr], ptr)
        _signature(self.user32.ReleaseCapture, [], i32)
        _signature(self.user32.GetCursorPos, [ctypes.POINTER(wintypes.POINT)], i32)
        _signature(self.user32.CreatePopupMenu, [], ptr)
        _signature(self.user32.AppendMenuW, [ptr, u32, ctypes.c_size_t, wstr], i32)
        _signature(self.user32.TrackPopupMenu, [ptr, u32, i32, i32, i32, ptr, ptr], i32)
        _signature(self.user32.DestroyMenu, [ptr], i32)
        _signature(self.user32.SetForegroundWindow, [ptr], i32)
        _signature(self.user32.RegisterWindowMessageW, [wstr], u32)
        _signature(self.user32.GetForegroundWindow, [], ptr)
        _signature(self.user32.GetDesktopWindow, [], ptr)
        _signature(self.user32.GetWindow, [ptr, u32], ptr)
        _signature(self.user32.SetWinEventHook, [u32, u32, ptr, WINEVENTPROC, u32, u32, u32], ptr)
        _signature(self.user32.UnhookWinEvent, [ptr], i32)
        _signature(self.user32.GetClassNameW, [ptr, wintypes.LPWSTR, i32], i32)
        _signature(self.user32.FindWindowW, [wstr, wstr], ptr)
        _signature(self.user32.FindWindowExW, [ptr, ptr, wstr, wstr], ptr)
        _signature(self.user32.GetWindowRect, [ptr, ctypes.POINTER(wintypes.RECT)], i32)
        _signature(self.user32.IsWindow, [ptr], i32)
        _signature(self.user32.IsWindowVisible, [ptr], i32)
        _signature(self.user32.GetWindowThreadProcessId, [ptr, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
        _signature(self.user32.MonitorFromRect, [ctypes.POINTER(wintypes.RECT), wintypes.DWORD], ptr)
        _signature(self.user32.GetMonitorInfoW, [ptr, ctypes.POINTER(MONITORINFO)], i32)
        _signature(self.user32.GetGuiResources, [ptr, u32], u32)

        _signature(self.gdi32.CreateCompatibleDC, [ptr], ptr)
        _signature(self.gdi32.DeleteDC, [ptr], i32)
        _signature(self.gdi32.CreateDIBSection,
            [ptr, ctypes.POINTER(BITMAPINFOHEADER), u32, ctypes.POINTER(ptr), ptr, u32], ptr)
        _signature(self.gdi32.SelectObject, [ptr, ptr], ptr)
        _signature(self.gdi32.DeleteObject, [ptr], i32)

        _signature(self.shell32.SHAppBarMessage, [wintypes.DWORD, ctypes.POINTER(APPBARDATA)], ctypes.c_size_t)
        _signature(self.shell32.SHQueryUserNotificationState, [ctypes.POINTER(i32)], ctypes.c_long)

        _signature(self.kernel32.CreateMutexW, [ptr, i32, wstr], ptr)
        _signature(self.kernel32.CloseHandle, [ptr], i32)
        _signature(self.kernel32.GetCurrentProcess, [], ptr)
        _signature(self.kernel32.GetCurrentProcessId, [], wintypes.DWORD)
        _signature(self.kernel32.GetCurrentThreadId, [], wintypes.DWORD)
        _signature(self.kernel32.GetModuleHandleW, [wstr], ptr)

        # DPI 相关函数较新，旧系统上可能没有，缺失时在调用处按"拿不到"处理
        try:
            _signature(self.user32.SetProcessDpiAwarenessContext, [ctypes.c_ssize_t], i32)
            _signature(self.user32.GetDpiForWindow, [ptr], u32)
            _signature(self.user32.GetDpiForSystem, [], u32)
        except AttributeError:
            pass
        if self.shcore is not None:
            try:
                _signature(self.shcore.SetProcessDpiAwareness, [i32], ctypes.c_long)
            except AttributeError:
                self.shcore = None

    # DPI -----------------------------------------------------------------

    def set_dpi_awareness(self):
        """创建窗口之前调用：先试每显示器 V2，再试 shcore，都失败就忽略。"""
        try:
            if self.user32.SetProcessDpiAwarenessContext(DPI_CONTEXT_PER_MONITOR_V2):
                return "per-monitor-v2"
        except (AttributeError, OSError):
            pass
        if self.shcore is not None:
            try:
                if self.shcore.SetProcessDpiAwareness(2) == 0:
                    return "per-monitor"
            except OSError:
                pass
        return "unchanged"

    def scale_for(self, hwnd):
        """GetDpiForWindow / 96；拿不到就 GetDpiForSystem / 96，再不行 1.0。"""
        dpi = 0
        if hwnd:
            try:
                dpi = self.user32.GetDpiForWindow(hwnd)
            except (AttributeError, OSError):
                dpi = 0
        if not dpi:
            try:
                dpi = self.user32.GetDpiForSystem()
            except (AttributeError, OSError):
                dpi = 0
        if not dpi:
            return 1.0
        return max(SCALE_MIN, min(SCALE_MAX, dpi / 96.0))

    # 窗口类、窗口与消息循环 ------------------------------------------------

    def register_class(self, wndproc):
        self._thread_id = self.kernel32.GetCurrentThreadId()
        self.hinstance = self.kernel32.GetModuleHandleW(None)
        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.style = 0
        wndclass.lpfnWndProc = wndproc
        wndclass.hInstance = self.hinstance
        wndclass.hCursor = self.user32.LoadCursorW(None, IDC_ARROW)
        wndclass.lpszClassName = WINDOW_CLASS
        if not self.user32.RegisterClassExW(ctypes.byref(wndclass)):
            raise OSError("RegisterClassExW failed, error %d" % ctypes.get_last_error())
        self._wndclass = wndclass
        self._class_registered = True

    def unregister_class(self):
        if self._class_registered:
            self._class_registered = False
            self.user32.UnregisterClassW(WINDOW_CLASS, self.hinstance)

    def create_window(self, x, y, width, height, owner=None):
        """无父窗口的顶层分层窗口，创建时不可见。

        owner 可指定主任务栏；系统抬升任务栏层级时（如打开开始菜单），小窗随之抬升。
        """
        ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
        hwnd = self.user32.CreateWindowExW(
            ex_style, WINDOW_CLASS, WINDOW_TITLE, WS_POPUP, x, y, width, height,
            owner or None, None, self.hinstance, None)
        if not hwnd:
            raise OSError("CreateWindowExW failed, error %d" % ctypes.get_last_error())
        return hwnd

    def destroy_window(self, hwnd):
        return bool(self.user32.DestroyWindow(hwnd))

    def post_quit(self):
        self.user32.PostQuitMessage(0)

    def post_thread_message(self, message):
        return bool(self.user32.PostThreadMessageW(self._thread_id, message, 0, 0))

    def def_window_proc(self, hwnd, msg, wparam, lparam):
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run_message_loop(self, thread_handler=None, error_handler=None):
        """返回 0 表示收到 WM_QUIT；-1 表示 GetMessageW 出错。"""
        msg = wintypes.MSG()
        result = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        while result > 0:
            if not msg.hWnd and msg.message == WM_APP_RECREATE:
                if thread_handler is not None:
                    try:
                        thread_handler(msg.message)
                    except Exception as exc:
                        if error_handler is not None:
                            error_handler(exc)
                        else:
                            say("thread message handler failed: %s: %s" % (type(exc).__name__, exc))
            else:
                self.user32.TranslateMessage(ctypes.byref(msg))
                self.user32.DispatchMessageW(ctypes.byref(msg))
            result = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        return result

    def register_message(self, name):
        return int(self.user32.RegisterWindowMessageW(name))

    def watch_window_events(self, handler):
        """进程外只读事件通知；回调只投递消息，不在回调里改变窗口层级。"""
        def callback(_hook, event, hwnd, object_id, child_id, _thread, _time):
            try:
                handler(event, hwnd or 0, object_id, child_id)
            except BaseException:
                # ctypes 回调不能让异常逃到系统。
                pass

        self._event_callback = WINEVENTPROC(callback)
        try:
            for event in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_REORDER):
                hook = self.user32.SetWinEventHook(
                    event, event, None, self._event_callback, 0, 0,
                    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
                if not hook:
                    raise OSError("SetWinEventHook failed for event %d" % event)
                self._event_hooks.append(hook)
        except BaseException:
            self.unwatch_window_events()
            raise

    def unwatch_window_events(self):
        """由注册通知的主线程注销；失败的句柄和回调保留，允许收尾时重试。"""
        remaining = []
        for hook in self._event_hooks:
            if not self.user32.UnhookWinEvent(hook):
                remaining.append(hook)
        self._event_hooks = remaining
        if not remaining:
            self._event_callback = None

    def post_z_order_check(self, hwnd):
        return bool(self.user32.PostMessageW(hwnd, WM_APP_Z_ORDER, 0, 0))

    def set_timer(self, hwnd, timer_id, interval_ms):
        interval_ms = max(10, min(int(interval_ms), MAX_TIMER_MS))
        return bool(self.user32.SetTimer(hwnd, timer_id, interval_ms, None))

    def kill_timer(self, hwnd, timer_id):
        return bool(self.user32.KillTimer(hwnd, timer_id))

    def show_window(self, hwnd, command):
        self.user32.ShowWindow(hwnd, command)

    def move_window(self, hwnd, x, y):
        """只移动：不改尺寸、不动 Z 序、不激活。"""
        return bool(self.user32.SetWindowPos(
            hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))

    def keep_topmost(self, hwnd):
        return bool(self.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))

    def update_layered(self, hwnd, x, y, width, height, bgra):
        """把预乘 alpha 的 BGRA 位图提交给分层窗口，同时设定位置与尺寸。

        每次提交都新建 DIB 与内存 DC，并在 finally 里按相反顺序全部释放，GDI 对象
        数量不随提交次数增长。
        """
        if len(bgra) != width * height * 4:
            raise ValueError("bitmap is %d bytes, expected %d" % (len(bgra), width * height * 4))
        hdc_screen = self.user32.GetDC(None)
        hdc_mem = None
        hbitmap = None
        old_bitmap = None
        try:
            hdc_mem = self.gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                raise OSError("CreateCompatibleDC failed")
            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # 负数：自顶向下，第一行就是图像的最上一行
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            hbitmap = self.gdi32.CreateDIBSection(
                hdc_screen, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not hbitmap or not bits.value:
                raise OSError("CreateDIBSection failed")
            old_bitmap = self.gdi32.SelectObject(hdc_mem, hbitmap)
            ctypes.memmove(bits.value, bgra, len(bgra))
            destination = wintypes.POINT(x, y)
            size = wintypes.SIZE(width, height)
            source = wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            if not self.user32.UpdateLayeredWindow(
                    hwnd, hdc_screen, ctypes.byref(destination), ctypes.byref(size), hdc_mem,
                    ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA):
                raise OSError("UpdateLayeredWindow failed, error %d" % ctypes.get_last_error())
        finally:
            if hdc_mem and old_bitmap:
                self.gdi32.SelectObject(hdc_mem, old_bitmap)
            if hbitmap:
                self.gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                self.gdi32.DeleteDC(hdc_mem)
            if hdc_screen:
                self.user32.ReleaseDC(None, hdc_screen)

    # 鼠标与菜单 ----------------------------------------------------------

    def cursor_pos(self):
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed, error %d" % ctypes.get_last_error())
        return point.x, point.y

    def set_capture(self, hwnd):
        self.user32.SetCapture(hwnd)

    def release_capture(self):
        self.user32.ReleaseCapture()

    def track_menu(self, hwnd, x, y):
        """弹出右键菜单，返回选中的命令号（取消返回 0）。

        TrackPopupMenu 自带模态消息循环，期间计时器消息照常送到窗口过程。弹出前把
        自己的窗口设为前台，否则点菜单以外的地方菜单不会收起；系统可能拒绝这次
        设置，不影响菜单本身，所以不检查返回值。
        """
        menu = self.user32.CreatePopupMenu()
        if not menu:
            raise OSError("CreatePopupMenu failed, error %d" % ctypes.get_last_error())
        try:
            for item_id, text in MENU_ITEMS:
                if text is None:
                    self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    self.user32.AppendMenuW(menu, MF_STRING, item_id, text)
            self.user32.SetForegroundWindow(hwnd)
            command = self.user32.TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, x, y, 0, hwnd, None)
            self.user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            self.user32.DestroyMenu(menu)
        return int(command)

    # 只读查询 ------------------------------------------------------------

    def foreground_hwnd(self):
        return self.user32.GetForegroundWindow() or 0

    def desktop_hwnd(self):
        return self.user32.GetDesktopWindow() or 0

    def taskbar_hwnd(self):
        return self.user32.FindWindowW("Shell_TrayWnd", None) or 0

    def window_owner(self, hwnd):
        """只读诊断；开始菜单调整任务栏 band 时 GW_OWNER 会临时变化，不能据此重建。"""
        return self.user32.GetWindow(hwnd, GW_OWNER) or 0

    def is_window(self, hwnd):
        return bool(self.user32.IsWindow(hwnd))

    def taskbar_above(self, hwnd):
        covered, allowed = self._z_order_state(hwnd)
        return covered and allowed

    def topmost_allowed(self, hwnd):
        return self._z_order_state(hwnd)[1]

    def window_rect(self, hwnd):
        rect = wintypes.RECT()
        if not hwnd or not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def window_pid(self, hwnd):
        """窗口所属进程号；句柄为空或窗口已销毁返回 0。"""
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        # 返回值是线程号，0 表示失败，此时 pid 的内容不可信
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return 0
        return int(pid.value)

    def monitor_rect_for(self, rect):
        """rect 所在显示器的完整矩形（rcMonitor）；任一步失败返回 None。

        用 MONITOR_DEFAULTTONULL：矩形不落在任何显示器上时得到 None，由调用方另想
        办法，而不是悄悄换成最近的显示器。
        """
        query = wintypes.RECT(*rect)
        monitor = self.user32.MonitorFromRect(ctypes.byref(query), MONITOR_DEFAULTTONULL)
        if not monitor:
            return None
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        area = info.rcMonitor
        return area.left, area.top, area.right, area.bottom

    def foreground_covers_taskbar(self, taskbar_rect):
        """前台是否为铺满本任务栏所在显示器的普通应用窗口，即全屏应用。

        有些机器上 SHQueryUserNotificationState 长期返回忙碌（桌面空闲时也是），单凭
        它不能判全屏，所以调用方还要靠这个函数确认。下列前台都不算全屏，否则挂件会
        把自己藏起来：
        - 任务栏、副任务栏、桌面（SHELL_SURFACE_CLASSES）：用户点任务栏空白处或桌面
          后它们成为前台，矩形等于任务栏或铺满显示器；前台停在任务栏时（例如开始菜单
          用 Esc 关闭后焦点回到任务栏）挂件会一直出不来；
        - 本进程自己的窗口（右键菜单期间本窗口是前台）；
        - 铺不满显示器的窗口：最大化窗口只比工作区多出几个像素，盖不住整条任务栏。
        目标矩形取任务栏所在显示器的完整矩形；查不到显示器时退回任务栏矩形本身。
        """
        hwnd = self.foreground_hwnd()
        if not hwnd or self.class_name(hwnd) in SHELL_SURFACE_CLASSES:
            return False
        if self.window_pid(hwnd) == self.kernel32.GetCurrentProcessId():
            return False
        foreground_rect = self.window_rect(hwnd)
        if foreground_rect is None:
            return False
        target = self.monitor_rect_for(taskbar_rect)
        if target is None:
            target = taskbar_rect
        return rect_inside(target, foreground_rect)

    def shell_popup_overlaps(self, popup, hwnd):
        """只避让真正覆盖小窗的可见系统面板，不因面板打开就放弃层级。"""
        if (not self.user32.IsWindowVisible(popup)
                or self.class_name(popup) not in TOPMOST_SKIP_CLASSES):
            return False
        popup_rect = self.window_rect(popup)
        own_rect = self.window_rect(hwnd)
        # 拿不到矩形时保守避让；下次事件或两秒检查会再确认。
        return popup_rect is None or own_rect is None or rects_overlap(popup_rect, own_rect)

    def _z_order_state(self, hwnd):
        """只读返回 (任务栏在上方, 允许恢复层级)，有界遍历以容忍重排竞争。"""
        tray = self.user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return False, False
        seen = {hwnd}
        covered = False
        previous = self.user32.GetWindow(hwnd, GW_HWNDPREV)
        for _index in range(MAX_Z_ORDER_WINDOWS):
            if not previous:
                return covered, True
            if previous in seen:
                return False, False
            if previous == tray:
                covered = True
            elif self.shell_popup_overlaps(previous, hwnd):
                return covered, False
            seen.add(previous)
            previous = self.user32.GetWindow(previous, GW_HWNDPREV)
        return False, False

    def class_name(self, hwnd):
        if not hwnd:
            return ""
        length = self.user32.GetClassNameW(hwnd, self._class_buffer, 256)
        return self._class_buffer.value if length > 0 else ""

    def tray_notify_rect(self):
        """主任务栏的通知区域（TrayNotifyWnd）矩形；找不到或不可见返回 None。

        只读：先找主任务栏，再在它下面找通知区域子窗口，读可见性与矩形。资源管理器
        重启后句柄会变，所以每次都重新查找。
        """
        tray = self.user32.FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return None
        notify = self.user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
        if not notify or not self.user32.IsWindowVisible(notify):
            return None
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(notify, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def taskbar_pos(self):
        """返回 ((left, top, right, bottom), edge)，失败返回 None。"""
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        if not self.shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(data)):
            return None
        rect = data.rc
        return (rect.left, rect.top, rect.right, rect.bottom), int(data.uEdge)

    def taskbar_autohide(self):
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        state = self.shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
        return bool(state & ABS_AUTOHIDE)

    def notification_state(self):
        state = ctypes.c_int(0)
        if self.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return state.value

    # 进程 ----------------------------------------------------------------

    def create_mutex(self, name):
        """返回 (句柄或 0, GetLastError)。last error 必须紧跟在调用之后取。"""
        handle = self.kernel32.CreateMutexW(None, False, name)
        return handle or 0, ctypes.get_last_error()

    def close_handle(self, handle):
        if handle:
            self.kernel32.CloseHandle(handle)

    def gui_resources(self, flag):
        """flag 0 为 GDI 对象数，1 为 USER 对象数。"""
        return int(self.user32.GetGuiResources(self.kernel32.GetCurrentProcess(), flag))


# ---------------------------------------------------------------------------
# 窗口与周期任务
# ---------------------------------------------------------------------------

_APP = None


def _wnd_proc(hwnd, msg, wparam, lparam):
    """WNDPROC 的唯一入口：任何异常都不能逃出回调。"""
    try:
        app = _APP
        if app is None:
            return 0
        return app.window_proc(hwnd, msg, wparam, lparam)
    except BaseException:
        return 0


# 回调对象必须放在模块级变量里：窗口类注册后系统还会持有函数指针，对象一旦被回收
# 就会在下一条消息上崩溃
_WNDPROC_REF = WNDPROC(_wnd_proc)


class WidgetApp:
    """窗口、定位与周期任务，全部在主线程。

    每个消息处理都整体 try/except，异常记日志后照常继续；计时器由系统重复投递，
    一次失败不影响下一次。
    """

    def __init__(self, opts, w32, log):
        self.opts = opts
        self.w32 = w32
        self.log = log
        self.data_dir = opts.data_dir
        self.reader = SnapshotReader(os.path.join(self.data_dir, SNAPSHOT_NAME))
        if opts.no_codex:
            self.codex = None
        else:
            self.codex = CodexReader(opts.codex_home)
        # pinger 只在 Refresh now 时才会启动子进程；没有 Codex 数据源或被 --no-codex-ping 关掉时为 None。
        self.pinger = None
        if self.codex is not None and not opts.no_codex_ping:
            self.pinger = CodexPinger(
                self.data_dir,
                model=opts.codex_ping_model or CODEX_PING_MODEL,
                exe_override=opts.codex_bin,
                codex_home=opts.codex_home)
        # 只在 Refresh now 时才写请求文件；被 --no-app-refresh 关掉时为 None。
        # 与 Codex 是否启用无关：--no-codex 时 Claude 块照样能用。
        self.app_refresher = None if opts.no_app_refresh else AppRefresher(self.data_dir)
        # 同一种 Codex 原因只记一次；原因变回空串后，下次再出现才重新记。
        self._codex_log_reason = ""
        self.pos_path = os.path.join(self.data_dir, POS_FILE_NAME)
        self.mode, self.offset = load_position(self.pos_path)
        self.light = read_light_theme()
        self.hwnd = 0
        self.owner = 0
        self._recreating = False
        self._owner_recreated_at = None
        self._owner_recreate_suppressed = False
        # 有所有者创建失败过的任务栏句柄；0 表示没有这类失败。
        self._owner_failed_tray = 0
        self._exit_deadline = None
        self.scale = 1.0
        self.geom = None
        self.shown_display = None
        self.taskbar = None
        self.visible = False
        self.exiting = False
        self.timers = set()
        self.snapshot_override = None
        self.drag = None
        self.menu_open = False
        self.msg_taskbar_created = 0
        self._busy = False
        self._again = False
        self._reassert_pending = False
        self._z_order_pending = False
        self._gdi = {}
        self._watchdog = None
        self._flashing = False
        self._caption = None          # (截止时间, 各提供方反馈文字)，None 表示没有
        self._caption_before = None   # 只在 Refresh now 读数据源期间非 None：刷新前各提供方最新的 observed_at
        # 点 Refresh now 那一刻 Codex 最新的 observed_at，ping 结束后拿它判断 new 还是 same。
        self._ping_before = None
        # 点 Refresh now 那一刻 Claude 最新的 observed_at，确认回来后拿它判断 new 还是 same。
        self._app_before = None

    # 基础设施 ----------------------------------------------------------------

    def _safe(self, func, *args):
        """执行一次回调，异常记日志后吞掉，返回是否成功。"""
        try:
            func(*args)
            return True
        except Exception as exc:
            self._log_exception(exc)
            return False

    def _log_exception(self, exc):
        try:
            self.log.log_exception(exc)
        except Exception:
            pass

    def set_timer(self, timer_id, interval_ms):
        """退出开始之后不再建计时器，否则会在销毁窗口后又冒出消息。"""
        if self.exiting or not self.hwnd:
            return False
        if not self.w32.set_timer(self.hwnd, timer_id, interval_ms):
            self.log.log("SetTimer", "SetTimer failed for timer %d" % timer_id)
            return False
        self.timers.add(timer_id)
        if timer_id == TIMER_EXIT and self._exit_deadline is None:
            self._exit_deadline = time.monotonic() + max(10, min(int(interval_ms), MAX_TIMER_MS)) / 1000.0
        return True

    def kill_timer(self, timer_id):
        if timer_id in self.timers:
            self.timers.discard(timer_id)
            if self.hwnd:
                self.w32.kill_timer(self.hwnd, timer_id)

    def _kill_all_timers(self):
        for timer_id in list(self.timers):
            self.kill_timer(timer_id)

    # 窗口过程 ----------------------------------------------------------------

    def window_proc(self, hwnd, msg, wparam, lparam):
        """返回消息结果；没有处理的消息交给系统默认处理。绝不抛异常。"""
        try:
            handled, result = self._dispatch(msg, wparam)
            if handled:
                return result
        except Exception as exc:
            self._log_exception(exc)
        try:
            return self.w32.def_window_proc(hwnd, msg, wparam, lparam)
        except Exception:
            return 0

    def _dispatch(self, msg, wparam):
        if msg == WM_MOUSEACTIVATE:
            # 点击小窗不激活它，也就不抢焦点
            return True, MA_NOACTIVATE
        if msg == WM_TIMER:
            self.on_timer(wparam)
            return True, 0
        if msg == WM_APP_Z_ORDER:
            self.on_z_order_check()
            return True, 0
        if msg == WM_LBUTTONDOWN:
            self.on_left_down()
            return True, 0
        if msg == WM_MOUSEMOVE:
            self.on_mouse_move(wparam)
            return True, 0
        if msg == WM_LBUTTONUP:
            self.on_left_up()
            return True, 0
        if msg == WM_CAPTURECHANGED:
            self.on_capture_lost()
            return True, 0
        if msg == WM_RBUTTONUP:
            self.on_right_up()
            return True, 0
        if msg in SYSTEM_CHANGE_MESSAGES or (self.msg_taskbar_created and msg == self.msg_taskbar_created):
            # 只有任务栏重建才忘掉上次拒绝有所有者创建的句柄；DPI/显示/设置变化不清。
            if self.msg_taskbar_created and msg == self.msg_taskbar_created:
                self._owner_failed_tray = 0
            self.on_system_change()
            return True, 0
        if msg == WM_CLOSE:
            self.request_exit()
            return True, 0
        if msg == WM_DESTROY:
            self.on_destroy()
            return True, 0
        return False, 0

    def on_timer(self, timer_id):
        if self.exiting:
            return
        if timer_id == TIMER_CHECK:
            self._owner_check()
            self.refresh_view(reassert_topmost=True)
        elif timer_id == TIMER_POLL:
            self.poll_snapshot()
        elif timer_id == TIMER_THEME:
            self.poll_theme()
        elif timer_id == TIMER_EXIT:
            self.kill_timer(TIMER_EXIT)
            self.request_exit()
        elif timer_id == TIMER_GDI_BEGIN:
            self.kill_timer(TIMER_GDI_BEGIN)
            self._gdi["before"] = self._gdi_counts()
            self._gdi["index"] = 0
            self.set_timer(TIMER_GDI_TICK, GDI_TICK_MS)
        elif timer_id == TIMER_GDI_TICK:
            self._gdi_tick()
        elif timer_id == TIMER_GDI_END:
            self.kill_timer(TIMER_GDI_END)
            self._gdi_report()
            self.request_exit()
        elif timer_id == TIMER_FLASH:
            self.kill_timer(TIMER_FLASH)
            self._end_flash()
        elif timer_id == TIMER_CAPTION:
            self.kill_timer(TIMER_CAPTION)
            # 计时器到点就直接擦。系统计时器可能比墙钟略早触发，不能再拿截止时间去比。
            self._caption = None
            self.refresh_view()
        elif timer_id == TIMER_PING:
            self._on_ping_timer()
        elif timer_id == TIMER_APP:
            self._on_app_timer()

    def on_destroy(self):
        if self._recreating:
            return
        if self.exiting:
            self.w32.unwatch_window_events()
            self._kill_all_timers()
            self.hwnd = 0
            self.w32.post_quit()
            return
        # 所有者销毁时系统也会销毁小窗；等销毁消息返回后再通过线程消息重建。
        self.log.log("WindowDestroyed", "window destroyed by the system; recovering via thread message")
        self.timers.clear()
        self.hwnd = 0
        self.visible = False
        if not self.w32.post_thread_message(WM_APP_RECREATE):
            self.log.log("RecreateWindow", "PostThreadMessageW failed")
            self.w32.post_quit()

    def _create_widget_window(self):
        tray = self.w32.taskbar_hwnd()
        try:
            hwnd = self.w32.create_window(0, 0, INITIAL_WINDOW_PX, INITIAL_WINDOW_PX, tray or None)
        except OSError as exc:
            if not tray:
                raise
            self._log_exception(exc)
            # 先记成无所有者，避免创建过程中重入时按旧句柄再重建。
            self.owner = 0
            hwnd = self.w32.create_window(0, 0, INITIAL_WINDOW_PX, INITIAL_WINDOW_PX, None)
            self._owner_failed_tray = tray
            self.owner = 0
            return hwnd
        if tray:
            # 有所有者创建成功后，这个任务栏可以再试。
            self._owner_failed_tray = 0
        self.owner = tray
        return hwnd

    def _owner_check(self):
        """按创建时所有者与任务栏句柄有效性判断重建。

        开始菜单打开时，Windows 调整任务栏 band 会临时改变 GW_OWNER，只记诊断日志。
        """
        if (self.exiting or self._recreating or self._busy or self.menu_open
                or self.drag is not None or not self.hwnd):
            return
        tray = self.w32.taskbar_hwnd()
        if not tray:
            return
        # 这个任务栏已经拒绝过有所有者创建，同一句柄不再反复重建。
        if self.owner == 0 and tray == self._owner_failed_tray:
            return
        if not self.owner or tray != self.owner or not self.w32.is_window(self.owner):
            if self.owner == 0:
                cause = "unowned window, taskbar 0x%X" % tray
            elif tray != self.owner:
                cause = "taskbar replaced 0x%X -> 0x%X" % (self.owner, tray)
            else:
                cause = "owner 0x%X no longer valid, taskbar 0x%X" % (self.owner, tray)
            now = time.monotonic()
            if self._owner_recreated_at is not None and now - self._owner_recreated_at < OWNER_RECREATE_SECONDS:
                if not self._owner_recreate_suppressed:
                    self._owner_recreate_suppressed = True
                    self.log.log("OwnerRecreateSuppressed", "recreate deferred for 30 s: " + cause)
                return
            self._owner_recreated_at = now
            self._owner_recreate_suppressed = False
            self._recreate_window(cause)
        else:
            actual_owner = self.w32.window_owner(self.hwnd)
            if actual_owner != tray:
                self.log.log("OwnerMismatch", "GW_OWNER=0x%X expected 0x%X; no recreate" % (actual_owner, tray))

    def _recreate_window(self, reason):
        self.log.log("OwnerChanged", reason)
        self._recreating = True
        try:
            self._kill_all_timers()
            old = self.hwnd
            if self.w32.is_window(old):
                self.w32.destroy_window(old)
            self.hwnd = 0
        finally:
            self._recreating = False
        try:
            self.hwnd = self._create_widget_window()
        except OSError as exc:
            self._log_exception(exc)
            self.request_exit()
            return
        self.visible = False
        self.geom = None
        self.shown_display = None
        self._z_order_pending = False
        self._flashing = False
        self._caption = None
        self._start_timers(recreated=True)
        self.refresh_view(force=True, reassert_topmost=True)

    def on_thread_message(self, message):
        if message != WM_APP_RECREATE or self.exiting or self.hwnd:
            return
        try:
            self.hwnd = self._create_widget_window()
        except OSError as exc:
            self._log_exception(exc)
            self.w32.post_quit()
            return
        self.visible = False
        self.geom = None
        self.shown_display = None
        self._z_order_pending = False
        self._flashing = False
        self._caption = None
        self._start_timers(recreated=True)
        self.refresh_view(force=True, reassert_topmost=True)

    # 显示与定位 --------------------------------------------------------------

    def on_window_event(self, event, hwnd, object_id, child_id):
        """合并窗口事件，每批最多投递一次检查；对象内部的列表重排不相关。"""
        if self.exiting or not self.hwnd or not hwnd or self._z_order_pending:
            return
        if event == EVENT_OBJECT_REORDER:
            if child_id != CHILDID_SELF:
                return
            if object_id != OBJID_WINDOW and not (
                    object_id == OBJID_CLIENT and hwnd == self.w32.desktop_hwnd()):
                return
        elif event != EVENT_SYSTEM_FOREGROUND:
            return
        self._z_order_pending = True
        if not self.w32.post_z_order_check(self.hwnd):
            self._z_order_pending = False

    def on_z_order_check(self):
        self._z_order_pending = False
        if self.exiting or not self.hwnd or not self.visible:
            return
        # 只在任务栏确实盖住小窗时恢复层级，避免自己的重排通知形成循环。
        if self.w32.taskbar_above(self.hwnd):
            self.refresh_view(reassert_topmost=True)

    def _current_snapshot(self):
        if self.snapshot_override is not None:
            return self.snapshot_override
        return self.reader.snapshot

    def refresh_view(self, force=False, reassert_topmost=False):
        """重新判断可见性、重算尺寸与位置，必要时重绘；所有触发源共用这一条路径。

        重入保护：UpdateLayeredWindow、SetWindowPos 这类调用会在内部处理别的线程
        发来的消息（例如系统广播的 WM_SETTINGCHANGE），那条消息又会走到这里；嵌套的
        请求只记个标记，等当前这一轮做完再补一轮。
        """
        if self.exiting or not self.hwnd:
            return
        self._reassert_pending |= reassert_topmost
        if self._busy:
            self._again = True
            return
        self._busy = True
        try:
            passes = 0
            self._again = True
            while self._again and passes < MAX_REFRESH_PASSES:
                self._again = False
                passes += 1
                reassert = self._reassert_pending
                self._reassert_pending = False
                self._refresh_once(force and passes == 1, reassert)
        finally:
            self._busy = False

    def _refresh_once(self, force, reassert_topmost):
        if self.menu_open or self.drag is not None:
            # 菜单或拖动进行中不挪窗口、不隐藏窗口
            return
        info = self.w32.taskbar_pos()
        if info is None:
            self.log.log("TaskbarPos", "taskbar position query failed")
            info = self.taskbar
            if info is None:
                self._hide()
                return
        self.taskbar = info
        rect, edge = info
        if edge not in SUPPORTED_EDGES:
            self.log.log("TaskbarEdge", "unsupported taskbar edge %d; widget hidden" % edge)
            self._hide()
            return
        if self.w32.taskbar_autohide():
            self._hide()
            return
        notification_state = self.w32.notification_state()
        if (notification_state in HIDE_NOTIFICATION_STATES
                or (notification_state == BUSY_NOTIFICATION_STATE
                    and self.w32.foreground_covers_taskbar(rect))):
            self._hide()
            return
        scale = self.w32.scale_for(self.hwnd)
        height = window_height(rect, scale)
        theme = "light" if self.light else "dark"
        # 句柄自测改写 snapshot_override 时仍走单提供方，避免把 Codex 画进自测画面。
        if self.snapshot_override is not None:
            codex_snapshot = None
            codex_available = False
        else:
            codex_snapshot = None if self.codex is None else self.codex.snapshot
            codex_available = bool(self.codex and self.codex.available)
        now = time.time()
        captions = None
        if self._caption is not None:
            # 截止时间是兜底：定时器没建成，或到点那次重绘被菜单、拖动、隐藏挡住时，
            # 之后两秒一次的 TIMER_CHECK 会因为截止时间已过而重绘擦掉反馈文字。
            deadline, texts = self._caption
            if now < deadline:
                captions = texts
            else:
                self._caption = None
        # asking 在 ping 或等待桌面会话期间持续显示，不受 2.5 秒反馈到期影响。
        # Codex 那一项只因 pinger.running 变成 asking，Claude 那一项只因
        # app_refresher.waiting 变成 asking，两项互不影响，可以同时成立。
        if self.pinger is not None and self.pinger.running:
            base = list(captions) if captions else []
            base = (base + ["", ""])[:2]
            base[1] = CAPTION_ASKING
            captions = tuple(base)
        if self.app_refresher is not None and self.app_refresher.waiting:
            base = list(captions) if captions else []
            base = (base + ["", ""])[:2]
            base[0] = CAPTION_ASKING
            captions = tuple(base)
        display = build_display(
            self._current_snapshot(), now, theme, scale, height,
            codex=codex_snapshot, codex_available=codex_available, captions=captions)
        tray = self.w32.tray_notify_rect() if self.mode == MODE_AUTO else None
        geom = compute_layout(rect, edge, scale, display.width, self.mode, self.offset, tray)
        self.scale = scale
        self._apply(display, geom, force)
        shown_now = not self.visible
        if shown_now:
            self.w32.show_window(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True
        # 只在需要时恢复置顶层级：任务栏确实盖在小窗上方，判据与 on_z_order_check 相同
        # （taskbar_above：任务栏在上方，且没有重叠的系统面板）。以前只要允许就每 2 秒无条件
        # 置顶一次，会和屏幕键盘、录屏浮层这类置顶窗口来回抢层级，还让系统里每个
        # EVENT_OBJECT_REORDER 钩子都被通知一遍。例外：小窗刚从隐藏变为显示（例如全屏应用
        # 退出之后）时它可能落在置顶带的任何位置，所以只要允许就无条件置顶一次；这一轮
        # 是不是 TIMER_CHECK 带着 reassert_topmost 来的都一样，否则恰好由 15 秒轮询重新
        # 显示的小窗就要等到任务栏盖住它才会被抬起来。
        if shown_now:
            raise_window = self.w32.topmost_allowed(self.hwnd)
        elif reassert_topmost:
            raise_window = self.w32.taskbar_above(self.hwnd)
        else:
            raise_window = False
        if raise_window:
            self.w32.keep_topmost(self.hwnd)

    # 最近一次渲染的未变暗画面：(显示元组, RGBA 图)。类级默认 None，首次渲染后落到实例上。
    _frame = None

    def _frame_image(self, display):
        """显示元组对应的未变暗画面，与上一次相同的元组直接复用。

        元组相等画面就相同（见 build_display），所以点 Refresh now 时变暗、恢复和前面那次
        带反馈文字的重绘用的是同一个元组，只渲染第一次，之后只重新预乘并提交。缓存里是
        未变暗的原图：变暗由调用方用 dim_image 另取新图，绝不改它。渲染抛错时缓存保持原样。
        """
        frame = self._frame
        if frame is not None and frame[0] == display:
            return frame[1]
        image = render_display(display)
        self._frame = (display, image)
        return image

    def _apply(self, display, geom, force):
        """内容或尺寸变了就重绘并提交位图（同时设定位置与尺寸），只是位置变了才移动。

        重绘时画面按显示元组缓存（_frame_image），变暗只作用在缓存图的副本上。
        """
        x, y, width, height = geom
        old = self.geom
        if force or old is None or old[2:] != (width, height) or display != self.shown_display:
            image = self._frame_image(display)
            if self._flashing:
                image = dim_image(image, FLASH_ALPHA_SCALE)
            data = premultiply_bgra(image)
            self.w32.update_layered(self.hwnd, x, y, width, height, data)
            self.shown_display = display
            self.geom = geom
            if _font_state["fallback"]:
                self.log.log("FontFallback", "segoeui.ttf not loaded; using the default font")
        elif abs(x - old[0]) > MOVE_THRESHOLD_PX or abs(y - old[1]) > MOVE_THRESHOLD_PX:
            self.move_to(x, y)

    def move_to(self, x, y):
        """只移动窗口，SetWindowPos 成功才记下新位置，返回是否成功。

        失败时 geom 保持原值，下一轮刷新仍会看到位置差异并重试；记下没移成的位置会让
        重试一直被压住，直到位置再次变化。
        """
        moved = bool(self.w32.move_window(self.hwnd, x, y))
        if moved:
            self.geom = (x, y, self.geom[2], self.geom[3])
        return moved

    def _hide(self):
        if self.visible:
            self.w32.show_window(self.hwnd, SW_HIDE)
            self.visible = False

    # 周期任务 ----------------------------------------------------------------

    def poll_snapshot(self, force=False):
        """读两个数据源再重绘。TIMER_POLL 与 Refresh now 共用。

        Refresh now 会先把刷新前的最新观测时间放进 self._caption_before。读完数据源后
        立刻取走并清掉这个标记，再算出反馈文字；第一次重绘就带着它。
        """
        self.reader.refresh(force=force)
        reason = self.reader.last_reason
        if reason and reason != "missing":
            self.log.log("SnapshotInvalid", reason)
        self._poll_codex(force)
        # refresh_view 内部的 UpdateLayeredWindow / SetWindowPos 会处理别的线程发来的消息，
        # 不能让那些路径看到仍然"刷新中"的标记，所以先取到局部变量并立刻置 None。
        before = self._caption_before
        self._caption_before = None
        if before is not None:
            self._arm_caption(before)
        self.refresh_view()

    def _latest_observed(self):
        """各提供方当前快照里最新的 observed_at：(Claude, Codex)；没有窗口或没有该数据源则 None。"""
        codex = None if self.codex is None else self.codex.snapshot
        return (_snapshot_observed_at(self.reader.snapshot), _snapshot_observed_at(codex))

    def _arm_caption(self, before):
        """读完数据源后算出各提供方的反馈文字，记下截止时间并起定时器。

        before 是刷新前 _latest_observed() 的结果。定时器建不起来也不用处理：
        set_timer 自己记日志，截止时间仍由之后的重绘兜底擦掉。
        """
        texts = [caption_text(
            before[0], _snapshot_observed_at(self.reader.snapshot), self.reader.last_reason)]
        if self.codex is None:
            texts.append("")
        else:
            texts.append(caption_text(
                before[1], _snapshot_observed_at(self.codex.snapshot), self.codex.last_reason))
        self._caption = (time.time() + CAPTION_MS / 1000.0, tuple(texts))
        self.set_timer(TIMER_CAPTION, CAPTION_MS)

    def _poll_codex(self, force):
        """刷新 Codex。异常只记日志，不打断已经完成的 Claude 刷新，也不挡住后面的重绘。"""
        reader = self.codex
        if reader is None:
            return
        try:
            reader.refresh(force=force)
        except Exception as exc:
            self._log_exception(exc)
            return
        reason = reader.last_reason
        if reason == "":
            self._codex_log_reason = ""
            return
        if reason == "no_codex" or reason == self._codex_log_reason:
            return
        self._codex_log_reason = reason
        self.log.log("CodexRead", reason)

    def _rewrite_codex_caption(self, rewrite):
        """改掉已有反馈文字里 Codex 那一项。没有反馈文字时什么也不做。"""
        if self._caption is None:
            return
        deadline, texts = self._caption
        pair = (tuple(texts) + ("", ""))[:2]
        self._caption = (deadline, (pair[0], rewrite(pair[1])))

    def _rewrite_claude_caption(self, rewrite):
        """改掉已有反馈文字里 Claude 那一项。没有反馈文字时什么也不做，截止时间不变。"""
        if self._caption is None:
            return
        deadline, texts = self._caption
        pair = (tuple(texts) + ("", ""))[:2]
        self._caption = (deadline, (rewrite(pair[0]), pair[1]))

    def _set_slot_caption(self, index, text):
        """把反馈文字里下标 index（0 是 Claude，1 是 Codex）那一项换成 text。

        只保留未过期的另一项，没有或已过期则另一项为空串。截止时间重新从现在算起，
        起 TIMER_CAPTION 并重绘一次。
        """
        now = time.time()
        pair = ["", ""]
        if self._caption is not None:
            deadline, texts = self._caption
            if now < deadline:
                pair = list((tuple(texts) + ("", ""))[:2])
        pair[index] = text
        self._caption = (now + CAPTION_MS / 1000.0, (pair[0], pair[1]))
        self.set_timer(TIMER_CAPTION, CAPTION_MS)
        self.refresh_view()

    def _start_codex_ping(self, before_codex):
        """只在 Refresh now 里调用，TIMER_POLL 不会走到。

        Codex 没装或还没用过（没有 sessions 目录）时，不替用户去启动它。
        """
        try:
            pinger = self.pinger
            if pinger is None or self.codex is None or not self.codex.available:
                return
            if self.exiting or not self.hwnd:
                return
            reason = pinger.start()
            if reason == "started":
                self._ping_before = before_codex
                if not self.set_timer(TIMER_PING, CODEX_PING_POLL_MS):
                    self._drop_ping()
                    self._rewrite_codex_caption(
                        lambda _text: codex_ping_caption("", "failed"))
                self.refresh_view()
            elif reason == "cooldown":
                if self._caption is not None:
                    self._rewrite_codex_caption(
                        lambda text: codex_ping_caption(text, "cooldown"))
                self.refresh_view()
            elif reason == "no_exe":
                self.log.log("CodexPing", "codex.exe not found; ping skipped")
            elif reason.startswith("spawn_error:"):
                self.log.log("CodexPing", reason)
                if self._caption is not None:
                    self._rewrite_codex_caption(
                        lambda _text: codex_ping_caption("", "failed"))
                self.refresh_view()
        except Exception as exc:
            self._log_exception(exc)

    def _on_ping_timer(self):
        """ping 的结果只在这里统一收口。只换 Codex 那一项（下标 1），
        Claude 那一项若仍未过期就保留。"""
        try:
            pinger = self.pinger
            if pinger is None:
                self.kill_timer(TIMER_PING)
                return
            result = pinger.poll()
            if result is None:
                if not pinger.running:
                    self.kill_timer(TIMER_PING)
                return
            self.kill_timer(TIMER_PING)
            self._poll_codex(True)
            outcome, detail = result
            if outcome == "failed" or outcome == "timeout":
                text = codex_ping_caption("", "failed")
                self.log.log("CodexPing", "ping %s (%s)" % (outcome, detail))
            else:
                text = caption_text(
                    self._ping_before,
                    _snapshot_observed_at(self.codex.snapshot),
                    self.codex.last_reason)
            self._ping_before = None
            self._set_slot_caption(1, text)
        except Exception as exc:
            self._log_exception(exc)

    def _stop_ping(self):
        """停掉正在跑的 ping。可重复调用，绝不抛异常。"""
        try:
            if self.pinger is not None:
                self.pinger.stop()
        except Exception as exc:
            self._log_exception(exc)

    def _drop_ping(self):
        """轮询计时器建不起来：没有计时器就没人收尾，ping 会一直停在 asking，所以放弃它。"""
        self.log.log("CodexPing", "poll timer unavailable; ping abandoned")
        self._stop_ping()
        self._ping_before = None

    def _start_app_refresh(self, before_claude):
        """只在 Refresh now 里调用，TIMER_POLL 绝不会走到。内部绝不抛异常。"""
        try:
            refresher = self.app_refresher
            if refresher is None or self.exiting or not self.hwnd:
                return
            reason = refresher.start()
            if reason == "started":
                self._app_before = before_claude
                if not self.set_timer(TIMER_APP, APP_REFRESH_POLL_MS):
                    self._drop_app_refresh()
                    self._rewrite_claude_caption(
                        lambda _text: app_refresh_caption("", "no_session"))
                self.refresh_view()
            elif reason == "waiting":
                pass
            elif reason == "cooldown":
                if self._caption is not None:
                    self._rewrite_claude_caption(
                        lambda text: app_refresh_caption(text, "cooldown"))
                self.refresh_view()
            elif reason == "no_dir":
                pass
            elif reason.startswith("write_error:"):
                self.log.log("AppRefresh", reason)
                if self._caption is not None:
                    self._rewrite_claude_caption(
                        lambda _text: app_refresh_caption("", "not_sent"))
                self.refresh_view()
        except Exception as exc:
            self._log_exception(exc)

    def _on_app_timer(self):
        """TIMER_APP：读确认文件，收口后只换 Claude 那一项。"""
        try:
            refresher = self.app_refresher
            if refresher is None:
                self.kill_timer(TIMER_APP)
                return
            result = refresher.poll()
            if result is None:
                if not refresher.waiting:
                    self.kill_timer(TIMER_APP)
                return
            self.kill_timer(TIMER_APP)
            outcome, _detail = result
            if outcome == "ok":
                self.reader.refresh(force=True)
                reason = self.reader.last_reason
                if reason and reason != "missing":
                    self.log.log("SnapshotInvalid", reason)
                text = caption_text(
                    self._app_before,
                    _snapshot_observed_at(self.reader.snapshot),
                    self.reader.last_reason)
            elif outcome == "unavailable":
                text = app_refresh_caption("", "no_limits")
            else:
                text = app_refresh_caption("", "no_session")
                self.log.log(
                    "AppRefresh", "no answer within %d s" % APP_REFRESH_WAIT_SECONDS)
            self._app_before = None
            self._set_slot_caption(0, text)
        except Exception as exc:
            self._log_exception(exc)

    def _stop_app_refresh(self):
        """停掉正在等的请求。可重复调用，绝不抛异常。"""
        try:
            if self.app_refresher is not None:
                self.app_refresher.stop()
        except Exception as exc:
            self._log_exception(exc)

    def _drop_app_refresh(self):
        """轮询计时器建不起来，没有计时器就没人收尾，Claude 项会一直停在 asking，所以放弃这次请求。"""
        self.log.log("AppRefresh", "poll timer unavailable; request abandoned")
        self._stop_app_refresh()
        self._app_before = None

    def poll_theme(self):
        light = read_light_theme()
        if light != self.light:
            self.light = light
            self.refresh_view()

    def on_system_change(self):
        """DPI、显示器、系统设置、任务栏重建：立即重读主题并重算位置。"""
        self._owner_check()
        self.light = read_light_theme()
        self.refresh_view()

    # 鼠标与菜单 ----------------------------------------------------------------

    def on_left_down(self):
        if self.geom is None or self.menu_open:
            return
        cursor_x, _cursor_y = self.w32.cursor_pos()
        # 先取得鼠标捕获，再建拖动状态：本窗口若已持有捕获，SetCapture 会同步发来
        # WM_CAPTURECHANGED，那时拖动状态还没建，不会把新拖动当成"拖动中失去捕获"而收尾
        self.w32.set_capture(self.hwnd)
        self.drag = {"cursor_x": cursor_x, "window_x": self.geom[0], "moved": False}

    def on_mouse_move(self, wparam):
        drag = self.drag
        if drag is None or self.taskbar is None or self.geom is None:
            return
        if not wparam & MK_LBUTTON:
            # 按键已经松开但没收到松开消息（例如在别处松开）
            self._finish_drag()
            return
        cursor_x, _cursor_y = self.w32.cursor_pos()
        delta = cursor_x - drag["cursor_x"]
        if not drag["moved"] and abs(delta) <= DRAG_THRESHOLD_PX:
            return
        drag["moved"] = True
        left, _top, right, _bottom = self.taskbar[0]
        x, y, width, _height = self.geom
        new_x = max(left, min(drag["window_x"] + delta, right - width))
        if new_x != x:
            self.move_to(new_x, y)

    def on_left_up(self):
        self._finish_drag()

    def on_capture_lost(self):
        if self.drag is not None:
            self._finish_drag(release=False)

    def _finish_drag(self, release=True):
        """结束拖动；移动超过阈值才切到手动模式并保存。

        先清掉 self.drag 再释放鼠标捕获：ReleaseCapture 会同步发出 WM_CAPTURECHANGED，
        那条消息进来时看到 drag 已为空，就不会重复收尾。
        """
        drag = self.drag
        self.drag = None
        if release:
            self.w32.release_capture()
        if drag is None or not drag["moved"] or self.geom is None or self.taskbar is None:
            return
        x, _y, width, _height = self.geom
        self.offset = offset_from_right_edge(x, width, self.taskbar[0][2], self.scale)
        self.mode = MODE_MANUAL
        self._save_position()
        self.refresh_view()

    def _end_flash(self):
        """结束变暗。先丢掉已显示的元组，下一轮即使被挡住也会重画未变暗的画面。

        丢掉的只是"窗口里现在是什么"的记录，渲染缓存（_frame_image）不受影响：重画只重新
        预乘并提交，不再渲染。
        """
        self._flashing = False
        self.shown_display = None
        self.refresh_view(force=True)

    def _refresh_now(self, before=None):
        """Refresh now：记下刷新前的观测时间，重读数据源并重绘（第一次重绘就带反馈文字），再变暗。

        before 是菜单打开那一刻的 _latest_observed()。菜单开着时 TIMER_POLL 仍会派发，
        新数据可能已经读进读取器；等菜单关了再取，before 会等于新数据，反馈就被写成 same。
        before 为 None 时才退回当场取（无参调用仍然成立）。
        poll_snapshot 仍是对外入口，不给它加参数：TIMER_POLL 与现有测试都按 force 单参数
        调用或替换它。反馈文字靠 _caption_before 接进去：只在读数据源期间非 None，
        poll_snapshot 读完就清掉；这里的 finally 保住异常路径也清掉。
        之后（读完数据源、反馈文字已经挂上）才请求 Codex 与桌面会话，这样
        asking / cooldown 是在已有反馈文字上改。
        """
        if before is None:
            before = self._latest_observed()
        self._caption_before = before
        try:
            self.poll_snapshot(force=True)
        finally:
            self._caption_before = None
        self._safe(self._start_codex_ping, before[1])
        self._safe(self._start_app_refresh, before[0])
        self._begin_flash()

    def _begin_flash(self):
        """刷新后把画面短暂变暗再恢复，数据没变时点击也有可见反馈。

        闪烁不进显示元组，所以开始和结束都用 force=True 重绘；显示元组通常与前面那次带
        反馈文字的重绘相同，此时命中渲染缓存，只重新预乘并提交，不再渲染。已经在闪时
        再点刷新只重设定时器，不再多画一次。重绘抛错也要建上定时器；定时器建不起来就
        立刻取消变暗，避免小窗一直停在暗的画面上。
        """
        if self.exiting or not self.hwnd:
            return
        already = self._flashing
        self._flashing = True
        try:
            if not already:
                self.refresh_view(force=True)
        finally:
            if not self.set_timer(TIMER_FLASH, FLASH_MS):
                self._end_flash()

    def _save_position(self):
        try:
            save_position(self.data_dir, self.mode, self.offset)
        except OSError as exc:
            self._log_exception(exc)

    def on_right_up(self):
        if self.menu_open or self.drag is not None:
            return
        x, y = self.w32.cursor_pos()
        # 菜单开着时 TIMER_POLL 仍会派发，新数据可能在这期间被读进读取器；
        # 刷新前的观测时间必须在菜单打开这一刻取，不能等菜单关了再取。
        before = self._latest_observed()
        self.menu_open = True
        try:
            command = self.w32.track_menu(self.hwnd, x, y)
        finally:
            self.menu_open = False
        if self.exiting:
            return
        if not self.hwnd and not self.w32.post_thread_message(WM_APP_RECREATE):
            self.log.log("RecreateWindow", "PostThreadMessageW failed")
            self.w32.post_quit()
        if command == MENU_REFRESH:
            self._refresh_now(before)
        elif command == MENU_SNAP:
            self.mode = MODE_AUTO
            self._save_position()
            self.refresh_view()
        elif command == MENU_QUIT:
            self.request_exit()

    # 退出 ----------------------------------------------------------------------

    def _arm_watchdog(self):
        """退出请求发出 10 秒后进程还在就强制结束。

        这是整个程序里唯一的线程：只在退出时启动一次，只睡眠和 os._exit，不碰窗口，
        因此主线程卡死（例如销毁窗口或菜单循环不返回）时它仍能生效。
        """
        def force_exit():
            try:
                self.log.log("WatchdogExit", "exit still pending after %d s; forcing exit" % WATCHDOG_SECONDS)
            finally:
                os._exit(WATCHDOG_EXIT_CODE)
        self._watchdog = threading.Timer(WATCHDOG_SECONDS, force_exit)
        self._watchdog.daemon = True
        self._watchdog.start()

    def request_exit(self):
        """Quit、WM_CLOSE 与 --exit-after 共用：先置标志，再停全部计时器，最后销毁窗口。

        窗口销毁后 WM_DESTROY 里 PostQuitMessage，消息循环结束；之后的收尾（注销窗口类、
        关互斥量）在循环外做。
        """
        if self.exiting:
            return
        self.exiting = True
        self._stop_ping()
        self._stop_app_refresh()
        self._flashing = False
        self._caption = None
        self.w32.unwatch_window_events()
        try:
            self._arm_watchdog()
        except Exception as exc:
            # 起不了看门狗（例如系统线程资源耗尽）也不能耽误正常退出
            self._log_exception(exc)
        self._kill_all_timers()
        hwnd = self.hwnd
        if not hwnd or not self.w32.destroy_window(hwnd):
            self.w32.post_quit()

    # 句柄自测（--selftest-gdi）----------------------------------------------------

    def _gdi_counts(self):
        return self.w32.gui_resources(0), self.w32.gui_resources(1)

    def _gdi_tick(self):
        """一轮自测；索引先加再执行，即使本轮出错也保证在固定轮数后结束。"""
        index = self._gdi.get("index", 0)
        if index >= self.opts.selftest_gdi:
            self.kill_timer(TIMER_GDI_TICK)
            self.set_timer(TIMER_GDI_END, GDI_SETTLE_MS)
            return
        self._gdi["index"] = index + 1
        self._gdi_body(index)

    def _gdi_body(self, index):
        """用不同的百分比重绘一次，再把窗口左右抖动 2 像素（走 SetWindowPos 那条路）。"""
        now = time.time()
        five = Win(float((index * 7) % 101), now + 3600.0, now)
        seven = Win(float((index * 13 + 5) % 101), now + 86400.0, now)
        self.snapshot_override = {"five_hour": five, "seven_day": seven}
        self.refresh_view(force=True)
        if self.geom is not None:
            x, y, _width, _height = self.geom
            self.move_to(x + (2 if index % 2 else -2), y)

    def _gdi_report(self):
        before = self._gdi.get("before", (0, 0))
        after = self._gdi_counts()
        say("GDI before=%d after=%d USER before=%d after=%d" % (before[0], after[0], before[1], after[1]))

    # 主流程 ----------------------------------------------------------------------

    def _start_timers(self, recreated=False):
        self.set_timer(TIMER_CHECK, WINDOW_CHECK_MS)
        self.set_timer(TIMER_POLL, SNAPSHOT_POLL_MS)
        self.set_timer(TIMER_THEME, THEME_POLL_MS)
        if recreated:
            if self._exit_deadline is not None:
                self.set_timer(TIMER_EXIT, max(10, int((self._exit_deadline - time.monotonic()) * 1000)))
            if self.pinger is not None and self.pinger.running:
                # 窗口重建清掉了全部计时器；补不上就放弃，否则没人再收尾这个 ping。
                if not self.set_timer(TIMER_PING, CODEX_PING_POLL_MS):
                    self._drop_ping()
            if self.app_refresher is not None and self.app_refresher.waiting:
                # 窗口重建清掉了全部计时器；补不上就放弃，否则没人再收尾这个请求。
                if not self.set_timer(TIMER_APP, APP_REFRESH_POLL_MS):
                    self._drop_app_refresh()
        elif self.opts.exit_after is not None:
            self.set_timer(TIMER_EXIT, int(self.opts.exit_after * 1000))
        if self.opts.selftest_gdi and not recreated:
            self.set_timer(TIMER_GDI_BEGIN, GDI_SETTLE_MS)

    def run(self):
        global _APP
        _APP = self
        try:
            self.w32.register_class(_WNDPROC_REF)
            try:
                self.msg_taskbar_created = self.w32.register_message("TaskbarCreated")
                self.hwnd = self._create_widget_window()
                self._safe(self.poll_snapshot)
                self._safe(self.refresh_view, False, True)
                # 注册失败仍能用两秒轮询兜底，不影响小窗启动。
                self._safe(self.w32.watch_window_events, self.on_window_event)
                self._start_timers()
                result = self.w32.run_message_loop(self.on_thread_message, self._log_exception)
                if result < 0:
                    self.log.log("MessageLoop", "GetMessageW failed")
            finally:
                self.w32.unwatch_window_events()
                self.w32.unregister_class()
        finally:
            self._stop_ping()
            self._stop_app_refresh()
            _APP = None
        return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

Options = collections.namedtuple(
    "Options",
    ["data_dir", "exit_after", "selftest_render", "selftest_gdi", "no_codex", "codex_home",
     "codex_bin", "no_codex_ping", "codex_ping_model", "no_app_refresh", "warnings"],
    defaults=(False, None, None, False, None, False, ()))


def _arg_text(text):
    """写进警告的参数文字：ascii() 保证纯 ASCII，超过 40 个字符截断。"""
    shown = ascii(text)
    return shown if len(shown) <= 40 else shown[:37] + "..."


def parse_args(argv):
    """宽松解析：未知参数、缺值或值不合法的参数一律忽略，每一处各记一条到 warnings。不做任何 I/O。"""
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    exit_after = None
    render_dir = None
    gdi_count = None
    no_codex = False
    codex_home_arg = None
    codex_bin_arg = None
    no_codex_ping = False
    ping_model = None
    no_app_refresh = False
    warnings = []
    names = ("--data-dir", "--exit-after", "--selftest-render", "--selftest-gdi",
             "--codex-home", "--codex-bin", "--codex-ping-model")
    index = 0
    while index < len(argv):
        name, equals, inline = argv[index].partition("=")
        if name == "--no-codex":
            no_codex = True
            index += 1
            continue
        if name == "--no-codex-ping":
            no_codex_ping = True
            index += 1
            continue
        if name == "--no-app-refresh":
            no_app_refresh = True
            index += 1
            continue
        if name not in names:
            warnings.append("ignored unknown argument " + _arg_text(argv[index]))
            index += 1
            continue
        if equals:
            value, step = inline, 1
        elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            value, step = argv[index + 1], 2
        else:
            warnings.append("ignored " + name + ", no value given")
            index += 1
            continue
        index += step
        if not value:
            warnings.append("ignored " + name + ", no value given")
        elif name == "--data-dir":
            data_dir = os.path.abspath(value)
        elif name == "--selftest-render":
            render_dir = os.path.abspath(value)
        elif name == "--codex-home":
            codex_home_arg = os.path.abspath(value)
        elif name == "--codex-bin":
            codex_bin_arg = os.path.abspath(value)
        elif name == "--codex-ping-model":
            if valid_ping_model(value):
                ping_model = value
            else:
                warnings.append("ignored invalid --codex-ping-model " + _arg_text(value))
        elif name == "--exit-after":
            try:
                seconds = float(value)
            except ValueError:
                seconds = None
            if seconds is not None and math.isfinite(seconds) and seconds >= 0:
                exit_after = seconds
            else:
                warnings.append("ignored invalid number for --exit-after: " + _arg_text(value))
        elif name == "--selftest-gdi":
            try:
                count = int(value)
            except ValueError:
                count = 0
            if count >= 1:
                gdi_count = count
            else:
                warnings.append("ignored invalid number for --selftest-gdi: " + _arg_text(value))
    return Options(data_dir, exit_after, render_dir, gdi_count, no_codex, codex_home_arg,
                   codex_bin_arg, no_codex_ping, ping_model, no_app_refresh, warnings)


def run_app(opts, log):
    w32 = Win32()
    handle, error = w32.create_mutex(MUTEX_NAME)
    if error == ERROR_ALREADY_EXISTS:
        w32.close_handle(handle)
        say("ClaudeUsageWidget is already running; exiting")
        return 0
    if not handle:
        log.log("MutexError", "CreateMutexW failed, error %d" % error)
        return 1
    try:
        w32.set_dpi_awareness()
        return WidgetApp(opts, w32, log).run()
    finally:
        w32.close_handle(handle)


def main(argv=None):
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    if opts.selftest_render:
        return selftest_render(opts.selftest_render)
    # 数据目录不存在就建（小窗的文件都写在这里）。建不成也照常启动，只说一声：日志也写不进去。
    try:
        os.makedirs(opts.data_dir, exist_ok=True)
    except (OSError, ValueError) as exc:
        say("data folder %s could not be created: %s: %s"
            % (opts.data_dir, type(exc).__name__, exc))
    log = ErrorLog(opts.data_dir)
    for text in opts.warnings:
        log.log("Argument", text)
        say(text)
    try:
        return run_app(opts, log)
    except Exception as exc:
        log.log_exception(exc)
        say("fatal: %s: %s" % (type(exc).__name__, exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
