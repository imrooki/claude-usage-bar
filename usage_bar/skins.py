# -*- coding: utf-8 -*-
"""皮肤层：用量等级色与两套进度条皮肤（Neon Minimal、Mecha Tech）。

从 usage_widget.py 拆出的模块之一。测试对 usage_widget 打的补丁经 _entry() 取回
入口模块，所以这里不直接引用被测试替换的名字（要画的基础条经 _entry()._compose_bar）。

等级按"已用百分比"分四档（见 usage_level）：<40 安全、40 到 <70 温和、70 到 <90
注意、>=90 危险；无效读数（None、非数字、非有限、负数）没有等级，只画空槽。

两套皮肤都通过 paint_bar 画进整窗画布：
  - Neon Minimal：圆角胶囊空槽、按等级着色的渐变填充、端点高光点、外圈淡辉光；
  - Mecha Tech：切角机匣、内凹槽、按等级着色的分段金属渐变填充、填充前缘亮边。
陈旧读数两套皮肤都画中性灰平填充（与旧行为一致），无数据只画空槽。
"""

import functools
import math
import sys

from PIL import Image, ImageDraw

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


def usage_level(pct):
    """已用百分比 -> 等级名；无效读数返回 none。

    分档：<40 安全、40 到 <70 温和、70 到 <90 注意、>=90 危险（边界含在上一档）。
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
    if value >= 90.0:
        return LEVEL_CRITICAL
    if value >= 70.0:
        return LEVEL_WARNING
    if value >= 40.0:
        return LEVEL_MODERATE
    return LEVEL_SAFE


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
              fill_width, level, stale, theme, track_rgba):
    """按皮肤把一条进度条画到整窗画布上。

    bar_left/top 是进度条左上角在画布里的位置；fill_width 是填充的像素宽（0 表示
    未知或重置，只画空槽）；level 是 usage_level 的结果；stale 为真时画中性灰。
    辉光画在进度条矩形之外，所以由这里直接合成到画布，而不是拼进条内。
    """
    if bar_width <= 0 or bar_height <= 0:
        return
    if skin == SKIN_MECHA:
        image = _mecha_bar(bar_width, bar_height, bar_radius, fill_width, level, stale, theme, track_rgba)
        canvas.paste(image, (bar_left, top))
        return
    image = _neon_bar(bar_width, bar_height, bar_radius, fill_width, level, stale, theme, track_rgba)
    if (not stale and level in LEVEL_RGB
            and 0 < min(fill_width, bar_width) <= bar_width):
        glow = _neon_glow(LEVEL_RGB[level], bar_width, bar_height, bar_radius)
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
_NEON_RIM_ALPHA = 70


def _neon_bar(bar_width, bar_height, bar_radius, fill_width, level, stale, theme, track_rgba):
    """一条 Neon Minimal 进度条（含轨道）；x 坐标为条内坐标。"""
    fill_width = max(0, min(fill_width, bar_width))
    if stale:
        fill_rgba = STALE_FILL_RGB + (FILL_ALPHA,)
    elif level in LEVEL_RGB and fill_width > 0:
        fill_rgba = LEVEL_RGB[level] + (FILL_ALPHA,)
    else:
        fill_rgba = None
    image = _entry()._compose_bar(bar_width, bar_height, bar_radius, fill_width, track_rgba, fill_rgba)
    if stale or fill_rgba is None or fill_width <= 0:
        return image
    # 有装饰：_compose_bar 的结果是缓存对象，只能读，所以先复制再画。
    image = image.copy()
    if level in LEVEL_RGB:
        fill_mask = _rounded_mask(fill_width, bar_height, bar_radius)
        image.paste(_neon_fill_template(LEVEL_RGB[level], fill_width, bar_height), (0, 0), fill_mask)
    rim = _neon_rim(LEVEL_RGB.get(level, (255, 255, 255)), bar_width, bar_height, bar_radius)
    if rim is not None:
        image.alpha_composite(rim)
    return image


@functools.lru_cache(maxsize=64)
def _neon_fill_template(rgb, width, height):
    """横向渐变乘纵向光泽的填充色模板（alpha 255，只用于按填充遮罩 paste）。"""
    out = bytearray(width * height * 4)
    for y in range(height):
        v = _NEON_FACTOR_V_TOP + (_NEON_FACTOR_V_BOTTOM - _NEON_FACTOR_V_TOP) * (
            y / (height - 1.0) if height > 1 else 0.0)
        for x in range(width):
            t = x / (width - 1.0) if width > 1 else 0.0
            factor = (_NEON_FACTOR_H + 0.18 * math.sin(math.pi * t)) * v
            base = (y * width + x) * 4
            out[base] = min(255, int(round(rgb[0] * factor)))
            out[base + 1] = min(255, int(round(rgb[1] * factor)))
            out[base + 2] = min(255, int(round(rgb[2] * factor)))
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


# ---------------------------------------------------------------------------
# Mecha Tech
# ---------------------------------------------------------------------------

_MECHA_SS = 4
_MECHA_CHASSIS = (44, 49, 58)
_MECHA_CHASSIS_ALPHA = 238
_MECHA_RIM = (96, 104, 118)
_MECHA_TOP = (128, 138, 156)
_MECHA_TICK = (150, 160, 176)
_MECHA_DIVIDER_ALPHA = 130
_MECHA_RECESS = {"light": (0, 0, 0, 80), "dark": (0, 0, 0, 110)}


def _mecha_bar(bar_width, bar_height, bar_radius, fill_width, level, stale, theme, track_rgba):
    """一条 Mecha Tech 进度条：切角机匣、内凹槽、槽内按等级着色填充。"""
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
    elif level in LEVEL_RGB and fill_width > 0:
        fill_rgba = LEVEL_RGB[level] + (FILL_ALPHA,)
    else:
        fill_rgba = None
    channel_fill = 0
    if fill_rgba is not None:
        channel_fill = min(ch_width, int(math.floor(
            min(fill_width, bar_width) * ch_width / float(bar_width) + 0.5)))
    bar = _entry()._compose_bar(ch_width, ch_height, ch_radius, channel_fill,
                                (0, 0, 0, 0), fill_rgba)
    image.alpha_composite(bar, (ch_left, ch_top))
    if channel_fill > 0 and not stale and level in LEVEL_RGB:
        image.alpha_composite(
            _mecha_fill_decor(LEVEL_RGB[level], ch_width, ch_height, channel_fill, ch_radius),
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
    """槽内填充的金属渐变、斜向分段刻线与前缘亮边（只覆盖填充区）。"""
    mask = _rounded_mask(width, height, radius)
    mask_data = mask.tobytes()
    seg = max(3, height + 1)
    out = bytearray(width * height * 4)
    for y in range(height):
        v = 0.88 + 0.20 * (y / (height - 1.0) if height > 1 else 0.0)
        for x in range(min(fill_width, width)):
            m = mask_data[y * width + x]
            if m == 0:
                continue
            factor = v * 0.82 if (x + y) % seg == 0 else v
            if x >= fill_width - 1:
                factor = min(1.35, factor * 1.35)
            base = (y * width + x) * 4
            out[base] = min(255, int(round(rgb[0] * factor)))
            out[base + 1] = min(255, int(round(rgb[1] * factor)))
            out[base + 2] = min(255, int(round(rgb[2] * factor)))
            out[base + 3] = m
    return Image.frombytes("RGBA", (width, height), bytes(out))
