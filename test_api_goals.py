"""Tests for Goal Mode REST API endpoints.

Covers:
1. GET /api/goals/{chat_id} — list goals
2. GET /api/goal/{goal_id} — full goal state
3. GET /api/goal/{goal_id}/journal — learnings only
4. POST /api/goal — create + decompose + start
5. POST /api/goal/{goal_id}/pause — pause running goal
6. POST /api/goal/{goal_id}/resume — resume paused goal
7. POST /api/goal/{goal_id}/cancel — cancel running goal
8. POST /api/goal/{goal_id}/replan — trigger replan
9. PATCH /api/goal/{goal_id}/config — update config
10. DELETE /api/goal/{goal_id} — delete goal
"""
import json
import os
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ["API_SECRET"] = "test-secret"

import api as api_server
from starlette.testclient import TestClient


def _make_goal(goal_id="goal_abc123", chat_id="12345", session_id="sid1",
               status="active", title="Test Goal", description="Do something",
               milestones=None, iterations=None, learnings=None, config=None, **extra):
    """Build a test goal dict."""
    goal = {
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
            {"id": "m1", "title": "Step 1", "status": "completed", "acceptance_criteria": []},
            {"id": "m2", "title": "Step 2", "status": "pending", "acceptance_criteria": []},
        ],
        "iterations": iterations or [],
        "learnings": learnings or [],
        "config": config or {
            "max_iterations": 50,
            "max_consecutive_failures": 5,
            "execution_mode": "auto",
            "auto_replan_threshold": 3,
            "verification_commands": [],
            "pause_between_iterations": False,
            "model": "opus",
        },
    }
    goal.update(extra)
    return goal


