#!/usr/bin/env python3
"""Todoist menu bar app: overdue and today tasks, quick actions, and a Pomodoro focus mode.

The menu lists overdue and today tasks (optionally limited to one project and its sub-projects, or
any Todoist filter). Each task has a submenu: focus, complete, move to today or the next working
day, open in Todoist, and optionally run a command on it in a new terminal tab (for example, start
a Claude Code session). Focusing puts the task and a countdown in the menu bar and in a small
floating window, and the time spent is logged as a comment on the task when the focus ends.

A background thread fetches tasks every refresh_secs and whenever the menu opens; the menu is built
from the latest snapshot, so it opens instantly. Focus state is saved, so a restart picks it up.

Settings come from ~/.config/todoist-menubar/config.json (or $TODOIST_MENUBAR_CONFIG); see the README.
"""

import datetime
import functools
import json
import os
import re
import shlex
import subprocess
import textwrap
import threading
import time
import traceback

import objc
import rumps
from AppKit import (NSApplication, NSAttributedString, NSColor, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName)
from Foundation import NSMutableAttributedString, NSObject
from PyObjCTools import AppHelper

import hotkeys
import todoist_api
from focus_panel import FocusPanel
from hotkeys import CMD, CONTROL, OPTION, SHIFT

CONFIG_FILE = os.path.expanduser(os.environ.get("TODOIST_MENUBAR_CONFIG", "~/.config/todoist-menubar/config.json"))
DEFAULTS = {
    "icon": "☑️",             # menu bar text, or an image path (relative paths are from the config folder)
    "icon_template": False,   # true for a one-colour image that macOS tints to match the menu bar
    "project": None,          # e.g. "Work": only this project and its sub-projects; also quick add's default
    "query": None,            # any Todoist filter; overrides "project"
    "task_command": None,     # e.g. "claude {content}"; adds a task submenu item that runs it in a new tab
    "task_command_label": "Work on in Claude",
    "terminal": "iTerm",      # or "Terminal"
    "waiting_label": "waiting_for",  # tasks with a label containing this get 💤 and sort last
    "refresh_secs": 120,
    "work_mins": 25,
    "break_mins": 5,
    "long_break_mins": 15,
    "long_break_every": 4,
    "log_min_secs": 60,       # focus shorter than this isn't logged as a comment on the task
    "title_chars": 30,        # task text shown in the menu bar while focusing
    "hotkey_menu": "ctrl+opt+cmd+t",  # null to turn off
    "hotkey_add": "ctrl+opt+cmd+a",
}


def load_config():
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            user = json.load(fh)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            print(f"Ignoring unknown settings in {CONFIG_FILE}: {', '.join(sorted(unknown))}")
        config.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"Couldn't read {CONFIG_FILE}, using defaults: {e}")
    return config


CONFIG = load_config()
PROJECT = CONFIG["project"]
QUERY = CONFIG["query"] or (f"(overdue | today) & ##{PROJECT}" if PROJECT else "overdue | today")
REFRESH_SECS = CONFIG["refresh_secs"]
WORK_MINS = CONFIG["work_mins"]
BREAK_MINS = CONFIG["break_mins"]
LONG_BREAK_MINS = CONFIG["long_break_mins"]
LONG_BREAK_EVERY = CONFIG["long_break_every"]
LOG_MIN_SECS = CONFIG["log_min_secs"]
TITLE_CHARS = CONFIG["title_chars"]
MENU_CHARS = 64
DETAIL_CHARS = 60  # wrap width for the title and description at the top of a task's submenu
DETAIL_LINES = 8
DEFAULT_ICON = DEFAULTS["icon"]


def resolve_icon(setting):
    """(image path, text): an image when the setting looks like a file path and exists, otherwise text"""
    setting = setting or DEFAULT_ICON
    if "/" not in setting and not setting.lower().endswith((".png", ".pdf", ".jpg", ".jpeg", ".tiff", ".icns")):
        return None, setting
    path = os.path.join(os.path.dirname(CONFIG_FILE), os.path.expanduser(setting))
    if os.path.isfile(path):
        return path, ""
    print(f"Icon {path} not found; using {DEFAULT_ICON}")
    return None, DEFAULT_ICON


