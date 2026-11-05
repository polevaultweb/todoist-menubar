"""Minimal Todoist API v1 client: the calls the menu bar app needs, standard library only.

Token from $TODOIST_API_TOKEN, else the Keychain item "todoist-api-token".
"""

import datetime
import json
import os
import subprocess
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

BASE = "https://api.todoist.com/api/v1"
_token = None


def token():
    global _token
    if not _token:
        _token = os.environ.get("TODOIST_API_TOKEN") or subprocess.run(
            ["security", "find-generic-password", "-s", "todoist-api-token", "-w"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not _token:
            raise RuntimeError("No Todoist token: set TODOIST_API_TOKEN or add a Keychain item named todoist-api-token")
    return _token


def _request(method, path, params=None, body=None):
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _paged(path, params):
    """Follow next_cursor through a list endpoint and return every result"""
    results, cursor = [], None
    while True:
        page = _request("GET", path, {**params, "limit": 200, **({"cursor": cursor} if cursor else {})})
        results += page["results"]
        cursor = page.get("next_cursor")
        if not cursor:
            return results


def filter_tasks(query):
    return _paged("tasks/filter", {"query": query})


def projects():
    return {p["id"]: p["name"] for p in _paged("projects", {})}


def close(task_id):
    """Complete a task. A recurring task moves to its next date, as in the app."""
    _request("POST", f"tasks/{task_id}/close")


def comment(task_id, content):
    _request("POST", "comments", body={"task_id": task_id, "content": content})


def quick_add(text):
    """Add a task from natural language, as in the app's quick add: dates, #project, @label, p1"""
    return _request("POST", "tasks/quick", body={"text": text})


def local_due(due):
    """(YYYY-MM-DD, HH:MM or None) in this Mac's time zone. Tasks with a fixed time zone come back in UTC."""
    d = due["date"]
    if len(d) <= 10:
        return d, None
    dt = datetime.datetime.fromisoformat(d)
    if dt.tzinfo:
        dt = dt.astimezone()
    return dt.date().isoformat(), dt.strftime("%H:%M")


def _moved_date(due, date):
    """The due date string for `date`, keeping the time of day (in the task's own zone, if it has one)"""
    old = due.get("date", "")
    if len(old) <= 10:
        return date
    dt = datetime.datetime.fromisoformat(old)
    if not dt.tzinfo:
        return date + old[10:]
    zone = ZoneInfo(due["timezone"]) if due.get("timezone") else datetime.datetime.now().astimezone().tzinfo
    local = dt.astimezone(zone)
    moved = datetime.datetime.combine(datetime.date.fromisoformat(date), local.time(), tzinfo=zone)
    return moved.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reschedule(tasks, date):
    """Move tasks to a date (YYYY-MM-DD), keeping any time of day and any recurrence.

    The REST update with due_date turns a recurring task into a one-off, so this uses a Sync API
    item_update that sends the existing due string back with the new date.
    """
    commands = []
    for t in tasks:
        due = t.get("due") or {}
        new_due = {"date": _moved_date(due, date)}
        if due.get("is_recurring"):
            new_due.update(string=due["string"], lang=due.get("lang") or "en", is_recurring=True)
        if due.get("timezone"):
            new_due["timezone"] = due["timezone"]
        commands.append({"type": "item_update", "uuid": str(uuid.uuid4()),
                         "args": {"id": t["id"], "due": new_due}})
    for i in range(0, len(commands), 100):  # Sync API takes up to 100 commands per request
        status = _request("POST", "sync", body={"commands": commands[i:i + 100]})["sync_status"]
        failed = {k: v for k, v in status.items() if v != "ok"}
        if failed:
            raise RuntimeError(f"Todoist rejected {len(failed)} update(s): {failed}")
