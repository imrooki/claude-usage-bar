# -*- coding: utf-8 -*-
"""渲染层：显示状态与显示元组、布局、文字与进度条渲染、样张输出。

从 usage_widget.py 拆出的模块之一，行为与原单文件实现一致。测试对 usage_widget
打的补丁经 _entry() 取回入口模块，所以这里不直接引用被测试替换的名字。
"""

import collections
import functools
import math
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from usage_bar.data import (
    CAPTION_BENIGN_REASONS,
    CAPTION_NO_FILE,
    CAPTION_READ_ERROR,
    DEFAULT_OFFSET,
    DEFAULT_SKIN,
    MODE_MANUAL,
    SKINS,
    Win,
    app_refresh_caption,
    codex_ping_caption,
)
from usage_bar.win32 import SUPPORTED_EDGES


def _entry():
    """入口模块（usage_widget 或 __main__）：测试对入口模块打的补丁必须在这里仍然可见。"""
    return sys.modules.get("usage_widget") or sys.modules["__main__"]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STALE_SECONDS = 1800
# 年龄文字的天数上限，更大的一律写 999d。畸形的 observed_at（例如 -1e300）经 _finite 检查后仍是
# 有限数，不封顶会得到几百位的天数，把小窗宽度撑到几千像素，右端锚定也随之失效。
MAX_AGE_DAYS = 999

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

INITIAL_WINDOW_PX = 8

# 颜色与 alpha（直通 alpha 的 RGBA；提交前才转成预乘 alpha）。进度条本身的颜色与
# 画法在皮肤层（usage_bar.skins）：填充渐变 fill_rgb、陈旧灰 STALE_FILL_RGB 都在那里。
BACKGROUND_ALPHA = 1
TEXT_RGB = {"light": (0x1B, 0x1B, 0x1B), "dark": (0xF2, 0xF2, 0xF2)}
TRACK_RGBA = {"light": (0, 0, 0, 48), "dark": (255, 255, 255, 56)}
TEXT_ALPHA = 255
TEXT_ALPHA_STALE = 150
FILL_ALPHA = 255

NODATA_LINES = ("Claude usage", "no data yet")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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
# skin 是进度条皮肤名（见 usage_bar.data 的 SKINS）；旧写法不带它时用默认皮肤，
# 所以默认皮肤下的显示元组与改动前相等，切皮肤才会改变元组、触发重绘。
Display = collections.namedtuple(
    "Display", ["kind", "rows", "theme", "scale", "width", "height", "ages", "captions", "skin"],
    defaults=((), (), DEFAULT_SKIN))


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
    return max(0, min(100, 100 - percent_int(pct)))


def fill_percent(pct):
    return max(0, min(100, 100 - percent_int(pct)))


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
        label, state, fill_percent(win.pct), _entry().usage_level(win.pct),
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

def _place_columns(origin):
    """从 origin 起按列宽与列间隙排出各列的 (左, 右)，返回 (字典, 排完后的右端)。

    单栏的列从左边距起，双栏的块从 0 起；两者共用这一套列定义，只差起点和末尾是否加右边距。
    """
    columns = {}
    x = origin
    for name, width, gap_after in (
        ("label", LABEL_W, GAP_LABEL_BAR),
        ("bar", BAR_W, GAP_BAR_PCT),
        ("pct", PCT_W, GAP_PCT_RESET),
        ("reset", RESET_W, 0),
    ):
        columns[name] = (x, x + width)
        x += width + gap_after
    return columns, x


def logical_columns():
    """按逻辑像素排出各列的 (左, 右)，返回 (字典, 总宽)。"""
    columns, x = _place_columns(MARGIN_L)
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
    return _place_columns(0)


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


def _vscale(scale, height):
    """窗口高度相对标称高度的比例，上限 1：任务栏矮时缩小，任务栏高时不放大。"""
    nominal_height = max(1, scaled(LOGICAL_H, scale))
    return min(1.0, height / float(nominal_height))


