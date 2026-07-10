"""Integration tests for Goal Mode end-to-end flows.

Tests:
1. Full lifecycle: create -> decompose -> start loop -> complete
2. Pause/resume mid-goal via API
3. Cancel mid-goal via API
4. User feedback injection during goal execution
5. WS event emission at key points
6. API CRUD: list -> get detail -> update config -> delete
7. Goal loop with iteration failure -> replan -> completion
8. Concurrent goal start blocked by busy session
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

os.environ["API_SECRET"] = "test-secret"

import api as api_server
from starlette.testclient import TestClient


def _make_goal(goal_id="goal_int1", chat_id="12345", session_id="sid1",
               status="active", title="Integration Test Goal",
               description="Build a widget", milestones=None, **kw):
    return {
        "id": goal_id,
        "chat_id": str(chat_id),
        "session_id": session_id,
        "cwd": "/tmp/test",
        "title": title,
        "description": description,
        "status": status,
        "created_at": "2026-06-19T10:00:00",
        "updated_at": "2026-06-19T10:00:00",
        "completed_at": None,
        "milestones": milestones or [
            {"id": "m1", "title": "Step 1", "status": "pending",
             "acceptance_criteria": ["check1"], "order": 1, "attempts": 0},
            {"id": "m2", "title": "Step 2", "status": "pending",
             "acceptance_criteria": ["check2"], "order": 2, "attempts": 0},
        ],
        "iterations": kw.get("iterations", []),
        "learnings": kw.get("learnings", []),
        "config": kw.get("config") or {
            "max_iterations": 50,
            "max_consecutive_failures": 5,
            "execution_mode": "claude-only",
            "auto_replan_threshold": 3,
            "max_total_time": 28800,
            "verification_commands": [],
            "pause_between_iterations": False,
            "model": "opus",
        },
    }


class IntegrationTestBase(unittest.TestCase):
    """Wire api refs with in-memory fakes, reusable across integration tests."""

    def setUp(self):
        self.justdoit_active = {}
        self.goal_active = {}
        self.goal_state = {}
        self.active_processes = {}
        self.goals_store = {}
        self.goals_index = {}
        self.feedback_queue = {}

        self.mock_is_allowed = MagicMock(return_value=True)
        self.mock_get_session_id = MagicMock(side_effect=lambda s: s.get("id", "sid"))
        self.mock_get_active_session = MagicMock(
            return_value={"id": "sid1", "name": "default", "cwd": "/tmp/test"})
        self.user_sessions = {
            "12345": {
                "sessions": [{"name": "default", "id": "sid1", "cwd": "/tmp/test"}],
                "active": "sid1",
            }
        }

        def fake_load_goal(goal_id):
            g = self.goals_store.get(goal_id)
            return dict(g) if g else None

        def fake_save_goal(goal):
            self.goals_store[goal["id"]] = goal

        def fake_load_goal_index():
            return dict(self.goals_index)

        def fake_create_goal(chat_id, session_id, cwd, description, config=None):
            import uuid
            goal_id = f"goal_{uuid.uuid4().hex[:8]}"
            goal = _make_goal(goal_id=goal_id, chat_id=str(chat_id),
                              session_id=session_id, status="planning",
                              description=description, milestones=[])
            if config:
                goal["config"].update(config)
            self.goals_store[goal_id] = goal
            chat_key = str(chat_id)
            if chat_key not in self.goals_index:
                self.goals_index[chat_key] = []
            self.goals_index[chat_key].append(goal_id)
            return goal

        def fake_list_goals(chat_id):
            ids = self.goals_index.get(str(chat_id), [])
            return [dict(self.goals_store[gid]) for gid in ids if gid in self.goals_store]

        def fake_delete_goal(goal_id):
            self.goals_store.pop(goal_id, None)
            for chat_key, ids in self.goals_index.items():
                if goal_id in ids:
                    ids.remove(goal_id)
                    break

        def fake_replan_goal(goal, session=None, chat_id=None):
            return ([
                {"id": "m_new1", "title": "Replanned step", "status": "pending",
                 "acceptance_criteria": ["new_check"], "order": 1, "attempts": 0},
            ], "Replanned after failures")

        def fake_decompose_goal(description, cwd, session=None, chat_id=None):
            return ("Decomposed: " + description[:30], [
                {"id": "m1", "title": "Step 1", "status": "pending",
                 "acceptance_criteria": ["check1"], "order": 1, "attempts": 0},
                {"id": "m2", "title": "Step 2", "status": "pending",
                 "acceptance_criteria": ["check2"], "order": 2, "attempts": 0},
            ])

        def fake_busy_reason(chat_id, session_id, ignore_goal_id=None):
            key = f"{chat_id}:{session_id}"
            if session_id in self.active_processes:
                return "Session is busy with an active CLI process"
            state = self.goal_state.get(key, {})
            if state.get("active") and state.get("goal_id") != ignore_goal_id:
                return "Goal is already running on this session"
            active_gid = self.goal_active.get(key)
            if active_gid and active_gid != ignore_goal_id:
                return "A goal is already running on this session"
            if self.justdoit_active.get(key, {}).get("active"):
                return "JustDoIt is already running on this session"
            return None

        def fake_reserve_goal(chat_id, session_id, goal_id, task="",
                              session_name="", phase="planning", loop_started=False):
            reason = fake_busy_reason(chat_id, session_id, ignore_goal_id=goal_id)
            if reason:
                return False, reason
            key = f"{chat_id}:{session_id}"
            resume_event = self.goal_state.get(key, {}).get("resume_event") or threading.Event()
            resume_event.set()
            self.goal_active[key] = goal_id
            self.goal_state[key] = {
                "active": True, "paused": False, "resume_event": resume_event,
                "goal_id": goal_id, "task": task, "phase": phase,
                "step": 0, "chat_id": str(chat_id), "session_name": session_name,
            }
            return True, None

        def fake_release_goal(chat_id, session_id, goal_id=None):
            key = f"{chat_id}:{session_id}"
            if goal_id and self.goal_active.get(key) not in (None, goal_id):
                return
            self.goal_active.pop(key, None)
            self.goal_state.pop(key, None)

        def fake_cancel_goal(chat_id, session_id, goal_id=None, reason="cancelled"):
            key = f"{chat_id}:{session_id}"
            state = self.goal_state.get(key)
            if state:
                state["active"] = False
                event = state.get("resume_event")
                if event:
                    event.set()
            gid = goal_id or self.goal_active.pop(key, None) or (state or {}).get("goal_id")
            self.goal_active.pop(key, None)
            goal = self.goals_store.get(gid)
            if goal:
                goal["status"] = "abandoned"
                goal["updated_at"] = datetime.now().isoformat()
            return gid

        self.mock_run_goal_loop = MagicMock()
        self.mock_ws_goal = MagicMock()
        self.mock_send = MagicMock()

        api_server.init_refs(
            handle_command=MagicMock(),
            handle_message=MagicMock(),
            handle_callback_query=MagicMock(),
            is_allowed=self.mock_is_allowed,
            get_active_session=self.mock_get_active_session,
            get_session_id=self.mock_get_session_id,
            user_sessions=self.user_sessions,
            active_processes=self.active_processes,
            justdoit_active=self.justdoit_active,
            goal_state=self.goal_state,
            ralph_active={},
            omni_active={},
            deepreview_active={},
            send_message=self.mock_send,
            send_message_no_ws=MagicMock(),
            cancelled_sessions=set(),
            ws_broadcast_status=MagicMock(),
            save_active_tasks=MagicMock(),
            user_feedback_queue=self.feedback_queue,
            get_active_sessions_data=lambda: {},
            goal_active=self.goal_active,
            goal_lock=threading.Lock(),
            get_session_busy_reason=fake_busy_reason,
            reserve_goal_session=fake_reserve_goal,
            release_goal_session=fake_release_goal,
            cancel_goal_session=fake_cancel_goal,
            load_goal=fake_load_goal,
            save_goal=fake_save_goal,
            load_goal_index=fake_load_goal_index,
            create_goal=fake_create_goal,
            list_goals=fake_list_goals,
            delete_goal=fake_delete_goal,
            replan_goal=fake_replan_goal,
            decompose_goal=fake_decompose_goal,
            run_goal_loop=self.mock_run_goal_loop,
            ws_broadcast_goal=self.mock_ws_goal,
            default_chat_id=12345,
        )

        self.client = TestClient(api_server.app)
        self.headers = {"Authorization": "Bearer test-secret"}

    def _add_goal(self, goal_id="goal_int1", **kw):
        goal = _make_goal(goal_id=goal_id, **kw)
        self.goals_store[goal_id] = goal
        chat_key = str(goal["chat_id"])
        if chat_key not in self.goals_index:
            self.goals_index[chat_key] = []
        if goal_id not in self.goals_index[chat_key]:
            self.goals_index[chat_key].append(goal_id)
        return goal


class TestFullLifecycle(IntegrationTestBase):
    """End-to-end: create -> list -> get -> pause -> resume -> complete -> delete."""

    def test_create_and_list(self):
        """POST /api/goal creates a goal, GET /api/goals lists it."""
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Build a widget",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        goal_id = data["goal_id"]
        self.assertIn(goal_id, self.goals_store)
        self.mock_run_goal_loop.assert_called_once()

        # List should include the new goal
        resp = self.client.get("/api/goals/12345", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        goals = resp.json()["goals"]
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["id"], goal_id)
        self.assertIn("milestones", goals[0])

    def test_create_get_detail(self):
        """Created goal can be fetched with full detail."""
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Full detail test",
        })
        goal_id = resp.json()["goal_id"]

        resp = self.client.get(f"/api/goal/{goal_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        detail = resp.json()
        self.assertEqual(detail["id"], goal_id)
        self.assertIn("milestones", detail)
        self.assertIn("config", detail)

    def test_full_crud_lifecycle(self):
        """Create -> list -> get -> config update -> delete."""
        # Create
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "CRUD lifecycle test",
            "config": {"max_iterations": 10},
        })
        self.assertEqual(resp.status_code, 200)
        goal_id = resp.json()["goal_id"]

        # Config update
        resp = self.client.patch(f"/api/goal/{goal_id}/config", headers=self.headers,
                                 json={"max_iterations": 25})
        self.assertEqual(resp.status_code, 200)
        updated = self.goals_store[goal_id]
        self.assertEqual(updated["config"]["max_iterations"], 25)

        # Release for deletion (simulate goal finishing)
        self.goal_active.pop("12345:sid1", None)
        self.goal_state.pop("12345:sid1", None)

        # Delete
        resp = self.client.delete(f"/api/goal/{goal_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(goal_id, self.goals_store)


class TestPauseResumeViaAPI(IntegrationTestBase):
    """Pause and resume running goals through the REST API."""

    def test_pause_running_goal(self):
        """POST /api/goal/{id}/pause pauses an active goal."""
        goal = self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": False,
            "resume_event": threading.Event(),
            "goal_id": "goal_int1",
        }
        self.goal_state["12345:sid1"]["resume_event"].set()

        resp = self.client.post("/api/goal/goal_int1/pause", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.goal_state["12345:sid1"]["paused"])
        self.assertEqual(self.goals_store["goal_int1"]["status"], "paused")

    def test_resume_paused_goal(self):
        """POST /api/goal/{id}/resume unpauses and restarts."""
        goal = self._add_goal(status="paused")
        self.goal_active["12345:sid1"] = "goal_int1"
        event = threading.Event()
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": True,
            "resume_event": event,
            "goal_id": "goal_int1",
        }

        resp = self.client.post("/api/goal/goal_int1/resume", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.goal_state["12345:sid1"]["paused"])
        self.assertTrue(event.is_set())
        self.assertEqual(self.goals_store["goal_int1"]["status"], "active")

    def test_resume_from_disk(self):
        """Resume a paused goal that has no in-memory state (e.g., after restart)."""
        goal = self._add_goal(status="paused")
        # No goal_active / goal_state — simulate post-restart state

        resp = self.client.post("/api/goal/goal_int1/resume", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.mock_run_goal_loop.assert_called_once()
        self.assertEqual(self.goals_store["goal_int1"]["status"], "active")


class TestCancelViaAPI(IntegrationTestBase):
    """Cancel running goals through various API paths."""

    def test_cancel_specific_goal(self):
        """POST /api/goal/{id}/cancel abandons the goal."""
        goal = self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": False,
            "resume_event": threading.Event(),
            "goal_id": "goal_int1",
        }

        resp = self.client.post("/api/goal/goal_int1/cancel", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.goals_store["goal_int1"]["status"], "abandoned")

    def test_cancel_via_generic_cancel_task(self):
        """POST /api/cancel-task also cancels a running goal."""
        goal = self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": False,
            "resume_event": threading.Event(),
            "goal_id": "goal_int1",
            "chat_id": "12345",
            "session_name": "default",
        }

        resp = self.client.post("/api/cancel-task", headers=self.headers,
                                json={"session": "default"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["mode"], "goal")
        self.assertEqual(self.goals_store["goal_int1"]["status"], "abandoned")


class TestWSEventEmission(IntegrationTestBase):
    """Verify WS events are emitted at the right points."""

    def test_create_starts_loop(self):
        """Creating a goal starts the loop thread (which emits WS events)."""
        self.client.post("/api/goal", headers=self.headers, json={
            "description": "WS test",
        })
        # The loop is started in a background thread — verify it was called
        self.mock_run_goal_loop.assert_called_once()
        # The WS events (started, milestone_started, etc.) are emitted inside the loop

    def test_cancel_emits_ws_event(self):
        """Cancelling a goal emits WS cancelled event."""
        self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": False,
            "resume_event": threading.Event(),
            "goal_id": "goal_int1",
        }

        self.client.post("/api/goal/goal_int1/cancel", headers=self.headers)
        events = [call.args[1] for call in self.mock_ws_goal.call_args_list]
        self.assertIn("cancelled", events)

    def test_pause_emits_ws_event(self):
        """Pausing a goal emits WS paused event."""
        self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": False,
            "resume_event": threading.Event(),
            "goal_id": "goal_int1",
        }
        self.goal_state["12345:sid1"]["resume_event"].set()

        self.client.post("/api/goal/goal_int1/pause", headers=self.headers)
        events = [call.args[1] for call in self.mock_ws_goal.call_args_list]
        self.assertIn("paused", events)


class TestConcurrencyBlocking(IntegrationTestBase):
    """Verify busy session blocks concurrent goal creation."""

    def test_busy_session_blocks_create(self):
        """Cannot create a goal when session has active CLI process."""
        self.active_processes["sid1"] = MagicMock()

        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Should be blocked",
        })
        self.assertEqual(resp.status_code, 409)
        self.assertIn("busy", resp.json()["detail"].lower())

    def test_existing_goal_blocks_second(self):
        """Cannot create a second goal on the same session."""
        self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": False,
            "resume_event": threading.Event(),
            "goal_id": "goal_int1",
        }

        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Second goal should fail",
        })
        self.assertEqual(resp.status_code, 409)

    def test_justdoit_blocks_goal(self):
        """Cannot create a goal when JustDoIt is running."""
        self.justdoit_active["12345:sid1"] = {"active": True}

        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Should be blocked by JustDoIt",
        })
        self.assertEqual(resp.status_code, 409)


class TestGoalListEnhanced(IntegrationTestBase):
    """Verify the enhanced goal list API returns all needed fields."""

    def test_list_includes_milestones(self):
        """Goal list includes milestone details for the Android app."""
        self._add_goal(milestones=[
            {"id": "m1", "title": "Done step", "status": "completed",
             "acceptance_criteria": ["c1"], "order": 1, "attempts": 2},
            {"id": "m2", "title": "Pending step", "status": "pending",
             "acceptance_criteria": ["c2", "c3"], "order": 2, "attempts": 0},
        ])

        resp = self.client.get("/api/goals/12345", headers=self.headers)
        goals = resp.json()["goals"]
        self.assertEqual(len(goals), 1)
        g = goals[0]
        self.assertEqual(g["milestones_total"], 2)
        self.assertEqual(g["milestones_done"], 1)
        self.assertEqual(len(g["milestones"]), 2)
        self.assertEqual(g["milestones"][0]["title"], "Done step")
        self.assertEqual(g["milestones"][0]["attempts"], 2)
        self.assertEqual(g["milestones"][1]["acceptance_criteria"], ["c2", "c3"])

    def test_list_includes_session_name(self):
        """Goal list resolves session_id to session name."""
        self._add_goal()

        resp = self.client.get("/api/goals/12345", headers=self.headers)
        g = resp.json()["goals"][0]
        self.assertEqual(g["session"], "default")

    def test_list_includes_running_paused_state(self):
        """Goal list includes is_running and is_paused flags."""
        self._add_goal(status="active")
        self.goal_active["12345:sid1"] = "goal_int1"
        self.goal_state["12345:sid1"] = {
            "active": True, "paused": True,
            "goal_id": "goal_int1",
        }

        resp = self.client.get("/api/goals/12345", headers=self.headers)
        g = resp.json()["goals"][0]
        self.assertTrue(g["is_running"])
        self.assertTrue(g["is_paused"])

    def test_list_includes_iteration_and_learnings_count(self):
        """Goal list includes current_iteration and learnings_count."""
        self._add_goal(
            iterations=[{"id": 1}, {"id": 2}, {"id": 3}],
            learnings=[{"insight": "a"}, {"insight": "b"}],
        )

        resp = self.client.get("/api/goals/12345", headers=self.headers)
        g = resp.json()["goals"][0]
        self.assertEqual(g["current_iteration"], 3)
        self.assertEqual(g["learnings_count"], 2)


class TestGoalLoopIntegration(unittest.TestCase):
    """Integration tests for the goal loop engine using bot.py directly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

        import bot
        self.bot = bot
        self._orig_goals_dir = bot.GOALS_DIR
        self._orig_index_file = bot.GOALS_INDEX_FILE
        self._orig_goal_active = bot.goal_active
        self._orig_goal_state = bot.goal_state
        self._orig_goal_lock = bot._goal_lock

        bot.GOALS_DIR = self.goals_dir
        bot.GOALS_INDEX_FILE = self.goals_dir / "index.json"
        bot.goal_active = {}
        bot.goal_state = {}
        bot._goal_lock = threading.Lock()

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GOALS_INDEX_FILE = self._orig_index_file
        self.bot.goal_active = self._orig_goal_active
        self.bot.goal_state = self._orig_goal_state
        self.bot._goal_lock = self._orig_goal_lock
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_goal(self, **overrides):
        config = {
            "max_iterations": 50, "max_consecutive_failures": 5,
            "execution_mode": "claude-only", "auto_replan_threshold": 3,
            "max_total_time": 28800, "verification_commands": [],
            "pause_between_iterations": False, "model": "opus",
        }
        goal = {
            "id": "goal_loop1", "chat_id": "12345", "session_id": "sid1",
            "cwd": "/tmp/test", "title": "Loop Test", "description": "Test loop",
            "status": "active",
            "created_at": "2026-06-19T10:00:00",
            "updated_at": "2026-06-19T10:00:00",
            "completed_at": None,
            "milestones": [
                {"id": "m1", "title": "Step 1", "status": "pending",
                 "acceptance_criteria": ["check1"], "order": 1, "attempts": 0},
            ],
            "iterations": [], "learnings": [], "config": config,
        }
        goal.update(overrides)
        self.bot._save_goal(goal)
        return goal

    @patch("bot._promote_learnings")
    @patch("bot._decay_global_learnings")
    @patch("bot._cancel_goal_checkin")
    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    @patch("bot._extract_learnings")
    def test_two_iteration_completion(self, mock_learn, mock_verify, mock_execute,
                                       mock_assess, mock_get_session,
                                       mock_ws_goal, mock_ws_status,
                                       mock_save, mock_send,
                                       mock_cancel_checkin, mock_decay, mock_promote):
        """Goal completes after 2 successful iterations on 2 milestones."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        goal = self._make_goal(milestones=[
            {"id": "m1", "title": "Step 1", "status": "pending",
             "acceptance_criteria": ["check1"], "order": 1, "attempts": 0},
            {"id": "m2", "title": "Step 2", "status": "pending",
             "acceptance_criteria": ["check2"], "order": 2, "attempts": 0},
        ])

        mock_assess.side_effect = [
            {"next_milestone_id": "m1", "recommended_action": "Do step 1"},
            {"next_milestone_id": "m2", "recommended_action": "Do step 2"},
            {"next_milestone_id": None},  # All done
        ]
        mock_execute.return_value = "Done"
        mock_verify.return_value = {
            "all_passed": True,
            "passed": [{"criterion": "check1", "evidence": "ok"}],
            "failed": [],
        }
        mock_learn.return_value = [{"category": "technical", "insight": "learned something"}]

        time_counter = [0]
        def advancing_time():
            time_counter[0] += 1
            return time_counter[0]

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", side_effect=advancing_time), \
             patch("time.sleep"):
            self.bot._run_goal_loop(12345, "sid1", "goal_loop1")

        goal = self.bot._load_goal("goal_loop1")
        self.assertEqual(goal["status"], "completed")
        self.assertEqual(len(goal["iterations"]), 2)
        self.assertEqual(len(goal["learnings"]), 2)

        # Verify WS events were emitted
        ws_events = [call.args[1] for call in mock_ws_goal.call_args_list]
        self.assertIn("started", ws_events)
        self.assertIn("milestone_started", ws_events)
        self.assertIn("milestone_completed", ws_events)
        self.assertIn("completed", ws_events)

    @patch("bot._schedule_goal_checkin")
    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    def test_execution_rate_limit_pauses_without_failed_iteration(
        self, mock_verify, mock_execute, mock_assess, mock_get_session,
        mock_ws_goal, mock_ws_status, mock_save, mock_send, mock_checkin
    ):
        """Execution rate limits pause the goal instead of becoming failed iterations."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        self._make_goal()
        mock_assess.return_value = {"next_milestone_id": "m1", "recommended_action": "Do step 1"}
        mock_execute.side_effect = self.bot.GoalRateLimitError("rate limit", wait_seconds=120)

        with patch("bot._check_pause", return_value=True), patch("time.time", return_value=1):
            self.bot._run_goal_loop(12345, "sid1", "goal_loop1")

        goal = self.bot._load_goal("goal_loop1")
        self.assertEqual(goal["status"], "paused")
        self.assertEqual(goal["pause_reason"], "rate_limited")
        self.assertIn("rate_limited_until", goal)
        self.assertEqual(goal["iterations"], [])
        self.assertEqual(goal["milestones"][0]["status"], "pending")
        mock_verify.assert_not_called()

    @patch("bot._schedule_goal_checkin")
    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    def test_assessment_transient_error_pauses_without_failed_iteration(
        self, mock_verify, mock_execute, mock_assess, mock_get_session,
        mock_ws_goal, mock_ws_status, mock_save, mock_send, mock_checkin
    ):
        """Transient assessment errors pause the goal instead of failing it.

        Retries disabled here (transient_max_retries=0) so the pause is immediate;
        the retry-then-pause behavior is covered by a dedicated test below.
        """
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        goal = self._make_goal()
        goal["config"]["transient_max_retries"] = 0
        self.bot._save_goal(goal)
        mock_assess.side_effect = self.bot.GoalTransientError(
            "goal assessment transient error: 503 Service Unavailable",
            wait_seconds=60,
        )

        with patch("bot._check_pause", return_value=True), patch("time.time", return_value=1):
            self.bot._run_goal_loop(12345, "sid1", "goal_loop1")

        goal = self.bot._load_goal("goal_loop1")
        self.assertEqual(goal["status"], "paused")
        self.assertEqual(goal["pause_reason"], "transient_error")
        self.assertIn("transient_retry_after", goal)
        self.assertEqual(goal["iterations"], [])
        self.assertEqual(goal["milestones"][0]["status"], "pending")
        mock_execute.assert_not_called()
        mock_verify.assert_not_called()

    @patch("bot._schedule_goal_checkin")
    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    def test_transient_error_retries_then_pauses(
        self, mock_verify, mock_execute, mock_assess, mock_get_session,
        mock_ws_goal, mock_ws_status, mock_save, mock_send, mock_checkin
    ):
        """A transient error is retried with backoff before the goal pauses."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        goal = self._make_goal()
        goal["config"]["transient_max_retries"] = 2
        goal["config"]["transient_retry_base_delay"] = 5
        self.bot._save_goal(goal)
        mock_assess.side_effect = self.bot.GoalTransientError(
            "goal assessment transient error: 503 Service Unavailable",
            wait_seconds=5,
        )

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", return_value=1), \
             patch("bot.time.sleep") as mock_sleep:
            self.bot._run_goal_loop(12345, "sid1", "goal_loop1")

        # 1 initial attempt + 2 retries = 3 calls; 2 backoff sleeps
        self.assertEqual(mock_assess.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        goal = self.bot._load_goal("goal_loop1")
        self.assertEqual(goal["status"], "paused")
        self.assertEqual(goal["pause_reason"], "transient_error")
        self.assertEqual(goal["iterations"], [])
        self.assertEqual(goal["milestones"][0]["status"], "pending")

    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    @patch("bot._extract_learnings")
    def test_assessment_done_cannot_complete_failed_milestone(
        self, mock_learn, mock_verify, mock_execute, mock_assess, mock_get_session,
        mock_ws_goal, mock_ws_status, mock_save, mock_send
    ):
        """A goal cannot complete while a milestone is still failed."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        goal = self._make_goal()
        goal["config"]["max_iterations"] = 2
        self.bot._save_goal(goal)

        mock_assess.side_effect = [
            {"next_milestone_id": "m1", "recommended_action": "Try once"},
            {"next_milestone_id": None, "recommended_action": "Done"},
        ]
        mock_execute.return_value = "output"
        mock_verify.return_value = {
            "all_passed": False,
            "passed": [],
            "failed": [{"criterion": "check1", "evidence": "still failing"}],
        }
        mock_learn.return_value = []

        time_counter = [0]
        def advancing_time():
            time_counter[0] += 1
            return time_counter[0]

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", side_effect=advancing_time), \
             patch("time.sleep"):
            self.bot._run_goal_loop(12345, "sid1", "goal_loop1")

        goal = self.bot._load_goal("goal_loop1")
        self.assertEqual(goal["status"], "paused")
        self.assertEqual(goal["milestones"][0]["status"], "failed")
        messages = [str(call.args[1]) for call in mock_send.call_args_list if len(call.args) > 1]
        self.assertTrue(any("still incomplete" in msg for msg in messages))

    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    @patch("bot._extract_learnings")
    @patch("bot._replan_goal")
    def test_failure_triggers_replan(self, mock_replan, mock_learn, mock_verify,
                                      mock_execute, mock_assess, mock_get_session,
                                      mock_ws_goal, mock_ws_status,
                                      mock_save, mock_send):
        """Consecutive failures trigger replan, then goal completes."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        goal = self._make_goal()
        goal["config"]["max_consecutive_failures"] = 2
        self.bot._save_goal(goal)

        # Fail twice (triggers replan), then succeed
        mock_assess.side_effect = [
            {"next_milestone_id": "m1", "recommended_action": "Try 1"},
            {"next_milestone_id": "m1", "recommended_action": "Try 2"},
            # After replan:
            {"next_milestone_id": "m_new", "recommended_action": "New approach"},
            {"next_milestone_id": None},
        ]
        mock_execute.return_value = "output"
        fail_result = {"all_passed": False, "passed": [],
                       "failed": [{"criterion": "check1", "evidence": "nope"}]}
        pass_result = {"all_passed": True,
                       "passed": [{"criterion": "new_check", "evidence": "ok"}],
                       "failed": []}
        mock_verify.side_effect = [fail_result, fail_result, pass_result]
        mock_learn.return_value = []
        mock_replan.return_value = (
            [{"id": "m_new", "title": "New step", "status": "pending",
              "acceptance_criteria": ["new_check"], "order": 1, "attempts": 0}],
            "Replanned after failures"
        )

        time_counter = [0]
        def advancing_time():
            time_counter[0] += 1
            return time_counter[0]

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", side_effect=advancing_time), \
             patch("time.sleep"), \
             patch("bot._cancel_goal_checkin"), \
             patch("bot._promote_learnings"), \
             patch("bot._decay_global_learnings"):
            self.bot._run_goal_loop(12345, "sid1", "goal_loop1")

        goal = self.bot._load_goal("goal_loop1")
        self.assertEqual(goal["status"], "completed")
        mock_replan.assert_called_once()

        ws_events = [call.args[1] for call in mock_ws_goal.call_args_list]
        self.assertIn("replan", ws_events)
        self.assertIn("completed", ws_events)


class TestUserFeedbackInjection(unittest.TestCase):
    """Test user feedback is drained and injected into goal actions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

        import bot
        self.bot = bot
        self._orig_goals_dir = bot.GOALS_DIR
        self._orig_index_file = bot.GOALS_INDEX_FILE
        bot.GOALS_DIR = self.goals_dir
        bot.GOALS_INDEX_FILE = self.goals_dir / "index.json"

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GOALS_INDEX_FILE = self._orig_index_file
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("bot.drain_user_feedback", return_value="\n\nUser says: focus on tests")
    @patch("bot.get_session_id", return_value="sess1")
    @patch("bot.run_claude_streaming", return_value=("done", [], None, None, None))
    @patch("bot.update_claude_session_id")
    def test_feedback_appended_to_prompt(self, mock_update, mock_stream, mock_gsid, mock_drain):
        """User feedback is appended to the execution prompt."""
        goal = {
            "id": "goal_fb", "chat_id": "123", "session_id": "sess1",
            "cwd": "/tmp/test", "title": "FB Test", "description": "test",
            "status": "active", "milestones": [], "iterations": [],
            "learnings": [{"insight": "past insight", "applies_to": ["m1"]}],
            "config": {"execution_mode": "claude-only", "model": "opus"},
        }
        milestone = {"id": "m1", "title": "Step", "acceptance_criteria": ["check"],
                      "attempts": 1}
        session = {"id": "sess1", "name": "default", "cwd": "/tmp/test"}

        self.bot._execute_goal_action(goal, milestone, "do the thing", 123, session)

        # Verify feedback was drained
        mock_drain.assert_called_once()
        # Verify the prompt sent to Claude includes the feedback
        prompt_arg = mock_stream.call_args.args[0]
        self.assertIn("User says: focus on tests", prompt_arg)

    @patch("bot._ws_broadcast_goal")
    @patch("bot.send_message")
    def test_interrupt_feedback_requeued_and_milestone_reset(self, mock_send, mock_ws):
        """Urgent ! feedback resets the current milestone and feeds the next execution."""
        chat_key = "123:sess1"
        goal = {
            "id": "goal_fb", "chat_id": "123", "session_id": "sess1",
            "cwd": "/tmp/test", "title": "FB Test", "description": "test",
            "status": "active", "milestones": [
                {"id": "m1", "title": "Step", "status": "in_progress",
                 "acceptance_criteria": ["check"], "attempts": 1}
            ],
            "iterations": [], "learnings": [], "config": {},
        }
        milestone = goal["milestones"][0]
        self.bot.goal_state[chat_key] = {"active": True, "interrupted": True}
        self.bot.user_feedback_queue[chat_key] = ["Stop and add tests first."]

        try:
            consumed = self.bot._goal_consume_interrupt(
                chat_key, 123, goal, "goal_fb", milestone
            )

            self.assertTrue(consumed)
            self.assertFalse(self.bot.goal_state[chat_key]["interrupted"])
            self.assertEqual(milestone["status"], "pending")
            self.assertIn("urgent feedback", self.bot.user_feedback_queue[chat_key][0])
            self.assertIn("Stop and add tests first.", self.bot.user_feedback_queue[chat_key][0])
            saved = self.bot._load_goal("goal_fb")
            self.assertEqual(saved["milestones"][0]["status"], "pending")
            mock_send.assert_called_once()
            mock_ws.assert_called_once()
        finally:
            self.bot.goal_state.pop(chat_key, None)
            self.bot.user_feedback_queue.pop(chat_key, None)


if __name__ == "__main__":
    unittest.main()
