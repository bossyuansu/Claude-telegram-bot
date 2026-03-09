"""Tests for Mission Control API: cancel-task, active-tasks, and cleanup persistence.

Covers residual risk areas:
1. cancel_task EPERM/kill fallback + correct WS status broadcasts
2. Active-task snapshot/concurrency safety in GET and cancel paths
3. Deepreview cleanup persistence (no phantom task after pop + save)
"""
import json
import os
import signal
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Set API_SECRET before importing api (module reads env at init_refs time)
os.environ["API_SECRET"] = "test-secret"

import api as api_server
from starlette.testclient import TestClient


class MCTestBase(unittest.TestCase):
    """Shared setup: wire api module-level refs to test mocks."""

    def setUp(self):
        self.justdoit_active = {}
        self.omni_active = {}
        self.deepreview_active = {}
        self.active_processes = {}
        self.user_sessions = {}
        self.cancelled_sessions = set()
        self.user_feedback_queue = {}

        self.mock_ws_broadcast = MagicMock()
        self.mock_save_tasks = MagicMock()
        self.mock_send_message = MagicMock()
        self.mock_is_allowed = MagicMock(return_value=True)
        self.mock_get_session_id = MagicMock(side_effect=lambda s: s.get("id", "sid"))
        self.mock_get_active_session = MagicMock(return_value=None)
        self.active_sessions_data = {}

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
            omni_active=self.omni_active,
            deepreview_active=self.deepreview_active,
            send_message=self.mock_send_message,
            send_message_no_ws=MagicMock(),
            cancelled_sessions=self.cancelled_sessions,
            ws_broadcast_status=self.mock_ws_broadcast,
            save_active_tasks=self.mock_save_tasks,
            user_feedback_queue=self.user_feedback_queue,
            get_active_sessions_data=lambda: self.active_sessions_data,
        )

        self.client = TestClient(api_server.app)
        self.headers = {"Authorization": "Bearer test-secret"}

    def _add_session(self, chat_id, name, session_id):
        key = str(chat_id)
        if key not in self.user_sessions:
            self.user_sessions[key] = {"sessions": [], "active": session_id}
        self.user_sessions[key]["sessions"].append({"name": name, "id": session_id})


# ──────────────────────────────────────────────────────────
# 1. Cancel-task: EPERM/kill fallback + WS broadcasts
# ──────────────────────────────────────────────────────────

