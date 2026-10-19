# -*- coding: utf-8 -*-
"""皮肤层：用量等级色与两套进度条皮肤（Neon Minimal、Mecha Tech）。

从 usage_widget.py 拆出的模块之一。测试对 usage_widget 打的补丁经 _entry() 取回
入口模块，所以这里不直接引用被测试替换的名字（要画的基础条经 _entry()._compose_bar）。

等级按"已用百分比"分四档（见 usage_level）：<40 安全、40 到 <70 温和、70 到 <80
注意、>=80 危险；无效读数（None、非数字、非有限、负数）没有等级，只画空槽。

两套皮肤都通过 paint_bar 画进整窗画布：
  - Neon Minimal：圆角胶囊空槽、充能态连续渐变填充（起点偏暗、笔尖最亮）、
    推进笔尖高光与局部辉光、外圈淡辉光；
  - Mecha Tech：切角机匣、内凹槽、充能态金属渐变填充（起点偏暗、笔尖高光）。
填充基色只由已用百分比决定（fill_rgb 的整条连续渐变，0% 浅绿 -> 50% 蓝 ->
80% 红 -> 100% 最浓的红），等级只用于分档判断与文字，不再决定填充色。
陈旧读数两套皮肤都画中性灰平填充（与旧行为一致），无数据只画空槽。
"""

import functools
import math
import sys

from PIL import Image, ImageDraw, ImageFilter

from usage_bar.data import (
    DEFAULT_SKIN,
    SKIN_LABELS,
    SKIN_MECHA,
    SKIN_NEON,
    SKINS,
)


def _entry():
    """入口模块（usage_widget 或 __main__）：测试对入口模块打的补丁必须在这里仍然可见。"""
    return sys.modules.get("usage_widget") or sys.modules["__main__"]


# ---------------------------------------------------------------------------
# 用量等级
# ---------------------------------------------------------------------------

LEVEL_SAFE = "safe"
LEVEL_MODERATE = "moderate"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"
LEVEL_NONE = "none"

LEVEL_RGB = {
    LEVEL_SAFE: (0x6F, 0xA5, 0x81),
    LEVEL_MODERATE: (0x74, 0xA1, 0xE8),
    LEVEL_WARNING: (0xE8, 0xB8, 0x6D),
    LEVEL_CRITICAL: (0xE9, 0x83, 0x86),
}

FILL_ALPHA = 255
# 陈旧行的进度条填充用固定中性灰，alpha 与正常行相同。
STALE_FILL_RGB = (0x8A, 0x8A, 0x8A)

# 填充基色的整条渐变锚点：0% 浅绿 -> 50% 蓝 -> 80% 红 -> 100% 最浓的红。
# 中段两处锚点与 LEVEL_RGB 的 moderate / critical 同色，段内线性过渡、越界夹到两端。
FILL_STOPS = (
    (0.0, (0xBE, 0xD6, 0xC6)),
    (50.0, (0x74, 0xA1, 0xE8)),
    (80.0, (0xE9, 0x83, 0x86)),
    (100.0, (0xC8, 0x4C, 0x52)),
)

# 充能态：填充从起点压暗到笔尖全亮，笔尖再叠高光（两套皮肤共用口径）。
FILL_HEAD_DIM = 0.28
FILL_TIP_CAP = 0.45


def usage_level(pct):
    """已用百分比 -> 等级名；无效读数返回 none。

    分档：<40 安全、40 到 <70 温和、70 到 <80 注意、>=80 危险（边界含在上一档）。
    布尔、非数字、非有限数、负数都是无效读数。
    """
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return LEVEL_NONE
    try:
        value = float(pct)
    except OverflowError:
        # 超大整数转不了 float，按无效处理
        return LEVEL_NONE
    if not math.isfinite(value) or value < 0:
        return LEVEL_NONE
    if value >= 80.0:
        return LEVEL_CRITICAL
    if value >= 70.0:
        return LEVEL_WARNING
    if value >= 40.0:
        return LEVEL_MODERATE
    return LEVEL_SAFE


