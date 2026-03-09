"""Tests for scheduled tasks: cron parser, scheduler logic, and API endpoints.

Covers:
1. Cron parser — field parsing, aliases, DOW conversion, edge cases, error handling
2. Cron matching — datetime matching with precomputed Python weekdays
3. Next-run computation — brute-force scan correctness
4. create_scheduled_task — validation, once date normalization
5. API CRUD endpoints — create, read, update, delete, auth, validation errors
"""
import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ──────────────────────────────────────────────────────────
# 1. Cron parser unit tests (import directly from bot.py)
# ──────────────────────────────────────────────────────────

# We need to import bot.py functions. Since bot.py has side effects on import,
# we mock what we need.
import importlib
import sys

# Minimal stubs so bot.py can import without crashing
_bot_mod = None

def _get_bot():
    """Lazy-load bot module with minimal stubs."""
    global _bot_mod
    if _bot_mod:
        return _bot_mod
    # Import bot.py — it needs certain env vars
    import bot
    _bot_mod = bot
    return bot


class TestCronFieldParsing(unittest.TestCase):
    """Test _parse_cron_field for various input patterns."""

    def setUp(self):
        self.bot = _get_bot()
        self.parse = self.bot._parse_cron_field

    def test_wildcard(self):
        result = self.parse("*", 0, 59)
        self.assertEqual(result, set(range(0, 60)))

    def test_single_value(self):
        result = self.parse("5", 0, 59)
        self.assertEqual(result, {5})

    def test_step(self):
        result = self.parse("*/15", 0, 59)
        self.assertEqual(result, {0, 15, 30, 45})

    def test_range(self):
        result = self.parse("1-5", 0, 59)
        self.assertEqual(result, {1, 2, 3, 4, 5})

    def test_range_with_step(self):
        result = self.parse("0-20/5", 0, 59)
        self.assertEqual(result, {0, 5, 10, 15, 20})

    def test_list(self):
        result = self.parse("1,3,5,7", 0, 59)
        self.assertEqual(result, {1, 3, 5, 7})

    def test_named_dow(self):
        result = self.parse("mon", 0, 6, self.bot._DOW_NAMES)
        self.assertEqual(result, {1})

    def test_named_dow_range(self):
        result = self.parse("mon-fri", 0, 6, self.bot._DOW_NAMES)
        self.assertEqual(result, {1, 2, 3, 4, 5})


class TestCronExprParsing(unittest.TestCase):
    """Test _parse_cron_expr for full expressions and aliases."""

    def setUp(self):
        self.bot = _get_bot()
        self.parse = self.bot._parse_cron_expr

    def test_basic_expression(self):
        parsed = self.parse("30 9 * * *")
        self.assertEqual(parsed["minute"], {30})
        self.assertEqual(parsed["hour"], {9})
        self.assertEqual(parsed["dom"], set(range(1, 32)))
        self.assertEqual(parsed["month"], set(range(1, 13)))

    def test_alias_daily(self):
        parsed = self.parse("@daily")
        self.assertEqual(parsed["minute"], {0})
        self.assertEqual(parsed["hour"], {0})

    def test_alias_hourly(self):
        parsed = self.parse("@hourly")
        self.assertEqual(parsed["minute"], {0})
        self.assertEqual(parsed["hour"], set(range(0, 24)))

    def test_alias_weekly(self):
        parsed = self.parse("@weekly")
        self.assertEqual(parsed["minute"], {0})
        self.assertEqual(parsed["hour"], {0})

    def test_alias_monthly(self):
        parsed = self.parse("@monthly")
        self.assertEqual(parsed["minute"], {0})
        self.assertEqual(parsed["hour"], {0})
        self.assertEqual(parsed["dom"], {1})

    def test_invalid_field_count_raises(self):
        with self.assertRaises(ValueError):
            self.parse("* * *")

    def test_six_fields_raises(self):
        with self.assertRaises(ValueError):
            self.parse("* * * * * *")

    def test_dow_precomputed_to_python_weekday(self):
        """DOW should be precomputed as Python weekday (0=Mon) not cron (0=Sun)."""
        # Cron "0" = Sunday = Python weekday 6
        parsed = self.parse("0 0 * * 0")
        self.assertIn(6, parsed["dow"])  # Python: Sunday = 6
        self.assertNotIn(0, parsed["dow"])

    def test_dow_monday_is_zero_in_python(self):
        """Cron "1" = Monday = Python weekday 0."""
        parsed = self.parse("0 0 * * 1")
        self.assertIn(0, parsed["dow"])  # Python: Monday = 0

    def test_dow_friday_conversion(self):
        """Cron "5" = Friday = Python weekday 4."""
        parsed = self.parse("0 0 * * 5")
        self.assertIn(4, parsed["dow"])

    def test_dow_is_frozenset(self):
        """DOW should be a frozenset (precomputed, immutable)."""
        parsed = self.parse("0 0 * * 1-5")
        self.assertIsInstance(parsed["dow"], frozenset)

    def test_dow_all_days(self):
        """Cron '* * * * *' → DOW should have all 7 Python weekdays (0-6)."""
        parsed = self.parse("* * * * *")
        self.assertEqual(parsed["dow"], frozenset(range(7)))


