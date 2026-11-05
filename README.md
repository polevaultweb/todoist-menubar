# Todoist menu bar

A macOS menu bar app for the Todoist tasks you need today: overdue and due today, with quick actions, quick add, global hotkeys and a Pomodoro focus mode that logs your time on the task.

Python with [rumps](https://github.com/jaredks/rumps) and PyObjC. Talks to the Todoist API v1 with the standard library.

## What it does

- **Menu bar:** ☑️ and a count of tasks due today or overdue, plus `· N⚠︎` when some are overdue. The icon can be any emoji or image (see `icon` below).
- **Menu:** Overdue and Today sections, sorted by priority (🔴 P1, 🟠 P2, 🔵 P3, ⚪️ P4), then date and time. Tasks with a "waiting for" label get 💤 and sink to the bottom. Overdue dates show in red. Hover over a task to see its description.
- **Each task has a submenu:** 🍅 Focus, ✓ Complete, → Today (overdue only), → Tomorrow (→ Mon on Fridays and weekends), Open in Todoist, and an optional custom command (see `task_command` below).
- **⤵ Move N overdue to today**, in one go.
- **⤳ Move N tasks left to tomorrow:** the end-of-day sweep. Moves every overdue and today task to the next working day, after a confirm.
- **＋ Add task… (⌃⌥⌘A from anywhere):** Todoist quick add. Dates, `#project`, `@label` and `p1` work as in the app.
- **⌃⌥⌘T** opens the menu from anywhere.
- **Focus:** the menu bar shows `🍅 24:13  Task name`, and a small floating window shows the task, the countdown and Pause, +5 min, Done ✓ and Stop. The window stays on top on every Space, including full-screen apps, doesn't take keyboard focus, and remembers where you dragged it. Its border is red while working, green on a break and slate when paused.
- **Pomodoro:** 25 minutes of work, then a 5-minute break (15 after every fourth), with a notification at each change. After a break the timer waits for you to click Start, so it doesn't run on while you're away. When a focus ends, the time is posted as a comment on the task, for example "🍅 30 min focus (1 pomodoro) · 4 Nov 2026".

**Rescheduling keeps recurrence.** Moving a task sends its existing due string back with the new date (Sync API `item_update`). The obvious REST update with `due_date` silently turns a recurring task into a one-off.

Tasks refresh every 2 minutes and whenever the menu opens. The menu is built from the last fetch, so it opens instantly.

## Install

Needs macOS and Python 3.9 or later.

```sh
git clone https://github.com/polevaultweb/todoist-menubar.git
cd todoist-menubar
security add-generic-password -a "$USER" -s todoist-api-token -w   # paste your API token
./install.sh
```

Your API token is in Todoist under Settings > Integrations > Developer. Setting `$TODOIST_API_TOKEN` works too, but launchd won't see your shell's environment, so the Keychain is easier.

`install.sh` creates a virtualenv, installs the dependencies and adds a launch agent that starts the app at login and restarts it if it crashes. If you quit from the menu, it stays quit until the next login or until you run `launchctl kickstart gui/$(id -u)/todoist-menubar`.

- Restart after changing code or config: `launchctl kickstart -k gui/$(id -u)/todoist-menubar`
- Log: `~/Library/Logs/todoist-menubar.log`
- Uninstall: `launchctl bootout gui/$(id -u)/todoist-menubar && rm ~/Library/LaunchAgents/todoist-menubar.plist`
- Run it once in the foreground: `.venv/bin/python3 todoist_menubar.py`

## Configure

Everything is optional. Create `~/.config/todoist-menubar/config.json` (or point `$TODOIST_MENUBAR_CONFIG` elsewhere) with any of these:

```json
{
  "project": "Work",
  "task_command": "claude {content}",
  "task_command_label": "Work on in Claude"
}
```

| Setting | Default | What it does |
|---------|---------|--------------|
| `icon` | `☑️` | Menu bar icon: text such as an emoji, or a path to an image (PNG, PDF and so on). Relative paths are from the config folder. It hides while you focus, when 🍅 or ☕ marks the app |
| `icon_template` | `false` | `true` for a one-colour image that macOS tints to match the menu bar, like most menu bar icons |
| `project` | none | Show only this project and its sub-projects; quick add with no `#project` goes here. Without it, every project shows and quick add goes to the Inbox |
| `query` | none | Any [Todoist filter](https://todoist.com/help/articles/introduction-to-filters-V98wIH); overrides `project` |
| `task_command` | none | Adds an item to each task's submenu that runs this shell command in a new terminal tab. Placeholders: `{id}`, and `{url}`, `{content}`, `{description}`, which are shell-quoted for you |
| `task_command_label` | `Work on in Claude` | The submenu item's title |
| `terminal` | `iTerm` | `iTerm` or `Terminal` |
| `waiting_label` | `waiting_for` | Tasks with a label containing this get 💤 and sort last |
| `refresh_secs` | `120` | How often to refetch |
| `work_mins`, `break_mins`, `long_break_mins`, `long_break_every` | `25`, `5`, `15`, `4` | Pomodoro timings |
| `log_min_secs` | `60` | Focus shorter than this isn't logged |
| `title_chars` | `30` | How much of the task shows in the menu bar while focusing |
| `hotkey_menu`, `hotkey_add` | `ctrl+opt+cmd+t`, `ctrl+opt+cmd+a` | Modifiers `ctrl`, `opt`, `cmd`, `shift` plus a key from `KEYS` in `hotkeys.py`; `null` turns one off |

Some `task_command` ideas:

- `claude {content}` starts a Claude Code session on the task
- `cd ~/work && claude "Help me with this Todoist task: "{content}` does the same in a set folder with a framing prompt
- `open {url}` opens the task in the browser instead of the app

To use your own image as the icon, put it in `~/.config/todoist-menubar/` and set `"icon": "my-icon.png"`. A 40×40 PNG with the artwork at about 32×32 in the middle matches the size of other menu bar icons. For a one-colour image that follows light and dark mode, use black on transparent and add `"icon_template": true`.

## Notes

- Global hotkeys use Carbon `RegisterEventHotKey` through ctypes, so they need no Accessibility permission. If another app already holds a combination, the log says so and the menu leaves it out.
- Tasks with a fixed time zone come back in UTC. The app shows them in local time and moves them in their own zone, so a 23:30 task stays at 23:30 across daylight saving changes.
- Completing a recurring task moves it to its next date, as in the Todoist app.
- Focus state is saved in `~/Library/Application Support/todoist-menubar/state.json`, so a restart carries on.
- Sounds come from `osascript` notifications: `NSSound.play()` hung the main thread, and `rumps.notification` needs a bundled app.
- Don't run it with `python3 -I`: isolated mode drops the script's folder from `sys.path` and the local imports fail.

Not affiliated with or endorsed by Doist. Todoist is a trademark of Doist.

## Licence

MIT
