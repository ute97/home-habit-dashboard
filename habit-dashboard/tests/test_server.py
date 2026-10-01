import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
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
            for table in ("completions", "freeze_events", "tasks", "goals", "habits", "profiles"):
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

    def test_task_goal_export_import_round_trip_and_validation(self):
        task = self.request("POST", "/api/tasks", {
            "profile_id": 1, "title": "Plan the week", "priority": "high",
        }, expected=201)
        goal = self.request("POST", "/api/goals", {
            "profile_id": 1, "title": "Read more", "target": 12, "progress": 3, "unit": "books",
            "milestones": ["First book", "Halfway there"],
        }, expected=201)
        exported = self.request("GET", "/api/export")
        self.request("DELETE", f"/api/tasks/{task['id']}", {"profile_id": 1})
        self.request("DELETE", f"/api/goals/{goal['id']}", {"profile_id": 1})
        self.request("POST", "/api/import", exported)
        state = self.request("GET", "/api/state?profile_id=1")
        self.assertEqual(state["tasks"][0]["title"], "Plan the week")
        self.assertEqual(state["goals"][0]["milestones"], ["First book", "Halfway there"])
        self.request("POST", "/api/habits", {
            "profile_id": 1, "name": "Invalid", "schedule_type": "weekdays", "schedule": {"weekdays": [9]},
        }, expected=400)

    def test_import_rejects_wrong_format_without_mutating_existing_data(self):
        self.request("POST", "/api/tasks", {"profile_id": 1, "title": "Keep me"}, expected=201)
        self.request("POST", "/api/import", {"format": "other", "version": 1, "tables": {}}, expected=400)
        state = self.request("GET", "/api/state?profile_id=1")
        self.assertEqual(state["tasks"][0]["title"], "Keep me")

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