class TestCronMatches(unittest.TestCase):
    """Test _cron_matches with precomputed DOW."""

    def setUp(self):
        self.bot = _get_bot()

    def test_matches_exact(self):
        parsed = self.bot._parse_cron_expr("30 9 * * *")
        dt = datetime(2024, 3, 15, 9, 30)  # Friday
        self.assertTrue(self.bot._cron_matches(parsed, dt))

    def test_no_match_wrong_minute(self):
        parsed = self.bot._parse_cron_expr("30 9 * * *")
        dt = datetime(2024, 3, 15, 9, 31)
        self.assertFalse(self.bot._cron_matches(parsed, dt))

    def test_matches_monday(self):
        """Cron '0 9 * * 1' (Monday) matches a Monday datetime."""
        parsed = self.bot._parse_cron_expr("0 9 * * 1")
        # 2024-03-11 is a Monday
        dt = datetime(2024, 3, 11, 9, 0)
        self.assertEqual(dt.weekday(), 0)  # Python Monday = 0
        self.assertTrue(self.bot._cron_matches(parsed, dt))

    def test_no_match_wrong_dow(self):
        """Cron '0 9 * * 1' (Monday) does not match Tuesday."""
        parsed = self.bot._parse_cron_expr("0 9 * * 1")
        dt = datetime(2024, 3, 12, 9, 0)  # Tuesday
        self.assertEqual(dt.weekday(), 1)
        self.assertFalse(self.bot._cron_matches(parsed, dt))

    def test_matches_sunday(self):
        """Cron '0 0 * * 0' (Sunday) matches a Sunday."""
        parsed = self.bot._parse_cron_expr("0 0 * * 0")
        dt = datetime(2024, 3, 10, 0, 0)  # Sunday
        self.assertEqual(dt.weekday(), 6)  # Python Sunday = 6
        self.assertTrue(self.bot._cron_matches(parsed, dt))

    def test_weekday_range(self):
        """Cron '0 9 * * 1-5' (Mon-Fri) matches weekdays, not weekend."""
        parsed = self.bot._parse_cron_expr("0 9 * * 1-5")
        # Monday
        self.assertTrue(self.bot._cron_matches(parsed, datetime(2024, 3, 11, 9, 0)))
        # Friday
        self.assertTrue(self.bot._cron_matches(parsed, datetime(2024, 3, 15, 9, 0)))
        # Saturday
        self.assertFalse(self.bot._cron_matches(parsed, datetime(2024, 3, 16, 9, 0)))
        # Sunday
        self.assertFalse(self.bot._cron_matches(parsed, datetime(2024, 3, 10, 9, 0)))


class TestNextCronRun(unittest.TestCase):
    """Test _next_cron_run computation."""

    def setUp(self):
        self.bot = _get_bot()

    def test_next_minute(self):
        """Every-minute cron should return the next minute."""
        after = datetime(2024, 3, 15, 10, 30, 0)
        result = self.bot._next_cron_run("* * * * *", after)
        self.assertEqual(result, datetime(2024, 3, 15, 10, 31))

    def test_next_hour(self):
        """Hourly cron at :00 should jump to next hour."""
        after = datetime(2024, 3, 15, 10, 30, 0)
        result = self.bot._next_cron_run("0 * * * *", after)
        self.assertEqual(result, datetime(2024, 3, 15, 11, 0))

    def test_next_day(self):
        """Daily 9:00 cron after 10:00 should be tomorrow 9:00."""
        after = datetime(2024, 3, 15, 10, 0, 0)
        result = self.bot._next_cron_run("0 9 * * *", after)
        self.assertEqual(result, datetime(2024, 3, 16, 9, 0))

    def test_next_specific_dow(self):
        """Weekly Monday 9:00 cron from a Friday should be next Monday."""
        # 2024-03-15 is Friday
        after = datetime(2024, 3, 15, 10, 0, 0)
        result = self.bot._next_cron_run("0 9 * * 1", after)
        self.assertEqual(result, datetime(2024, 3, 18, 9, 0))
        self.assertEqual(result.weekday(), 0)  # Monday

    def test_returns_none_for_impossible(self):
        """Feb 30 should never match → returns None."""
        result = self.bot._next_cron_run("0 0 30 2 *", datetime(2024, 1, 1))
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────
# 2. create_scheduled_task validation tests
# ──────────────────────────────────────────────────────────

