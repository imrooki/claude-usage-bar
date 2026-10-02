# claude-usage-bar

A small transparent widget for the Windows 11 taskbar that keeps your **Claude plan usage** in view: the 5-hour and the 7-day rate-limit windows, drawn as two horizontal bars with the percentage and the reset time. It sits right next to the notification area (to the left of the `^` button) and has no background, so your taskbar shows through.

```
 5h [########--]  65%  16:50
 7d [###-------]  33%  Sun
```

Bars are green below 50 %, amber from 50 % to 80 %, and red from 80 % up.

## How it works

```
Claude Code session (desktop app Code tab, or terminal)
  |   usage-feed plugin (a Claude Code "mod"): on session start, after every turn,
  |   and whenever a rate-limit window moves by a whole percentage point, it takes
  |   the usage figures Claude Code itself reports and merges them into data/usage.json
  v
usage_widget.py: reads data/usage.json every 15 s and draws the two bars
```

- The numbers come only from Claude Code's documented [mods API](https://code.claude.com/docs/en/plugins/mods/overview). The project never reads your login token, never calls an unofficial endpoint and adds no requests of its own.
- Numbers change only while a Claude Code session is active, because they come from the last API response of a session. After 30 minutes without an update the bars fade and an age such as `45m` appears. Once a window's reset time has passed its row shows `reset` until fresh data arrives.
- With several sessions open, the plugin merges readings instead of blindly overwriting: inside one window period the usage can only go up, so an idle session's older reading does not replace a newer one, and a later reset time means a new period. There is no file lock, so in a rare race one update can be lost until the next event.

## Requirements

- Windows 11. Developed and tested on build 26100 with the taskbar at the bottom and 100 % display scaling. Only the primary taskbar is handled, and only a bottom or top taskbar.
- Python 3 with [Pillow](https://pypi.org/project/pillow/) (developed with Python 3.13 and Pillow 11). The widget uses nothing else outside the standard library.
- Claude Code with mod support. Claude Code's documentation says mods need v2.1.287 or later; this project was tested in the Claude desktop app's Code tab with Claude Code 2.1.286.
- A Claude Pro or Max plan. Rate-limit windows are only reported for subscription plans.

## Install

Clone the repository and create the folder that will hold the snapshot. It is git-ignored, so it does not exist after cloning.

```powershell
git clone https://github.com/imrooki/claude-usage-bar.git
cd claude-usage-bar
mkdir data
pip install pillow
```

Register the local plugin marketplace and install the plugin. `dataDir` must be the full path of the `data` folder you just created, written with forward slashes.

```powershell
claude plugin marketplace add ./plugins
claude plugin install usage-feed@usage-bar-local --config dataDir=C:/full/path/to/claude-usage-bar/data
```

The plugin accepts only a local absolute path. It refuses relative paths, network (UNC) paths and any path containing `onedrive`, and then writes nothing at all.

Open a new Claude Code session (or run `/reload-plugins` in an open one), then start the widget:

```powershell
pythonw usage_widget.py
```

By default the widget reads `data/usage.json` next to the script. To use another folder pass `--data-dir <folder>`; it has to be the same folder you gave the plugin.

To start it at login, put a shortcut in your Startup folder (press Win+R and run `shell:startup`) whose target is `pythonw.exe` and whose arguments are the full path of `usage_widget.py` in quotes. The widget runs as a single instance, so a second start exits at once.

## Using the widget

- **Drag** it sideways to move it. It then stays where you put it.
- **Right-click** for the menu: `Refresh now`, `Snap to tray` (go back to following the notification area automatically) and `Quit`.
- It hides itself while a full-screen app is running and while the taskbar is set to auto-hide.

Known limitations:

- Windows 11 does not let ordinary windows live inside the notification area, so the widget sits beside it, not inside.
- After the right-click menu closes, keyboard focus stays on the widget until you click another window.
- Only the primary monitor's taskbar is handled.

## Design notes

- The widget is a separate top-level layered window with per-pixel transparency. It never attaches itself to the taskbar and never injects anything into Explorer, because a cross-process parent/child window would tie the two processes' input handling together. To follow the notification area it only reads the window rectangle of `TrayNotifyWnd` (read-only queries, every 2 s), so it moves when tray icons come and go.
- The widget makes no network connections and starts no subprocesses. It reads one registry value (the light/dark setting) and writes only `widget_pos.json` and a small error log (capped at 64 KB) into the data folder.
- The plugin calls five Claude Code APIs (`session.usage`, `session.id`, `clock.now`, `fs.read`, `fs.write`), hooks only `session.start` and `session.measure`, and writes only `usage.json` in the folder you configured. Every hook runs the engine's own handling first, and a failure inside the plugin is swallowed, so it cannot change how a session behaves.

`usage.json` looks like this (`resets_at` is Unix seconds, `observed_at` is when the value was last confirmed):

```json
{
  "schema": 1,
  "written_at": 1790900000.123,
  "windows": {
    "five_hour": {"used_percentage": 65.0, "resets_at": 1790909400, "observed_at": 1790900000.123, "session_id": "..."},
    "seven_day": {"used_percentage": 33.0, "resets_at": 1791093600, "observed_at": 1790899000.456, "session_id": "..."}
  }
}
```

## Repository layout

| Path | Content |
|---|---|
| `usage_widget.py` | The taskbar widget |
| `plugins/.claude-plugin/marketplace.json` | Local marketplace that lists the plugin |
| `plugins/usage-feed/` | The Claude Code plugin: `hooks/register.ts` and its tests in `tests/` |

The plugin has 53 tests that run on Claude Code's own test kit:

```powershell
claude plugin validate ./plugins/usage-feed
claude plugin test ./plugins/usage-feed
```

## Uninstall

Choose `Quit` in the widget's right-click menu, delete the Startup shortcut if you made one, then remove the marketplace. This also uninstalls the plugin.

```powershell
claude plugin marketplace remove usage-bar-local
```

## Disclaimer

Unofficial project, not affiliated with or endorsed by Anthropic. It relies on Claude Code's mods API, which may change.