def render_metrics(scale, height):
    """窗口高度对应的字号、进度槽高度与圆角（像素）。

    任务栏太矮时窗口高度小于标称高度，字号、行高、进度槽按比例缩小；宽度由列布局
    决定，不随之变化。
    """
    vscale = _vscale(scale, height)
    return {
        "vscale": vscale,
        "font_px": max(MIN_FONT_PX, scaled(FONT_PX * vscale, scale)),
        "bar_height": max(2, scaled(BAR_H * vscale, scale)),
        "bar_radius": max(1, scaled(BAR_R * vscale, scale)),
    }


def dual_render_metrics(scale, height):
    """双栏的页眉高度、行高、字号与进度槽。单栏的 render_metrics 不走这里。"""
    vscale = _vscale(scale, height)
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


def _nodata_display(theme, scale, height, caption, skin=DEFAULT_SKIN):
    """无数据布局：没有进度条行，ages 恒为 ()，反馈文字至多一项。"""
    return Display(
        "nodata", NODATA_LINES, theme, scale,
        display_width("nodata", scale, height, caption), height,
        (), _stored_strings((caption,)), skin)


def _with_fills(rows, fills, offset):
    """动画中间帧：把 normal 行的 fill 换成给定浮点百分比（其余行不动）。

    fills 是四条进度条的中间值（双栏按 5h、7d、5h、7d 顺序，offset 指出本块从哪条起），
    缺项或为 None 时该行保持原值。含在显示元组里的仍是取整后的整数百分比，元组相等
    仍然一定意味着画面相同。
    """
    if fills is None:
        return rows
    out = []
    for index, row in enumerate(rows):
        value = fills[offset + index] if offset + index < len(fills) else None
        if value is None or row.state != "normal":
            out.append(row)
        else:
            out.append(row._replace(fill=max(0, min(100, int(math.floor(value + 0.5))))))
    return tuple(out)


def apply_fills(display, fills):
    """动画中间帧：把显示元组里各条进度条的填充长度换成给定浮点百分比。

    fills 的顺序与 app._display_fills 一致（5h、7d、5h、7d）；在 build_display 之后、
    画出之前调用，返回换过填充长度的新显示元组。无进度条的布局与 fills 为 None 时
    原样返回；换上的是取整后的整数百分比，元组相等仍然一定意味着画面相同。
    """
    if fills is None:
        return display
    if display.kind == "dual":
        rows = tuple(
            block._replace(rows=_with_fills(block.rows, fills, 2 * index))
            for index, block in enumerate(display.rows))
    elif display.kind == "data":
        rows = _with_fills(display.rows, fills, 0)
    else:
        return display
    return display._replace(rows=rows)


def build_display(snapshot, now, theme, scale, height, codex=None, codex_available=False, captions=None,
                  skin=DEFAULT_SKIN, fills=None):
    """把快照与当前时间整理成不可变的显示元组；元组相等就不用重绘。

    填充长度由取整后的百分比决定，这样元组相等一定意味着画面相同。宽度由内容、
    scale 与高度推出，放进元组是为了让尺寸变化也触发重绘。
    codex_available 为假时只画 Claude，结果与原来相同。为真时左 Claude、右 Codex；
    两边都没有任何窗口时仍是无数据。
    ages、captions 按提供方各一条（下标 0 是 Claude，1 是 Codex）；全空时存成 ()。
    有反馈文字时优先显示它，否则才显示年龄文字。nodata 没有进度条行可挂年龄文字，
    只保留反馈文字（单栏取 Claude 的；双栏两侧都没有窗口时 read error 优先，否则取
    第一条非空的），存成只有一项的 captions，渲染时顶替第二行 "no data yet"。
    skin 是进度条皮肤名。fills 是填充长度动画的中间值（见 _with_fills），None 表示静态。
    """
    five, seven = _window_pair(snapshot)
    if not codex_available:
        if five is None and seven is None:
            return _nodata_display(theme, scale, height, _caption_at(captions, 0), skin)
        age = _age_text(five, seven, now)
        caption = _caption_at(captions, 0)
        aside = caption or age
        rows = _with_fills(_metric_rows(five, seven, now), fills, 0)
        width = display_width("data", scale, height, aside)
        return Display(
            "data", rows, theme, scale, width, height,
            _stored_strings((age,)), _stored_strings((caption,)), skin)
    codex_five, codex_seven = _window_pair(codex)
    if five is None and seven is None and codex_five is None and codex_seven is None:
        return _nodata_display(
            theme, scale, height,
            _nodata_caption((_caption_at(captions, 0), _caption_at(captions, 1))), skin)
    blocks = (
        BlockView(PROVIDER_TAGS[0], _with_fills(_metric_rows(five, seven, now), fills, 0)),
        BlockView(PROVIDER_TAGS[1], _with_fills(_metric_rows(codex_five, codex_seven, now), fills, 2)),
    )
    ages = (
        _age_text(five, seven, now),
        _age_text(codex_five, codex_seven, now),
    )
    caption_pair = (_caption_at(captions, 0), _caption_at(captions, 1))
    width = display_width("dual", scale, height)
    return Display(
        "dual", blocks, theme, scale, width, height,
        _stored_strings(ages), _stored_strings(caption_pair), skin)


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


