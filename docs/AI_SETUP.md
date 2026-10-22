# AI-assisted setup

[English guide](../README.md) | [简体中文指南](../README.zh-CN.md) | [Technical reference](REFERENCE.md)

Paste the prompt below into Claude Code or another coding assistant that can run commands on your Windows machine. The assistant should inspect your existing setup before proposing changes, obtain your approval before changing user-level settings, and leave refresh clicks to you. Read [Claude app refresh](REFERENCE.md#app-refresh) and [Codex usage](REFERENCE.md#codex-usage), especially the quota costs and unofficial-interface caveats, before using it.

```text
Deploy the claude-usage-bar project on this Windows 11 machine.

Repository: https://github.com/imrooki/claude-usage-bar
Goal: a transparent taskbar widget shows my Claude plan usage (5-hour and 7-day
windows). The numbers come from the usage-feed Claude Code plugin in that repository,
which writes data/usage.json; usage_widget.py draws them. If the Codex CLI is
installed, the widget also shows a Codex block on its own. The widget's right-click
menu has "Refresh now"; read the README and the docs/REFERENCE.md sections
"Refresh now and the app's usage tool" and
"Codex usage". Read them first.

If an earlier copy of this project or plugin is already set up on this machine,
update it in place (git pull --ff-only, reuse its folder and settings) instead of
creating a second copy. A plugin update is only picked up by sessions that start after
it, so ask me to open a new Claude Code session afterwards, and restart the widget
(Quit from its right-click menu, then start it again).

1. Clone the repository into a local folder that is not inside OneDrive and not on a
   network share (the plugin refuses such paths). Create the "data" folder inside it
   (the widget also creates it on its first start).
2. Make sure Python 3 with Pillow is available (pip install pillow). Tell me which
   python.exe and pythonw.exe you will use. Reuse an existing environment; do not
   create one without my request.
3. Validate the plugin: claude plugin validate <repo>\plugins\usage-feed must pass.
4. Ask for my approval before changing my user-level settings or disabling an
   existing plugin. Once approved, load the plugin for Claude desktop app sessions
   with the documented CLAUDE_CODE_PLUGIN_DIRS setting.
   Back up %USERPROFILE%\.claude\settings.json, then
   merge these keys into it without removing any existing key, and show me the diff:
     "env": { "CLAUDE_CODE_PLUGIN_DIRS": "<absolute path of <repo>\plugins\usage-feed>" }
     "pluginConfigs": { "usage-feed@inline": { "options": {
         "dataDir": "<absolute path of <repo>\data, written with forward slashes>" } } }
   If usage-feed@usage-bar-local is installed from a plugin marketplace, disable it:
   claude plugin disable usage-feed@usage-bar-local
   (A marketplace install is only for terminal sessions: in the desktop app it loads
   but its hooks never run, so it must not be the only way the plugin is loaded.)
5. Start the widget without a console window: pythonw "<repo>\usage_widget.py"
   It runs as a single instance.
6. Ask me to open a new Claude Code session and send one message. Then check:
   - <repo>\data\usage.json was written by that session (its session_id) and the
     widget shows the numbers;
   - <repo>\data\usage-feed-events.json has entries for that session (an "ev":
     "session.start" and an "ev": "session.measure" entry). If there is none, the
     plugin is not running in that session: re-check step 4 and the
     "When the numbers do not move" section in docs/REFERENCE.md.
   If the Codex CLI is installed for my Windows user, the widget shows a second
   "Codex" block on its own; nothing needs configuring for it.
7. Ask me to right-click the widget and choose "Refresh now" once, myself. Do not
   click it for me. Then check that <repo>\data\refresh-ack.json has "status": "ok"
   and that the Claude block showed "new HH:MM" or "same HH:MM". "no session" means
   no desktop-app session with the plugin loaded answered; see the README. The
   click also sends one tiny request through my own Codex CLI when the Codex block
   is shown; tell me that before I click, and mention that --no-codex-ping and
   --no-app-refresh switch the two requests off.
8. Only if I confirm: create a shortcut in my Startup folder (shell:startup) whose
   target is pythonw.exe and whose argument is the full path of usage_widget.py in
   quotes.

Rules: use only the documented Claude Code plugin mechanisms. Do not read or copy
login tokens, cookies or auth.json, do not call any unofficial usage endpoint, and do
not send any model request yourself to refresh usage; the only requests this project
makes are the two that happen when I click "Refresh now" (the Claude one uses the
desktop app's own usage tool, the Codex one a tiny codex exec request). Do not commit
or push anything. At the end, list every file and setting you changed.
```