def fill_rgb(pct):
    """已用百分比 -> 填充基色；无效读数返回 None（与 usage_level 同口径）。

    整条按 FILL_STOPS 连续渐变：0% 浅绿、50% 蓝、80% 红、100% 最浓的红；
    段内线性过渡，越界值夹到两端。填充的充能感（起点压暗、笔尖高光与辉光）
    由两套皮肤在基色之上叠加。
    """
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    try:
        value = float(pct)
    except OverflowError:
        return None
    if not math.isfinite(value):
        return None
    value = min(100.0, max(0.0, value))
    if value <= FILL_STOPS[0][0]:
        return FILL_STOPS[0][1]
    for (left, rgb_left), (right, rgb_right) in zip(FILL_STOPS, FILL_STOPS[1:]):
        if value <= right:
            weight = (value - left) / (right - left)
            return tuple(int(round(a + (b - a) * weight))
                         for a, b in zip(rgb_left, rgb_right))
    return FILL_STOPS[-1][1]


# ---------------------------------------------------------------------------
# 共用：圆角遮罩
# ---------------------------------------------------------------------------

_MASK_SS = 4


@functools.lru_cache(maxsize=512)
def _rounded_mask(width, height, radius):
    """圆角矩形遮罩：先放大 _MASK_SS 倍再缩回，边缘才平滑。结果只读。"""
    width = max(1, int(width))
    height = max(1, int(height))
    big = Image.new("L", (width * _MASK_SS, height * _MASK_SS), 0)
    corner = min(radius, width / 2.0, height / 2.0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, width * _MASK_SS - 1, height * _MASK_SS - 1),
        radius=int(math.floor(corner * _MASK_SS + 0.5)), fill=255)
    return big.resize((width, height), Image.Resampling.BOX)


