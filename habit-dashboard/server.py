#!/usr/bin/env python3
"""Small, dependency-free HTTP API and SQLite store for the HA app."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "habits.sqlite3"
WEB_DIR = Path(__file__).parent / "www"
MAX_BODY = 20_000_000
DB_LOCK = threading.Lock()
WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
HABIT_TYPES = {"daily", "weekdays", "weekly_days", "weekly_target", "monthly_dates", "monthly_target"}
TASK_PRIORITIES = {"low", "normal", "high"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean_text(value: object, label: str, limit: int = 200, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ApiError(f"{label} must be text.")
    result = value.strip()
    if required and not result:
        raise ApiError(f"{label} is required.")
    if len(result) > limit:
        raise ApiError(f"{label} must be at most {limit} characters.")
    return result


def valid_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ApiError(f"{label} must be an ISO date.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as err:
        raise ApiError(f"{label} must be an ISO date.") from err


def vacation_dates(start_value: object, end_value: object, today: date) -> tuple[str, str]:
    start = valid_date(start_value, "Vacation start date")
    end = valid_date(end_value, "Vacation end date")
    if start != start_value or end != end_value:
        raise ApiError("Vacation dates must use YYYY-MM-DD format.")
    if start < today.isoformat():
        raise ApiError("Vacation ranges cannot start in the past.")
    if start > end:
        raise ApiError("Vacation start date must be on or before its end date.")
    return start, end


def parse_options_timezone() -> ZoneInfo:
    options_path = Path(os.environ.get("APP_OPTIONS_PATH", DATA_DIR / "options.json"))
    name = "Etc/UTC"
    if options_path.exists():
        try:
            options = json.loads(options_path.read_text(encoding="utf-8"))
            name = options.get("timezone", name)
        except (OSError, json.JSONDecodeError) as err:
            raise RuntimeError(f"Cannot read app options at {options_path}: {err}") from err
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError) as err:
        raise RuntimeError(f"Invalid IANA timezone in app options: {name}") from err


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


@contextmanager
def database():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                freeze_tokens INTEGER NOT NULL DEFAULT 0 CHECK (freeze_tokens >= 0),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                schedule_type TEXT NOT NULL,
                schedule_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                created_on TEXT NOT NULL,
                last_reconciled TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS completions (
                habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
                completed_on TEXT NOT NULL,
                PRIMARY KEY (habit_id, completed_on)
            );
            CREATE TABLE IF NOT EXISTS freeze_events (
                id INTEGER PRIMARY KEY,
                habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
                period_key TEXT NOT NULL,
                protected_on TEXT NOT NULL,
                UNIQUE (habit_id, period_key)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                due_date TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                shared INTEGER NOT NULL DEFAULT 0 CHECK (shared IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS vacations (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (start_date <= end_date)
            );
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY,
                profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                target REAL NOT NULL DEFAULT 1,
                progress REAL NOT NULL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT '',
                due_date TEXT,
                milestones_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS habits_profile ON habits(profile_id, active);
            CREATE INDEX IF NOT EXISTS tasks_profile ON tasks(profile_id, done, due_date);
            CREATE INDEX IF NOT EXISTS goals_profile ON goals(profile_id);
            """
        )
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "shared" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN shared INTEGER NOT NULL DEFAULT 0 CHECK (shared IN (0, 1))")
        if db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO profiles (name, freeze_tokens, created_at) VALUES (?, 0, ?)",
                ("Me", datetime.now().isoformat(timespec="seconds")),
            )