ICON, ICON_TEXT = resolve_icon(CONFIG["icon"])

MODIFIERS = {"ctrl": CONTROL, "control": CONTROL, "opt": OPTION, "option": OPTION, "alt": OPTION,
             "cmd": CMD, "command": CMD, "shift": SHIFT}


def parse_hotkey(text):
    """"ctrl+opt+cmd+t" -> ("t", flags), or None if empty or invalid. Key names are in hotkeys.KEYS"""
    if not text:
        return None
    *mods, key = text.lower().replace(" ", "").split("+")
    if key not in hotkeys.KEYS or not mods or any(m not in MODIFIERS for m in mods):
        print(f"Hotkey {text!r} not understood; use modifiers ctrl, opt, cmd, shift and a key from hotkeys.KEYS")
        return None
    return key, functools.reduce(lambda a, m: a | MODIFIERS[m], mods, 0)


HOTKEY_MENU = parse_hotkey(CONFIG["hotkey_menu"])
HOTKEY_ADD = parse_hotkey(CONFIG["hotkey_add"])

PRIORITY = {4: "🔴", 3: "🟠", 2: "🔵", 1: "⚪️"}
STATE_FILE = os.path.expanduser("~/Library/Application Support/todoist-menubar/state.json")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def clean(text):
    """Markdown links to their text, newlines to spaces"""
    return " ".join(LINK.sub(r"\1", text or "").split())


def clip(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def mmss(secs):
    secs = max(0, int(round(secs)))
    return f"{secs // 60}:{secs % 60:02d}"


def notify(title, text, sound="Glass"):
    """osascript rather than rumps.notification, which needs a bundled app"""
    subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                      "display notification (item 2 of argv) with title (item 1 of argv) sound name (item 3 of argv)",
                      "-e", "end run", title, text, sound])


def task_url(task):
    return f"https://app.todoist.com/app/task/{task['id']}"


def next_working_day(today=None):
    """Tomorrow, or Monday from Friday to Sunday"""
    today = today or datetime.date.today()
    return today + datetime.timedelta(days={4: 3, 5: 2}.get(today.weekday(), 1))


def day_label(date):
    return "tomorrow" if date == datetime.date.today() + datetime.timedelta(days=1) else date.strftime("%a %-d %b")


def plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


class MenuDelegate(NSObject):
    def initWithApp_(self, app):
        self = objc.super(MenuDelegate, self).init()
        if self is None:
            return None
        self.app = app
        return self

    def menuWillOpen_(self, menu):
        self.app.build_menu()
        self.app.wake.set()