class GoalAPITestBase(unittest.TestCase):
    """Shared setup: wire api module-level refs to test mocks."""

    def setUp(self):
        self.justdoit_active = {}
        self.goal_active = {}
        self.goal_state = {}
        self.active_processes = {}
        self.goals_store = {}  # goal_id -> goal dict (in-memory fake)
        self.goals_index = {}  # chat_id -> [goal_ids]

        self.mock_is_allowed = MagicMock(return_value=True)
        self.mock_get_session_id = MagicMock(side_effect=lambda s: s.get("id", "sid"))
        self.mock_get_active_session = MagicMock(return_value={"id": "sid1", "name": "default", "cwd": "/tmp/test"})
        self.user_sessions = {
            "12345": {
                "sessions": [{"name": "default", "id": "sid1", "cwd": "/tmp/test"}],
                "active": "sid1",
            }
        }

        def fake_load_goal(goal_id):
            g = self.goals_store.get(goal_id)
            # Return a copy to simulate file-based loading
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
                              description=description, milestones=[], config=config)
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
            return (goal.get("milestones", []), "Replanned successfully")

        def fake_decompose_goal(description, cwd, session=None, chat_id=None):
            return ("Decomposed Title", [
                {"id": "m1", "title": "Step 1", "status": "pending", "acceptance_criteria": []},
                {"id": "m2", "title": "Step 2", "status": "pending", "acceptance_criteria": []},
            ])

        self.mock_run_goal_loop = MagicMock()
        self.mock_ws_goal = MagicMock()

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

        def fake_reserve_goal(chat_id, session_id, goal_id, task="", session_name="", phase="planning", loop_started=False):
            reason = fake_busy_reason(chat_id, session_id, ignore_goal_id=goal_id)
            if reason:
                return False, reason
            key = f"{chat_id}:{session_id}"
            resume_event = self.goal_state.get(key, {}).get("resume_event") or threading.Event()
            resume_event.set()
            self.goal_active[key] = goal_id
            self.goal_state[key] = {
                "active": True,
                "paused": False,
                "resume_event": resume_event,
                "goal_id": goal_id,
                "task": task,
                "phase": phase,
                "step": 0,
                "chat_id": str(chat_id),
                "session_name": session_name,
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
            omni_active={},
            deepreview_active={},
            send_message=MagicMock(),
            send_message_no_ws=MagicMock(),
            cancelled_sessions=set(),
            ws_broadcast_status=MagicMock(),
            save_active_tasks=MagicMock(),
            user_feedback_queue={},
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

    def _add_goal(self, goal_id="goal_abc123", chat_id="12345", **kwargs):
        """Insert a goal into the fake store."""
        goal = _make_goal(goal_id=goal_id, chat_id=chat_id, **kwargs)
        self.goals_store[goal_id] = goal
        chat_key = str(chat_id)
        if chat_key not in self.goals_index:
            self.goals_index[chat_key] = []
        if goal_id not in self.goals_index[chat_key]:
            self.goals_index[chat_key].append(goal_id)
        return goal


class TestListGoals(GoalAPITestBase):

    def test_list_empty(self):
        resp = self.client.get("/api/goals/12345", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["goals"], [])

    def test_list_with_goals(self):
        self._add_goal("goal_1", title="Goal A")
        self._add_goal("goal_2", title="Goal B", status="completed")
        resp = self.client.get("/api/goals/12345", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        goals = resp.json()["goals"]
        self.assertEqual(len(goals), 2)
        self.assertEqual(goals[0]["title"], "Goal A")
        self.assertEqual(goals[1]["status"], "completed")

    def test_list_includes_running_flag(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        resp = self.client.get("/api/goals/12345", headers=self.headers)
        goals = resp.json()["goals"]
        self.assertTrue(goals[0]["is_running"])

    def test_list_milestone_counts(self):
        self._add_goal("goal_1")
        resp = self.client.get("/api/goals/12345", headers=self.headers)
        g = resp.json()["goals"][0]
        self.assertEqual(g["milestones_total"], 2)
        self.assertEqual(g["milestones_done"], 1)

    def test_list_unauthorized_chat(self):
        self.mock_is_allowed.return_value = False
        resp = self.client.get("/api/goals/99999", headers=self.headers)
        self.assertEqual(resp.status_code, 403)

    def test_list_default_chat_id(self):
        self._add_goal("goal_1")
        resp = self.client.get("/api/goals/0", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["goals"]), 1)


class TestGetGoal(GoalAPITestBase):

    def test_get_existing(self):
        self._add_goal("goal_1", learnings=[{"text": "learned something"}])
        resp = self.client.get("/api/goal/goal_1", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], "goal_1")
        self.assertEqual(len(data["milestones"]), 2)
        self.assertIn("is_running", data)
        self.assertIn("is_paused", data)

    def test_get_not_found(self):
        resp = self.client.get("/api/goal/nonexistent", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_get_shows_paused(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        self.goal_state["12345:sid1"] = {"active": True, "paused": True, "goal_id": "goal_1"}
        resp = self.client.get("/api/goal/goal_1", headers=self.headers)
        data = resp.json()
        self.assertTrue(data["is_running"])
        self.assertTrue(data["is_paused"])


class TestGetGoalJournal(GoalAPITestBase):

    def test_journal(self):
        self._add_goal("goal_1", learnings=[
            {"text": "Lesson 1", "iteration": 1},
            {"text": "Lesson 2", "iteration": 2},
        ])
        resp = self.client.get("/api/goal/goal_1/journal", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["goal_id"], "goal_1")
        self.assertEqual(len(data["learnings"]), 2)

    def test_journal_not_found(self):
        resp = self.client.get("/api/goal/nonexistent/journal", headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class TestCreateGoal(GoalAPITestBase):

    def test_create_success(self):
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Build a widget",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "created")
        self.assertIn("goal_id", data)
        self.assertEqual(data["title"], "Decomposed Title")
        self.assertEqual(data["milestones"], 2)
        # Verify run_goal_loop was called
        self.mock_run_goal_loop.assert_called_once()

    def test_create_conflict_running(self):
        self.goal_active["12345:sid1"] = "goal_existing"
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Another goal",
        })
        self.assertEqual(resp.status_code, 409)

    def test_create_conflict_busy(self):
        self.justdoit_active["12345:sid1"] = {"active": True}
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Another goal",
        })
        self.assertEqual(resp.status_code, 409)

    def test_create_no_session(self):
        self.mock_get_active_session.return_value = None
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "No session",
        })
        self.assertEqual(resp.status_code, 400)

    def test_create_with_session_name(self):
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "With session",
            "session_name": "default",
        })
        self.assertEqual(resp.status_code, 200)