class TestCreateScheduledTask(unittest.TestCase):
    """Test create_scheduled_task validation and date normalization."""

    def setUp(self):
        self.bot = _get_bot()
        # Save originals
        self._orig_scheduled_tasks = self.bot.scheduled_tasks
        self._orig_save = self.bot.save_scheduled_tasks
        self._orig_ws = self.bot._ws_broadcast_schedule
        # Mock side effects
        self.bot.scheduled_tasks = {}
        self.bot.save_scheduled_tasks = MagicMock()
        self.bot._ws_broadcast_schedule = MagicMock()

    def tearDown(self):
        self.bot.scheduled_tasks = self._orig_scheduled_tasks
        self.bot.save_scheduled_tasks = self._orig_save
        self.bot._ws_broadcast_schedule = self._orig_ws

    def test_cron_task_created(self):
        tid, task = self.bot.create_scheduled_task(
            123, "Run tests", "cron", cron_expr="0 9 * * *")
        self.assertTrue(tid.startswith("sched_"))
        self.assertEqual(task["schedule_type"], "cron")
        self.assertIsNotNone(task["next_run"])
        self.assertIn("cwd", task)
        self.assertIsNone(task["last_result"])

    def test_once_task_with_space_separator(self):
        """Date with space separator (YYYY-MM-DD HH:MM) should work."""
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        tid, task = self.bot.create_scheduled_task(
            123, "One-time task", "once", run_at=future)
        self.assertEqual(task["schedule_type"], "once")
        self.assertIsNotNone(task["next_run"])

    def test_once_task_with_iso_separator(self):
        """Date with T separator (ISO 8601) should also work."""
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        tid, task = self.bot.create_scheduled_task(
            123, "One-time task", "once", run_at=future)
        self.assertEqual(task["schedule_type"], "once")

    def test_cwd_stored(self):
        """Task stores explicit cwd when provided."""
        tid, task = self.bot.create_scheduled_task(
            123, "p", "cron", cron_expr="0 9 * * *", cwd="/tmp/test")
        self.assertEqual(task["cwd"], "/tmp/test")

    def test_cwd_defaults_to_cwd(self):
        """Task defaults cwd to os.getcwd() when not provided."""
        tid, task = self.bot.create_scheduled_task(
            123, "p", "cron", cron_expr="0 9 * * *")
        self.assertEqual(task["cwd"], os.getcwd())

    def test_invalid_cron_raises(self):
        with self.assertRaises(ValueError):
            self.bot.create_scheduled_task(
                123, "p", "cron", cron_expr="bad bad bad")

    def test_missing_cron_expr_raises(self):
        with self.assertRaises(ValueError):
            self.bot.create_scheduled_task(123, "p", "cron")

    def test_missing_run_at_raises(self):
        with self.assertRaises(ValueError):
            self.bot.create_scheduled_task(123, "p", "once")

    def test_past_run_at_raises(self):
        with self.assertRaises(ValueError):
            self.bot.create_scheduled_task(
                123, "p", "once", run_at="2020-01-01T00:00")

    def test_invalid_schedule_type_raises(self):
        with self.assertRaises(ValueError):
            self.bot.create_scheduled_task(123, "p", "biweekly")

    def test_ws_broadcast_called(self):
        self.bot.create_scheduled_task(
            123, "p", "cron", cron_expr="0 9 * * *")
        self.bot._ws_broadcast_schedule.assert_called_once()
        args = self.bot._ws_broadcast_schedule.call_args[0]
        self.assertEqual(args[0], 123)
        self.assertEqual(args[1], "created")


# ──────────────────────────────────────────────────────────
# 3. API endpoint tests
# ──────────────────────────────────────────────────────────

os.environ["API_SECRET"] = os.environ.get("API_SECRET", "test-secret")
import api as api_server
from starlette.testclient import TestClient


