# claude-usage-bar

A small transparent widget for the Windows 11 taskbar that keeps your **Claude plan usage** in view: the 5-hour and the 7-day rate-limit windows, drawn as two horizontal bars with the used percentage and the reset time. If the **Codex CLI** is installed, its 5-hour and weekly limits appear in a second block next to Claude's. The widget sits right next to the notification area (to the left of the `^` button) and has no background, so your taskbar shows through.

```
 5h [########--]  65%  16:50
 7d [###-------]  33%  Sun
```

With Codex installed, each block carries its name above the bars:

```
           Claude                            Codex
 5h [###-------]  31%  21:40     5h [#---------]   5%  00:23
 7d [#########-]  88%  Sun       7d [#####-----]  50%  Fri
```

Percentages are the share already used, as Claude's usage page shows them (Codex's own status shows the share left, so 5 % used there reads as 95 % left). Bars are green below 50 %, amber from 50 % to 80 %, and red from 80 % up.

## How it works

```
Claude Code session (desktop app Code tab, or terminal)
  |   usage-feed plugin (a Claude Code "mod"): on session start, after every turn,
  |   and whenever a rate-limit window moves by a whole percentage point, it takes
  |   the usage figures Claude Code itself reports and merges them into data/usage.json
  v
usage_widget.py: reads data/usage.json every 15 s and draws the bars
  ^
  |   Codex CLI / Codex app: after each model response it appends a record with the
  |   current rate limits to its local session log, %USERPROFILE%\.codex\sessions\...
  |   The widget reads the last such record, read-only, every 15 s. When you click
  |   "Refresh now" it also starts your own Codex CLI once for a tiny request, so that
  |   Codex logs the account's current limits (see "Codex usage").
```

- The Claude numbers come only from Claude Code's documented [mods API](https://code.claude.com/docs/en/plugins/mods/overview). The project never reads your login token, never calls an unofficial endpoint and adds no requests of its own for Claude. The one request the widget can cause is the optional Codex one described under "Codex usage"; it is sent by your own Codex CLI, never by the widget, and can be turned off.
- Numbers change only while a Claude Code session is active, because they come from the last API response of a session. After 30 minutes without an update the bars turn grey and an age label (`41m`, `3h`, `2d`) shows how old the reading is. Once a window's reset time has passed its row shows `reset` until fresh data arrives.
- With several sessions open, the plugin merges readings instead of blindly overwriting: inside one window period the usage can only go up, so an idle session's older reading does not replace a newer one, and a later reset time means a new period. There is no file lock, so in a rare race one update can be lost until the next event.

## Requirements