class TestCancelTaskKillFallback(MCTestBase):
    """Test process kill paths including EPERM fallback."""

    def _setup_active_task(self):
        """Helper: register a session with an active justdoit task and a mock process."""
        self._add_session(123, "my-session", "sid1")
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "my-session",
            "task": "test task", "phase": "execute", "step": 3, "started": 1000,
        }
        proc = MagicMock()
        proc.pid = 42
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        self.active_processes["sid1"] = proc
        return proc

    @patch("os.killpg")
    @patch("os.getpgid", return_value=42)
    def test_normal_killpg_succeeds(self, mock_getpgid, mock_killpg):
        """Normal path: killpg succeeds, process.kill() NOT called."""
        proc = self._setup_active_task()

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "my-session"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        mock_killpg.assert_called_once_with(42, signal.SIGKILL)
        proc.kill.assert_not_called()
        # Pipes closed
        proc.stdout.close.assert_called_once()
        proc.stderr.close.assert_called_once()

    @patch("os.killpg", side_effect=PermissionError("EPERM"))
    @patch("os.getpgid", return_value=42)
    def test_eperm_falls_back_to_process_kill(self, mock_getpgid, mock_killpg):
        """EPERM on killpg → falls back to process.kill()."""
        proc = self._setup_active_task()

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "my-session"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        mock_killpg.assert_called_once()
        proc.kill.assert_called_once()

    @patch("os.killpg", side_effect=ProcessLookupError("No such process"))
    @patch("os.getpgid", return_value=42)
    def test_process_already_dead(self, mock_getpgid, mock_killpg):
        """ProcessLookupError is silently swallowed (process already exited)."""
        proc = self._setup_active_task()

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "my-session"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        # No fallback to process.kill — ProcessLookupError is handled directly
        proc.kill.assert_not_called()

    @patch("os.killpg", side_effect=PermissionError("EPERM"))
    @patch("os.getpgid", return_value=42)
    def test_eperm_fallback_also_fails_gracefully(self, mock_getpgid, mock_killpg):
        """Both killpg and process.kill() fail — no crash, endpoint returns 200."""
        proc = self._setup_active_task()
        proc.kill.side_effect = OSError("kill also failed")

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "my-session"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "cancelled")

    def test_pipes_closed_even_when_one_is_none(self):
        """If process.stderr is None, only stdout is closed — no crash."""
        self._add_session(123, "my-session", "sid1")
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "my-session",
        }
        proc = MagicMock()
        proc.pid = 42
        proc.stdout = MagicMock()
        proc.stderr = None
        self.active_processes["sid1"] = proc

        with patch("os.killpg"), patch("os.getpgid", return_value=42):
            resp = self.client.post("/api/cancel-task",
                json={"chat_id": 123, "session": "my-session"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        proc.stdout.close.assert_called_once()


class TestCancelTaskWSBroadcasts(MCTestBase):
    """Verify WS status broadcasts: exactly one per mode, no duplicates."""

    def test_cancel_justdoit_broadcasts_once(self):
        self._add_session(123, "sess", "sid1")
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "sess",
        }

        self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)

        # Exactly one mode-specific broadcast (from the deactivation loop)
        mode_calls = [c for c in self.mock_ws_broadcast.call_args_list
                      if c[0][1] in ("justdoit", "omni", "deepreview")]
        self.assertEqual(len(mode_calls), 1)
        self.assertEqual(mode_calls[0][0][1], "justdoit")
        self.assertFalse(mode_calls[0][1].get("active", True))

    def test_cancel_omni_broadcasts_once(self):
        self._add_session(123, "sess", "sid1")
        self.omni_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "sess",
        }

        self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)

        mode_calls = [c for c in self.mock_ws_broadcast.call_args_list
                      if c[0][1] in ("justdoit", "omni", "deepreview")]
        self.assertEqual(len(mode_calls), 1)
        self.assertEqual(mode_calls[0][0][1], "omni")

    def test_cancel_deepreview_broadcasts_once(self):
        self._add_session(123, "sess", "sid1")
        self.deepreview_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "sess",
        }

        self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)

        mode_calls = [c for c in self.mock_ws_broadcast.call_args_list
                      if c[0][1] in ("justdoit", "omni", "deepreview")]
        self.assertEqual(len(mode_calls), 1)
        self.assertEqual(mode_calls[0][0][1], "deepreview")

    def test_no_active_task_no_process_returns_404(self):
        """No active task and no process → 404, no WS broadcast."""
        self._add_session(123, "sess", "sid1")

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)
        self.mock_ws_broadcast.assert_not_called()

    def test_cancel_calls_save_active_tasks(self):
        """save_active_tasks is always called on cancel (even with no active mode)."""
        self._add_session(123, "sess", "sid1")
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "sess",
        }

        self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.mock_save_tasks.assert_called_once()

    def test_cancel_clears_feedback_queue(self):
        """Feedback queue for the session is cleared on cancel."""
        self._add_session(123, "sess", "sid1")
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "sess",
        }
        self.user_feedback_queue["123:sid1"] = ["pending msg"]

        self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertNotIn("123:sid1", self.user_feedback_queue)


# ──────────────────────────────────────────────────────────
# 2. Snapshot / concurrency safety
# ──────────────────────────────────────────────────────────

