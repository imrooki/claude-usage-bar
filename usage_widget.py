# -*- coding: utf-8 -*-
"""Claude 用量小窗：贴在 Windows 任务栏通知区域左边的两行横向进度条，没有背景色。

上一行是 5 小时窗口，下一行是 7 天窗口，每行带百分比与重置时间。数据只来自
<data-dir>\\usage.json（由别的程序写入）。本程序不联网、不起其它程序、不碰任何
凭证文件；窗口是无父窗口的顶层分层窗口（逐像素 alpha），以主任务栏为所有者，随
任务栏一起抬升层级。对资源管理器窗口只读查询；跨线程所有权会连接输入队列，主线程
不能阻塞。

运行：pythonw usage_widget.py [--data-dir DIR]
测试用参数：--exit-after SEC、--selftest-render OUTDIR、--selftest-gdi N

代码分三层：
  1. 纯函数（快照读取与校验、显示状态、显示元组、布局、文字、渲染、预乘转换），可离线测试；
  2. Win32 薄封装（ctypes，只用白名单里的函数）；
  3. 窗口与周期任务（WidgetApp，全部在主线程，周期任务用 SetTimer 与 WM_TIMER）。
"""

import collections
import ctypes
import ctypes.wintypes as wintypes
import functools
import json
import math
import os
import sys
import threading
import time
import winreg

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
FILL_ALPHA_STALE = 120

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
# 纯函数层：显示状态、文字、显示元组
# ---------------------------------------------------------------------------

RowView = collections.namedtuple(
    "RowView", ["label", "state", "fill", "tier", "pct_text", "reset_text"])
Display = collections.namedtuple(
    "Display", ["kind", "rows", "theme", "scale", "width", "height"])


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


def display_width(kind, scale, height):
    """小窗宽度（像素）：数据行按列布局算，无数据时按两行文字的实际宽度算。"""
    if kind == "nodata":
        font = get_font(render_metrics(scale, height)["font_px"])
        text_width = int(math.ceil(max(font.getlength(line) for line in NODATA_LINES)))
        return scaled(MARGIN_L, scale) + text_width + scaled(MARGIN_R, scale)
    return scaled(logical_columns()[1], scale)


def build_display(snapshot, now, theme, scale, height):
    """把快照与当前时间整理成不可变的显示元组；元组相等就不用重绘。

    填充长度由取整后的百分比决定，这样元组相等一定意味着画面相同。宽度由内容、
    scale 与高度推出，放进元组是为了让尺寸变化也触发重绘。
    """
    five = snapshot.get("five_hour") if snapshot else None
    seven = snapshot.get("seven_day") if snapshot else None
    if five is None and seven is None:
        width = display_width("nodata", scale, height)
        return Display("nodata", NODATA_LINES, theme, scale, width, height)
    rows = (build_row("5h", five, now, False), build_row("7d", seven, now, True))
    width = display_width("data", scale, height)
    return Display("data", rows, theme, scale, width, height)


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


