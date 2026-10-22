# README artwork

[English README](../../README.md) | [中文说明](../../README.zh-CN.md)

These PNGs are **rendered previews, not desktop screenshots**. They use the
application's actual `build_display`, `build_rings_display`, and `render_display`
functions, with invented usage values and a fixed sample clock. Background cards
and headings are presentation artwork, not extra widget UI. The widget is enlarged
for readability, so the images do not represent its physical on-screen size.

| Asset | Content | Dimensions |
|---|---|---|
| `overview.png` | Claude and Codex full bars | 1440 × 610 |
| `display-modes.png` | Full bar and Rings at identical render scale | 1440 × 750 |
| `bar-skins.png` | Neon Minimal and Mecha Tech full-bar skins | 1440 × 660 |

All images use the same sample readings: Claude **70% / 24% remaining** and Codex
**97% / 64% remaining**, for 5h / 7d respectively. Both providers show sample reset
labels `16:50` and `Sun`. The grey unfilled part of a fresh bar is the empty track,
not a stale reading. In Rings, the inner ring is 5h and the outer ring is 7d.

## Regenerate on Windows

From the repository root, use an existing Python installation with Pillow:

```powershell
python -B docs\render_previews.py
```

The script requires the Windows Segoe UI fonts and overwrites only the three
generated PNGs in this folder. It does not launch the app or read `data`, Codex
logs, account information, or credentials. It sends no requests and does not
change the widget's settings. Python and Pillow are the same dependencies used
by the app; no extra design package is required.

The images are stored in the repository and referenced with relative paths, so
they work in GitHub branch previews without a separate image host. The current
release packaging script does not bundle `docs` or the translated README; release
ZIP readers can use the online-documentation link at the top of the main README.

These documentation assets are covered by the repository's [MIT license](../../LICENSE).