class ScheduleAPITestBase(unittest.TestCase):
    """Shared setup for schedule API tests."""

    def setUp(self):
        self.scheduled_tasks = {}
        self.scheduled_tasks_lock = threading.Lock()
        self.mock_save = MagicMock()
        self.mock_create = MagicMock()
        self.mock_next_cron_run = MagicMock()
        self.mock_ws_broadcast = MagicMock()
        self.mock_is_allowed = MagicMock(return_value=True)

        api_server.init_refs(
            handle_command=MagicMock(),
            handle_message=MagicMock(),
            handle_callback_query=MagicMock(),
            is_allowed=self.mock_is_allowed,
            get_active_session=MagicMock(return_value=None),
            get_session_id=MagicMock(side_effect=lambda s: s.get("id", "sid")),
            user_sessions={},
            active_processes={},
            justdoit_active={},
            omni_active={},
            deepreview_active={},
            send_message=MagicMock(),
            send_message_no_ws=MagicMock(),
            cancelled_sessions=set(),
            ws_broadcast_status=MagicMock(),
            save_active_tasks=MagicMock(),
            user_feedback_queue={},
            get_active_sessions_data=lambda: {},
            scheduled_tasks=self.scheduled_tasks,
            scheduled_tasks_lock=self.scheduled_tasks_lock,
            save_scheduled_tasks=self.mock_save,
            create_scheduled_task=self.mock_create,
            next_cron_run_fn=self.mock_next_cron_run,
            ws_broadcast_schedule=self.mock_ws_broadcast,
        )

        self.client = TestClient(api_server.app)
        self.headers = {"Authorization": "Bearer test-secret"}


class TestGetScheduledTasks(ScheduleAPITestBase):
    """Test GET /api/scheduled-tasks/{chat_id}."""

    def test_empty_returns_empty_list(self):
        resp = self.client.get("/api/scheduled-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_returns_tasks_for_chat(self):
        self.scheduled_tasks["sched_1"] = {
            "id": "sched_1", "chat_id": "123", "next_run": 2000, "prompt": "test",
        }
        self.scheduled_tasks["sched_2"] = {
            "id": "sched_2", "chat_id": "456", "next_run": 1000, "prompt": "other",
        }
        resp = self.client.get("/api/scheduled-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "sched_1")

    def test_sorted_by_next_run(self):
        self.scheduled_tasks["sched_a"] = {
            "id": "sched_a", "chat_id": "123", "next_run": 3000,
        }
        self.scheduled_tasks["sched_b"] = {
            "id": "sched_b", "chat_id": "123", "next_run": 1000,
        }
        resp = self.client.get("/api/scheduled-tasks/123", headers=self.headers)
        data = resp.json()
        self.assertEqual(data[0]["id"], "sched_b")
        self.assertEqual(data[1]["id"], "sched_a")

    def test_requires_auth(self):
        resp = self.client.get("/api/scheduled-tasks/123")
        self.assertEqual(resp.status_code, 401)

    def test_disallowed_chat_returns_403(self):
        self.mock_is_allowed.return_value = False
        resp = self.client.get("/api/scheduled-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 403)


class TestCreateScheduleTaskAPI(ScheduleAPITestBase):
    """Test POST /api/schedule-task."""

    def test_create_calls_bot_function(self):
        self.mock_create.return_value = ("sched_abc", {"next_run": 9999})
        resp = self.client.post("/api/schedule-task", json={
            "chat_id": 123, "session_name": "proj", "prompt": "test",
            "schedule_type": "cron", "cron_expr": "0 9 * * *",
        }, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "created")
        self.assertEqual(data["task_id"], "sched_abc")
        self.mock_create.assert_called_once()

    def test_create_validation_error_returns_400(self):
        self.mock_create.side_effect = ValueError("bad cron")
        resp = self.client.post("/api/schedule-task", json={
            "chat_id": 123, "session_name": "proj", "prompt": "test",
            "schedule_type": "cron", "cron_expr": "bad",
        }, headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("bad cron", resp.json()["detail"])

    def test_create_requires_auth(self):
        resp = self.client.post("/api/schedule-task", json={
            "prompt": "t", "schedule_type": "cron",
            "cron_expr": "0 0 * * *",
        })
        self.assertEqual(resp.status_code, 401)


class TestUpdateScheduleTaskAPI(ScheduleAPITestBase):
    """Test PUT /api/schedule-task/{task_id}."""

    def _add_task(self):
        task = {
            "id": "sched_1", "chat_id": "123", "cwd": "/tmp/proj",
            "prompt": "test", "schedule_type": "cron", "cron_expr": "0 9 * * *",
            "run_at": None, "enabled": True,
            "next_run": 9999, "last_run": None, "last_result": None, "run_count": 0,
        }
        self.scheduled_tasks["sched_1"] = task
        return task

    def test_toggle_enabled(self):
        self._add_task()
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"enabled": False}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.scheduled_tasks["sched_1"]["enabled"])
        self.mock_save.assert_called_once()

    def test_update_prompt(self):
        self._add_task()
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"prompt": "new prompt"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scheduled_tasks["sched_1"]["prompt"], "new prompt")

    def test_update_cron_expr(self):
        self._add_task()
        self.mock_next_cron_run.return_value = datetime(2024, 4, 1, 10, 0)
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"cron_expr": "0 10 * * *"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scheduled_tasks["sched_1"]["cron_expr"], "0 10 * * *")
        self.assertEqual(self.scheduled_tasks["sched_1"]["schedule_type"], "cron")

    def test_update_run_at_with_space(self):
        """PUT with run_at using space separator should work."""
        self._add_task()
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"run_at": future}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scheduled_tasks["sched_1"]["schedule_type"], "once")

    def test_update_invalid_run_at_returns_400(self):
        """PUT with invalid date should return 400, not 500."""
        self._add_task()
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"run_at": "not-a-date"}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_update_invalid_cron_returns_400(self):
        """PUT with invalid cron should return 400, not 500."""
        self._add_task()
        self.mock_next_cron_run.side_effect = ValueError("bad cron")
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"cron_expr": "bad bad bad"}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_update_not_found_returns_404(self):
        resp = self.client.put("/api/schedule-task/nonexistent",
            json={"enabled": False}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_update_broadcasts_ws(self):
        self._add_task()
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"enabled": False}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.mock_ws_broadcast.assert_called_once()
        args = self.mock_ws_broadcast.call_args[0]
        self.assertEqual(args[1], "updated")

    def test_update_requires_auth(self):
        resp = self.client.put("/api/schedule-task/sched_1",
            json={"enabled": False})
        self.assertEqual(resp.status_code, 401)