class TestPauseGoal(GoalAPITestBase):

    def test_pause_running(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        resume_event = threading.Event()
        resume_event.set()
        self.goal_state["12345:sid1"] = {"active": True, "paused": False, "resume_event": resume_event, "goal_id": "goal_1"}

        resp = self.client.post("/api/goal/goal_1/pause", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "paused")
        self.assertTrue(self.goal_state["12345:sid1"]["paused"])
        self.assertFalse(resume_event.is_set())

    def test_pause_not_running(self):
        self._add_goal("goal_1")
        resp = self.client.post("/api/goal/goal_1/pause", headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_pause_not_found(self):
        resp = self.client.post("/api/goal/nonexistent/pause", headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class TestResumeGoal(GoalAPITestBase):

    def test_resume_paused_loop(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        resume_event = threading.Event()
        self.goal_state["12345:sid1"] = {"active": True, "paused": True, "resume_event": resume_event, "goal_id": "goal_1"}

        resp = self.client.post("/api/goal/goal_1/resume", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.goal_state["12345:sid1"]["paused"])
        self.assertTrue(resume_event.is_set())

    def test_resume_paused_on_disk(self):
        self._add_goal("goal_1", status="paused")
        resp = self.client.post("/api/goal/goal_1/resume", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "resumed")
        # Verify _run_goal_loop was started
        self.mock_run_goal_loop.assert_called_once()

    def test_resume_rate_limited_goal_before_reset_returns_429(self):
        reset_at = (datetime.now() + timedelta(minutes=12)).isoformat()
        self._add_goal(
            "goal_1",
            status="paused",
            rate_limited_until=reset_at,
            pause_reason="rate_limited",
        )

        resp = self.client.post("/api/goal/goal_1/resume", headers=self.headers)

        self.assertEqual(resp.status_code, 429)
        self.assertIn("rate-limited", resp.json()["detail"])
        self.mock_run_goal_loop.assert_not_called()

    def test_resume_already_running(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        self.goal_state["12345:sid1"] = {"active": True, "paused": False, "goal_id": "goal_1"}
        resp = self.client.post("/api/goal/goal_1/resume", headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_resume_completed_goal(self):
        self._add_goal("goal_1", status="completed")
        resp = self.client.post("/api/goal/goal_1/resume", headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not paused", resp.json()["detail"])


class TestCancelGoal(GoalAPITestBase):

    def test_cancel_running(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        resume_event = threading.Event()
        self.goal_state["12345:sid1"] = {"active": True, "paused": True, "resume_event": resume_event, "goal_id": "goal_1"}

        resp = self.client.post("/api/goal/goal_1/cancel", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.goal_state["12345:sid1"]["active"])
        self.assertTrue(resume_event.is_set())  # Unblocked

    def test_cancel_not_running(self):
        self._add_goal("goal_1")
        resp = self.client.post("/api/goal/goal_1/cancel", headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_cancel_not_found(self):
        resp = self.client.post("/api/goal/nonexistent/cancel", headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class TestReplanGoal(GoalAPITestBase):

    def test_replan_active(self):
        self._add_goal("goal_1", status="active")
        resp = self.client.post("/api/goal/goal_1/replan", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "replanned")
        self.assertIn("rationale", data)

    def test_replan_completed(self):
        self._add_goal("goal_1", status="completed")
        resp = self.client.post("/api/goal/goal_1/replan", headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_replan_not_found(self):
        resp = self.client.post("/api/goal/nonexistent/replan", headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class TestGoalConfig(GoalAPITestBase):

    def test_update_config(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "max_iterations": 100,
            "model": "sonnet",
        })
        self.assertEqual(resp.status_code, 200)
        cfg = resp.json()["config"]
        self.assertEqual(cfg["max_iterations"], 100)
        self.assertEqual(cfg["model"], "sonnet")

    def test_update_config_unknown_key_rejected(self):
        """Unknown keys are rejected instead of silently dropped."""
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "nonexistent_key": "value",
        })
        self.assertEqual(resp.status_code, 422)

    def test_update_config_empty(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={})
        self.assertEqual(resp.status_code, 400)

    def test_update_config_not_found(self):
        resp = self.client.patch("/api/goal/nonexistent/config", headers=self.headers, json={
            "max_iterations": 10,
        })
        self.assertEqual(resp.status_code, 404)


class TestDeleteGoal(GoalAPITestBase):

    def test_delete_goal(self):
        self._add_goal("goal_1")
        resp = self.client.delete("/api/goal/goal_1", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "deleted")
        # Verify it's gone
        resp2 = self.client.get("/api/goal/goal_1", headers=self.headers)
        self.assertEqual(resp2.status_code, 404)

    def test_delete_running_blocked(self):
        self._add_goal("goal_1")
        self.goal_active["12345:sid1"] = "goal_1"
        resp = self.client.delete("/api/goal/goal_1", headers=self.headers)
        self.assertEqual(resp.status_code, 409)

    def test_delete_not_found(self):
        resp = self.client.delete("/api/goal/nonexistent", headers=self.headers)
        self.assertEqual(resp.status_code, 404)


class TestConfigValidation(GoalAPITestBase):
    """Test config validation on PATCH and POST endpoints."""

    def test_patch_invalid_max_iterations(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "max_iterations": 0,
        })
        self.assertEqual(resp.status_code, 422)

    def test_patch_max_iterations_too_high(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "max_iterations": 9999,
        })
        self.assertEqual(resp.status_code, 422)

    def test_patch_invalid_execution_mode(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "execution_mode": "invalid_mode",
        })
        self.assertEqual(resp.status_code, 422)

    def test_patch_invalid_model(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "model": "gpt-4",
        })
        self.assertEqual(resp.status_code, 422)

    def test_patch_valid_execution_modes(self):
        self._add_goal("goal_1")
        for mode in ["auto", "claude", "codex", "codex_reviewed", "justdoit", "omni", "claude-only"]:
            resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
                "execution_mode": mode,
            })
            self.assertEqual(resp.status_code, 200, f"Mode {mode} should be valid")

    def test_patch_invalid_max_total_time(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "max_total_time": 10,  # Below 60s minimum
        })
        self.assertEqual(resp.status_code, 422)

    def test_patch_invalid_verification_commands(self):
        self._add_goal("goal_1")
        resp = self.client.patch("/api/goal/goal_1/config", headers=self.headers, json={
            "verification_commands": [123, 456],  # Not strings
        })
        self.assertEqual(resp.status_code, 422)

    def test_create_with_invalid_config(self):
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Test goal",
            "config": {"max_iterations": -5},
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid config", resp.json()["detail"])

    def test_create_with_valid_config(self):
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Test goal",
            "config": {"max_iterations": 100, "model": "sonnet"},
        })
        self.assertEqual(resp.status_code, 200)

    def test_create_with_unknown_config_keys_rejected(self):
        """Unknown config keys in POST are rejected."""
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Test goal",
            "config": {"unknown_key": "value", "max_iterations": 10},
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid config", resp.json()["detail"])