- Windows 11. Developed on build 26100 and tested on build 26200, with the taskbar at the bottom and 100 % display scaling. Only the primary taskbar is handled, and only a bottom or top taskbar.
- Python 3 with [Pillow](https://pypi.org/project/pillow/) (developed with Python 3.13 and Pillow 11). The widget uses nothing else outside the standard library.
- Claude Code with mod support. Claude Code's documentation says mods need v2.1.287 or later; this project was tested in the Claude desktop app's Code tab with Claude Code 2.1.286, with the plugin loaded through `CLAUDE_CODE_PLUGIN_DIRS` (see Install).
- A Claude Pro or Max plan. Rate-limit windows are only reported for subscription plans.
- Optional: the Codex CLI or Codex app signed in with a ChatGPT plan, for the Codex block. Nothing needs to be configured for it. The Codex request on `Refresh now` additionally needs the native `codex.exe` of the Codex CLI to be found (see "Codex usage").

## Install

### Let an AI assistant deploy it

Paste this prompt into Claude Code (or another coding assistant that can run commands on your Windows machine):

```text
Deploy the claude-usage-bar project on this Windows 11 machine.

Repository: https://github.com/imrooki/claude-usage-bar
Goal: a transparent taskbar widget shows my Claude plan usage (5-hour and 7-day
windows). The numbers come from the usage-feed Claude Code plugin in that repository,
which writes data/usage.json; usage_widget.py draws them.

If an earlier copy of this project or plugin is already set up on this machine,
update it in place (pull the latest version and reuse its folder and settings)
instead of creating a second copy.

1. Clone the repository into a local folder that is not inside OneDrive and not on a
   network share (the plugin refuses such paths). Create the "data" folder inside it.
2. Make sure Python 3 with Pillow is available (pip install pillow). Tell me which
   python.exe and pythonw.exe you will use.
3. Validate the plugin: claude plugin validate <repo>\plugins\usage-feed must pass.
4. Load the plugin for Claude desktop app sessions with the documented
   CLAUDE_CODE_PLUGIN_DIRS setting. Back up %USERPROFILE%\.claude\settings.json, then
   merge these keys into it without removing any existing key, and show me the diff:
     "env": { "CLAUDE_CODE_PLUGIN_DIRS": "<absolute path of <repo>\plugins\usage-feed>" }
     "pluginConfigs": { "usage-feed@inline": { "options": {
         "dataDir": "<absolute path of <repo>\data, written with forward slashes>" } } }
   If usage-feed@usage-bar-local is installed from a plugin marketplace, disable it:
   claude plugin disable usage-feed@usage-bar-local
5. Start the widget without a console window: pythonw "<repo>\usage_widget.py"
   It runs as a single instance.
6. Ask me to open a new Claude Code session and send one message. Then check that
   <repo>\data\usage.json was written by that session (its session_id) and that the
   widget shows the numbers. If the Codex CLI is installed for my Windows user, the
   widget shows a second "Codex" block on its own; nothing needs configuring for it.
7. Only if I confirm: create a shortcut in my Startup folder (shell:startup) whose
   target is pythonw.exe and whose argument is the full path of usage_widget.py in
   quotes.

Rules: use only the documented Claude Code plugin mechanisms. Do not read or copy
login tokens or cookies, do not call any unofficial usage endpoint, and do not send
extra model requests to refresh usage. Do not commit or push anything. At the end,
list every file and setting you changed.
```

### Install by hand

Clone the repository and create the folder that will hold the snapshot. It is git-ignored, so it does not exist after cloning.

```powershell
git clone https://github.com/imrooki/claude-usage-bar.git
cd claude-usage-bar
mkdir data
pip install pillow
```

**Claude desktop app (Code tab).** Load the plugin from its folder with the documented `CLAUDE_CODE_PLUGIN_DIRS` setting, which is meant for sessions that the desktop app starts. Merge these keys into `%USERPROFILE%\.claude\settings.json`, keeping everything else in that file, and replace the paths with your own:

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "C:\\full\\path\\to\\claude-usage-bar\\plugins\\usage-feed"
  },
  "pluginConfigs": {
    "usage-feed@inline": {
      "options": { "dataDir": "C:/full/path/to/claude-usage-bar/data" }
    }
  }
}
```

In testing with Claude Code 2.1.286, the copy installed from a plugin marketplace loaded but never wrote `usage.json` from desktop Code sessions, while the same plugin loaded through `CLAUDE_CODE_PLUGIN_DIRS` did. If you installed the marketplace copy earlier, disable it with `claude plugin disable usage-feed@usage-bar-local`; the folder copy overrides it in any case. When Claude Code loads the plugin from its folder it generates `tsconfig.json` and `.claude-plugin/types/` there; both are git-ignored.

**Terminal only.** You can instead register the local marketplace and install the plugin:

```powershell
claude plugin marketplace add ./plugins
claude plugin install usage-feed@usage-bar-local --config dataDir=C:/full/path/to/claude-usage-bar/data
```

Either way, `dataDir` must be the full path of the `data` folder, written with forward slashes. The plugin accepts only a local absolute path. It refuses relative paths, network (UNC) paths and any path containing `onedrive`, and then writes nothing at all.

Open a new Claude Code session, send one message, then start the widget:

```powershell
pythonw usage_widget.py
```

After the first completed turn, `data/usage.json` should carry that session's `session_id`. If it does not, start Claude Code with `--debug-file <path>` and look for `hooks module usage-feed@inline loaded` (or `usage-feed@usage-bar-local` for the marketplace copy) in that file.

By default the widget reads `data/usage.json` next to the script. To use another folder pass `--data-dir <folder>`; it has to be the same folder you gave the plugin.

To start it at login, put a shortcut in your Startup folder (press Win+R and run `shell:startup`) whose target is `pythonw.exe` and whose arguments are the full path of `usage_widget.py` in quotes. The widget runs as a single instance, so a second start exits at once.

## Updating while using chat and Code

Chat and Claude Code count toward the [same subscription limits](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work) when signed into the same account. A fresh Code reading therefore reflects the shared quota, including chat usage. The widget updates when the plugin receives those readings.

The widget cannot continuously refresh account limits while only chat is active. `Refresh now` re-reads the local snapshot; it does not query the account. Old readings retain their original timestamps and turn grey, with an age label, after 30 minutes. For a current reading while Code is idle, use Claude's own usage page. The widget does not extract login tokens or add background account requests.

## Codex usage

The Codex block needs no setup. The Codex CLI and the Codex app write a local session log for each session under `%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl` (or under `CODEX_HOME` if you set that variable). After every model response they append a `token_count` record that carries the current rate limits: the 5-hour window (`primary`) and the weekly window (`secondary`), each with the used percentage and the reset time. The widget reads the last such record of the most recently written session log.

- **Automatic detection.** The widget looks in the Codex folder of the Windows user it runs as. Without a `sessions` folder it shows only the Claude block, exactly as before; once Codex has been used, the Codex block appears on its own.
- **Read-only and local.** Only lines that contain both `"token_count"` and `"rate_limits"` are parsed, so your prompts and answers in the same files are never read into the widget. It never opens `auth.json` or any other Codex credential file, and the widget itself makes no network connection. Only records whose `limit_id` is missing or `codex` are used.
- **Refresh now asks Codex for a fresh reading.** When you click `Refresh now` and the Codex block is shown (a `sessions` folder exists), the widget starts your own Codex CLI once, without a window, through its non-interactive mode, with a fixed command line and no shell: `codex.exe exec -m gpt-6-luna -c model_reasoning_effort="low" --ignore-user-config --ignore-rules -s read-only -C <data folder>\codex-ping --skip-git-repo-check --json ok`. Codex answers the one-word prompt and, as for every response, writes the account's current limits into a new session log, which the widget then reads. This is the only way to see Codex use that happened on another computer or through another provider. It costs a small request on your Codex plan (measured: about 17,000 input tokens, partly cached, and about ten output tokens on the small model `gpt-6-luna`, 4 to 7 s). Limits on it: at most one request every 5 minutes (a click inside that time shows `cooldown`), stopped after 60 s, never started by the 15 s polling, never started when Codex has not been used on this computer. Each request leaves one small session file in Codex's own `sessions` folder, which may also appear in Codex's own session list. The widget never reads your Codex login file; the Codex CLI uses its own sign-in, as it does when you run it yourself. Whether this is acceptable under your own plan's terms is your decision; turn it off with `--no-codex-ping`.
- **Finding the Codex CLI.** Only the native `codex.exe` is used, never the `codex.cmd` shim. The widget takes `--codex-bin <file>` if you give one (it must be an existing `.exe`, otherwise nothing is started), else a `codex.exe` in a PATH folder (only absolute folders count; empty and relative PATH entries are ignored, so the widget's own working folder is never searched), else the copy inside the npm package `@openai/codex` under `%APPDATA%\npm\node_modules`. If none is found, `Refresh now` only re-reads the logs and writes one line to `widget_error.log`.
- **Not an official interface.** The session-log format belongs to Codex and may change in a future release. If it does, the Codex block keeps its last numbers, which turn grey after 30 minutes, or shows `--` if it never read any; the Claude block is not affected.
- **What updates it.** Interactive Codex use updates it, and so does the request that `Refresh now` sends. Runs started with `codex exec --ephemeral` write no session log and therefore never show up.
- **Options.** `--no-codex` hides the block (and so also stops the request). `--no-codex-ping` keeps the block but never starts Codex. `--codex-home <folder>` reads another Codex home; the request then runs with `CODEX_HOME` set to the same folder. `--codex-bin <file>` names the `codex.exe` to start. `--codex-ping-model <name>` replaces `gpt-6-luna` (letters, digits, `.`, `_`, `:` and `-` only), for example if your plan does not offer that model; a refused model ends in `ping failed`.

## When the numbers change

| What | How often |
|---|---|
| The widget re-reads `data/usage.json` and the Codex session log | every 15 s |
| The widget looks for a newer Codex session log | at most every 60 s |
| A block turns grey and shows its age when its newest reading is older than | 30 min |
| The result of `Refresh now` stays on screen for | about 2.5 s |
| The Codex request of `Refresh now` runs at most | once every 5 min |
| The Codex request is given up after | 60 s |

The widget can only show what its sources have written. Claude's numbers arrive when a Code session finishes a turn or a limit moves by a whole point; Codex's numbers arrive after each Codex model response. A reading from an ongoing Codex session therefore shows up within about 15 s, one from a newly started session within about 75 s. `Refresh now` in the right-click menu re-reads both sources at once, looks for a new Codex session log immediately and, if the 5 minute limit allows, asks Codex for a fresh reading (it takes 4 to 7 s; the Codex block shows `asking` meanwhile).

The grey of an old reading is drawn at full strength on purpose: on a transparent taskbar a faded bar is almost invisible, a grey one is not. The age label shows the age of the oldest stale reading of that block, rounded down: minutes below 2 h, hours below 48 h, then days (never more than `999d`).

## When the numbers do not move

First rule out the two usual causes, which are not faults:

- **The usage happened somewhere the widget cannot see.** Claude Code sessions on another computer, the phone app, claude.ai in a browser and the desktop Chat tab do not write to this folder. They count against the same limits, but the widget only learns about them once a local Code session reports a fresh reading.
- **The session reports no limits.** Claude Code hands the plugin rate-limit figures only from the last response that carried them, so a session that has not completed a turn yet, or whose responses carried none, gives the plugin nothing to write.

To see which one it is, click `Refresh now` in the right-click menu. For about 2.5 s the widget shows what the click found next to the numbers (in the block header when two blocks are shown):

| Text | Meaning |
|---|---|
| `new 14:59` | A reading newer than the one on screen was found; 14:59 is when it was taken (local time). |
| `same 14:29` | Nothing newer exists in the data; the newest reading was taken at 14:29. The widget works, the source has not written anything since. |
| `no data` | There is no reading at all yet for this block. |
| `read error` | The data (the Claude data file, or the Codex session log for the Codex block) exists but could not be read or parsed. |
| `asking` | Codex block only: your Codex CLI is running the small request (4 to 7 s). The text stays until it ends, then the result shows as `new` or `same`. |
| `cooldown` | Codex block only: the last request was less than 5 minutes ago, so no new one was sent. |
| `ping failed` | Codex block only: the request could not be started, exited with an error (for example a model your plan does not offer, or no sign-in), or ran for more than 60 s. `widget_error.log` has the exit code. |

Each block gets its own text, so a fresh Claude block and a stale Codex block can say `new` and `same` side by side.

For the Codex block this also fixes the first cause above: Codex use on another computer is invisible to a local log, but the answer to the request carries the account's current limits, so a click gives you the real numbers.

If a Code session is running and the widget still says `same`, look at the plugin's own log, `usage-feed-events.json`, in the same folder as `usage.json`. The plugin adds one entry per hook call and keeps the latest 200. It is plain JSON; open it in any editor, newest entry last:

```json
{"t": 1790900000123, "ev": "session.measure", "sid": "...", "n": 2, "kinds": ["five_hour", "seven_day"], "kept": 2, "out": "wrote", "why": "", "held": [], "changed": []}
```

| Field | Meaning |
|---|---|
| `t` | Time of the call, in milliseconds since 1970 |
| `ev` | `session.start` or `session.measure` (after every turn or whenever a limit moves) |
| `sid` | The session that made the call |
| `n`, `kinds` | How many limit windows Claude Code handed over, and which kinds (`five_hour`, `seven_day`, `spend_limit`) |
| `kept` | How many of them were usable |
| `out` | `wrote` (usage.json updated), `skipped` (nothing to write) or `failed` (the write failed) |
| `why` | Empty when `out` is `wrote`. Otherwise `no_rate_limits` (the session reported none), `all_dropped` (every window was unusable, see `drops`), `bad_clock` (no valid time from the engine), `usage_error:<name>` (reading the usage failed) or `write_error:<name>` (writing failed) |
| `drops` | Why windows were unusable, as counts, for example `{"no_resets_at": 2}`; left out when nothing was dropped. Reasons: `not_object`, `unknown_kind`, `bad_percent`, `no_resets_at`, `bad_resets_at`, `duplicate_kind` |
| `held` | Windows whose new reading did not replace the stored one, because it was lower in the same period or belonged to an earlier period; the stored value was kept |
| `changed` | The names of the metrics that changed, as the engine reported them (empty at session start) |

How to read it: many `no_rate_limits` entries mean that session never receives limits (not a fault of the widget or the plugin). `all_dropped` with `no_resets_at` means Claude Code sent percentages without a reset time, which the plugin does not accept. `held` filled in again and again means another session wrote a higher reading in the same period. No new entries at all while you work usually means the plugin is not loaded in that session; see the install section for the debug-file check.

The log holds no file paths, working folders, prompts or model names, only what the table lists. It is written without a lock; with several sessions at once an entry can be lost, and in a rare race the whole log restarts from empty. It is for troubleshooting only and nothing reads it back.

## Using the widget

- **Drag** it sideways to move it. It then stays where you put it.
- **Right-click** for the menu: `Refresh now`, `Snap to tray` (go back to following the notification area automatically) and `Quit`. `Refresh now` re-reads both sources at once, dims the widget for about 0.3 s and then shows for about 2.5 s what it found (`new 14:59`, `same 14:29`, `no data` or `read error`, see "When the numbers do not move"), so you can see that it ran even when no new reading has arrived. With the Codex block shown it also sends the small Codex request described under "Codex usage".
- It hides itself while a full-screen app is running (an ordinary application window covering the whole monitor of the taskbar) and while the taskbar is set to auto-hide. Clicking the taskbar or the desktop does not count as full screen.
- It stays visible while the Start menu, Quick Settings or the notification overflow is open, and after a click on empty taskbar space. This was checked with screenshots on build 26200, opening Start and Quick Settings both by keyboard and by mouse.

Known limitations:

- Windows 11 does not let ordinary windows live inside the notification area, so the widget sits beside it, not inside.
- After the right-click menu closes, keyboard focus stays on the widget until you click another window.
- Only the primary monitor's taskbar is handled.
- If the system clock ran far ahead while Codex was writing and was corrected later, the Codex block can stay on an old reading for about as long as the clock was ahead, or until Codex writes again.
- When Explorer restarts, the widget recreates its window under the new taskbar. This was tested with one real restart; two restarts within 30 s can leave the widget without an owner for up to about 30 s, during which an open Start menu covers it.

## Design notes

- The widget is a top-level layered window with per-pixel transparency, created with the primary taskbar (`Shell_TrayWnd`) as its owner through the documented `hWndParent` argument of `CreateWindowEx`. While the Start menu is open, Windows moves the taskbar into a higher window band than ordinary topmost windows, and it moves the taskbar's owned windows with it; an unowned topmost window stays hidden underneath. The widget is not a child window, and nothing is injected into Explorer.
- Trade-off of the owner link: a cross-thread owner relationship attaches the input queues of the widget thread and the taskbar thread, so a hung widget could delay taskbar input. The widget therefore does only short work on its main thread (small file reads and small renders).
- Ownership cannot be transferred after a window is created. When the taskbar window is replaced (for example after an Explorer restart), it destroys and recreates its own window, at most once every 30 s. If the widget started without an owner because no taskbar existed yet, it does the same once a taskbar appears. If creating the owned window failed and the widget fell back to an unowned one, it retries only for a different taskbar window or after the `TaskbarCreated` broadcast, so it does not flicker every 30 s. It does not use `GetWindow(GW_OWNER)` for that decision: on build 26200 that call returns no owner while the Start menu or a mouse-opened Quick Settings is showing, and the taskbar again once it closes, while the widget keeps following the taskbar. A mismatch there is only logged.
- After an Explorer restart (tested once with `taskkill` and `start explorer.exe` on build 26200) the widget recreated its window under the new taskbar within the same second, and it again stayed visible over the Start menu.
- To follow the notification area the widget reads the window rectangle of `TrayNotifyWnd` (read-only queries, every 2 s), so it moves when tray icons come and go.
- Foreground and window-order notifications use out-of-context WinEvent hooks. When the taskbar covers the widget, for example after the Start menu closes, it restores its own window order without taking focus; repeated notifications are coalesced and the 2 s check remains as a fallback. Only overlapping shell panel rectangles prevent that restoration. Full-screen, auto-hide and menu rules still apply. Some machines report a busy notification state all the time, so a busy state alone does not hide the widget: the foreground must also be an ordinary application window covering the whole monitor of the taskbar. The taskbar, the desktop and the widget's own windows never count.
- The widget makes no network connections. Its only subprocess is the optional Codex request after a click on `Refresh now`: one `Popen` call with a fixed argument list (no shell, standard streams closed, no window), a non-blocking check every 0.5 s on the main thread, `terminate` then `kill` after 60 s, and a stop on quit; there is no extra thread. It reads one registry value (the light/dark setting), `data/usage.json` and, read-only, the tail of the newest Codex session logs, and it writes only `widget_pos.json` and a small error log (capped at 64 KB) into the data folder. Looking for the newest Codex session log stats the files of the last 14 date folders at most once a minute (about 2 ms with 1,200 session logs); reading takes at most 1 MB from the end of a log.
- The plugin calls five Claude Code APIs (`session.usage`, `session.id`, `clock.now`, `fs.read`, `fs.write`), hooks only `session.start` and `session.measure`, and writes only `usage.json` and the troubleshooting log `usage-feed-events.json` in the folder you configured. Every hook runs the engine's own handling first, and a failure inside the plugin is swallowed, so it cannot change how a session behaves.

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
| `tests/` | Widget tests: window events, taskbar ownership and window recovery, full-screen detection, the Codex session-log reader, the two-block display, the stale-reading and refresh-result display, and the Codex request of `Refresh now` (with a fake process, never the real Codex) |

The plugin has 75 tests that run on Claude Code's own test kit:

```powershell
claude plugin validate ./plugins/usage-feed
claude plugin test ./plugins/usage-feed
```

Widget regression tests run on Windows with Python and Pillow:

```powershell
python -B -m unittest discover -s tests -v
```

To also check real Windows event delivery, repeated reordering, window ownership and the real message loop, enable the native tests. They create only their own off-screen windows and do not operate the real taskbar:

```powershell
$env:CLAUDE_USAGE_NATIVE_TEST = '1'
python -B -m unittest discover -s tests -v
Remove-Item Env:CLAUDE_USAGE_NATIVE_TEST
```

One more test is off by default because it sends a real request through your Codex CLI (a small one, it uses a little of your Codex allowance and leaves a session file). It needs `CLAUDE_USAGE_CODEX_PING_TEST=1` and a Python of 3.10 or newer:

```powershell
$env:CLAUDE_USAGE_CODEX_PING_TEST = '1'
python -B -m unittest tests.test_codex_ping.RealPingTests -v
Remove-Item Env:CLAUDE_USAGE_CODEX_PING_TEST
```

## Uninstall

Choose `Quit` in the widget's right-click menu and delete the Startup shortcut if you made one. If you load the plugin through `CLAUDE_CODE_PLUGIN_DIRS`, remove that key from `env` and the `usage-feed@inline` entry from `pluginConfigs` in `%USERPROFILE%\.claude\settings.json`. If you installed it from the marketplace, remove the marketplace, which also uninstalls the plugin:

```powershell
claude plugin marketplace remove usage-bar-local
```

## Disclaimer

Unofficial project, not affiliated with or endorsed by Anthropic or OpenAI. It relies on Claude Code's mods API and on the format of Codex's local session logs, which may change. The Codex request on `Refresh now` runs your own signed-in Codex CLI the way a script would and uses a little of your Codex allowance; check that it suits your plan, or start the widget with `--no-codex-ping`.