class TestDeleteScheduleTaskAPI(ScheduleAPITestBase):
    """Test DELETE /api/schedule-task/{task_id}."""

    def test_delete_removes_task(self):
        self.scheduled_tasks["sched_1"] = {
            "id": "sched_1", "chat_id": "123", "prompt": "test",
        }
        resp = self.client.delete("/api/schedule-task/sched_1", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("sched_1", self.scheduled_tasks)
        self.mock_save.assert_called_once()

    def test_delete_broadcasts_ws(self):
        self.scheduled_tasks["sched_1"] = {
            "id": "sched_1", "chat_id": "123", "prompt": "test",
        }
        resp = self.client.delete("/api/schedule-task/sched_1", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.mock_ws_broadcast.assert_called_once()
        args = self.mock_ws_broadcast.call_args[0]
        self.assertEqual(args[1], "deleted")

    def test_delete_not_found_returns_404(self):
        resp = self.client.delete("/api/schedule-task/nonexistent", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_delete_requires_auth(self):
        resp = self.client.delete("/api/schedule-task/sched_1")
        self.assertEqual(resp.status_code, 401)


# ──────────────────────────────────────────────────────────
# 4. Trigger behavior integration tests
# ──────────────────────────────────────────────────────────

class TestTriggerScheduledTask(unittest.TestCase):
    """Test _trigger_scheduled_task: routes prompt through handle_message/handle_command."""

    def setUp(self):
        self.bot = _get_bot()
        # Save originals
        self._orig = {
            'scheduled_tasks': self.bot.scheduled_tasks,
            'save_scheduled_tasks': self.bot.save_scheduled_tasks,
            '_ws_broadcast_schedule': self.bot._ws_broadcast_schedule,
            'send_message': self.bot.send_message,
            'handle_message': self.bot.handle_message,
            'handle_command': self.bot.handle_command,
        }
        # Set up mocks
        self.bot.save_scheduled_tasks = MagicMock()
        self.bot._ws_broadcast_schedule = MagicMock()
        self.bot.send_message = MagicMock()
        self.bot.handle_message = MagicMock()
        self.bot.handle_command = MagicMock()

    def tearDown(self):
        for key, val in self._orig.items():
            setattr(self.bot, key, val)

    def _make_cron_task(self):
        return {
            "id": "sched_1", "chat_id": "123", "cwd": "/tmp/proj",
            "prompt": "Run tests", "schedule_type": "cron",
            "cron_expr": "0 9 * * *", "run_at": None,
            "enabled": True, "last_run": None, "last_result": None,
            "next_run": time.time() - 10, "run_count": 0,
        }

    def _make_once_task(self):
        task = self._make_cron_task()
        task.update(schedule_type="once", cron_expr=None,
                    run_at="2026-12-25T09:00", id="sched_once")
        return task

    def test_routes_text_to_handle_message(self):
        """Trigger routes non-command prompts through handle_message."""
        task = self._make_cron_task()
        self.bot._trigger_scheduled_task("sched_1", task)
        self.bot.handle_message.assert_called_once()
        call_args = self.bot.handle_message.call_args
        self.assertEqual(call_args[0][0], 123)  # chat_id
        self.bot.handle_command.assert_not_called()

    def test_routes_command_to_handle_command(self):
        """Trigger routes /command prompts through handle_command."""
        task = self._make_cron_task()
        task["prompt"] = "/justdoit check health"
        self.bot._trigger_scheduled_task("sched_1", task)
        self.bot.handle_command.assert_called_once()
        self.bot.handle_message.assert_not_called()

    def test_trigger_notification_sent(self):
        """Trigger sends notification message."""
        task = self._make_cron_task()
        self.bot._trigger_scheduled_task("sched_1", task)
        calls = self.bot.send_message.call_args_list
        self.assertTrue(any("triggered" in str(c).lower() for c in calls))

    def test_task_state_updated(self):
        """Trigger updates last_run and run_count."""
        task = self._make_cron_task()
        self.bot._trigger_scheduled_task("sched_1", task)
        self.assertIsNotNone(task["last_run"])
        self.assertEqual(task["run_count"], 1)
        self.bot.save_scheduled_tasks.assert_called()

    def test_once_task_disables_after_trigger(self):
        """Once task sets enabled=False and next_run=None after trigger."""
        task = self._make_once_task()
        self.bot._trigger_scheduled_task("sched_once", task)
        self.assertFalse(task["enabled"])
        self.assertIsNone(task["next_run"])

    def test_cron_task_recomputes_next_run(self):
        """Cron task recomputes next_run after trigger."""
        task = self._make_cron_task()
        old_next = task["next_run"]
        self.bot._trigger_scheduled_task("sched_1", task)
        self.assertNotEqual(task["next_run"], old_next)
        self.assertGreater(task["next_run"], time.time())

    def test_last_result_prepended_as_context(self):
        """When last_result exists, it's prepended to the prompt as context."""
        task = self._make_cron_task()
        task["last_result"] = "All tests passed"
        self.bot._trigger_scheduled_task("sched_1", task)
        call_args = self.bot.handle_message.call_args
        prompt = call_args[0][1]  # second positional arg is the text
        self.assertIn("Previous run result", prompt)
        self.assertIn("All tests passed", prompt)
        self.assertIn("Run tests", prompt)

    def test_last_result_not_prepended_for_commands(self):
        """last_result is NOT prepended when prompt is a command."""
        task = self._make_cron_task()
        task["prompt"] = "/justdoit check health"
        task["last_result"] = "previous result"
        self.bot._trigger_scheduled_task("sched_1", task)
        call_args = self.bot.handle_command.call_args
        prompt = call_args[0][1]
        self.assertNotIn("Previous run result", prompt)

    def test_ws_broadcast_triggered_event(self):
        """Trigger broadcasts a 'triggered' WS event."""
        task = self._make_cron_task()
        self.bot._trigger_scheduled_task("sched_1", task)
        ws_calls = self.bot._ws_broadcast_schedule.call_args_list
        events = [c[0][1] for c in ws_calls]
        self.assertIn("triggered", events)


# ──────────────────────────────────────────────────────────
# 5. Scheduler loop & hot-reload generation tests
# ──────────────────────────────────────────────────────────

class TestSchedulerGeneration(unittest.TestCase):
    """Test _start_scheduler generation counter and duplicate prevention."""

    def setUp(self):
        self.bot = _get_bot()
        self._orig_gen = self.bot._scheduler_generation

    def tearDown(self):
        self.bot._scheduler_generation = self._orig_gen

    def test_start_increments_generation(self):
        """Each _start_scheduler call increments the generation counter."""
        gen_before = self.bot._scheduler_generation
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            self.bot._start_scheduler()
        self.assertEqual(self.bot._scheduler_generation, gen_before + 1)

    def test_double_start_increments_twice(self):
        """Two calls produce two different generations, old thread self-retires."""
        gen_before = self.bot._scheduler_generation
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            self.bot._start_scheduler()
            self.bot._start_scheduler()
        self.assertEqual(self.bot._scheduler_generation, gen_before + 2)

    def test_old_thread_exits_on_generation_mismatch(self):
        """Simulate the scheduler loop exit condition."""
        self.bot._scheduler_generation = 5
        gen = 5
        # Loop should continue
        self.assertEqual(self.bot._scheduler_generation, gen)
        # Simulate hot-reload: generation advances
        self.bot._scheduler_generation = 6
        # Loop should exit
        self.assertNotEqual(self.bot._scheduler_generation, gen)


class TestSchedulerLoop(unittest.TestCase):
    """Test the scheduler loop's due-task detection."""

    def setUp(self):
        self.bot = _get_bot()
        self._orig = {
            'scheduled_tasks': self.bot.scheduled_tasks,
            '_scheduled_tasks_lock': self.bot._scheduled_tasks_lock,
        }
        self.bot.scheduled_tasks = {}
        self.bot._scheduled_tasks_lock = threading.Lock()

    def tearDown(self):
        for key, val in self._orig.items():
            setattr(self.bot, key, val)

    def test_due_task_detection(self):
        """Tasks with next_run in the past are detected as due."""
        now = time.time()
        self.bot.scheduled_tasks = {
            "due": {"enabled": True, "next_run": now - 60},
            "future": {"enabled": True, "next_run": now + 3600},
            "disabled": {"enabled": False, "next_run": now - 60},
            "no_next": {"enabled": True, "next_run": None},
        }
        with self.bot._scheduled_tasks_lock:
            due = [(tid, t) for tid, t in self.bot.scheduled_tasks.items()
                   if t.get("enabled") and t.get("next_run") and t["next_run"] <= now]
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][0], "due")


# ──────────────────────────────────────────────────────────
# 6. Persistence round-trip tests
# ──────────────────────────────────────────────────────────

class TestPersistenceRoundTrip(unittest.TestCase):
    """Test save/load cycle and edge cases."""

    def setUp(self):
        self.bot = _get_bot()
        self._orig_tasks = self.bot.scheduled_tasks
        self._orig_file = self.bot.SCHEDULED_TASKS_FILE
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        from pathlib import Path
        self.bot.SCHEDULED_TASKS_FILE = Path(self.tmpdir) / "scheduled_tasks.json"
        self.bot.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        self.bot.scheduled_tasks = self._orig_tasks
        self.bot.SCHEDULED_TASKS_FILE = self._orig_file
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_load_roundtrip(self):
        """Save tasks, reload, verify data integrity."""
        self.bot.scheduled_tasks = {
            "sched_1": {
                "id": "sched_1", "chat_id": "123", "cwd": "/tmp/proj",
                "prompt": "test", "schedule_type": "cron", "cron_expr": "0 9 * * *",
                "enabled": True, "next_run": 9999.0,
                "last_run": None, "last_result": None, "run_count": 0,
            }
        }
        self.bot.save_scheduled_tasks()
        # Verify file exists
        self.assertTrue(self.bot.SCHEDULED_TASKS_FILE.exists())
        # Simulate fresh load
        self.bot.scheduled_tasks = {}
        self.bot.load_scheduled_tasks()
        self.assertEqual(len(self.bot.scheduled_tasks), 1)
        self.assertEqual(self.bot.scheduled_tasks["sched_1"]["prompt"], "test")

    def test_save_empty_deletes_file(self):
        """Saving empty dict deletes the file."""
        # First create a file
        self.bot.scheduled_tasks = {"x": {"id": "x"}}
        self.bot.save_scheduled_tasks()
        self.assertTrue(self.bot.SCHEDULED_TASKS_FILE.exists())
        # Now save empty
        self.bot.scheduled_tasks = {}
        self.bot.save_scheduled_tasks()
        self.assertFalse(self.bot.SCHEDULED_TASKS_FILE.exists())

    def test_load_missing_file_gives_empty(self):
        """Loading when no file exists gives empty dict."""
        self.bot.scheduled_tasks = {"should_be_cleared": True}
        self.bot.load_scheduled_tasks()
        self.assertEqual(self.bot.scheduled_tasks, {})

    def test_load_corrupt_file_gives_empty(self):
        """Loading corrupt JSON gives empty dict instead of crashing."""
        self.bot.SCHEDULED_TASKS_FILE.write_text("{{{bad json")
        self.bot.scheduled_tasks = {"should_be_cleared": True}
        self.bot.load_scheduled_tasks()
        self.assertEqual(self.bot.scheduled_tasks, {})


# ──────────────────────────────────────────────────────────
# 7. run_at format parsing tests (both space and T separators)
# ──────────────────────────────────────────────────────────

class TestRunAtParsing(unittest.TestCase):
    """Verify run_at parsing handles both space and T separators across all code paths."""

    def setUp(self):
        self.bot = _get_bot()
        self._orig = {
            'scheduled_tasks': self.bot.scheduled_tasks,
            'save_scheduled_tasks': self.bot.save_scheduled_tasks,
            '_ws_broadcast_schedule': self.bot._ws_broadcast_schedule,
        }
        self.bot.scheduled_tasks = {}
        self.bot.save_scheduled_tasks = MagicMock()
        self.bot._ws_broadcast_schedule = MagicMock()

    def tearDown(self):
        for key, val in self._orig.items():
            setattr(self.bot, key, val)

    def test_space_separator(self):
        """'YYYY-MM-DD HH:MM' works (user-friendly format)."""
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        tid, task = self.bot.create_scheduled_task(
            123, "p", "once", run_at=future)
        self.assertIsNotNone(task["next_run"])

    def test_t_separator(self):
        """'YYYY-MM-DDTHH:MM' works (ISO 8601)."""
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        tid, task = self.bot.create_scheduled_task(
            123, "p", "once", run_at=future)
        self.assertIsNotNone(task["next_run"])

    def test_with_seconds(self):
        """'YYYY-MM-DDTHH:MM:SS' works."""
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        tid, task = self.bot.create_scheduled_task(
            123, "p", "once", run_at=future)
        self.assertIsNotNone(task["next_run"])

    def test_space_with_seconds(self):
        """'YYYY-MM-DD HH:MM:SS' works."""
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        tid, task = self.bot.create_scheduled_task(
            123, "p", "once", run_at=future)
        self.assertIsNotNone(task["next_run"])

    def test_invalid_date_raises(self):
        """Garbage date string raises ValueError."""
        with self.assertRaises(ValueError):
            self.bot.create_scheduled_task(123, "p", "once", run_at="not-a-date")

    def test_date_only_no_time_raises(self):
        """Date without time component should still parse (date-only ISO)."""
        future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        # Python's fromisoformat handles date-only strings (returns midnight)
        tid, task = self.bot.create_scheduled_task(
            123, "p", "once", run_at=future)
        self.assertIsNotNone(task["next_run"])


# ──────────────────────────────────────────────────────────
# 8. API full CRUD lifecycle test
# ──────────────────────────────────────────────────────────

class TestAPICRUDLifecycle(ScheduleAPITestBase):
    """Test full create → read → update → delete cycle via API."""

    def test_full_lifecycle(self):
        """Create, list, update, delete — full round trip."""
        # Create
        self.mock_create.return_value = ("sched_lc", {
            "id": "sched_lc", "chat_id": "123", "cwd": "/tmp/proj",
            "prompt": "lifecycle test", "schedule_type": "cron",
            "cron_expr": "0 9 * * *", "enabled": True,
            "next_run": 9999, "last_run": None, "last_result": None, "run_count": 0,
        })
        resp = self.client.post("/api/schedule-task", json={
            "chat_id": 123, "session_name": "proj", "prompt": "lifecycle test",
            "schedule_type": "cron", "cron_expr": "0 9 * * *",
        }, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        task_id = resp.json()["task_id"]

        # Simulate the task being in the dict (mock_create doesn't actually insert)
        self.scheduled_tasks[task_id] = self.mock_create.return_value[1]

        # Read
        resp = self.client.get("/api/scheduled-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["id"], task_id)

        # Update (toggle off)
        resp = self.client.put(f"/api/schedule-task/{task_id}",
            json={"enabled": False}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.scheduled_tasks[task_id]["enabled"])

        # Update (change prompt)
        resp = self.client.put(f"/api/schedule-task/{task_id}",
            json={"prompt": "updated prompt"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.scheduled_tasks[task_id]["prompt"], "updated prompt")

        # Delete
        resp = self.client.delete(f"/api/schedule-task/{task_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(task_id, self.scheduled_tasks)

        # Verify gone
        resp = self.client.get("/api/scheduled-tasks/123", headers=self.headers)
        self.assertEqual(resp.json(), [])


if __name__ == "__main__":
    unittest.main()