class TodoistMenubar(rumps.App):
    def __init__(self):
        super().__init__("Todoist", title=ICON_TEXT, icon=ICON, template=bool(CONFIG["icon_template"]),
                         quit_button=None)
        # Replaced whole by the fetch thread, read on the main thread
        self.snapshot = {"tasks": [], "projects": {}, "updated": None, "error": None}
        self.hidden = set()  # ids completed or moved locally, hidden until the next fetch confirms it
        self.wake = threading.Event()
        self.focus = self.load_focus()
        self.hotkey_labels = {}  # filled once the hotkeys are registered
        self.panel = FocusPanel({
            "toggle": self.toggle_pause, "extend": self.extend,
            "done": self.complete_focused, "stop": self.stop_focus,
        })
        self.build_menu()
        self._menu_delegate = MenuDelegate.alloc().initWithApp_(self)
        self._menu._menu.setDelegate_(self._menu_delegate)
        threading.Thread(target=self._fetch_loop, daemon=True).start()
        # AppKit must be touched on the main thread, so a timer applies state to the title and panel
        self._timer = rumps.Timer(self.tick, 1)
        self._timer.start()
        if self.focus:
            self.panel.show()
        for name, hotkey, action in (("menu", HOTKEY_MENU, self.open_menu), ("add", HOTKEY_ADD, self.add_task)):
            if not hotkey:
                continue
            key, mods = hotkey
            # Carbon calls back inside event dispatch; callAfter runs the action once that's finished
            if hotkeys.register(key, mods, lambda action=action: AppHelper.callAfter(action)):
                self.hotkey_labels[name] = hotkeys.label(key, mods)

    # ── Fetching ────────────────────────────────────────────────────────────

    def _fetch_loop(self):
        while True:
            self.fetch()
            self.wake.wait(REFRESH_SECS)
            self.wake.clear()

    def fetch(self):
        try:
            projects = self.snapshot["projects"] or todoist_api.projects()
            tasks = todoist_api.filter_tasks(QUERY)
            self.hidden &= {t["id"] for t in tasks}  # forget ids the server no longer returns
            self.snapshot = {"tasks": tasks, "projects": projects, "updated": time.time(), "error": None}
        except Exception as e:
            traceback.print_exc()
            self.snapshot = {**self.snapshot, "error": str(e)}

    def refresh_soon(self):
        self.wake.set()

    def tasks(self):
        """(overdue, today), sorted: waiting-for last, then priority, then date and time"""
        today = datetime.date.today().isoformat()
        overdue, due_today = [], []
        for t in self.snapshot["tasks"]:
            if t["id"] in self.hidden or not t.get("due"):
                continue
            (overdue if todoist_api.local_due(t["due"])[0] < today else due_today).append(t)
        def key(t):
            date, at = todoist_api.local_due(t["due"])
            # All-day tasks have no time; sort them after timed ones that day, as Todoist does
            return self.waiting(t), -t["priority"], date, at or "24:00", t.get("child_order", 0)
        return sorted(overdue, key=key), sorted(due_today, key=key)

    @staticmethod
    def waiting(t):
        marker = CONFIG["waiting_label"]
        return bool(marker) and any(marker in label for label in t.get("labels", []))

    # ── Menu ────────────────────────────────────────────────────────────────

    def _item(self, title, callback=None, task=None, tooltip=None, parent=None, attributed=None):
        menu = parent if parent is not None else self.menu
        while title in menu:  # rumps keys items by title, so pad duplicates with zero-width spaces
            title += "​"
        item = rumps.MenuItem(title, callback=callback)
        item.task = task
        if tooltip:
            item._menuitem.setToolTip_(tooltip)
        if attributed:
            item._menuitem.setAttributedTitle_(attributed)
        menu.add(item)
        return item

    def _task_title(self, t, overdue):
        date, at = todoist_api.local_due(t["due"])
        when = []
        if overdue:
            when.append(datetime.date.fromisoformat(date).strftime("%a %-d %b"))
        if at:
            when.append(at)
        project = self.snapshot["projects"].get(t["project_id"], "")
        prefix = ("💤 " if self.waiting(t) else "") + PRIORITY.get(t["priority"], "")
        text = f"{prefix} {clip(clean(t['content']), MENU_CHARS)}"
        meta = "   " + " · ".join(when + ([project] if project and project != PROJECT else []))
        attributed = NSMutableAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: NSFont.menuFontOfSize_(0)})
        if meta.strip():
            attributed.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(meta, {
                NSFontAttributeName: NSFont.menuFontOfSize_(11),
                NSForegroundColorAttributeName: NSColor.systemRedColor() if overdue else NSColor.secondaryLabelColor(),
            }))
        return text, attributed

    @staticmethod
    def _task_details(t):
        """The full title (if the menu clips it) and the description, wrapped, for the top of the submenu.
        A tooltip on the task would open over the submenu when the menu sits at the right of the screen."""
        title = clean(t["content"])
        lines = textwrap.wrap(title, DETAIL_CHARS) if len(title) > MENU_CHARS else []
        notes = []
        for para in LINK.sub(r"\1", t.get("description") or "").strip().splitlines():
            notes += textwrap.wrap(para, DETAIL_CHARS) or [""]
        while notes and not notes[-1]:
            notes.pop()
        if len(notes) > DETAIL_LINES:
            notes = notes[:DETAIL_LINES]
            notes[-1] = clip(notes[-1], DETAIL_CHARS - 1) + "…"
        if not lines and not notes:
            return None
        attributed = NSMutableAttributedString.alloc().initWithString_attributes_(
            "\n".join(lines), {NSFontAttributeName: NSFont.boldSystemFontOfSize_(12)})
        if notes:
            attributed.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
                ("\n" if lines else "") + "\n".join(notes),
                {NSFontAttributeName: NSFont.menuFontOfSize_(11),
                 NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
        return attributed

    def _add_task(self, t, overdue):
        text, attributed = self._task_title(t, overdue)
        parent = self._item(text, attributed=attributed)
        details = self._task_details(t)
        if details:
            self._item(details.string(), parent=parent, attributed=details)  # no callback, so not clickable
            parent.add(rumps.separator)
        self._item("🍅 Focus", self.on_focus, t, parent=parent)
        self._item("✓ Complete", self.on_complete, t, parent=parent)
        parent.add(rumps.separator)
        if overdue:
            self._item("→ Today", self.on_today, t, parent=parent)
        self._item(f"→ {day_label(next_working_day()).capitalize()}", self.on_tomorrow, t, parent=parent)
        parent.add(rumps.separator)
        self._item("Open in Todoist", self.on_open, t, parent=parent)
        if CONFIG["task_command"]:
            self._item(CONFIG["task_command_label"], self.on_command, t, parent=parent,
                       tooltip=f"Opens a new {CONFIG['terminal']} tab running: {CONFIG['task_command']}")

    def build_menu(self):
        self.menu.clear()
        snap = self.snapshot
        overdue, due_today = self.tasks()

        if self.focus:
            f = self.focus
            label = "Break" if f["phase"] == "break" else "Focusing"
            self._item(f"{label}: {clip(f['task']['content'], 48)}")
            self._item("    Show timer window", lambda _: self.panel.show())
            self._item("    Resume" if f.get("paused_remaining") is not None else "    Pause",
                       lambda _: self.toggle_pause())
            self._item("    Complete task and stop", lambda _: self.complete_focused())
            self._item("    Stop focus", lambda _: self.stop_focus())
            self.menu.add(rumps.separator)

        add = self._item("＋ Add task…", lambda _: self.add_task(),
                         tooltip=f"Quick add: dates, #project, @label and p1 work as in Todoist; {self.default_project_hint()}")
        if "add" in self.hotkey_labels:
            self._with_shortcut(add, "＋ Add task…", self.hotkey_labels["add"])
        self.menu.add(rumps.separator)

        if snap["error"]:
            self._item(f"⚠️ {clip(snap['error'], 70)}")
        if snap["updated"] is None and not snap["error"]:
            self._item("Loading…")

        if overdue:
            self._item(f"Overdue ({len(overdue)})")
            for t in overdue:
                self._add_task(t, True)
            self._item(f"⤵ Move {len(overdue)} overdue to today", self.on_move_overdue,
                       tooltip="Keeps times and recurrence")
            self.menu.add(rumps.separator)

        if snap["updated"] is not None:
            self._item(f"Today ({len(due_today)})")
            for t in due_today:
                self._add_task(t, False)
            if not due_today:
                self._item("    Nothing left today 🎉")
            left = overdue + due_today
            if left:
                self._item(f"⤳ Move {plural(len(left), 'task')} left to {day_label(next_working_day())}",
                           self.on_punt, tooltip="End of day: overdue and today, keeping times and recurrence")
            self.menu.add(rumps.separator)

        updated = datetime.datetime.fromtimestamp(snap["updated"]).strftime("%H:%M") if snap["updated"] else "never"
        self._item(f"Refresh (updated {updated})", lambda _: self.refresh_soon())
        if "menu" in self.hotkey_labels:
            self._item(f"Open this menu: {self.hotkey_labels['menu']}")
        self._item("Open Todoist Today", lambda _: subprocess.Popen(["open", "https://app.todoist.com/app/today"]))
        self._item("Quit", lambda _: rumps.quit_application())

    def _with_shortcut(self, item, text, shortcut):
        """Show a shortcut in grey after the title (NSMenuItem key equivalents only work while the menu is open)"""
        attributed = NSMutableAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: NSFont.menuFontOfSize_(0)})
        attributed.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            f"   {shortcut}", {NSFontAttributeName: NSFont.menuFontOfSize_(11),
                               NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}))
        item._menuitem.setAttributedTitle_(attributed)

    def open_menu(self):
        self._nsapp.nsstatusitem.button().performClick_(None)

    # ── Task actions ────────────────────────────────────────────────────────

    def _background(self, work, ids, failure):
        """Hide tasks straight away, do the API call off the main thread, then refetch"""
        self.hidden |= set(ids)

        def run():
            try:
                work()
            except Exception as e:
                traceback.print_exc()
                self.hidden -= set(ids)
                notify("Todoist", f"{failure}: {e}", sound="Basso")
            self.refresh_soon()
        threading.Thread(target=run, daemon=True).start()

    def complete(self, task):
        log = None
        if self.focus and self.focus["task"]["id"] == task["id"]:
            log = self.end_focus(log=False)

        def work():
            if log:
                self._log(task["id"], log)
            todoist_api.close(task["id"])
        self._background(work, [task["id"]], f"Couldn't complete \"{clip(clean(task['content']), 40)}\"")

    def on_complete(self, sender):
        self.complete(sender.task)

    def on_today(self, sender):
        today = datetime.date.today().isoformat()
        # Moving to today keeps it in the list, so don't hide it
        threading.Thread(target=lambda: self._reschedule([sender.task], today), daemon=True).start()

    def on_tomorrow(self, sender):
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        self._background(lambda: todoist_api.reschedule([sender.task], tomorrow), [sender.task["id"]],
                         "Couldn't move to tomorrow")

    def on_punt(self, _):
        overdue, due_today = self.tasks()
        left, date = overdue + due_today, next_working_day()
        if not left:
            return
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if rumps.alert(f"Move {plural(len(left), 'task')} to {day_label(date)}?",
                       "Every overdue and today task in the menu moves. Times and recurrence are kept.",
                       ok="Move", cancel="Cancel") != 1:
            return
        ids = [t["id"] for t in left]

        def work():
            todoist_api.reschedule(left, date.isoformat())
            notify("Todoist", f"Moved {plural(len(left), 'task')} to {day_label(date)}")
        self._background(work, ids, "Couldn't move today's tasks")

    @staticmethod
    def default_project_hint():
        return f"no #project means #{PROJECT}" if PROJECT else "no #project means the Inbox"

    def add_task(self):
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        hint = self.default_project_hint()
        window = rumps.Window(f"Dates, #project, @label and p1 work as in Todoist; {hint}.",
                              "Add a Todoist task", ok="Add", cancel="Cancel", dimensions=(360, 24))
        window._alert.window().setInitialFirstResponder_(window._textfield)
        response = window.run()
        text = response.text.strip()
        if response.clicked != 1 or not text:
            return
        if PROJECT and not re.search(r"(^|\s)#[^\W\d]", text):  # "#Work" names a project; "#123" doesn't
            text += f" #{PROJECT}"

        def run():
            try:
                task = todoist_api.quick_add(text)
                due = task.get("due")
                when = f" · {due['string']}" if due else " · no date"
                notify("Added to Todoist", clean(task["content"]) + when)
            except Exception as e:
                traceback.print_exc()
                notify("Todoist", f"Couldn't add task: {e}", sound="Basso")
            self.refresh_soon()
        threading.Thread(target=run, daemon=True).start()

    def on_move_overdue(self, _):
        overdue, _ = self.tasks()
        if overdue:
            threading.Thread(target=lambda: self._reschedule(overdue, datetime.date.today().isoformat(), True),
                             daemon=True).start()

    def _reschedule(self, tasks, date, announce=False):
        try:
            todoist_api.reschedule(tasks, date)
            if announce:
                notify("Todoist", f"Moved {len(tasks)} overdue task{'s' * (len(tasks) != 1)} to today")
        except Exception as e:
            traceback.print_exc()
            notify("Todoist", f"Couldn't reschedule: {e}", sound="Basso")
        self.fetch()

    def on_open(self, sender):
        subprocess.Popen(["open", task_url(sender.task)])

    @staticmethod
    def task_command(task):
        """task_command with {id} as is and {url}, {content}, {description} shell-quoted"""
        command = CONFIG["task_command"].replace("{id}", task["id"])
        for name, value in (("url", task_url(task)), ("content", clean(task["content"])),
                            ("description", (task.get("description") or "").strip())):
            command = command.replace("{" + name + "}", shlex.quote(value))
        return command

    def on_command(self, sender):
        """Run task_command in a new terminal tab (or window, if none is open)"""
        if CONFIG["terminal"].lower() == "terminal":
            script = '''
            on run argv
                tell application "Terminal"
                    activate
                    do script (item 1 of argv)
                end tell
            end run'''
        else:
            script = '''
            on run argv
                tell application "iTerm"
                    activate
                    if (count of windows) = 0 then
                        create window with default profile
                    else
                        tell current window to create tab with default profile
                    end if
                    tell current session of current window to write text (item 1 of argv)
                end tell
            end run'''
        subprocess.Popen(["osascript", "-e", script, self.task_command(sender.task)])

    # ── Focus ───────────────────────────────────────────────────────────────

    def on_focus(self, sender):
        t = sender.task
        if self.focus:
            self.end_focus()  # logs time on the previous task
        self.focus = {"task": {"id": t["id"], "content": clean(t["content"]), "priority": t["priority"]},
                      "phase": "work", "ends_at": time.time() + WORK_MINS * 60, "phase_secs": WORK_MINS * 60,
                      "paused_remaining": None, "done": 0, "worked": 0}
        self.save_focus()
        self.panel.show()
        self.tick()

    def remaining(self):
        f = self.focus
        return f["paused_remaining"] if f["paused_remaining"] is not None else f["ends_at"] - time.time()

    def toggle_pause(self):
        f = self.focus
        if not f:
            return
        if f["paused_remaining"] is None:
            f["paused_remaining"] = max(0, f["ends_at"] - time.time())
        else:
            f["ends_at"] = time.time() + f["paused_remaining"]
            f["paused_remaining"] = None
        self.save_focus()
        self.tick()

    def extend(self):
        f = self.focus
        if not f:
            return
        if f["paused_remaining"] is not None:
            f["paused_remaining"] += 300
        else:
            f["ends_at"] = max(f["ends_at"], time.time()) + 300
        f["phase_secs"] = f.get("phase_secs", WORK_MINS * 60) + 300
        self.save_focus()
        self.tick()

    def complete_focused(self):
        if self.focus:
            task = self.focus["task"]
            worked = self.worked()
            self.complete(task)
            notify("Done ✓", f"{task['content']}  ·  {worked // 60} min focus")

    def stop_focus(self):
        self.end_focus()

    def worked(self):
        """Seconds of work so far: finished pomodoros plus the elapsed part of the current one"""
        f = self.focus
        secs = f.get("worked", 0)
        if f["phase"] == "work":
            secs += max(0, f.get("phase_secs", WORK_MINS * 60) - self.remaining())
        return int(secs)

    def end_focus(self, log=True):
        """End the focus. Returns (seconds, pomodoros); with log, also posts them as a task comment."""
        f, summary = self.focus, None
        if f:
            summary = (self.worked(), f["done"])
            if log and summary[0] >= LOG_MIN_SECS:
                threading.Thread(target=self._log, args=(f["task"]["id"], summary), daemon=True).start()
        self.focus = None
        self.save_focus()
        self.panel.hide()
        self.tick()
        return summary if summary and summary[0] >= LOG_MIN_SECS else None

    @staticmethod
    def _log(task_id, summary):
        secs, pomodoros = summary
        text = f"🍅 {round(secs / 60)} min focus"
        if pomodoros:
            text += f" ({plural(pomodoros, 'pomodoro')})"
        text += f" · {datetime.date.today().strftime('%-d %b %Y')}"
        try:
            todoist_api.comment(task_id, text)
        except Exception as e:
            traceback.print_exc()
            notify("Todoist", f"Couldn't log focus time: {e}", sound="Basso")

    def phase_finished(self):
        f = self.focus
        if f["phase"] == "work":
            f["done"] += 1
            f["worked"] = f.get("worked", 0) + f.get("phase_secs", WORK_MINS * 60)
            long_break = f["done"] % LONG_BREAK_EVERY == 0
            mins = LONG_BREAK_MINS if long_break else BREAK_MINS
            f.update(phase="break", ends_at=time.time() + mins * 60, phase_secs=mins * 60, paused_remaining=None)
            notify(f"🍅 {f['done']} done — take {mins}", f["task"]["content"])
        else:
            # Wait for a click to start the next one, so an unattended timer doesn't run on
            f.update(phase="work", paused_remaining=WORK_MINS * 60, phase_secs=WORK_MINS * 60)
            notify("Break over", f"Resume when ready: {f['task']['content']}")
        self.panel.show()
        self.save_focus()

    def tick(self, _=None):
        f = self.focus
        if not f:
            overdue, due_today = self.tasks()
            n = len(overdue) + len(due_today)
            title = f"{ICON_TEXT} {n}" if n else ICON_TEXT  # an image icon leaves ICON_TEXT empty
            if overdue:
                title += f" · {len(overdue)}⚠︎"
            if self.title != title:
                self.title = title
            if self.icon != ICON:
                self.icon = ICON
            return
        if f["paused_remaining"] is None and f["ends_at"] <= time.time():
            self.phase_finished()
        secs = self.remaining()
        paused = f["paused_remaining"] is not None
        task = f["task"]["content"]
        if f["phase"] == "break":
            self.title = f"☕ {mmss(secs)}{' ⏸' if paused else ''}"
            phase = f"Break · {f['done']} 🍅 done"
            clock = f"☕ {mmss(secs)}"
        else:
            self.title = f"🍅 {mmss(secs)}{' ⏸' if paused else ''}  {clip(task, TITLE_CHARS)}"
            phase = f"Pomodoro {f['done'] + 1}" + (" · paused" if paused else "")
            clock = f"🍅 {mmss(secs)}"
        if self.icon is not None:  # the 🍅 or ☕ title marks the app while focusing; an icon would crowd it
            self.icon = None
        if "priority" not in f["task"]:  # focus saved before priority was stored: take it from the task list
            match = next((t for t in self.snapshot["tasks"] if t["id"] == f["task"]["id"]), None)
            if match:
                f["task"]["priority"] = match["priority"]
                self.save_focus()
        panel_task = f"{PRIORITY.get(f['task'].get('priority'), '')} {task}".strip()
        self.panel.update(panel_task, clock, phase, "Start" if paused and f["phase"] == "work"
                          and secs >= WORK_MINS * 60 else ("Resume" if paused else "Pause"),
                          "paused" if paused else f["phase"])

    def load_focus(self):
        try:
            with open(STATE_FILE) as fh:
                return json.load(fh).get("focus")
        except (OSError, ValueError):
            return None

    def save_focus(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as fh:
            json.dump({"focus": self.focus}, fh)


if __name__ == "__main__":
    TodoistMenubar().run()
