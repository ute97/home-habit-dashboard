import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        server.DATA_DIR = server.Path(cls.temp_dir.name)
        server.DB_PATH = server.DATA_DIR / "test.sqlite3"
        server.init_db()
        server.Handler.timezone = ZoneInfo("Etc/UTC")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        cls.temp_dir.cleanup()

    def request(self, method, path, payload=None, expected=200):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.base + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        response_body = response.read()
        self.assertEqual(response.status, expected, response_body.decode())
        if response.status == 204:
            return None
        return json.loads(response_body)

    def setUp(self):
        with server.DB_LOCK, server.database() as db:
            for table in ("completions", "freeze_events", "tasks", "vacations", "goals", "habits", "profiles"):
                db.execute(f"DELETE FROM {table}")
            db.execute(
                "INSERT INTO profiles (id, name, freeze_tokens, created_at) VALUES (1, 'Me', 0, 'test')"
            )

    def test_state_serves_default_profile_and_static_page(self):
        state = self.request("GET", "/api/state")
        self.assertEqual(state["profile"]["name"], "Me")
        self.assertEqual(state["habits"], [])
        response = urlopen(self.base + "/")
        self.assertIn(b"Home Habit Dashboard", response.read())
        script = urlopen(self.base + "/app.js")
        self.assertEqual(script.headers.get_content_type(), "text/javascript")

    def test_profile_habit_completion_and_profile_scoped_deletion(self):
        profile = self.request("POST", "/api/profiles", {"name": "Alex"}, expected=201)
        profile_id = profile["id"]
        habit = self.request("POST", "/api/habits", {
            "profile_id": profile_id,
            "name": "Read",
            "schedule_type": "weekdays",
            "schedule": {"weekdays": [0, 2, 4]},
        }, expected=201)
        day = date.today().isoformat()
        self.request("POST", f"/api/habits/{habit['id']}/toggle", {
            "profile_id": profile_id, "date": day,
        })
        state = self.request("GET", f"/api/state?profile_id={profile_id}&today={day}")
        self.assertEqual(state["habits"][0]["completions"], [day])
        self.request("DELETE", f"/api/profiles/{profile_id}", {}, expected=200)
        self.request("GET", f"/api/state?profile_id={profile_id}", expected=404)

    def test_profile_reset_clears_owned_data_and_preserves_profile(self):
        second = self.request("POST", "/api/profiles", {"name": "Alex"}, expected=201)
        self.request("POST", "/api/profiles/1/tokens", {"amount": 3})
        self.request("POST", f"/api/profiles/{second['id']}/tokens", {"amount": 2})
        first_habit = self.request("POST", "/api/habits", {
            "profile_id": 1, "name": "First habit", "schedule_type": "daily", "schedule": {},
        }, expected=201)
        second_habit = self.request("POST", "/api/habits", {
            "profile_id": second["id"], "name": "Second habit", "schedule_type": "daily", "schedule": {},
        }, expected=201)
        day = date.today().isoformat()
        for habit in (first_habit, second_habit):
            self.request("POST", f"/api/habits/{habit['id']}/toggle", {
                "profile_id": 1 if habit is first_habit else second["id"], "date": day,
            })
        first_task = self.request("POST", "/api/tasks", {
            "profile_id": 1, "title": "Shared task", "shared": True,
        }, expected=201)
        second_task = self.request("POST", "/api/tasks", {
            "profile_id": second["id"], "title": "Alex task",
        }, expected=201)
        first_goal = self.request("POST", "/api/goals", {
            "profile_id": 1, "title": "First goal",
        }, expected=201)
        second_goal = self.request("POST", "/api/goals", {
            "profile_id": second["id"], "title": "Alex goal",
        }, expected=201)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        first_vacation = self.request("POST", "/api/profiles/1/vacations", {
            "start_date": tomorrow, "end_date": tomorrow,
        }, expected=201)
        second_vacation = self.request("POST", f"/api/profiles/{second['id']}/vacations", {
            "start_date": tomorrow, "end_date": tomorrow,
        }, expected=201)
        with server.DB_LOCK, server.database() as db:
            for habit in (first_habit, second_habit):
                db.execute(
                    "INSERT INTO freeze_events (habit_id, period_key, protected_on) VALUES (?, ?, ?)",
                    (habit["id"], f"day:{day}", day),
                )

        self.request("POST", "/api/profiles/1/reset", {}, expected=200)

        first_state = self.request("GET", "/api/state?profile_id=1")
        second_state = self.request("GET", f"/api/state?profile_id={second['id']}")
        self.assertEqual(first_state["profile"]["name"], "Me")
        self.assertEqual(first_state["profile"]["freeze_tokens"], 0)
        self.assertEqual(first_state["habits"], [])
        self.assertEqual(first_state["tasks"], [])
        self.assertEqual(first_state["goals"], [])
        self.assertEqual(first_state["vacations"], [])
        self.assertEqual([habit["name"] for habit in second_state["habits"]], ["Second habit"])
        self.assertEqual([task["id"] for task in second_state["tasks"]], [second_task["id"]])
        self.assertEqual([goal["id"] for goal in second_state["goals"]], [second_goal["id"]])
        self.assertEqual([vacation["id"] for vacation in second_state["vacations"]], [second_vacation["id"]])
        self.assertNotIn(first_task["id"], {task["id"] for task in second_state["tasks"]})
        self.assertNotIn(first_goal["id"], {goal["id"] for goal in second_state["goals"]})
        self.assertNotIn(first_vacation["id"], {vacation["id"] for vacation in second_state["vacations"]})
        self.request("POST", f"/api/profiles/{second['id']}/reset", {}, expected=200)
        last_profile_state = self.request("GET", f"/api/state?profile_id={second['id']}")
        self.assertEqual(last_profile_state["profile"]["name"], "Alex")
        self.assertEqual(last_profile_state["tasks"], [])
        self.request("POST", "/api/profiles/999/reset", {}, expected=404)

    def test_daily_miss_spends_one_token_exactly_once(self):
        today = date.today()
        profile_id = 1
        self.request("POST", "/api/profiles/1/tokens", {"amount": 1})
        self.request("POST", "/api/habits", {
            "profile_id": profile_id, "name": "Walk", "schedule_type": "daily", "schedule": {},
        }, expected=201)
        tomorrow = today + timedelta(days=1)
        state = self.request("GET", f"/api/state?profile_id=1&today={(today + timedelta(days=2)).isoformat()}")
        self.assertEqual(state["profile"]["freeze_tokens"], 0)
        self.assertEqual(state["habits"][0]["freezes"], [tomorrow.isoformat()])
        self.assertEqual(state["habits"][0]["freeze_periods"], [f"day:{tomorrow.isoformat()}"])
        repeated = self.request("GET", f"/api/state?profile_id=1&today={(today + timedelta(days=2)).isoformat()}")
        self.assertEqual(repeated["profile"]["freeze_tokens"], 0)
        self.assertEqual(len(repeated["habits"][0]["freezes"]), 1)

    def test_weekly_target_spends_at_most_one_token_for_missed_period(self):
        today = date.today()
        profile = self.request("POST", "/api/profiles", {"name": "Weekly"}, expected=201)
        profile_id = profile["id"]
        self.request("POST", f"/api/profiles/{profile_id}/tokens", {"amount": 1})
        habit = self.request("POST", "/api/habits", {
            "profile_id": profile_id, "name": "Swim", "schedule_type": "weekly_target", "schedule": {"target": 3},
        }, expected=201)
        next_monday = today + timedelta(days=(7 - today.weekday()))
        after_week = next_monday + timedelta(days=1)
        state = self.request("GET", f"/api/state?profile_id={profile_id}&today={after_week.isoformat()}")
        self.assertEqual(state["profile"]["freeze_tokens"], 0)
        self.assertEqual(len(state["habits"][0]["freezes"]), 1)
        self.assertEqual(state["habits"][0]["freeze_periods"], [f"week:{server.week_key(next_monday - timedelta(days=1))}"])
        self.request("GET", f"/api/state?profile_id={profile_id}&today={after_week.isoformat()}")
        with server.DB_LOCK, server.database() as db:
            count = db.execute("SELECT COUNT(*) FROM freeze_events WHERE habit_id = ?", (habit["id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_target_vacations_require_full_week_or_month_coverage(self):
        week_start = date(2025, 1, 6)
        second_week_start = date(2025, 1, 13)
        month_start = date(2025, 2, 1)
        partial_month_start = date(2025, 3, 1)
        with server.DB_LOCK, server.database() as db:
            db.execute("UPDATE profiles SET freeze_tokens = 50 WHERE id = 1")
            for start, end in (
                (week_start, week_start + timedelta(days=6)),
                (second_week_start, second_week_start + timedelta(days=5)),
                (month_start, date(2025, 2, 28)),
                (partial_month_start, date(2025, 3, 30)),
            ):
                db.execute(
                    "INSERT INTO vacations (profile_id, start_date, end_date, created_at) VALUES (1, ?, ?, 'test')",
                    (start.isoformat(), end.isoformat()),
                )
            habits = {}
            for name, kind, target, created, reconciled in (
                ("Full week", "weekly_target", 3, week_start, week_start - timedelta(days=1)),
                ("Partial week", "weekly_target", 3, second_week_start, second_week_start - timedelta(days=1)),
                ("Full month", "monthly_target", 4, month_start, month_start - timedelta(days=1)),
                ("Partial month", "monthly_target", 4, partial_month_start, partial_month_start - timedelta(days=1)),
            ):
                cursor = db.execute(
                    """INSERT INTO habits
                    (profile_id, name, schedule_type, schedule_json, created_on, last_reconciled)
                    VALUES (1, ?, ?, ?, ?, ?)""",
                    (name, kind, json.dumps({"target": target}), created.isoformat(), reconciled.isoformat()),
                )
                habits[name] = cursor.lastrowid
            server.reconcile_profile(db, 1, date(2025, 4, 1))
            events = {
                name: {row["period_key"] for row in db.execute(
                    "SELECT period_key FROM freeze_events WHERE habit_id = ?", (habit_id,)
                )}
                for name, habit_id in habits.items()
            }
        self.assertNotIn(f"week:{server.week_key(week_start + timedelta(days=6))}", events["Full week"])
        self.assertIn(f"week:{server.week_key(second_week_start + timedelta(days=6))}", events["Partial week"])
        self.assertNotIn("month:2025-02", events["Full month"])
        self.assertIn("month:2025-03", events["Partial month"])

    def test_task_goal_export_import_round_trip_and_validation(self):
        task = self.request("POST", "/api/tasks", {
            "profile_id": 1, "title": "Plan the week", "priority": "high", "shared": True,
        }, expected=201)
        today = date.today()
        vacation = self.request("POST", "/api/profiles/1/vacations", {
            "start_date": (today + timedelta(days=2)).isoformat(),
            "end_date": (today + timedelta(days=4)).isoformat(),
        }, expected=201)
        goal = self.request("POST", "/api/goals", {
            "profile_id": 1, "title": "Read more", "target": 12, "progress": 3, "unit": "books",
            "milestones": ["First book", "Halfway there"],
        }, expected=201)
        exported = self.request("GET", "/api/export")
        self.assertEqual(exported["version"], 2)
        self.assertEqual(exported["tables"]["tasks"][0]["shared"], 1)
        self.assertEqual(len(exported["tables"]["vacations"]), 1)
        self.request("DELETE", f"/api/tasks/{task['id']}", {"profile_id": 1})
        self.request("DELETE", f"/api/vacations/{vacation['id']}", {"profile_id": 1})
        self.request("DELETE", f"/api/goals/{goal['id']}", {"profile_id": 1})
        self.request("POST", "/api/import", exported)
        state = self.request("GET", "/api/state?profile_id=1")
        self.assertEqual(state["tasks"][0]["title"], "Plan the week")
        self.assertTrue(state["tasks"][0]["shared"])
        self.assertEqual(len(state["vacations"]), 1)
        self.assertEqual(state["goals"][0]["milestones"], ["First book", "Halfway there"])
        self.request("POST", "/api/habits", {
            "profile_id": 1, "name": "Invalid", "schedule_type": "weekdays", "schedule": {"weekdays": [9]},
        }, expected=400)

    def test_shared_task_is_visible_and_completable_from_every_profile(self):
        second_profile = self.request("POST", "/api/profiles", {"name": "Alex"}, expected=201)
        personal = self.request("POST", "/api/tasks", {
            "profile_id": 1, "title": "Personal task",
        }, expected=201)
        shared = self.request("POST", "/api/tasks", {
            "profile_id": 1, "title": "Shared task", "shared": True,
        }, expected=201)

        first_state = self.request("GET", "/api/state?profile_id=1")
        second_state = self.request("GET", f"/api/state?profile_id={second_profile['id']}")
        self.assertEqual({task["id"] for task in first_state["tasks"]}, {personal["id"], shared["id"]})
        self.assertEqual([task["id"] for task in second_state["tasks"]], [shared["id"]])
        self.assertTrue(second_state["tasks"][0]["shared"])

        self.request("PATCH", f"/api/tasks/{shared['id']}", {
            "profile_id": second_profile["id"], "title": "Shared task", "notes": "",
            "due_date": None, "priority": "normal", "done": True, "shared": True,
        })
        for profile_id in (1, second_profile["id"]):
            tasks = self.request("GET", f"/api/state?profile_id={profile_id}")["tasks"]
            self.assertTrue(next(task for task in tasks if task["id"] == shared["id"])["done"])

    def test_existing_database_adds_personal_task_scope_idempotently(self):
        original_data_dir, original_db_path = server.DATA_DIR, server.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            server.DATA_DIR = Path(temp_dir)
            server.DB_PATH = server.DATA_DIR / "legacy.sqlite3"
            db = server.sqlite3.connect(server.DB_PATH)
            db.executescript(
                """
                CREATE TABLE profiles (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    freeze_tokens INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', due_date TEXT,
                    priority TEXT NOT NULL DEFAULT 'normal', done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, completed_at TEXT
                );
                INSERT INTO profiles (id, name, created_at) VALUES (1, 'Legacy', 'test');
                INSERT INTO tasks (id, profile_id, title, created_at) VALUES (4, 1, 'Keep personal', 'test');
                """
            )
            db.close()
            try:
                server.init_db()
                server.init_db()
                with server.database() as migrated:
                    task = migrated.execute("SELECT * FROM tasks WHERE id = 4").fetchone()
                    self.assertEqual(task["shared"], 0)
                    self.assertEqual(migrated.execute("SELECT COUNT(*) FROM vacations").fetchone()[0], 0)
            finally:
                server.DATA_DIR, server.DB_PATH = original_data_dir, original_db_path

    def test_vacation_skips_covered_daily_freezes_and_is_profile_scoped(self):
        today = date.today()
        second_profile = self.request("POST", "/api/profiles", {"name": "Alex"}, expected=201)
        vacation = self.request("POST", "/api/profiles/1/vacations", {
            "start_date": (today + timedelta(days=1)).isoformat(),
            "end_date": (today + timedelta(days=2)).isoformat(),
        }, expected=201)
        self.assertIn("id", vacation)
        self.request("POST", "/api/profiles/1/tokens", {"amount": 1})
        habit = self.request("POST", "/api/habits", {
            "profile_id": 1, "name": "Walk", "schedule_type": "daily", "schedule": {},
        }, expected=201)

        first_state = self.request("GET", f"/api/state?profile_id=1&today={(today + timedelta(days=4)).isoformat()}")
        second_state = self.request("GET", f"/api/state?profile_id={second_profile['id']}")
        self.assertEqual(first_state["vacations"], [{
            "id": vacation["id"], "start_date": (today + timedelta(days=1)).isoformat(),
            "end_date": (today + timedelta(days=2)).isoformat(),
        }])
        self.assertEqual(second_state["vacations"], [])
        self.assertEqual(first_state["profile"]["freeze_tokens"], 0)
        self.assertEqual(first_state["habits"][0]["freezes"], [(today + timedelta(days=3)).isoformat()])
        with server.DB_LOCK, server.database() as db:
            events = [row["protected_on"] for row in db.execute(
                "SELECT protected_on FROM freeze_events WHERE habit_id = ? ORDER BY protected_on",
                (habit["id"],),
            )]
        self.assertEqual(events, [(today + timedelta(days=3)).isoformat()])
        self.request("POST", "/api/profiles/1/vacations", {
            "start_date": (today - timedelta(days=1)).isoformat(),
            "end_date": today.isoformat(),
        }, expected=400)

    def test_import_rejects_wrong_format_without_mutating_existing_data(self):
        self.request("POST", "/api/tasks", {"profile_id": 1, "title": "Keep me"}, expected=201)
        self.request("POST", "/api/import", {"format": "other", "version": 1, "tables": {}}, expected=400)
        state = self.request("GET", "/api/state?profile_id=1")
        self.assertEqual(state["tasks"][0]["title"], "Keep me")

    def test_version_one_import_defaults_shared_tasks_and_clears_vacations(self):
        self.request("POST", "/api/tasks", {"profile_id": 1, "title": "Legacy task"}, expected=201)
        today = date.today()
        self.request("POST", "/api/profiles/1/vacations", {
            "start_date": (today + timedelta(days=1)).isoformat(),
            "end_date": (today + timedelta(days=2)).isoformat(),
        }, expected=201)
        exported = self.request("GET", "/api/export")
        legacy_tables = {
            name: exported["tables"][name]
            for name in ("profiles", "habits", "completions", "freeze_events", "tasks", "goals")
        }
        for task in legacy_tables["tasks"]:
            task.pop("shared")
        legacy = {"format": "home-habit-dashboard", "version": 1, "tables": legacy_tables}
        self.request("POST", "/api/import", legacy)
        state = self.request("GET", "/api/state?profile_id=1")
        self.assertFalse(state["tasks"][0]["shared"])
        self.assertEqual(state["vacations"], [])

    def test_import_preserves_non_default_profile_ids(self):
        with server.DB_LOCK, server.database() as db:
            db.execute("DELETE FROM profiles")
            db.execute(
                "INSERT INTO profiles (id, name, freeze_tokens, created_at) VALUES (7, 'Riley', 0, 'test')"
            )
        self.request("POST", "/api/tasks", {"profile_id": 7, "title": "Keep profile id"}, expected=201)
        exported = self.request("GET", "/api/export")
        self.request("POST", "/api/import", exported)
        restored = self.request("GET", "/api/state?profile_id=7")
        self.assertEqual(restored["profile"]["name"], "Riley")
        self.assertEqual(restored["tasks"][0]["title"], "Keep profile id")
        self.request("GET", "/api/state?profile_id=1", expected=404)


if __name__ == "__main__":
    unittest.main()