class TestSessionResolution(GoalAPITestBase):
    """Test _resolve_goal_session behavior."""

    def test_invalid_session_name_returns_404(self):
        """Providing a non-existent session_name should 404, not fall back."""
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Test goal",
            "session_name": "nonexistent_session",
        })
        self.assertEqual(resp.status_code, 404)
        self.assertIn("not found", resp.json()["detail"])

    def test_no_session_name_falls_back_to_active(self):
        """Without session_name, should use the active session."""
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Test goal",
        })
        self.assertEqual(resp.status_code, 200)

    def test_valid_session_name_works(self):
        """Valid session_name should resolve correctly."""
        resp = self.client.post("/api/goal", headers=self.headers, json={
            "description": "Test goal",
            "session_name": "default",
        })
        self.assertEqual(resp.status_code, 200)


class TestAuthRequired(GoalAPITestBase):

    def test_no_auth_rejected(self):
        """All goal endpoints require auth."""
        endpoints = [
            ("GET", "/api/goals/12345"),
            ("GET", "/api/goal/goal_1"),
            ("GET", "/api/goal/goal_1/journal"),
            ("POST", "/api/goal"),
            ("POST", "/api/goal/goal_1/pause"),
            ("POST", "/api/goal/goal_1/resume"),
            ("POST", "/api/goal/goal_1/cancel"),
            ("POST", "/api/goal/goal_1/replan"),
            ("PATCH", "/api/goal/goal_1/config"),
            ("DELETE", "/api/goal/goal_1"),
        ]
        for method, path in endpoints:
            resp = self.client.request(method, path)
            self.assertEqual(resp.status_code, 401, f"{method} {path} should require auth")


if __name__ == "__main__":
    unittest.main()
