"""Build README artwork from the real widget renderer and invented sample data.

Run from the repository root with Python and Pillow. This module never starts
the app, reads runtime data or credentials, or sends a refresh request.
"""

from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import usage_widget as widget

OUT = ROOT / "docs" / "images"
BG = "#0d1117"
PANEL = "#161d28"
EDGE = "#2b3545"
TEXT = "#f0f4fa"
MUTED = "#a9b6c8"
ACCENT = "#83c9ee"
WIDTH = 1440
FONT_ROOT = Path("C:/Windows/Fonts")


def font(size, bold=False):
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    path = FONT_ROOT / name
    if not path.is_file():
        raise RuntimeError("README artwork requires the Windows Segoe UI fonts")
    return ImageFont.truetype(str(path), size)


def canvas(height):
    return Image.new("RGBA", (WIDTH, height), BG)


def label(draw, xy, text, size=24, fill=TEXT, bold=False):
    draw.text(xy, text, font=font(size, bold), fill=fill)


def card(draw, box, fill=PANEL):
    draw.rounded_rectangle(box, radius=18, fill=fill, outline=EDGE, width=2)


def demo(mode="bar", skin="neon", scale=2.5):
    now = time.mktime((2026, 10, 2, 15, 0, 0, 0, 0, -1))
    reset_5h = time.mktime((2026, 10, 2, 16, 50, 0, 0, 0, -1))
    reset_7d = time.mktime((2026, 10, 4, 12, 0, 0, 0, 0, -1))
    claude = {
        "five_hour": widget.Win(30.0, reset_5h, now - 60),
        "seven_day": widget.Win(76.0, reset_7d, now - 60),
    }
    codex = {
        "five_hour": widget.Win(3.0, reset_5h, now - 60),
        "seven_day": widget.Win(36.0, reset_7d, now - 60),
    }
    build = widget.build_rings_display if mode == "rings" else widget.build_display
    display = build(
        claude, now, "dark", scale, widget.scaled(widget.LOGICAL_H, scale),
        codex=codex, codex_available=True, skin=skin,
    )
    return widget.render_display(display)


def save(image, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    image.convert("RGB").save(path, optimize=True)
    print(f"{name}: {image.width} x {image.height}, {path.stat().st_size:,} bytes")


def overview():
    image = canvas(610)
    draw = ImageDraw.Draw(image)
    label(draw, (72, 42), "CLAUDE + CODEX  /  WINDOWS 11", 22, ACCENT, True)
    label(draw, (1145, 45), "RENDERED DEMO", 19, MUTED)
    label(draw, (70, 98), "Your usage. Always in view.", 66, bold=True)
    label(draw, (73, 190), "5-hour and weekly limits, right beside your system tray.", 29, MUTED)
    card(draw, (70, 274, 1370, 448))
    bars = demo(scale=2.75)
    image.alpha_composite(bars, ((WIDTH - bars.width) // 2, 307))
    for x, heading, detail in (
        (74, "Transparent overlay", "Your taskbar shows through"),
        (530, "Full bar or compact rings", "Auto-fit when space gets tight"),
        (1000, "Local-first", "No credential scraping"),
    ):
        label(draw, (x, 484), heading, 25, TEXT, True)
        label(draw, (x, 525), detail, 21, MUTED)
    label(draw, (74, 576), "Real widget renderer. Sample data. Enlarged for readability.", 17, MUTED)
    save(image, "overview.png")


def display_modes():
    image = canvas(750)
    draw = ImageDraw.Draw(image)
    label(draw, (70, 44), "One widget. Two ways to read it.", 49, bold=True)
    label(draw, (73, 116), "Same sample readings. Same render scale. Less space in Rings.", 27, MUTED)
    for top, mode, title, hint in (
        (202, "bar", "Full bar", "432 px wide at 100% display scaling"),
        (412, "rings", "Rings", "258 px wide at 100% display scaling"),
    ):
        label(draw, (73, top - 6), title, 30, TEXT, True)
        label(draw, (310, top + 1), hint, 23, MUTED)
        card(draw, (70, top + 49, 1370, top + 177))
        image.alpha_composite(demo(mode=mode, scale=2.3), (124, top + 66))
    label(draw, (74, 628), "AUTO", 22, ACCENT, True)
    label(draw, (180, 624), "Full bar when there is room. Rings when crowded. Hidden if neither fits.", 25)
    label(draw, (74, 674), "Inner ring: 5h. Outer ring: 7d. Percentages show the allowance remaining.", 23, MUTED)
    label(draw, (74, 718), "Real widget renderer. Sample data. Enlarged for readability.", 17, MUTED)
    save(image, "display-modes.png")


def bar_skins():
    image = canvas(660)
    draw = ImageDraw.Draw(image)
    label(draw, (70, 44), "Pick your progress-bar style.", 49, bold=True)
    label(draw, (73, 116), "Two built-in skins. Identical sample readings.", 27, MUTED)
    for top, skin, title in (
        (202, "neon", "Neon Minimal  /  default"),
        (402, "mecha", "Mecha Tech"),
    ):
        label(draw, (73, top - 6), title, 29, TEXT, True)
        card(draw, (70, top + 46, 1370, top + 172))
        image.alpha_composite(demo(skin=skin, scale=2.3), (124, top + 62))
    label(draw, (74, 600), "Right-click > Progress Bar Skin. Ring rendering is shared by both skins.", 23, MUTED)
    label(draw, (74, 630), "Real widget renderer. Sample data. Enlarged for readability.", 17, MUTED)
    save(image, "bar-skins.png")


if __name__ == "__main__":
    overview()
    display_modes()
    bar_skins()