def _compose_bar(width, height, radius, fill_width, track_rgba, fill_rgba):
    """进度条（空槽加填充）的直通 alpha RGBA 图，像素在整数域里精确合成。

    填充覆盖的地方是"替换"空槽而不是叠在空槽上：叠加会让 stale 填充的 alpha 从
    120 变成 145，并被空槽的黑色染暗。逐像素公式（f、t 为填充与空槽的覆盖度 0..255，
    Af、At 为两者的 alpha）：
        权重 wf = f * Af * 255，wt = (255 - f) * t * At
        颜色 = (Cf * wf + Ct * wt) / (wf + wt)，alpha = (wf + wt) / 255²
    内部像素（f = 255）恰好得到填充色与 Af，空槽内部（f = 0，t = 255）恰好得到
    空槽色与 At；圆角边缘按覆盖度在预乘空间里过渡，不会出现染暗的毛边。
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
    """按数字字形高度垂直居中（以基线定位），align 为 "l" 或 "r"。"""
    digit_top = font.getbbox("0", anchor="ls")[1]
    baseline = int(math.floor(center_y - digit_top / 2.0 + 0.5))
    draw.text((x, baseline), text, font=font, fill=ink, anchor="ls" if align == "l" else "rs")


def render_display(display):
    """把显示元组画成直通 alpha 的 RGBA 图（纯函数：同输入同输出）。

    没有任何可见背景：先在全透明画布上画进度条和文字，最后把所有 alpha 为 0 的像素
    抬到 alpha = 1（颜色 0），肉眼不可见，但整个小窗矩形都能接收鼠标，拖动和右键才
    不会落空。画法上的两个约束：
      - 文字画在一张"预先填好文字颜色、alpha 为 0"的图层上，抗锯齿边缘只改变 alpha，
        颜色保持不变；直接画在透明底上会把颜色朝黑色混合，预乘后边缘就偏暗偏细。
      - 进度条由 _compose_bar 精确合成，保证内部像素恰为规定的颜色与 alpha。
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
        for index, line in enumerate(display.rows):
            _draw_text(text_draw, font, left, (index + 0.5) * row_height, line,
                       text_rgb + (TEXT_ALPHA,), "l")
    else:
        edges = pixel_columns(scale)
        bar_left, bar_right = edges["bar"]
        bar_width = bar_right - bar_left
        bar_height = metrics["bar_height"]
        bar_radius = metrics["bar_radius"]
        for index, row in enumerate(display.rows):
            center = (index + 0.5) * row_height
            stale = row.state == "stale"
            ink = text_rgb + (TEXT_ALPHA_STALE if stale else TEXT_ALPHA,)
            _draw_text(text_draw, font, edges["label"][0], center, row.label, ink, "l")
            fill_rgba = None
            fill_width = 0
            if row.state in ("normal", "stale"):
                fill_width = int(math.floor(row.fill * bar_width / 100.0 + 0.5))
                fill_rgba = TIER_RGB[row.tier] + (FILL_ALPHA_STALE if stale else FILL_ALPHA,)
            top = int(math.floor(center - bar_height / 2.0 + 0.5))
            canvas.paste(
                _compose_bar(bar_width, bar_height, bar_radius, fill_width, track_rgba, fill_rgba),
                (bar_left, top))
            _draw_text(text_draw, font, edges["pct"][1], center, row.pct_text, ink, "r")
            if row.reset_text:
                _draw_text(text_draw, font, edges["reset"][0], center, row.reset_text, ink, "l")
    canvas.alpha_composite(text_layer)
    red, green, blue, alpha = canvas.split()
    return Image.merge("RGBA", (red, green, blue, alpha.point(_ALPHA_FLOOR)))


@functools.lru_cache(maxsize=1)
def _premultiply_table():
    """按 alpha 分行、按通道值分列：floor(c * a / 255)。

    向下取整保证预乘后的通道值不超过 alpha；UpdateLayeredWindow 要求预乘数据满足
    这一点，否则会画出错误的颜色。
    """
    return tuple(bytes((c * a) // 255 for c in range(256)) for a in range(256))


def premultiply_bgra(image):
    """RGBA 图转成预乘 alpha 的 BGRA 字节串（每个像素 B、G、R 乘 a/255 后向下取整）。"""
    if image.mode != "RGBA":
        raise ValueError("expected an RGBA image, got %s" % image.mode)
    data = image.tobytes()
    alphas = data[3::4]
    table = _premultiply_table()
    out = bytearray(len(data))
    out[0::4] = bytes(table[a][c] for a, c in zip(alphas, data[2::4]))
    out[1::4] = bytes(table[a][c] for a, c in zip(alphas, data[1::4]))
    out[2::4] = bytes(table[a][c] for a, c in zip(alphas, data[0::4]))
    out[3::4] = alphas
    return bytes(out)


# ---------------------------------------------------------------------------
# 样张（--selftest-render）
# ---------------------------------------------------------------------------

SAMPLE_SCALES = (1.0, 1.25, 1.5)
SAMPLE_COMBOS = tuple((theme, scale) for theme in ("light", "dark") for scale in SAMPLE_SCALES)
SAMPLE_STATES = ("nodata", "green", "amber", "red", "full", "stale", "reset")
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

    if name == "nodata":
        return None
    if name == "green":
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
    raise ValueError("unknown sample state: %s" % name)


def sample_display(name, theme, scale):
    """样张用的显示元组。时间固定，样张在任何时区、任何日期下都一样。"""
    now = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))
    height = scaled(LOGICAL_H, scale)
    return build_display(_sample_snapshot(name, now), now, theme, scale, height)


def sample_filename(index, name, theme, scale):
    return "%02d_%s_%s_%.2f.png" % (index, name, theme, scale)


def composite_on(image, background_rgb):
    """RGBA 图合成到纯色背景上，返回 RGB 图（模拟任务栏背景，样张用）。"""
    base = Image.new("RGBA", image.size, tuple(background_rgb) + (255,))
    base.alpha_composite(image)
    return base.convert("RGB")