def paint_bar(canvas, skin, bar_left, top, bar_width, bar_height, bar_radius,
              fill_width, level, stale, theme, track_rgba, fill_pct=None):
    """按皮肤把一条进度条画到整窗画布上。

    bar_left/top 是进度条左上角在画布里的位置；fill_width 是填充的像素宽（0 表示
    未知或重置，只画空槽）；level 是 usage_level 的结果；stale 为真时画中性灰。
    fill_pct 是已用百分比（动画中可带小数），决定填充基色；缺省时按 fill_width
    反推。填充本身呈充能态（起点偏暗、推进笔尖最亮），笔尖的局部辉光画在进度条
    矩形之外，所以由这里直接合成到画布，而不是拼进条内。
    """
    if bar_width <= 0 or bar_height <= 0:
        return
    if fill_pct is None:
        fill_pct = fill_width * 100.0 / bar_width
    base_rgb = None if stale else fill_rgb(fill_pct)
    if skin == SKIN_MECHA:
        image = _mecha_bar(bar_width, bar_height, bar_radius, fill_width, level,
                           stale, theme, track_rgba, base_rgb)
        canvas.paste(image, (bar_left, top))
        return
    image = _neon_bar(bar_width, bar_height, bar_radius, fill_width, level,
                      stale, theme, track_rgba, base_rgb)
    if (not stale and base_rgb is not None and level in LEVEL_RGB
            and 0 < min(fill_width, bar_width) <= bar_width):
        tip = _neon_tip_glow(base_rgb, bar_height)
        canvas.paste(tip, (bar_left + min(fill_width, bar_width) - tip.width // 2,
                           top - (tip.height - bar_height) // 2), tip)
        glow = _neon_glow(base_rgb, bar_width, bar_height, bar_radius)
        canvas.paste(glow, (bar_left - (glow.width - bar_width) // 2,
                            top - (glow.height - bar_height) // 2))
    canvas.paste(image, (bar_left, top))


# ---------------------------------------------------------------------------
# Neon Minimal
# ---------------------------------------------------------------------------

# 外圈辉光的淡 alpha；横向渐变与纵向光泽的系数（压低高光，颜色更沉）。
_NEON_FACTOR_H = 0.88
_NEON_FACTOR_V_TOP = 1.05
_NEON_FACTOR_V_BOTTOM = 0.95
_NEON_GLOW_ALPHA = 34
_NEON_TIP_GLOW_ALPHA = 110
_NEON_RIM_ALPHA = 70


def _neon_bar(bar_width, bar_height, bar_radius, fill_width, level, stale, theme,
              track_rgba, fill_base_rgb):
    """一条 Neon Minimal 进度条（含轨道）；x 坐标为条内坐标。"""
    fill_width = max(0, min(fill_width, bar_width))
    if stale:
        fill_rgba = STALE_FILL_RGB + (FILL_ALPHA,)
    elif level in LEVEL_RGB and fill_width > 0 and fill_base_rgb is not None:
        fill_rgba = fill_base_rgb + (FILL_ALPHA,)
    else:
        fill_rgba = None
    image = _entry()._compose_bar(bar_width, bar_height, bar_radius, fill_width, track_rgba, fill_rgba)
    if stale or fill_rgba is None or fill_width <= 0:
        return image
    # 有装饰：_compose_bar 的结果是缓存对象，只能读，所以先复制再画。
    image = image.copy()
    fill_mask = _rounded_mask(fill_width, bar_height, bar_radius)
    image.paste(_neon_fill_template(fill_base_rgb, fill_width, bar_height), (0, 0), fill_mask)
    rim = _neon_rim(fill_base_rgb, bar_width, bar_height, bar_radius)
    if rim is not None:
        image.alpha_composite(rim)
    return image


@functools.lru_cache(maxsize=64)
def _neon_fill_template(rgb, width, height):
    """填充色模板：充能渐变（起点暗、笔尖亮）乘纵向光泽，笔尖再加一段高光。

    alpha 255，只用于按填充遮罩 paste；width 是填充宽，模板最右端就是推进笔尖。
    """
    out = bytearray(width * height * 4)
    cap = min(max(2, width // 4), 10)
    for y in range(height):
        v = _NEON_FACTOR_V_TOP + (_NEON_FACTOR_V_BOTTOM - _NEON_FACTOR_V_TOP) * (
            y / (height - 1.0) if height > 1 else 0.0)
        for x in range(width):
            t = x / (width - 1.0) if width > 1 else 0.0
            factor = (_NEON_FACTOR_H + 0.18 * math.sin(math.pi * t)) * v
            factor *= 1.0 - FILL_HEAD_DIM * (1.0 - t)
            cap_t = 0.0
            if x >= width - cap:
                cap_t = (x - (width - cap)) / float(max(1, cap - 1))
            base = (y * width + x) * 4
            for channel in range(3):
                value = rgb[channel] * factor
                value += (255.0 - value) * (FILL_TIP_CAP * cap_t)
                out[base + channel] = min(255, int(round(value)))
            out[base + 3] = 255
    return Image.frombytes("RGBA", (width, height), bytes(out))


@functools.lru_cache(maxsize=64)
def _neon_rim(rgb, bar_width, bar_height, bar_radius):
    """空槽内圈的一条同色淡描边；太小的条返回 None。"""
    if bar_width < 3 or bar_height < 3:
        return None
    outer = _rounded_mask(bar_width, bar_height, bar_radius)
    inner = _rounded_mask(bar_width - 2, bar_height - 2, max(0, bar_radius - 1))
    ring = outer.copy()
    ring.paste(0, (1, 1), inner)
    rim = Image.new("RGBA", (bar_width, bar_height), rgb + (0,))
    rim.putalpha(ring.point(lambda value: value * _NEON_RIM_ALPHA // 255))
    return rim


@functools.lru_cache(maxsize=64)
def _neon_glow(rgb, bar_width, bar_height, bar_radius):
    """胶囊外一圈淡辉光（含透明中孔），贴到画布上时以条中心对齐。"""
    margin = max(1, min(3, bar_height // 3))
    outer = _rounded_mask(bar_width + 2 * margin, bar_height + 2 * margin, bar_radius + margin)
    inner = _rounded_mask(bar_width, bar_height, bar_radius)
    ring = outer.copy()
    ring.paste(0, (margin, margin), inner)
    glow = Image.new("RGBA", ring.size, rgb + (0,))
    glow.putalpha(ring.point(lambda value: value * _NEON_GLOW_ALPHA // 255))
    return glow


@functools.lru_cache(maxsize=128)
def _neon_tip_glow(rgb, bar_height):
    """推进笔尖的局部光斑（充能感的辉光），贴到画布上时以笔尖为中心对齐。"""
    margin = max(2, bar_height // 2)
    width = max(8, bar_height * 2)
    height = bar_height + 2 * margin
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (width * 0.2, 1, width * 0.8, height - 2), fill=rgb + (_NEON_TIP_GLOW_ALPHA,))
    return glow.filter(ImageFilter.GaussianBlur(max(2.0, bar_height * 0.35)))


# ---------------------------------------------------------------------------
# Mecha Tech
# ---------------------------------------------------------------------------

_MECHA_SS = 4
_MECHA_CHASSIS = (108, 116, 130)
_MECHA_CHASSIS_ALPHA = 238
_MECHA_RIM = (146, 156, 172)
_MECHA_TOP = (146, 156, 172)
_MECHA_TICK = (178, 186, 200)
_MECHA_DIVIDER_ALPHA = 130
_MECHA_RECESS = {"light": (0, 0, 0, 80), "dark": (0, 0, 0, 110)}


def _mecha_bar(bar_width, bar_height, bar_radius, fill_width, level, stale, theme,
               track_rgba, fill_base_rgb):
    """一条 Mecha Tech 进度条：切角机匣、内凹槽、槽内充能态渐变填充。"""
    image = _mecha_chassis(bar_width, bar_height)
    cap = max(2, min(4, bar_height // 2))
    ch_left = cap
    ch_right = max(ch_left + 1, bar_width - cap)
    ch_width = ch_right - ch_left
    ch_height = max(1, bar_height - 2)
    ch_top = (bar_height - ch_height) // 2
    ch_radius = max(0, bar_radius - 1)
    recess = Image.new("RGBA", (ch_width, ch_height), _MECHA_RECESS.get(theme, _MECHA_RECESS["light"]))
    image.paste(recess, (ch_left, ch_top), _rounded_mask(ch_width, ch_height, ch_radius))
    _mecha_caps(image, bar_width, bar_height, cap, ch_top, ch_height)
    if stale:
        fill_rgba = STALE_FILL_RGB + (FILL_ALPHA,)
    elif level in LEVEL_RGB and fill_width > 0 and fill_base_rgb is not None:
        fill_rgba = fill_base_rgb + (FILL_ALPHA,)
    else:
        fill_rgba = None
    channel_fill = 0
    if fill_rgba is not None:
        channel_fill = min(ch_width, int(math.floor(
            min(fill_width, bar_width) * ch_width / float(bar_width) + 0.5)))
    bar = _entry()._compose_bar(ch_width, ch_height, ch_radius, channel_fill,
                                (0, 0, 0, 0), fill_rgba)
    image.alpha_composite(bar, (ch_left, ch_top))
    if channel_fill > 0 and not stale and fill_base_rgb is not None:
        image.alpha_composite(
            _mecha_fill_decor(fill_base_rgb, ch_width, ch_height, channel_fill, ch_radius),
            (ch_left, ch_top))
    return image


def _mecha_chassis(bar_width, bar_height):
    """切角八角机匣加描边与顶部高光线，四倍超采样后缩回。"""
    ss = _MECHA_SS
    big = Image.new("RGBA", (bar_width * ss, bar_height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    w, h = bar_width, bar_height
    cut = max(1, min(h // 2, int(math.floor(h * 0.35 + 0.5))))
    points = [
        (cut, 0), (w - cut - 1, 0), (w - 1, cut), (w - 1, h - cut - 1),
        (w - cut - 1, h - 1), (cut, h - 1), (0, h - cut - 1), (0, cut),
    ]
    outline = [(x * ss, y * ss) for x, y in points]
    draw.polygon(outline, fill=_MECHA_CHASSIS + (_MECHA_CHASSIS_ALPHA,))
    cap = max(2, min(4, h // 2))
    if cap < w - cap:
        for cap_points in (
            [(0, cut * ss), (cut * ss, 0), (cap * ss - 1, 0),
             (cap * ss - 1, (h - 1) * ss), (cut * ss, (h - 1) * ss),
             (0, (h - cut - 1) * ss)],
            [((w - 1) * ss, cut * ss), ((w - cut - 1) * ss, 0),
             ((w - cap) * ss, 0), ((w - cap) * ss, (h - 1) * ss),
             ((w - cut - 1) * ss, (h - 1) * ss), ((w - 1) * ss, (h - cut - 1) * ss)],
        ):
            draw.polygon(cap_points, fill=_MECHA_RIM + (255,))
    draw.line(outline + [outline[0]], fill=_MECHA_RIM + (255,), width=ss)
    if h >= 5:
        draw.line([(cut * ss, ss // 2), ((w - cut) * ss, ss // 2)],
                  fill=_MECHA_TOP + (255,), width=ss)
    return big.resize((bar_width, bar_height), Image.Resampling.BOX)


def _mecha_caps(image, bar_width, bar_height, cap, ch_top, ch_height):
    """左右端盖：分隔线加一条中央亮刻线。"""
    draw = ImageDraw.Draw(image)
    for x in (cap - 1, bar_width - cap):
        if 0 <= x < bar_width:
            draw.line([(x, ch_top), (x, ch_top + ch_height - 1)],
                      fill=_MECHA_CHASSIS + (255,))
    if cap >= 3:
        y = bar_height // 2
        width = cap - 2
        draw.line([(1, y), (1 + width - 1, y)], fill=_MECHA_TICK + (255,))
        draw.line([(bar_width - 1 - width, y), (bar_width - 2, y)], fill=_MECHA_TICK + (255,))


def _mecha_fill_decor(rgb, width, height, fill_width, radius):
    """槽内填充：金属纵向光泽、斜向分段刻线、充能渐变与笔尖高光（只覆盖填充区）。"""
    mask = _rounded_mask(width, height, radius)
    mask_data = mask.tobytes()
    seg = max(3, height + 1)
    span = max(1.0, fill_width - 1.0)
    cap = min(max(2, fill_width // 4), 10)
    center_y = (height - 1) / 2.0
    bloom_radius = max(1.0, height * 1.1)
    out = bytearray(width * height * 4)
    for y in range(height):
        v = 0.88 + 0.20 * (y / (height - 1.0) if height > 1 else 0.0)
        for x in range(min(fill_width, width)):
            m = mask_data[y * width + x]
            if m == 0:
                continue
            factor = v * 0.82 if (x + y) % seg == 0 else v
            factor *= 1.0 - FILL_HEAD_DIM * (1.0 - x / span)
            cap_t = 0.0
            if x >= fill_width - cap:
                cap_t = (x - (fill_width - cap)) / float(max(1, cap - 1))
            dx = x - (fill_width - 1)
            dy = (y - center_y) * 2.0
            distance = math.sqrt(dx * dx + dy * dy)
            bloom = 0.0
            if distance < bloom_radius:
                bloom = 0.5 * FILL_TIP_CAP * (1.0 - distance / bloom_radius)
            highlight = min(1.0, FILL_TIP_CAP * cap_t + bloom)
            base = (y * width + x) * 4
            for channel in range(3):
                value = rgb[channel] * factor
                value += (255.0 - value) * highlight
                out[base + channel] = min(255, int(round(value)))
            out[base + 3] = m
    return Image.frombytes("RGBA", (width, height), bytes(out))