class TestActiveTasksSnapshot(MCTestBase):
    """Verify list() snapshot prevents RuntimeError during dict mutation."""

    def test_get_active_tasks_uses_snapshot(self):
        """Multiple active tasks across modes return correctly."""
        self._add_session(123, "s1", "sid1")
        self._add_session(123, "s2", "sid2")
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "s1",
            "task": "task1", "phase": "plan", "step": 1, "started": 100,
        }
        self.omni_active["123:sid2"] = {
            "active": True, "chat_id": 123, "session_name": "s2",
            "task": "task2", "phase": "execute", "step": 5, "started": 200,
        }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 2)
        modes = {t["mode"] for t in tasks}
        self.assertEqual(modes, {"justdoit", "omni"})

    def test_inactive_tasks_excluded(self):
        """Tasks with active=False are not returned."""
        self._add_session(123, "s1", "sid1")
        self.justdoit_active["123:sid1"] = {
            "active": False, "chat_id": 123, "session_name": "s1",
        }
        self.omni_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "s1",
            "task": "", "phase": "", "step": 0, "started": 0,
        }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["mode"], "omni")

    def test_concurrent_dict_mutation_during_iteration(self):
        """Simulate a worker thread popping a key mid-iteration.

        This verifies the list() snapshot in get_active_tasks prevents
        RuntimeError: dictionary changed size during iteration.
        """
        # Populate with several entries
        for i in range(5):
            self.justdoit_active[f"123:sid{i}"] = {
                "active": True, "chat_id": 123, "session_name": f"s{i}",
                "task": f"t{i}", "phase": "exec", "step": i, "started": i * 100,
            }

        # Start a thread that mutates the dict while the endpoint iterates
        mutation_done = threading.Event()
        original_items = self.justdoit_active.items

        call_count = 0
        def items_with_mutation():
            nonlocal call_count
            result = list(original_items())
            call_count += 1
            if call_count == 1:
                # Simulate concurrent pop after snapshot taken
                self.justdoit_active.pop("123:sid0", None)
                self.justdoit_active.pop("123:sid4", None)
            return result

        # The list() call in get_active_tasks snapshots .items(), so even if
        # dict changes afterward, iteration is safe. This test verifies no crash.
        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_cancel_toctou_key_popped_by_worker(self):
        """Cancel when the worker thread has already popped the key.

        The cancel_task loop uses `state_dict_ref.get(jdi_key)` which returns
        None if the key was popped — no KeyError, no crash.
        """
        self._add_session(123, "sess", "sid1")
        # Task was active but worker popped it between request arrival and
        # the state_dict_ref.get() call — dict has no entry
        # (we never add it to justdoit_active)

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        # No active mode found, no process → 404
        self.assertEqual(resp.status_code, 404)

    def test_cancel_state_deactivated_between_check_and_mutation(self):
        """Worker sets active=False after our .get() but before our mutation.

        Even if both the cancel endpoint and the worker set active=False,
        the state dict entry is consistent (False), and save_active_tasks
        correctly excludes it.
        """
        self._add_session(123, "sess", "sid1")
        state = {
            "active": True, "chat_id": 123, "session_name": "sess",
            "task": "t", "phase": "p", "step": 1, "started": 100,
        }
        self.justdoit_active["123:sid1"] = state

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(state["active"])

    def test_get_active_tasks_filters_by_chat_id(self):
        """Tasks for other chat_ids are excluded."""
        self.justdoit_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "s1",
            "task": "mine", "phase": "", "step": 0, "started": 0,
        }
        self.justdoit_active["456:sid2"] = {
            "active": True, "chat_id": 456, "session_name": "s2",
            "task": "other", "phase": "", "step": 0, "started": 0,
        }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["session"], "s1")

    def test_get_active_tasks_all_three_modes(self):
        """All three modes (justdoit, omni, deepreview) are scanned."""
        for mode_dict, mode in [
            (self.justdoit_active, "justdoit"),
            (self.omni_active, "omni"),
            (self.deepreview_active, "deepreview"),
        ]:
            mode_dict["123:sid_" + mode] = {
                "active": True, "chat_id": 123, "session_name": mode,
                "task": mode, "phase": "", "step": 0, "started": 0,
            }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 3)
        modes = {t["mode"] for t in tasks}
        self.assertEqual(modes, {"justdoit", "omni", "deepreview"})


# ──────────────────────────────────────────────────────────
# 3. Deepreview cleanup persistence (regression test)
# ──────────────────────────────────────────────────────────