def compose_contact_sheet(rows):
    """rows: [(状态名, [每个组合一张 RGBA 图])]。

    每个状态占三行，分别把六种组合合成到三种底色上；左侧写状态名与底色名。
    """
    pad = 10
    pad_v = 6
    label_width = 170
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
    """每个状态在两种主题与三种 scale 下各出一张 RGBA PNG，再拼 contact_sheet.png。"""
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for index, name in enumerate(SAMPLE_STATES, 1):
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
    """错误日志：data-dir 不存在就不写；超过 64 KB 先清空；同一种异常 10 分钟只记一次。"""

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
            with open(self.path, "wb" if oversized else "ab") as handle:
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
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
WINEVENTPROC = ctypes.WINFUNCTYPE(
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
        display = build_display(self._current_snapshot(), time.time(), theme, scale, height)
        tray = self.w32.tray_notify_rect() if self.mode == MODE_AUTO else None
        geom = compute_layout(rect, edge, scale, display.width, self.mode, self.offset, tray)
        self.scale = scale
        self._apply(display, geom, force)
        if not self.visible:
            self.w32.show_window(self.hwnd, SW_SHOWNOACTIVATE)
            self.visible = True
        if reassert_topmost and self.w32.topmost_allowed(self.hwnd):
            self.w32.keep_topmost(self.hwnd)

    def _apply(self, display, geom, force):
        """内容或尺寸变了就重绘并提交位图（同时设定位置与尺寸），只是位置变了才移动。"""
        x, y, width, height = geom
        old = self.geom
        if force or old is None or old[2:] != (width, height) or display != self.shown_display:
            data = premultiply_bgra(render_display(display))
            self.w32.update_layered(self.hwnd, x, y, width, height, data)
            self.shown_display = display
            self.geom = geom
            if _font_state["fallback"]:
                self.log.log("FontFallback", "segoeui.ttf not loaded; using the default font")
        elif abs(x - old[0]) > MOVE_THRESHOLD_PX or abs(y - old[1]) > MOVE_THRESHOLD_PX:
            self.move_to(x, y)

    def move_to(self, x, y):
        self.w32.move_window(self.hwnd, x, y)
        self.geom = (x, y, self.geom[2], self.geom[3])

    def _hide(self):
        if self.visible:
            self.w32.show_window(self.hwnd, SW_HIDE)
            self.visible = False

    # 周期任务 ----------------------------------------------------------------

    def poll_snapshot(self, force=False):
        self.reader.refresh(force=force)
        reason = self.reader.last_reason
        if reason and reason != "missing":
            self.log.log("SnapshotInvalid", reason)
        self.refresh_view()

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

    def _save_position(self):
        try:
            save_position(self.data_dir, self.mode, self.offset)
        except OSError as exc:
            self._log_exception(exc)

    def on_right_up(self):
        if self.menu_open or self.drag is not None:
            return
        x, y = self.w32.cursor_pos()
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
            self.poll_snapshot(force=True)
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
            _APP = None
        return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

Options = collections.namedtuple(
    "Options", ["data_dir", "exit_after", "selftest_render", "selftest_gdi"])


def parse_args(argv):
    """宽松解析：未知参数、缺值或值不合法的参数一律忽略。"""
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    exit_after = None
    render_dir = None
    gdi_count = None
    names = ("--data-dir", "--exit-after", "--selftest-render", "--selftest-gdi")
    index = 0
    while index < len(argv):
        name, equals, inline = argv[index].partition("=")
        if name not in names:
            index += 1
            continue
        if equals:
            value, step = inline, 1
        elif index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            value, step = argv[index + 1], 2
        else:
            index += 1
            continue
        index += step
        if name == "--data-dir" and value:
            data_dir = os.path.abspath(value)
        elif name == "--selftest-render" and value:
            render_dir = os.path.abspath(value)
        elif name == "--exit-after":
            try:
                seconds = float(value)
            except ValueError:
                continue
            if math.isfinite(seconds) and seconds >= 0:
                exit_after = seconds
        elif name == "--selftest-gdi":
            try:
                count = int(value)
            except ValueError:
                continue
            if count >= 1:
                gdi_count = count
    return Options(data_dir, exit_after, render_dir, gdi_count)


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
    log = ErrorLog(opts.data_dir)
    try:
        return run_app(opts, log)
    except Exception as exc:
        log.log_exception(exc)
        say("fatal: %s: %s" % (type(exc).__name__, exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