def week_key(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def vacation_intervals(db: sqlite3.Connection, profile_id: int) -> list[tuple[date, date]]:
    return [
        (date.fromisoformat(row["start_date"]), date.fromisoformat(row["end_date"]))
        for row in db.execute(
            "SELECT start_date, end_date FROM vacations WHERE profile_id = ? ORDER BY start_date, end_date",
            (profile_id,),
        )
    ]


def vacation_covers_day(day: date, ranges: list[tuple[date, date]]) -> bool:
    return any(start <= day <= end for start, end in ranges)


def vacation_covers_period(start: date, end: date, ranges: list[tuple[date, date]]) -> bool:
    cursor = start
    for range_start, range_end in ranges:
        if range_end < cursor:
            continue
        if range_start > cursor:
            return False
        cursor = max(cursor, range_end + timedelta(days=1))
        if cursor > end:
            return True
    return False


def schedule_matches(habit: sqlite3.Row, day: date) -> bool:
    schedule = json.loads(habit["schedule_json"])
    kind = habit["schedule_type"]
    if kind == "daily":
        return True
    if kind == "weekdays":
        return day.weekday() in schedule.get("weekdays", [])
    if kind == "weekly_days":
        return day.weekday() in schedule.get("weekdays", [])
    if kind == "monthly_dates":
        return day.day in schedule.get("dates", [])
    return False


def grant_freeze_if_available(db: sqlite3.Connection, habit: sqlite3.Row, key: str, day: date) -> None:
    existing = db.execute(
        "SELECT 1 FROM freeze_events WHERE habit_id = ? AND period_key = ?",
        (habit["id"], key),
    ).fetchone()
    if existing:
        return
    profile = db.execute("SELECT freeze_tokens FROM profiles WHERE id = ?", (habit["profile_id"],)).fetchone()
    if profile and profile["freeze_tokens"] > 0:
        db.execute("UPDATE profiles SET freeze_tokens = freeze_tokens - 1 WHERE id = ?", (habit["profile_id"],))
        db.execute(
            "INSERT INTO freeze_events (habit_id, period_key, protected_on) VALUES (?, ?, ?)",
            (habit["id"], key, day.isoformat()),
        )


def reconcile_profile(db: sqlite3.Connection, profile_id: int, today: date) -> None:
    vacations = vacation_intervals(db, profile_id)
    habits = db.execute("SELECT * FROM habits WHERE profile_id = ? AND active = 1", (profile_id,)).fetchall()
    for habit in habits:
        start = date.fromisoformat(habit["last_reconciled"]) + timedelta(days=1)
        created = date.fromisoformat(habit["created_on"])
        cursor = max(start, created)
        changed = False
        while cursor < today:
            kind = habit["schedule_type"]
            if kind in {"daily", "weekdays", "monthly_dates"} and schedule_matches(habit, cursor):
                complete = db.execute(
                    "SELECT 1 FROM completions WHERE habit_id = ? AND completed_on = ?",
                    (habit["id"], cursor.isoformat()),
                ).fetchone()
                if not complete and not vacation_covers_day(cursor, vacations):
                    key = f"day:{cursor.isoformat()}"
                    grant_freeze_if_available(db, habit, key, cursor)
            elif kind == "weekly_target" and cursor.weekday() == 6:
                schedule = json.loads(habit["schedule_json"])
                count = db.execute(
                    "SELECT COUNT(*) FROM completions WHERE habit_id = ? AND completed_on BETWEEN ? AND ?",
                    (habit["id"], (cursor - timedelta(days=6)).isoformat(), cursor.isoformat()),
                ).fetchone()[0]
                week_start = cursor - timedelta(days=6)
                if count < schedule["target"] and not vacation_covers_period(week_start, cursor, vacations):
                    grant_freeze_if_available(db, habit, f"week:{week_key(cursor)}", cursor)
            elif kind == "monthly_target":
                next_day = cursor + timedelta(days=1)
                if next_day.month != cursor.month:
                    schedule = json.loads(habit["schedule_json"])
                    month_start = cursor.replace(day=1).isoformat()
                    count = db.execute(
                        "SELECT COUNT(*) FROM completions WHERE habit_id = ? AND completed_on BETWEEN ? AND ?",
                        (habit["id"], month_start, cursor.isoformat()),
                    ).fetchone()[0]
                    if count < schedule["target"] and not vacation_covers_period(
                        cursor.replace(day=1), cursor, vacations
                    ):
                        grant_freeze_if_available(db, habit, f"month:{month_key(cursor)}", cursor)
            cursor += timedelta(days=1)
            changed = True
        if changed:
            db.execute(
                "UPDATE habits SET last_reconciled = ? WHERE id = ?",
                ((today - timedelta(days=1)).isoformat(), habit["id"]),
            )


def validate_schedule(kind: object, raw: object) -> tuple[str, str]:
    if kind not in HABIT_TYPES:
        raise ApiError("Choose a supported habit schedule.")
    if not isinstance(raw, dict):
        raise ApiError("Schedule settings must be an object.")
    schedule: dict[str, object]
    if kind in {"weekdays", "weekly_days"}:
        days = raw.get("weekdays")
        if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
            raise ApiError("Choose at least one valid weekday.")
        schedule = {"weekdays": sorted(set(days))}
    elif kind == "monthly_dates":
        dates = raw.get("dates")
        if not isinstance(dates, list) or not dates or any(type(d) is not int or d not in range(1, 32) for d in dates):
            raise ApiError("Choose at least one month date from 1 to 31.")
        schedule = {"dates": sorted(set(dates))}
    elif kind in {"weekly_target", "monthly_target"}:
        maximum = 7 if kind == "weekly_target" else 31
        target = raw.get("target")
        if type(target) is not int or not 1 <= target <= maximum:
            raise ApiError(f"Target must be a whole number between 1 and {maximum}.")
        schedule = {"target": target}
    else:
        schedule = {}
    return str(kind), json.dumps(schedule)


def profile_exists(db: sqlite3.Connection, profile_id: int) -> None:
    if not db.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone():
        raise ApiError("Profile not found.", 404)


def habit_for_profile(db: sqlite3.Connection, habit_id: int, profile_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM habits WHERE id = ? AND profile_id = ?", (habit_id, profile_id)).fetchone()
    if not row:
        raise ApiError("Habit not found.", 404)
    return row


def state_payload(db: sqlite3.Connection, profile_id: int, today: date) -> dict[str, object]:
    profile_exists(db, profile_id)
    reconcile_profile(db, profile_id, today)
    profile = dict(db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone())
    habits = []
    for row in db.execute("SELECT * FROM habits WHERE profile_id = ? ORDER BY active DESC, id", (profile_id,)):
        habit = dict(row)
        habit["schedule"] = json.loads(habit.pop("schedule_json"))
        habit["active"] = bool(habit["active"])
        habit["completions"] = [
            item["completed_on"]
            for item in db.execute(
                "SELECT completed_on FROM completions WHERE habit_id = ? AND completed_on >= ? ORDER BY completed_on",
                (row["id"], (today - timedelta(days=365)).isoformat()),
            )
        ]
        habit["freezes"] = [
            item["protected_on"]
            for item in db.execute(
                "SELECT protected_on FROM freeze_events WHERE habit_id = ? ORDER BY protected_on",
                (row["id"],),
            )
        ]
        habit["freeze_periods"] = [
            item["period_key"]
            for item in db.execute(
                "SELECT period_key FROM freeze_events WHERE habit_id = ? ORDER BY period_key",
                (row["id"],),
            )
        ]
        habits.append(habit)
    tasks = []
    for row in db.execute(
        "SELECT * FROM tasks WHERE profile_id = ? OR shared = 1 ORDER BY done, due_date IS NULL, due_date, id",
        (profile_id,),
    ):
        task = dict(row)
        task["done"] = bool(task["done"])
        task["shared"] = bool(task["shared"])
        tasks.append(task)
    goals = []
    for row in db.execute("SELECT * FROM goals WHERE profile_id = ? ORDER BY id DESC", (profile_id,)):
        goal = dict(row)
        goal["milestones"] = json.loads(goal.pop("milestones_json"))
        goals.append(goal)
    return {
        "today": today.isoformat(),
        "profile": profile,
        "profiles": [dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY id")],
        "habits": habits,
        "tasks": tasks,
        "vacations": [
            dict(row)
            for row in db.execute(
                "SELECT id, start_date, end_date FROM vacations WHERE profile_id = ? ORDER BY start_date, end_date, id",
                (profile_id,),
            )
        ],
        "goals": goals,
    }


def export_data(db: sqlite3.Connection) -> dict[str, object]:
    tables = ("profiles", "habits", "completions", "freeze_events", "tasks", "goals", "vacations")
    return {
        "format": "home-habit-dashboard",
        "version": 2,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "tables": {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in tables
        },
    }


def import_data(db: sqlite3.Connection, payload: object) -> None:
    if (
        not isinstance(payload, dict)
        or payload.get("format") != "home-habit-dashboard"
        or type(payload.get("version")) is not int
        or payload["version"] not in {1, 2}
    ):
        raise ApiError("This file is not a supported dashboard export.")
    version = payload["version"]
    tables = payload.get("tables")
    expected = ("profiles", "habits", "completions", "freeze_events", "tasks", "goals")
    if version == 2:
        expected += ("vacations",)
    if not isinstance(tables, dict) or any(not isinstance(tables.get(name), list) for name in expected):
        raise ApiError("The export file is incomplete or invalid.")
    with db:
        for table in ("vacations", "goals", "tasks", "freeze_events", "completions", "habits", "profiles"):
            db.execute(f"DELETE FROM {table}")
        for table in expected:
            for item in tables[table]:
                if not isinstance(item, dict):
                    raise ApiError(f"Invalid row in {table}.")
                columns = {
                    "profiles": {"id", "name", "freeze_tokens", "created_at"},
                    "habits": {"id", "profile_id", "name", "description", "schedule_type", "schedule_json", "active", "created_on", "last_reconciled"},
                    "completions": {"habit_id", "completed_on"},
                    "freeze_events": {"id", "habit_id", "period_key", "protected_on"},
                    "tasks": {"id", "profile_id", "title", "notes", "due_date", "priority", "done", "created_at", "completed_at"}
                    | ({"shared"} if version == 2 else set()),
                    "goals": {"id", "profile_id", "title", "description", "target", "progress", "unit", "due_date", "milestones_json", "created_at"},
                    "vacations": {"id", "profile_id", "start_date", "end_date", "created_at"},
                }[table]
                if set(item) != columns:
                    raise ApiError(f"Invalid fields in {table}.")
                item = dict(item)
                if table == "tasks" and version == 1:
                    item["shared"] = 0
                if table == "tasks" and version == 2 and (
                    type(item["shared"]) not in (int, bool) or item["shared"] not in (0, 1)
                ):
                    raise ApiError("Invalid shared value in tasks.")
                if table == "vacations":
                    start = valid_date(item["start_date"], "Vacation start date")
                    end = valid_date(item["end_date"], "Vacation end date")
                    if start != item["start_date"] or end != item["end_date"] or start > end:
                        raise ApiError("Invalid vacation date range in export.")
                names = list(item)
                db.execute(
                    f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    [item[name] for name in names],
                )
        if db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
            raise ApiError("An import must contain at least one profile.")


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeHabitDashboard/0.1"
    timezone = ZoneInfo("Etc/UTC")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as err:
            raise ApiError("Invalid request body length.") from err
        if length <= 0 or length > MAX_BODY:
            raise ApiError("Request body is empty or too large.", 413)
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise ApiError("Request body must be valid JSON.") from err

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                query = parse_qs(parsed.query)
                profile_id = int(query.get("profile_id", ["1"])[0])
                today = datetime.now(self.timezone).date()
                if query.get("today"):
                    today = date.fromisoformat(valid_date(query["today"][0], "today"))
                with DB_LOCK, database() as db:
                    self.send_json(state_payload(db, profile_id, today))
                return
            if parsed.path == "/api/export":
                with DB_LOCK, database() as db:
                    self.send_json(export_data(db))
                return
            if parsed.path.startswith("/api/"):
                self.send_json({"error": "Not found."}, 404)
                return
            self.serve_static(parsed.path)
        except ApiError as err:
            self.send_json({"error": str(err)}, err.status)
        except ValueError as err:
            self.send_json({"error": f"Invalid request: {err}"}, 400)
        except sqlite3.DatabaseError as err:
            print(f"[dashboard] Database error during GET: {err}", flush=True)
            self.send_json({"error": "A database operation failed. Check the app logs."}, 500)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            payload = self.read_json()
            if path == "/api/import":
                with DB_LOCK, database() as db:
                    import_data(db, payload)
                self.send_json({"ok": True})
                return
            with DB_LOCK, database() as db:
                result = self.mutate(db, path, payload)
            created = path in {"/api/profiles", "/api/habits", "/api/tasks", "/api/goals"} or bool(
                re.fullmatch(r"/api/profiles/\d+/vacations", path)
            )
            self.send_json(result, 201 if created else 200)
        except ApiError as err:
            self.send_json({"error": str(err)}, err.status)
        except (ValueError, KeyError, TypeError) as err:
            self.send_json({"error": f"Invalid request: {err}"}, 400)
        except sqlite3.IntegrityError as err:
            print(f"[dashboard] Data integrity error during POST: {err}", flush=True)
            self.send_json({"error": "The data conflicts with the current database. Check the import file and app logs."}, 400)
        except sqlite3.DatabaseError as err:
            print(f"[dashboard] Database error during POST: {err}", flush=True)
            self.send_json({"error": "A database operation failed. Check the app logs."}, 500)

    def do_PATCH(self) -> None:
        try:
            with DB_LOCK, database() as db:
                result = self.mutate(db, urlparse(self.path).path, self.read_json())
            self.send_json(result)
        except ApiError as err:
            self.send_json({"error": str(err)}, err.status)
        except (ValueError, KeyError, TypeError) as err:
            self.send_json({"error": f"Invalid request: {err}"}, 400)
        except sqlite3.IntegrityError as err:
            print(f"[dashboard] Data integrity error during PATCH: {err}", flush=True)
            self.send_json({"error": "The data conflicts with the current database. Check the app logs."}, 400)
        except sqlite3.DatabaseError as err:
            print(f"[dashboard] Database error during PATCH: {err}", flush=True)
            self.send_json({"error": "A database operation failed. Check the app logs."}, 500)

    def do_DELETE(self) -> None:
        try:
            with DB_LOCK, database() as db:
                payload = self.read_json() if self.headers.get("Content-Length") else {}
                result = self.mutate(db, urlparse(self.path).path, payload)
            self.send_json(result)
        except ApiError as err:
            self.send_json({"error": str(err)}, err.status)
        except (ValueError, KeyError, TypeError) as err:
            self.send_json({"error": f"Invalid request: {err}"}, 400)
        except sqlite3.IntegrityError as err:
            print(f"[dashboard] Data integrity error during DELETE: {err}", flush=True)
            self.send_json({"error": "The data conflicts with the current database. Check the app logs."}, 400)
        except sqlite3.DatabaseError as err:
            print(f"[dashboard] Database error during DELETE: {err}", flush=True)
            self.send_json({"error": "A database operation failed. Check the app logs."}, 500)

    def mutate(self, db: sqlite3.Connection, path: str, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise ApiError("Request body must be an object.")
        profile_match = re.fullmatch(r"/api/profiles/(\d+)(?:/(tokens))?", path)
        vacation_profile_match = re.fullmatch(r"/api/profiles/(\d+)/vacations", path)
        vacation_match = re.fullmatch(r"/api/vacations/(\d+)", path)
        habit_match = re.fullmatch(r"/api/habits/(\d+)(?:/(toggle))?", path)
        task_match = re.fullmatch(r"/api/tasks/(\d+)", path)
        goal_match = re.fullmatch(r"/api/goals/(\d+)", path)
        method = self.command
        today = datetime.now(self.timezone).date()

        if vacation_profile_match and method == "POST":
            profile_id = int(vacation_profile_match.group(1))
            profile_exists(db, profile_id)
            start, end = vacation_dates(payload.get("start_date"), payload.get("end_date"), today)
            with db:
                cursor = db.execute(
                    "INSERT INTO vacations (profile_id, start_date, end_date, created_at) VALUES (?, ?, ?, ?)",
                    (profile_id, start, end, datetime.now().isoformat(timespec="seconds")),
                )
            return {"id": cursor.lastrowid}
        if vacation_match:
            vacation_id = int(vacation_match.group(1))
            profile_id = int(payload.get("profile_id", 0))
            profile_exists(db, profile_id)
            vacation = db.execute(
                "SELECT * FROM vacations WHERE id = ? AND profile_id = ?", (vacation_id, profile_id)
            ).fetchone()
            if not vacation:
                raise ApiError("Vacation not found.", 404)
            if vacation["start_date"] < today.isoformat():
                raise ApiError("Started vacation ranges cannot be changed.")
            if method == "PATCH":
                start, end = vacation_dates(payload.get("start_date"), payload.get("end_date"), today)
                with db:
                    db.execute(
                        "UPDATE vacations SET start_date = ?, end_date = ? WHERE id = ?",
                        (start, end, vacation_id),
                    )
                return {"ok": True}
            if method == "DELETE":
                with db:
                    db.execute("DELETE FROM vacations WHERE id = ?", (vacation_id,))
                return {"ok": True}

        if path == "/api/profiles" and method == "POST":
            name = clean_text(payload.get("name"), "Profile name", 60)
            with db:
                cursor = db.execute(
                    "INSERT INTO profiles (name, freeze_tokens, created_at) VALUES (?, 0, ?)",
                    (name, datetime.now().isoformat(timespec="seconds")),
                )
            return {"id": cursor.lastrowid, "name": name, "freeze_tokens": 0}

        if profile_match:
            profile_id = int(profile_match.group(1))
            action = profile_match.group(2)
            profile_exists(db, profile_id)
            if action == "tokens" and method == "POST":
                amount = payload.get("amount")
                if type(amount) is not int or not 1 <= amount <= 100:
                    raise ApiError("Grant between 1 and 100 tokens.")
                with db:
                    db.execute("UPDATE profiles SET freeze_tokens = freeze_tokens + ? WHERE id = ?", (amount, profile_id))
                return {"ok": True}
            if not action and method == "PATCH":
                name = clean_text(payload.get("name"), "Profile name", 60)
                with db:
                    db.execute("UPDATE profiles SET name = ? WHERE id = ?", (name, profile_id))
                return {"ok": True}
            if not action and method == "DELETE":
                if db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] <= 1:
                    raise ApiError("Keep at least one profile.")
                with db:
                    db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
                return {"ok": True}

        if path == "/api/habits" and method == "POST":
            profile_id = int(payload.get("profile_id", 0))
            profile_exists(db, profile_id)
            name = clean_text(payload.get("name"), "Habit name", 100)
            description = clean_text(payload.get("description", ""), "Description", 500, required=False)
            kind, schedule = validate_schedule(payload.get("schedule_type"), payload.get("schedule", {}))
            with db:
                cursor = db.execute(
                    """INSERT INTO habits
                    (profile_id, name, description, schedule_type, schedule_json, created_on, last_reconciled)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, name, description, kind, schedule, today.isoformat(), today.isoformat()),
                )
            return {"id": cursor.lastrowid}
        if habit_match:
            habit_id = int(habit_match.group(1))
            action = habit_match.group(2)
            profile_id = int(payload.get("profile_id", 0))
            habit = habit_for_profile(db, habit_id, profile_id)
            if action == "toggle" and method == "POST":
                day = date.fromisoformat(valid_date(payload.get("date", today.isoformat()), "Completion date"))
                with db:
                    existing = db.execute(
                        "SELECT 1 FROM completions WHERE habit_id = ? AND completed_on = ?",
                        (habit_id, day.isoformat()),
                    ).fetchone()
                    if existing:
                        db.execute("DELETE FROM completions WHERE habit_id = ? AND completed_on = ?", (habit_id, day.isoformat()))
                    else:
                        db.execute("INSERT INTO completions (habit_id, completed_on) VALUES (?, ?)", (habit_id, day.isoformat()))
                return {"completed": not bool(existing)}
            if not action and method == "PATCH":
                name = clean_text(payload.get("name"), "Habit name", 100)
                description = clean_text(payload.get("description", ""), "Description", 500, required=False)
                kind, schedule = validate_schedule(payload.get("schedule_type"), payload.get("schedule", {}))
                active = payload.get("active", True)
                if type(active) is not bool:
                    raise ApiError("Active must be true or false.")
                with db:
                    db.execute(
                        """UPDATE habits SET name = ?, description = ?, schedule_type = ?,
                        schedule_json = ?, active = ? WHERE id = ?""",
                        (name, description, kind, schedule, int(active), habit_id),
                    )
                return {"ok": True}
            if not action and method == "DELETE":
                with db:
                    db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
                return {"ok": True}

        if path == "/api/tasks" and method == "POST":
            profile_id = int(payload.get("profile_id", 0))
            profile_exists(db, profile_id)
            title = clean_text(payload.get("title"), "Task title", 160)
            notes = clean_text(payload.get("notes", ""), "Notes", 1000, required=False)
            due = valid_date(payload["due_date"], "Due date") if payload.get("due_date") else None
            priority = payload.get("priority", "normal")
            shared = payload.get("shared", False)
            if priority not in TASK_PRIORITIES:
                raise ApiError("Choose low, normal, or high priority.")
            if type(shared) is not bool:
                raise ApiError("Shared must be true or false.")
            with db:
                cursor = db.execute(
                    """INSERT INTO tasks (profile_id, title, notes, due_date, priority, created_at, shared)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, title, notes, due, priority, datetime.now().isoformat(timespec="seconds"), int(shared)),
                )
            return {"id": cursor.lastrowid}
        if task_match:
            task_id = int(task_match.group(1))
            profile_id = int(payload.get("profile_id", 0))
            profile_exists(db, profile_id)
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row or (row["profile_id"] != profile_id and not row["shared"]):
                raise ApiError("Task not found.", 404)
            if method == "PATCH":
                title = clean_text(payload.get("title"), "Task title", 160)
                notes = clean_text(payload.get("notes", ""), "Notes", 1000, required=False)
                due = valid_date(payload["due_date"], "Due date") if payload.get("due_date") else None
                priority = payload.get("priority", "normal")
                done = payload.get("done", False)
                shared = payload.get("shared", bool(row["shared"]))
                if priority not in TASK_PRIORITIES or type(done) is not bool or type(shared) is not bool:
                    raise ApiError("Invalid task priority or completion state.")
                if row["profile_id"] != profile_id and shared != bool(row["shared"]):
                    raise ApiError("Only the task's profile can change its sharing setting.", 403)
                with db:
                    db.execute(
                        """UPDATE tasks SET title = ?, notes = ?, due_date = ?, priority = ?,
                        done = ?, completed_at = ?, shared = ? WHERE id = ?""",
                        (title, notes, due, priority, int(done), datetime.now().isoformat(timespec="seconds") if done else None, int(shared), task_id),
                    )
                return {"ok": True}
            if method == "DELETE":
                with db:
                    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                return {"ok": True}

        if path == "/api/goals" and method == "POST":
            profile_id = int(payload.get("profile_id", 0))
            profile_exists(db, profile_id)
            title = clean_text(payload.get("title"), "Goal title", 160)
            description = clean_text(payload.get("description", ""), "Description", 1000, required=False)
            target = payload.get("target", 1)
            progress = payload.get("progress", 0)
            unit = clean_text(payload.get("unit", ""), "Unit", 30, required=False)
            due = valid_date(payload["due_date"], "Due date") if payload.get("due_date") else None
            milestones = payload.get("milestones", [])
            if not isinstance(target, (int, float)) or isinstance(target, bool) or target <= 0:
                raise ApiError("Goal target must be greater than zero.")
            if not isinstance(progress, (int, float)) or isinstance(progress, bool) or progress < 0:
                raise ApiError("Goal progress cannot be negative.")
            if not isinstance(milestones, list) or len(milestones) > 30 or any(not isinstance(x, str) or len(x) > 120 for x in milestones):
                raise ApiError("Use up to 30 milestone names, each at most 120 characters.")
            with db:
                cursor = db.execute(
                    """INSERT INTO goals
                    (profile_id, title, description, target, progress, unit, due_date, milestones_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, title, description, target, progress, unit, due, json.dumps(milestones), datetime.now().isoformat(timespec="seconds")),
                )
            return {"id": cursor.lastrowid}
        if goal_match:
            goal_id = int(goal_match.group(1))
            profile_id = int(payload.get("profile_id", 0))
            if not db.execute("SELECT 1 FROM goals WHERE id = ? AND profile_id = ?", (goal_id, profile_id)).fetchone():
                raise ApiError("Goal not found.", 404)
            if method == "PATCH":
                title = clean_text(payload.get("title"), "Goal title", 160)
                description = clean_text(payload.get("description", ""), "Description", 1000, required=False)
                target, progress = payload.get("target", 1), payload.get("progress", 0)
                unit = clean_text(payload.get("unit", ""), "Unit", 30, required=False)
                due = valid_date(payload["due_date"], "Due date") if payload.get("due_date") else None
                milestones = payload.get("milestones", [])
                if not isinstance(target, (int, float)) or isinstance(target, bool) or target <= 0:
                    raise ApiError("Goal target must be greater than zero.")
                if not isinstance(progress, (int, float)) or isinstance(progress, bool) or progress < 0:
                    raise ApiError("Goal progress cannot be negative.")
                if not isinstance(milestones, list) or len(milestones) > 30 or any(not isinstance(x, str) or len(x) > 120 for x in milestones):
                    raise ApiError("Use up to 30 milestone names, each at most 120 characters.")
                with db:
                    db.execute(
                        """UPDATE goals SET title = ?, description = ?, target = ?, progress = ?,
                        unit = ?, due_date = ?, milestones_json = ? WHERE id = ?""",
                        (title, description, target, progress, unit, due, json.dumps(milestones), goal_id),
                    )
                return {"ok": True}
            if method == "DELETE":
                with db:
                    db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
                return {"ok": True}

        raise ApiError("Not found.", 404)

    def serve_static(self, path: str) -> None:
        requested = "index.html" if path in {"", "/"} else path.lstrip("/")
        file_path = (WEB_DIR / requested).resolve()
        if not file_path.is_relative_to(WEB_DIR.resolve()) or not file_path.is_file():
            self.send_error(404)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }
        content_type = content_types.get(file_path.suffix, "application/octet-stream")
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    init_db()
    Handler.timezone = parse_options_timezone()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    print(f"Home Habit Dashboard listening on :8099 (timezone={Handler.timezone})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