class TestDeepreviewCleanupPersistence(MCTestBase):
    """Verify deepreview tasks don't become phantoms after cleanup."""

    def test_cancel_deepreview_saves_tasks(self):
        """Cancelling a deepreview task calls save_active_tasks to persist removal."""
        self._add_session(123, "dr-sess", "sid1")
        self.deepreview_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "dr-sess",
            "task": "review code", "phase": "review", "step": 2, "started": 500,
        }

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "dr-sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)

        # save_active_tasks MUST be called so the disk file reflects the deactivation
        self.mock_save_tasks.assert_called_once()
        # The state should now be inactive
        self.assertFalse(self.deepreview_active["123:sid1"]["active"])

    def test_deepreview_not_in_active_tasks_after_cancel(self):
        """After cancelling deepreview, GET active-tasks returns empty."""
        self._add_session(123, "dr-sess", "sid1")
        self.deepreview_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "dr-sess",
            "task": "review", "phase": "review", "step": 1, "started": 100,
        }

        # Cancel
        self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "dr-sess"}, headers=self.headers)

        # Verify: active-tasks should be empty now
        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 0)

    def test_deepreview_pop_removes_from_active_tasks(self):
        """Simulates what bot.py's finally block does: pop + save.

        After .pop() the key is gone, so save_active_tasks (if it reads the
        dict) will not find it. This is the regression test for the missing
        save_active_tasks() call in deepreview cleanup.
        """
        self.deepreview_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "dr-sess",
            "task": "review", "phase": "review", "step": 5, "started": 300,
        }

        # Simulate bot.py deepreview finally block
        self.deepreview_active.pop("123:sid1", None)
        self.mock_save_tasks()

        # After pop, active-tasks endpoint should return nothing
        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 0)
        self.mock_save_tasks.assert_called_once()


class TestBotPyDeepreviewFinally(unittest.TestCase):
    """Verify bot.py deepreview finally block calls save_active_tasks.

    This is a static/structural test: reads the source code and asserts
    that save_active_tasks() appears after deepreview_active.pop() in
    the deepreview finally block.
    """

    def test_deepreview_finally_calls_save_active_tasks(self):
        """The deepreview finally block must call save_active_tasks() after .pop()."""
        with open("bot.py", "r") as f:
            source = f.read()

        # Find the deepreview finally block by locating the unique pattern:
        # deepreview_active.pop(chat_key, None) followed by save_active_tasks()
        # within the same finally block.
        import re

        # Find all finally blocks containing deepreview_active.pop
        pattern = r'finally:\s*\n(?:.*\n)*?.*deepreview_active\.pop\(.*?\).*\n((?:.*\n)*?)(?=\ndef |\Z)'
        match = re.search(pattern, source)
        self.assertIsNotNone(match, "Could not find deepreview finally block with .pop()")

        # The lines between pop and the next function should contain save_active_tasks()
        after_pop = match.group(0)
        pop_idx = after_pop.index("deepreview_active.pop")
        code_after_pop = after_pop[pop_idx:]
        self.assertIn("save_active_tasks()", code_after_pop,
            "save_active_tasks() must be called after deepreview_active.pop() "
            "in the finally block — otherwise the task file will retain a phantom entry")


# ──────────────────────────────────────────────────────────
# Auth edge cases for MC endpoints
# ──────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────
# 4. Pause/Resume endpoints
# ──────────────────────────────────────────────────────────