def _draw_rows(canvas, text_draw, font, rows, edges, metrics, text_rgb, track_rgba, row_height,
               skin, theme, origin=0.0):
    """画一个块里的两行：标签、进度条、百分比、重置时间。

    origin 为 0 时行中心与原来的单栏相同。双栏传入页眉高度，两行排在页眉下面。
    进度条交给当前皮肤的 paint_bar（充能渐变、陈旧灰、装饰都在皮肤层）。陈旧行的文字
    仍用 TEXT_ALPHA_STALE 变淡。
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
        fill_width = 0
        if row.state in ("normal", "stale"):
            fill_width = int(math.floor(row.fill * bar_width / 100.0 + 0.5))
        top = int(math.floor(center - bar_height / 2.0 + 0.5))
        _entry().paint_bar(canvas, skin, bar_left, top, bar_width, bar_height, bar_radius,
                           fill_width, row.tier, stale, theme, track_rgba, row.fill)
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
                       dual["row_height"], display.skin, theme, header_height)
    else:
        edges = pixel_columns(scale)
        _draw_rows(canvas, text_draw, font, display.rows, edges, metrics, text_rgb, track_rgba,
                   row_height, display.skin, theme)
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


def sample_display(name, theme, scale, skin=DEFAULT_SKIN):
    """样张用的显示元组。时间固定，样张在任何时区、任何日期下都一样。"""
    now = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))
    height = scaled(LOGICAL_H, scale)
    snapshot = _sample_snapshot(name, now)
    captions = _sample_captions(name, now)
    if name not in DUAL_SAMPLE_STATES and name not in EXTRA_DUAL_SAMPLE_STATES:
        return build_display(snapshot, now, theme, scale, height, captions=captions, skin=skin)
    return build_display(
        snapshot, now, theme, scale, height,
        codex=_sample_codex_snapshot(name, now), codex_available=True, captions=captions, skin=skin)


def sample_filename(index, name, theme, scale, skin=None):
    """默认皮肤保持原来的文件名；其他皮肤在名字里带上皮肤名。"""
    if skin is None:
        return "%02d_%s_%s_%.2f.png" % (index, name, theme, scale)
    return "%02d_%s_%s_%s_%.2f.png" % (index, name, skin, theme, scale)


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
    """每套皮肤下，每个样张状态在两种主题与三种 scale 下各出一张 RGBA PNG。

    默认皮肤的文件名与 contact_sheet.png 与原来完全一致（测试按名字核对）；其他皮肤
    在文件名里带上皮肤名，图拼成 contact_sheet_<皮肤>.png。
    """
    os.makedirs(out_dir, exist_ok=True)
    for skin in SKINS:
        default = skin == DEFAULT_SKIN
        rows = []
        for index, name in enumerate(ALL_SAMPLE_STATES, 1):
            images = []
            for theme, scale in SAMPLE_COMBOS:
                image = _entry().render_display(sample_display(name, theme, scale, skin))
                path = os.path.join(out_dir, sample_filename(index, name, theme, scale, None if default else skin))
                image.save(path)
                _entry().say(path)
                images.append(image)
            rows.append((name, images))
        sheet_path = os.path.join(out_dir, "contact_sheet%s.png" % ("" if default else "_" + skin))
        compose_contact_sheet(rows).save(sheet_path)
        _entry().say(sheet_path)
    return 0
