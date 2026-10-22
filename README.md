# claude-usage-bar

**English** | [简体中文](README.zh-CN.md)

**Claude and Codex usage, right on your Windows 11 taskbar.**

A small transparent widget that keeps your **5-hour and weekly allowance remaining** in view, with reset times, compact rings, and an automatic layout that adapts to the space beside your system tray.

![Claude and Codex remaining allowance shown by the real widget renderer](docs/images/overview.png)

*Rendered preview with invented sample data, enlarged for readability. This is not a live desktop screenshot.*

[Download](https://github.com/imrooki/claude-usage-bar/releases) · [Install](#install) · [Display modes](#display-modes) · [Refresh & privacy](#refresh-and-privacy) · [Troubleshooting](#when-the-numbers-do-not-move)

> Reading this from a release ZIP? The complete illustrated documentation and Chinese version are available in the [online README](https://github.com/imrooki/claude-usage-bar#readme).

## At a glance

- **Two providers, one place.** Claude usage from the included Claude Code plugin; optional Codex usage from local session logs.
- **Transparent and unobtrusive.** Sits beside the notification area, follows the tray, and hides for full-screen apps or an auto-hidden taskbar.
- **Full bars or compact rings.** Auto fits the layout to the available taskbar space; either mode can also be pinned manually.
- **Readable at a glance.** Remaining percentages, reset times, and grey stale readings with an age label.
- **Local-first.** No credential scraping and no background account requests. Optional click-to-refresh requests are explained [below](#refresh-and-privacy).

## Display modes

![Full bar and Rings compared at the same scale with identical sample readings](docs/images/display-modes.png)

| Mode | Behaviour |
|---|---|
| **Auto (fit taskbar)** — default | Full bars when there is room, rings when the taskbar gets crowded, hidden if neither fits. |
| **Full bar** | Always uses horizontal bars. |
| **Rings** | Double rings plus the provider name, percentages, and reset times in a smaller footprint. |

**Percentages mean remaining, not used.** A full bar means plenty of allowance. The colour moves from green through teal, blue, and purple toward red as the allowance runs out. In Rings, the **inner ring is 5h** and the **outer ring is 7d**.

<details>
<summary><strong>Progress-bar skins: Neon Minimal and Mecha Tech</strong></summary>

![Neon Minimal and Mecha Tech progress-bar skins with identical sample readings](docs/images/bar-skins.png)

Choose **Progress Bar Skin** in the right-click menu. Neon Minimal is the default. Skins change full bars; ring rendering is shared by both skins. All previews use the real application renderer and sample data.

</details>

## Requirements

| Component | Requirement |
|---|---|
| System | Windows 11; primary taskbar at the bottom or top. Developed on build 26100 and tested on build 26200, at 100% display scaling. |
| Claude | Claude Pro or Max, plus Claude Code with mod support. The included `usage-feed` plugin must be loaded in your Code session. |
| Source install | Python 3 and [Pillow](https://pypi.org/project/pillow/); developed with Python 3.13 and Pillow 11. Release ZIPs include Python. |
| Optional Codex block | Codex CLI or Codex app, signed in with a ChatGPT plan, with local session logs. Click-to-refresh also needs a native `codex.exe`. |
| Optional Claude account refresh | An open Code session in the Claude desktop app, with the plugin loaded. Terminal sessions cannot provide the app's usage tool. |

Windows UI Automation is built into Windows and needs no extra package. For the tested Claude Code versions and integration caveats, see the [technical reference](docs/REFERENCE.md#how-it-works).

## Install

### From the release ZIP — no Python needed

1. Download `claude-usage-bar-<version>-<commit>.zip` from [Releases](https://github.com/imrooki/claude-usage-bar/releases) and verify it against the accompanying `.sha256` file.
2. Extract the **whole folder** somewhere local, for example `C:\Tools\claude-usage-bar`. Do not use OneDrive or a network share.
3. Configure the Claude Code plugin using **step 2 under [Install by hand](#install-by-hand)** below. The executable alone cannot obtain Claude usage.
4. Fully quit and restart the Claude desktop app, then launch `claude-usage-bar.exe`.
5. Open a **new** Code session and send one message. After the reply, the usage should appear within about 15 seconds.

Keep `plugins` and `data` beside the executable. Windows SmartScreen may warn because the executable is unsigned; verify the download and checksum before deciding whether to run it. The ZIP also includes `INSTALL.txt`.

### Install by hand

#### 1. Get the source and dependencies

Run in a local folder outside OneDrive and network shares:

```powershell
git clone https://github.com/imrooki/claude-usage-bar.git
cd claude-usage-bar
mkdir data
python -m pip install pillow
claude plugin validate ./plugins/usage-feed
```

Use the same Python installation for Pillow and `pythonw`. The data folder must exist before the plugin can write; the widget also creates it on startup.

#### 2. Load the Claude Code plugin

For the **Claude desktop app's Code tab**, back up `%USERPROFILE%\.claude\settings.json`, then merge the following entries. **Keep all existing settings** and replace the example paths with your actual folder:

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "C:\\Tools\\claude-usage-bar\\plugins\\usage-feed"
  },
  "pluginConfigs": {
    "usage-feed@inline": {
      "options": {
        "dataDir": "C:/Tools/claude-usage-bar/data"
      }
    }
  }
}
```

- `dataDir` must be a **local absolute path with forward slashes**. Relative paths, UNC paths, and paths containing `onedrive` are rejected.
- If you already have an `env` or `pluginConfigs` object, merge into it rather than creating a duplicate key or replacing the object. Preserve any existing plugin configuration.
- Use the folder-loading method above for desktop sessions. In testing with Claude Code 2.1.286, a marketplace copy loaded but its hooks did not run in the desktop app. If that copy is installed, disable it with `claude plugin disable usage-feed@usage-bar-local`.
- Fully quit and restart the desktop app after changing settings. Existing Code sessions keep their previously loaded plugin.

<details>
<summary><strong>Terminal-only Claude Code installation</strong></summary>

Instead of the desktop folder-loading setup, you can register the included local marketplace:

```powershell
claude plugin marketplace add ./plugins
claude plugin install usage-feed@usage-bar-local --config dataDir=C:/Tools/claude-usage-bar/data
```

Replace the data path with yours, and open a new terminal session. Automatic usage updates work, but terminal sessions do not have the Claude desktop app's account-refresh tool.

</details>

#### 3. Start the widget and a new Code session

```powershell
pythonw usage_widget.py
```

For a release ZIP, double-click `claude-usage-bar.exe` instead. Then open a new Claude Code session and send one message. Check that `data/usage.json` appears and the widget shows your numbers. Codex is detected automatically when its local sessions folder exists; no plugin is needed for that block.

By default the widget and plugin share `data` beside the script or executable. If you pass `--data-dir <folder>`, make sure it is **the same folder** as the plugin's `dataDir`.

Prefer assistant-guided setup? Use the [copyable deployment prompt](docs/AI_SETUP.md).

## Using the widget

| Action | Result |
|---|---|
| Drag sideways | Move the widget; the position is saved. |
| Right-click → **Snap to tray** | Restore automatic positioning beside the system tray. |
| Right-click → **Display** | Choose Auto, Full bar, or Rings. |
| Right-click → **Progress Bar Skin** | Choose Neon Minimal or Mecha Tech for full bars. |
| Right-click → **Refresh now** | Re-read local sources and, unless disabled, request fresh account readings. See the cost and privacy notes below. |
| Right-click → **Quit** | Exit the widget. |

**Start at login:** press Win+R, run `shell:startup`, and add a shortcut to the executable. For a source install, target your `pythonw.exe` and put the full quoted path to `usage_widget.py` in its arguments. Only one widget instance runs at a time.

**Updating:** quit the widget first. For a release, replace the executable and `plugins` with the new ZIP's versions while keeping `data` and your settings. For a source install, update your local checkout without discarding local changes. Restart the widget and open a new Code session; already-open sessions retain their old plugin. See the [reference](docs/REFERENCE.md#using-the-widget) for further limitations.

## Refresh and privacy

The widget re-reads local files every **15 seconds**. It can only show what its sources have written; it is **not a live account-usage API**. Readings older than **30 minutes** turn grey with an age label, and a row whose reset time has passed shows `reset` until new data arrives.

| Source | Normal updates | What **Refresh now** adds by default |
|---|---|---|
| **Claude** | The plugin writes usage after a Code turn or a rate-limit event. | Asks an open desktop Code session to call the app's own usage tool. This is an account-tool call, not a model prompt. Normally limited to one successful request per 60 seconds. |
| **Codex** | Reads rate-limit records from local Codex session logs. | Starts your signed-in Codex CLI for one small model request, normally `gpt-6-luna`. **This uses some Codex allowance** and leaves a session log. At most once every 5 minutes. |

**No refresh request is sent automatically by background polling.** The widget itself makes no network connections and never opens login tokens, cookies, or `auth.json`. The optional requests are made by your Claude Code session or your own Codex CLI after a click. Codex log tails are scanned locally for usage records; the widget does not use conversation content for its display.

To keep both displays but disable both outgoing refresh actions:

```powershell
pythonw usage_widget.py --no-app-refresh --no-codex-ping
```

For a release build, use the same flags after `claude-usage-bar.exe` in a shortcut. Local polling and manual re-reading still work.

The Claude app's usage tool and Codex session-log format are **not stable, documented integration interfaces**. If they change, readings may become stale or unavailable. For exact request behaviour, prior cost measurements, timeouts, and data handling, see [Claude refresh](docs/REFERENCE.md#app-refresh), [Codex usage](docs/REFERENCE.md#codex-usage), and [timing details](docs/REFERENCE.md#when-the-numbers-change).

### Options

| Flag | Purpose |
|---|---|
| `--data-dir <folder>` | Change the widget's data folder; must match the Claude plugin configuration. |
| `--no-app-refresh` | Disable the Claude account-tool request on Refresh now. |
| `--no-codex` | Hide Codex and disable its refresh request. |
| `--no-codex-ping` | Show Codex from local logs without launching CLI requests. |
| `--codex-home <folder>` | Read another Codex home; refresh uses that same home. |
| `--codex-bin <file>` | Select an existing native `codex.exe`, not the `.cmd` shim. |
| `--codex-ping-model <name>` | Replace the default refresh model if it is unavailable on your plan. |

## When the numbers do not move

First check the folder paths, whether the plugin is loaded, and whether you opened a **new** Code session after installation or an update. The widget cannot see remote activity until a local source reports fresh account limits.

| What you see | What to check |
|---|---|
| `--`, `no data`, or `no file` | Complete a Code turn; check that `data/usage.json` exists and both data paths match. Codex needs a local non-ephemeral session with rate-limit records. |
| `same HH:MM` | The newest source reading has not changed. This does not mean the widget is broken. |
| Grey bars or an age label | The newest reading is more than 30 minutes old; get a fresh source reading. |
| `no session` | Keep a desktop-app Code session open with the folder-loaded plugin. Terminal sessions cannot answer the app-tool request. |
| `no limits` | The app answered but has no subscription limits for this account, for example an API-key account. |
| `cooldown` | Wait before requesting another refresh. Claude and Codex have separate cooldowns. |
| `ping failed` | Check Codex sign-in, the native CLI path, and model availability; inspect `data/widget_error.log`. |
| `read error` or `not sent` | Check local file permissions and the error log. |

The plugin's troubleshooting log is `data/usage-feed-events.json`. Look for `session.start` and `session.measure` entries for your new session. For every caption, log field, and merge rule, see the [detailed troubleshooting guide](docs/REFERENCE.md#when-the-numbers-do-not-move).

### Known limitations

- Only the **primary** taskbar is supported. The widget sits beside the tray, not inside it, and **cannot reserve taskbar space**.
- Auto hides the widget when even compact rings do not fit. If UI Automation is unavailable, Auto falls back to taskbar alignment: rings for centred buttons, full bars for left alignment.
- The widget hides while an ordinary app covers an entire display on any monitor, or while the taskbar is set to auto-hide.
- After its context menu closes, keyboard focus stays on the widget until you click another window.
- Account-tool and log-format changes can break optional integrations. See the [reference](docs/REFERENCE.md#using-the-widget) for Explorer recovery and clock-related edge cases.

## Development

The app uses Python, Pillow, and Windows APIs. The Claude Code plugin is under `plugins/usage-feed`; the widget is split into `usage_bar` modules for data, rendering, Windows integration, and application logic.

- [Architecture, data format, and repository layout](docs/REFERENCE.md#design-notes)
- [Tests and release build instructions](docs/REFERENCE.md#building-a-release) — native-window tests and real Codex requests are explicitly opt-in.
- [Documentation artwork](docs/images/README.md) — reproducible, offline previews generated by `docs/render_previews.py` without reading your usage data.

## Uninstall

Quit the widget and remove its Startup shortcut if present. In `%USERPROFILE%\.claude\settings.json`, remove only this project's plugin-directory entry and `usage-feed@inline` configuration, preserving unrelated settings. For a marketplace install, run:

```powershell
claude plugin marketplace remove usage-bar-local
```

Then remove the project folder if you no longer need its local data.

## License and disclaimer

[MIT](LICENSE). Unofficial project; not affiliated with or endorsed by Anthropic or OpenAI. The Claude desktop usage tool and Codex log format may change. Optional Codex refresh uses your own signed-in CLI and a little of your plan allowance; check whether that suits your plan, or disable it with `--no-codex-ping`.