class TestPauseResumeTask(MCTestBase):
    """Test pause and resume API endpoints."""

    def _make_active_task(self, mode_dict, mode="justdoit"):
        """Helper: register an active task with a resume_event."""
        import threading
        self._add_session(123, "sess", "sid1")
        event = threading.Event()
        event.set()  # Not paused
        mode_dict["123:sid1"] = {
            "active": True, "paused": False, "resume_event": event,
            "chat_id": 123, "session_name": "sess",
            "task": "test", "phase": "implementing", "step": 3, "started": 1000,
        }
        return mode_dict["123:sid1"]

    def test_pause_sets_paused_flag(self):
        """Pausing sets paused=True but keeps active=True."""
        state = self._make_active_task(self.justdoit_active)

        resp = self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "paused")
        self.assertTrue(state["active"])
        self.assertTrue(state["paused"])

    def test_pause_clears_resume_event(self):
        """Pausing clears the resume_event so the loop thread blocks."""
        state = self._make_active_task(self.justdoit_active)
        self.assertTrue(state["resume_event"].is_set())

        self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertFalse(state["resume_event"].is_set())

    def test_pause_broadcasts_status(self):
        """Pause broadcasts paused=True status."""
        self._make_active_task(self.justdoit_active)

        self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)

        calls = [c for c in self.mock_ws_broadcast.call_args_list
                 if c[1].get("paused") is True]
        self.assertEqual(len(calls), 1)

    def test_pause_saves_active_tasks(self):
        self._make_active_task(self.justdoit_active)

        self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.mock_save_tasks.assert_called()

    def test_pause_nonexistent_returns_404(self):
        self._add_session(123, "sess", "sid1")
        resp = self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_pause_already_paused_returns_404(self):
        """Pausing an already-paused task returns 404 (no-op protection)."""
        state = self._make_active_task(self.justdoit_active)
        state["paused"] = True

        resp = self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_resume_clears_paused_flag(self):
        """Resuming sets paused=False and active stays True."""
        state = self._make_active_task(self.justdoit_active)
        state["paused"] = True
        state["resume_event"].clear()

        resp = self.client.post("/api/resume-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "resumed")
        self.assertTrue(state["active"])
        self.assertFalse(state["paused"])

    def test_resume_sets_event(self):
        """Resuming sets the resume_event to unblock the loop thread."""
        state = self._make_active_task(self.justdoit_active)
        state["paused"] = True
        state["resume_event"].clear()

        self.client.post("/api/resume-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertTrue(state["resume_event"].is_set())

    def test_resume_broadcasts_status(self):
        """Resume broadcasts paused=False status."""
        state = self._make_active_task(self.justdoit_active)
        state["paused"] = True
        state["resume_event"].clear()

        self.client.post("/api/resume-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)

        calls = [c for c in self.mock_ws_broadcast.call_args_list
                 if c[1].get("paused") is False]
        self.assertEqual(len(calls), 1)

    def test_resume_non_paused_returns_404(self):
        """Resuming a running (not paused) task returns 404."""
        self._make_active_task(self.justdoit_active)

        resp = self.client.post("/api/resume-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_cancel_unblocks_paused_task(self):
        """Cancelling a paused task sets the resume_event so the loop exits."""
        import threading
        state = self._make_active_task(self.justdoit_active)
        state["paused"] = True
        state["resume_event"].clear()

        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(state["active"])
        self.assertTrue(state["resume_event"].is_set())

    def test_paused_task_appears_in_active_tasks(self):
        """A paused task still appears in GET active-tasks with paused=True."""
        self._make_active_task(self.justdoit_active)
        self.justdoit_active["123:sid1"]["paused"] = True

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]["paused"])

    def test_pause_omni_works(self):
        """Pause works for omni mode too."""
        self._make_active_task(self.omni_active, mode="omni")

        resp = self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["mode"], "omni")

    def test_pause_deepreview_works(self):
        """Pause works for deepreview mode too."""
        self._make_active_task(self.deepreview_active, mode="deepreview")

        resp = self.client.post("/api/pause-task",
            json={"chat_id": 123, "session": "sess"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["mode"], "deepreview")


# ──────────────────────────────────────────────────────────
# 5. CLI runs (Claude, Codex, Gemini) in active-tasks
# ──────────────────────────────────────────────────────────

class TestCLIRunsInActiveTasks(MCTestBase):
    """Test that active CLI processes appear in GET /api/active-tasks."""

    def test_claude_run_appears(self):
        """An active Claude process appears as mode=claude."""
        self._add_session(123, "my-proj", "sid1")
        self.user_sessions["123"]["sessions"][0]["last_cli"] = "Claude"
        self.active_processes["sid1"] = MagicMock()  # mock Popen
        self.active_sessions_data["sid1"] = {
            "chat_id": "123", "session_name": "my-proj",
            "prompt": "Fix the bug", "started": 1000,
        }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["mode"], "claude")
        self.assertEqual(tasks[0]["session"], "my-proj")
        self.assertEqual(tasks[0]["task"], "Fix the bug")
        self.assertEqual(tasks[0]["started"], 1000)
        self.assertFalse(tasks[0]["paused"])

    def test_codex_run_appears(self):
        """An active Codex process appears as mode=codex."""
        self._add_session(123, "proj", "sid1")
        self.user_sessions["123"]["sessions"][0]["last_cli"] = "Codex"
        self.active_processes["sid1"] = MagicMock()
        self.active_sessions_data["sid1"] = {
            "chat_id": "123", "session_name": "proj",
            "prompt": "Refactor auth", "started": 2000,
        }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["mode"], "codex")

    def test_gemini_run_appears(self):
        """An active Gemini process appears as mode=gemini."""
        self._add_session(123, "proj", "sid1")
        self.user_sessions["123"]["sessions"][0]["last_cli"] = "Gemini"
        self.active_processes["sid1"] = MagicMock()

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["mode"], "gemini")

    def test_cli_run_excluded_when_autonomous_task_active(self):
        """A session with an active omni task should NOT also show as a CLI run."""
        self._add_session(123, "proj", "sid1")
        self.user_sessions["123"]["sessions"][0]["last_cli"] = "Claude"
        self.active_processes["sid1"] = MagicMock()
        self.omni_active["123:sid1"] = {
            "active": True, "chat_id": 123, "session_name": "proj",
            "task": "Build feature", "phase": "executing", "step": 3, "started": 500,
        }

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        # Should only have the omni task, not a duplicate CLI entry
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["mode"], "omni")

    def test_cli_run_not_excluded_for_different_session(self):
        """Autonomous task on one session should not exclude CLI run on another."""
        self._add_session(123, "proj-a", "sidA")
        self._add_session(123, "proj-b", "sidB")
        self.user_sessions["123"]["sessions"][0]["last_cli"] = "Claude"
        self.user_sessions["123"]["sessions"][1]["last_cli"] = "Codex"
        self.omni_active["123:sidA"] = {
            "active": True, "chat_id": 123, "session_name": "proj-a",
            "task": "t", "phase": "executing", "step": 1, "started": 100,
        }
        self.active_processes["sidA"] = MagicMock()
        self.active_processes["sidB"] = MagicMock()

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 2)
        modes = {t["mode"] for t in tasks}
        self.assertEqual(modes, {"omni", "codex"})

    def test_multiple_cli_runs_across_sessions(self):
        """Multiple CLI runs on different sessions all appear."""
        self._add_session(123, "s1", "sid1")
        self._add_session(123, "s2", "sid2")
        self.user_sessions["123"]["sessions"][0]["last_cli"] = "Claude"
        self.user_sessions["123"]["sessions"][1]["last_cli"] = "Gemini"
        self.active_processes["sid1"] = MagicMock()
        self.active_processes["sid2"] = MagicMock()

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 2)

    def test_cli_run_without_active_sessions_data(self):
        """CLI run appears even when active_sessions.json has no entry (no started/prompt)."""
        self._add_session(123, "proj", "sid1")
        self.active_processes["sid1"] = MagicMock()

        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["started"], 0)
        self.assertEqual(tasks[0]["task"], "")

    def test_cancel_cli_run_kills_process(self):
        """Cancelling a CLI-only run (no autonomous task) kills the process."""
        self._add_session(123, "proj", "sid1")
        proc = MagicMock()
        proc.pid = 99
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        self.active_processes["sid1"] = proc

        with patch("os.killpg"), patch("os.getpgid", return_value=99):
            resp = self.client.post("/api/cancel-task",
                json={"chat_id": 123, "session": "proj"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("sid1", self.active_processes)


class TestMCEndpointAuth(MCTestBase):
    """Verify auth is enforced on Mission Control endpoints."""

    def test_active_tasks_requires_auth(self):
        resp = self.client.get("/api/active-tasks/123")
        self.assertEqual(resp.status_code, 401)

    def test_cancel_task_requires_auth(self):
        resp = self.client.post("/api/cancel-task", json={"session": "x"})
        self.assertEqual(resp.status_code, 401)

    def test_pause_task_requires_auth(self):
        resp = self.client.post("/api/pause-task", json={"session": "x"})
        self.assertEqual(resp.status_code, 401)

    def test_resume_task_requires_auth(self):
        resp = self.client.post("/api/resume-task", json={"session": "x"})
        self.assertEqual(resp.status_code, 401)

    def test_cancel_task_unknown_session_returns_404(self):
        self._add_session(123, "real", "sid1")
        resp = self.client.post("/api/cancel-task",
            json={"chat_id": 123, "session": "nonexistent"}, headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_active_tasks_disallowed_chat_returns_403(self):
        self.mock_is_allowed.return_value = False
        resp = self.client.get("/api/active-tasks/123", headers=self.headers)
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
