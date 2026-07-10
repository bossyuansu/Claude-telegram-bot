"""Tests for Goal Mode data model, persistence (Phase 0), and decomposition engine (Phase 1)."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY


class TestGoalPersistence(unittest.TestCase):
    """Test goal CRUD operations, index management, and crash recovery."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

        # Import bot and patch paths
        import bot
        self.bot = bot
        self._orig_goals_dir = bot.GOALS_DIR
        self._orig_index_file = bot.GOALS_INDEX_FILE
        self._orig_data_dir = bot.DATA_DIR
        self._orig_active_tasks_file = bot.ACTIVE_TASKS_FILE
        self._orig_goal_active = bot.goal_active
        self._orig_goal_state = bot.goal_state
        self._orig_goal_lock = bot._goal_lock

        bot.GOALS_DIR = self.goals_dir
        bot.GOALS_INDEX_FILE = self.goals_dir / "index.json"
        bot.DATA_DIR = Path(self.tmpdir)
        bot.ACTIVE_TASKS_FILE = Path(self.tmpdir) / "active_tasks.json"
        bot.goal_active = {}
        bot.goal_state = {}
        bot._goal_lock = threading.Lock()

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GOALS_INDEX_FILE = self._orig_index_file
        self.bot.DATA_DIR = self._orig_data_dir
        self.bot.ACTIVE_TASKS_FILE = self._orig_active_tasks_file
        self.bot.goal_active = self._orig_goal_active
        self.bot.goal_state = self._orig_goal_state
        self.bot._goal_lock = self._orig_goal_lock
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_goal(self):
        """Creating a goal should write a file and update the index."""
        goal = self.bot._create_goal(
            chat_id="12345",
            session_id="sess_abc",
            cwd="/tmp/test-project",
            description="Add unit tests to the API",
        )

        self.assertTrue(goal["id"].startswith("goal_"))
        self.assertEqual(goal["chat_id"], "12345")
        self.assertEqual(goal["session_id"], "sess_abc")
        self.assertEqual(goal["cwd"], "/tmp/test-project")
        self.assertEqual(goal["description"], "Add unit tests to the API")
        self.assertEqual(goal["status"], "planning")
        self.assertEqual(goal["milestones"], [])
        self.assertEqual(goal["iterations"], [])
        self.assertEqual(goal["learnings"], [])

        # File should exist
        goal_file = self.goals_dir / f"{goal['id']}.json"
        self.assertTrue(goal_file.exists())

        # Index should be updated
        index = self.bot._load_goal_index()
        self.assertIn("12345", index)
        self.assertIn(goal["id"], index["12345"])

    def test_create_goal_with_custom_config(self):
        """Custom config should merge with defaults."""
        goal = self.bot._create_goal(
            chat_id="12345",
            session_id="sess_abc",
            cwd="/tmp",
            description="test",
            config={"max_iterations": 100, "execution_mode": "omni"},
        )

        self.assertEqual(goal["config"]["max_iterations"], 100)
        self.assertEqual(goal["config"]["execution_mode"], "omni")
        # Defaults preserved
        self.assertEqual(goal["config"]["max_consecutive_failures"], 5)
        self.assertEqual(goal["config"]["model"], "opus")

    def test_load_goal(self):
        """Loading a goal should return the same data that was saved."""
        goal = self.bot._create_goal("12345", "sess_abc", "/tmp", "test goal")
        loaded = self.bot._load_goal(goal["id"])
        self.assertEqual(loaded["id"], goal["id"])
        self.assertEqual(loaded["description"], "test goal")

    def test_load_nonexistent_goal(self):
        """Loading a nonexistent goal should return None."""
        result = self.bot._load_goal("goal_doesnotexist")
        self.assertIsNone(result)

    def test_save_goal_updates(self):
        """Saving updates to a goal should persist them."""
        goal = self.bot._create_goal("12345", "sess_abc", "/tmp", "test")
        goal["status"] = "active"
        goal["title"] = "My Goal"
        goal["milestones"] = [
            {"id": "m1", "title": "Step 1", "status": "pending", "order": 1,
             "description": "", "acceptance_criteria": [], "depends_on": [], "attempts": 0, "completed_at": None}
        ]
        self.bot._save_goal(goal)

        loaded = self.bot._load_goal(goal["id"])
        self.assertEqual(loaded["status"], "active")
        self.assertEqual(loaded["title"], "My Goal")
        self.assertEqual(len(loaded["milestones"]), 1)

    def test_delete_goal(self):
        """Deleting a goal should remove the file and index entry."""
        goal = self.bot._create_goal("12345", "sess_abc", "/tmp", "test")
        goal_id = goal["id"]

        self.bot._delete_goal(goal_id)

        # File gone
        self.assertFalse((self.goals_dir / f"{goal_id}.json").exists())
        # Index cleaned
        index = self.bot._load_goal_index()
        self.assertNotIn(goal_id, index.get("12345", []))

    def test_delete_nonexistent_goal(self):
        """Deleting a nonexistent goal should not raise."""
        self.bot._delete_goal("goal_doesnotexist")  # Should not raise

    def test_list_goals(self):
        """Listing goals should return all goals for a chat."""
        g1 = self.bot._create_goal("12345", "s1", "/tmp", "goal one")
        g2 = self.bot._create_goal("12345", "s2", "/tmp", "goal two")
        g3 = self.bot._create_goal("99999", "s3", "/tmp", "other chat")

        goals = self.bot._list_goals("12345")
        self.assertEqual(len(goals), 2)
        ids = {g["id"] for g in goals}
        self.assertIn(g1["id"], ids)
        self.assertIn(g2["id"], ids)

        goals_other = self.bot._list_goals("99999")
        self.assertEqual(len(goals_other), 1)
        self.assertEqual(goals_other[0]["id"], g3["id"])

    def test_list_goals_empty(self):
        """Listing goals for a chat with none should return empty list."""
        goals = self.bot._list_goals("00000")
        self.assertEqual(goals, [])

    def test_goal_index_multiple_chats(self):
        """Index should track goals across multiple chats."""
        self.bot._create_goal("111", "s1", "/tmp", "a")
        self.bot._create_goal("222", "s2", "/tmp", "b")
        self.bot._create_goal("111", "s3", "/tmp", "c")

        index = self.bot._load_goal_index()
        self.assertEqual(len(index["111"]), 2)
        self.assertEqual(len(index["222"]), 1)

    def test_empty_index(self):
        """Loading index when no file exists returns empty dict."""
        index = self.bot._load_goal_index()
        self.assertEqual(index, {})

    def test_save_active_tasks_includes_goals(self):
        """save_active_tasks should include active goals."""
        goal = self.bot._create_goal("12345", "sess_abc", "/tmp", "test goal")
        goal["status"] = "active"
        goal["title"] = "Test Goal"
        goal["milestones"] = [
            {"id": "m1", "title": "Step 1", "status": "in_progress", "order": 1,
             "description": "", "acceptance_criteria": [], "depends_on": [], "attempts": 0, "completed_at": None}
        ]
        self.bot._save_goal(goal)
        self.bot.goal_active["12345:sess_abc"] = goal["id"]
        self.bot.goal_state["12345:sess_abc"] = {
            "active": True,
            "paused": False,
            "goal_id": goal["id"],
            "task": "Test Goal",
            "phase": "Step 1",
            "step": 1,
            "chat_id": "12345",
            "session_name": "test",
            "started": 1000,
        }

        # Mock the other active dicts to be empty
        orig_jdi = self.bot.justdoit_active
        orig_omni = self.bot.omni_active
        orig_dr = self.bot.deepreview_active
        orig_ralph = self.bot.ralph_active
        self.bot.justdoit_active = {}
        self.bot.omni_active = {}
        self.bot.deepreview_active = {}
        self.bot.ralph_active = {}
        try:
            self.bot.save_active_tasks()

            with open(self.bot.ACTIVE_TASKS_FILE) as f:
                tasks = json.load(f)

            self.assertIn("12345:sess_abc", tasks)
            task = tasks["12345:sess_abc"]
            self.assertEqual(task["type"], "goal")
            self.assertEqual(task["goal_id"], goal["id"])
            self.assertEqual(task["task"], "Test Goal")
            self.assertEqual(task["phase"], "Step 1")
        finally:
            self.bot.justdoit_active = orig_jdi
            self.bot.omni_active = orig_omni
            self.bot.deepreview_active = orig_dr
            self.bot.ralph_active = orig_ralph

    def test_goal_default_config(self):
        """Goal should have sensible defaults in config."""
        goal = self.bot._create_goal("12345", "s1", "/tmp", "test")
        cfg = goal["config"]
        self.assertEqual(cfg["max_iterations"], 50)
        self.assertEqual(cfg["max_consecutive_failures"], 5)
        self.assertEqual(cfg["execution_mode"], "auto")
        self.assertEqual(cfg["auto_replan_threshold"], 3)
        self.assertEqual(cfg["verification_commands"], [])
        self.assertFalse(cfg["pause_between_iterations"])
        self.assertEqual(cfg["model"], "opus")

    def test_atomic_write_survives_concurrent_access(self):
        """Multiple threads creating goals should not corrupt state."""
        errors = []

        def create_goal(i):
            try:
                self.bot._create_goal(
                    chat_id="12345",
                    session_id=f"sess_{i}",
                    cwd="/tmp",
                    description=f"goal {i}",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_goal, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        index = self.bot._load_goal_index()
        self.assertEqual(len(index.get("12345", [])), 10)
        goals = self.bot._list_goals("12345")
        self.assertEqual(len(goals), 10)


class TestExtractJsonFromText(unittest.TestCase):
    """Test _extract_json_from_text helper."""

    def setUp(self):
        import bot
        self.bot = bot

    def test_plain_json(self):
        text = '{"title": "test", "milestones": []}'
        result = self.bot._extract_json_from_text(text)
        self.assertEqual(result["title"], "test")

    def test_json_in_markdown_fence(self):
        text = 'Here is the plan:\n```json\n{"title": "test", "milestones": []}\n```\nDone.'
        result = self.bot._extract_json_from_text(text)
        self.assertEqual(result["title"], "test")

    def test_json_in_plain_fence(self):
        text = '```\n{"title": "test", "milestones": []}\n```'
        result = self.bot._extract_json_from_text(text)
        self.assertEqual(result["title"], "test")

    def test_json_with_preamble(self):
        text = 'Based on my analysis, here is the plan:\n\n{"title": "test", "milestones": [{"id": "m1"}]}'
        result = self.bot._extract_json_from_text(text)
        self.assertEqual(result["title"], "test")
        self.assertEqual(len(result["milestones"]), 1)

    def test_empty_input(self):
        self.assertIsNone(self.bot._extract_json_from_text(""))
        self.assertIsNone(self.bot._extract_json_from_text(None))
        self.assertIsNone(self.bot._extract_json_from_text("  "))

    def test_invalid_json(self):
        self.assertIsNone(self.bot._extract_json_from_text("not json at all"))

    def test_nested_braces(self):
        text = '{"config": {"nested": {"deep": true}}, "value": 1}'
        result = self.bot._extract_json_from_text(text)
        self.assertTrue(result["config"]["nested"]["deep"])

    def test_strings_with_braces(self):
        text = '{"msg": "use {placeholder} here", "ok": true}'
        result = self.bot._extract_json_from_text(text)
        self.assertEqual(result["msg"], "use {placeholder} here")
        self.assertTrue(result["ok"])

    def test_escaped_quotes(self):
        text = '{"msg": "he said \\"hello\\"", "ok": true}'
        result = self.bot._extract_json_from_text(text)
        self.assertTrue(result["ok"])


class TestDecomposeGoal(unittest.TestCase):
    """Test _decompose_goal with mocked Claude calls."""

    def setUp(self):
        import bot
        self.bot = bot

    @patch("bot.run_claude")
    def test_basic_decomposition(self, mock_claude):
        mock_claude.return_value = (json.dumps({
            "title": "Add health endpoint",
            "milestones": [
                {
                    "id": "m1",
                    "title": "Create /health route",
                    "description": "Add GET /health returning 200",
                    "acceptance_criteria": ["/health returns 200", "response includes uptime"],
                    "order": 1,
                    "depends_on": []
                },
                {
                    "id": "m2",
                    "title": "Integration test",
                    "description": "Verify full stack",
                    "acceptance_criteria": ["all tests pass"],
                    "order": 2,
                    "depends_on": ["m1"]
                }
            ]
        }), [])

        title, milestones = self.bot._decompose_goal("Add a health check endpoint", "/tmp")
        self.assertEqual(title, "Add health endpoint")
        self.assertEqual(len(milestones), 2)
        self.assertEqual(milestones[0]["status"], "pending")
        self.assertEqual(milestones[0]["attempts"], 0)
        self.assertIsNone(milestones[0]["completed_at"])
        self.assertEqual(milestones[1]["depends_on"], ["m1"])
        mock_claude.assert_called_once()
        # Verify planning model used
        call_kwargs = mock_claude.call_args
        self.assertEqual(call_kwargs.kwargs.get("model") or call_kwargs[1].get("model"),
                         self.bot.CLAUDE_PLANNING_MODEL)

    @patch("bot.run_claude")
    def test_decomposition_with_markdown_fence(self, mock_claude):
        """Claude sometimes wraps JSON in markdown fences."""
        mock_claude.return_value = (
            'Here is my plan:\n```json\n' + json.dumps({
                "title": "Test Goal",
                "milestones": [{"id": "m1", "title": "Step 1", "order": 1}]
            }) + '\n```\nLet me know if this looks good.',
            []
        )
        title, milestones = self.bot._decompose_goal("test", "/tmp")
        self.assertEqual(title, "Test Goal")
        self.assertEqual(len(milestones), 1)
        # Defaults should be filled
        self.assertEqual(milestones[0]["acceptance_criteria"], [])
        self.assertEqual(milestones[0]["depends_on"], [])
        self.assertEqual(milestones[0]["status"], "pending")

    @patch("bot.run_claude")
    def test_decomposition_fills_defaults(self, mock_claude):
        """Milestones missing optional fields get defaults."""
        mock_claude.return_value = (json.dumps({
            "title": "Minimal",
            "milestones": [{"title": "Just a title"}]  # Missing id, order, etc.
        }), [])
        title, milestones = self.bot._decompose_goal("test", "/tmp")
        m = milestones[0]
        self.assertEqual(m["id"], "m1")  # auto-assigned
        self.assertEqual(m["order"], 1)
        self.assertEqual(m["description"], "")
        self.assertEqual(m["acceptance_criteria"], [])
        self.assertEqual(m["depends_on"], [])

    @patch("bot.run_claude")
    def test_decomposition_invalid_json_raises(self, mock_claude):
        mock_claude.return_value = ("I can't produce valid JSON today, sorry!", [])
        with self.assertRaises(ValueError) as ctx:
            self.bot._decompose_goal("test", "/tmp")
        self.assertIn("Failed to parse", str(ctx.exception))

    @patch("bot.run_claude")
    def test_decomposition_missing_milestones_raises(self, mock_claude):
        mock_claude.return_value = (json.dumps({"title": "No milestones here"}), [])
        with self.assertRaises(ValueError) as ctx:
            self.bot._decompose_goal("test", "/tmp")
        self.assertIn("missing", str(ctx.exception).lower())

    @patch("bot.run_claude")
    def test_decomposition_empty_milestones_raises(self, mock_claude):
        mock_claude.return_value = (json.dumps({"title": "Empty", "milestones": []}), [])
        with self.assertRaises(ValueError):
            self.bot._decompose_goal("test", "/tmp")


class TestReplanGoal(unittest.TestCase):
    """Test _replan_goal with mocked Claude calls."""

    def setUp(self):
        import bot
        self.bot = bot

    def _make_goal(self):
        return {
            "id": "goal_test",
            "description": "Migrate API",
            "cwd": "/tmp",
            "milestones": [
                {"id": "m1", "title": "Setup", "status": "completed", "order": 1,
                 "description": "", "acceptance_criteria": [], "depends_on": [],
                 "attempts": 1, "completed_at": "2026-01-01"},
                {"id": "m2", "title": "Migrate routes", "status": "failed", "order": 2,
                 "description": "", "acceptance_criteria": ["routes work"],
                 "depends_on": ["m1"], "attempts": 3, "completed_at": None},
                {"id": "m3", "title": "Tests", "status": "pending", "order": 3,
                 "description": "", "acceptance_criteria": [], "depends_on": ["m2"],
                 "attempts": 0, "completed_at": None},
            ],
            "iterations": [
                {"id": 1, "milestone_id": "m2", "action": "migrate /users",
                 "outcome": "failure"},
                {"id": 2, "milestone_id": "m2", "action": "migrate /users (retry)",
                 "outcome": "failure"},
            ],
            "learnings": [
                {"category": "technical", "insight": "Flask blueprints need explicit teardown"}
            ],
            "config": {},
        }

    @patch("bot.run_claude")
    def test_replan_preserves_completed(self, mock_claude):
        mock_claude.return_value = (json.dumps({
            "milestones": [
                {"id": "m_new_1", "title": "Migrate routes (smaller batches)", "order": 2,
                 "acceptance_criteria": ["routes work"], "depends_on": []},
                {"id": "m_new_2", "title": "Final verification", "order": 3,
                 "acceptance_criteria": ["all tests pass"], "depends_on": ["m_new_1"]},
            ],
            "replan_rationale": "Breaking migration into smaller steps"
        }), [])

        goal = self._make_goal()
        merged, rationale = self.bot._replan_goal(goal)

        # Completed milestone preserved as first
        self.assertEqual(merged[0]["id"], "m1")
        self.assertEqual(merged[0]["status"], "completed")

        # New milestones appended
        self.assertEqual(len(merged), 3)  # 1 completed + 2 new
        self.assertEqual(merged[1]["status"], "pending")
        self.assertEqual(merged[2]["status"], "pending")
        self.assertIn("smaller", rationale.lower())

    @patch("bot.run_claude")
    def test_replan_fills_defaults(self, mock_claude):
        mock_claude.return_value = (json.dumps({
            "milestones": [{"title": "New step"}],
            "replan_rationale": "simplified"
        }), [])

        goal = self._make_goal()
        merged, _ = self.bot._replan_goal(goal)
        new_m = merged[1]  # After the 1 completed milestone
        self.assertTrue(new_m["id"].startswith("m_new_"))
        self.assertEqual(new_m["status"], "pending")
        self.assertEqual(new_m["attempts"], 0)

    @patch("bot.run_claude")
    def test_replan_invalid_json_raises(self, mock_claude):
        mock_claude.return_value = ("Sorry, can't replan", [])
        with self.assertRaises(ValueError):
            self.bot._replan_goal(self._make_goal())


class TestVerifyMilestone(unittest.TestCase):
    """Test _verify_milestone with mocked Claude calls."""

    def setUp(self):
        import bot
        self.bot = bot
        self.tmpdir = tempfile.mkdtemp()
        self.cwd = Path(self.tmpdir)
        (self.cwd / "package.json").write_text(json.dumps({
            "scripts": {
                "test": "jest",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
                "build": "tsc",
            }
        }))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_goal(self, verification_commands=None):
        return {
            "cwd": str(self.cwd),
            "config": {"verification_commands": verification_commands or []},
        }

    @patch("bot.run_claude")
    def test_all_criteria_pass(self, mock_claude):
        mock_claude.return_value = (json.dumps({
            "results": [
                {"criterion": "file exists", "satisfied": True, "evidence": "checked with ls"},
                {"criterion": "tests pass", "satisfied": True, "evidence": "ran npm test"},
            ]
        }), [])

        goal = self._make_goal()
        milestone = {"title": "Test", "acceptance_criteria": ["file exists", "tests pass"]}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertTrue(result["all_passed"])
        self.assertEqual(len(result["passed"]), 2)
        self.assertEqual(len(result["failed"]), 0)

    @patch("bot.run_claude")
    def test_some_criteria_fail(self, mock_claude):
        mock_claude.return_value = (json.dumps({
            "results": [
                {"criterion": "file exists", "satisfied": True, "evidence": "found it"},
                {"criterion": "tests pass", "satisfied": False, "evidence": "3 failures"},
            ]
        }), [])

        goal = self._make_goal()
        milestone = {"title": "Test", "acceptance_criteria": ["file exists", "tests pass"]}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["passed"]), 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["evidence"], "3 failures")

    @patch("bot.run_claude")
    def test_empty_criteria_no_commands_passes(self, mock_claude):
        """No criteria and no commands -> vacuously passes."""
        goal = self._make_goal()
        milestone = {"title": "Test", "acceptance_criteria": []}
        result = self.bot._verify_milestone(goal, milestone)
        self.assertTrue(result["all_passed"])
        mock_claude.assert_not_called()

    @patch("bot.run_claude")
    def test_parse_failure_treats_as_failed(self, mock_claude):
        mock_claude.return_value = ("I couldn't verify anything", [])

        goal = self._make_goal()
        milestone = {"title": "Test", "acceptance_criteria": ["criterion A"]}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("parse error", result["failed"][0]["evidence"].lower())

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_commands_pass_included(self, mock_claude, mock_subprocess):
        """Passing verification commands appear as passed results."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="All tests passed", stderr=""
        )
        mock_claude.return_value = (json.dumps({
            "results": [
                {"criterion": "tests pass", "satisfied": True, "evidence": "npm test passed"},
            ]
        }), [])

        goal = self._make_goal(verification_commands=["npm test"])
        milestone = {"title": "Test", "acceptance_criteria": ["tests pass"]}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertTrue(result["all_passed"])
        # 1 from command + 1 from Claude criteria
        self.assertEqual(len(result["passed"]), 2)
        mock_subprocess.assert_called_once()
        self.assertEqual(mock_subprocess.call_args.args[0], ["npm", "test"])
        self.assertFalse(mock_subprocess.call_args.kwargs["shell"])

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_milestone_verification_commands_are_run(self, mock_claude, mock_subprocess):
        """Milestone-specific verification commands are run in addition to goal config."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        mock_claude.return_value = (json.dumps({
            "results": [
                {"criterion": "tests pass", "satisfied": True, "evidence": "ok"},
            ]
        }), [])

        goal = self._make_goal()
        milestone = {
            "title": "Test",
            "acceptance_criteria": ["tests pass"],
            "verification_commands": ["python3 -m pytest -q"],
        }
        result = self.bot._verify_milestone(goal, milestone)

        self.assertTrue(result["all_passed"])
        mock_subprocess.assert_called_once()
        self.assertEqual(mock_subprocess.call_args.args[0], ["python3", "-m", "pytest", "-q"])

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_command_failure_enforced(self, mock_claude, mock_subprocess):
        """Failed verification command causes all_passed=False even if Claude says criteria pass."""
        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="", stderr="FAIL: 3 tests failed"
        )
        mock_claude.return_value = (json.dumps({
            "results": [
                {"criterion": "code compiles", "satisfied": True, "evidence": "compiles fine"},
            ]
        }), [])

        goal = self._make_goal(verification_commands=["npm test"])
        milestone = {"title": "Test", "acceptance_criteria": ["code compiles"]}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertFalse(result["all_passed"])
        # Command failed -> 1 failed entry
        cmd_failures = [f for f in result["failed"] if f["criterion"].startswith("Command:")]
        self.assertEqual(len(cmd_failures), 1)
        self.assertIn("exit code 1", cmd_failures[0]["evidence"])

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_command_timeout_classified_transient_raises(self, mock_claude, mock_subprocess):
        """A timed-out command that the classifier judges TRANSIENT raises GoalTransientError."""
        mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd="npm test", timeout=120)
        goal = self._make_goal(verification_commands=["npm test"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        with patch.object(self.bot, "_goal_classify_command_failure", return_value=True) as mc:
            with self.assertRaises(self.bot.GoalTransientError):
                self.bot._verify_milestone(goal, milestone)
            # timeout routed through the classifier (timed_out=True)
            self.assertTrue(mc.called)

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_command_infra_failure_classified_transient_raises(self, mock_claude, mock_subprocess):
        """An infra-looking failure the classifier judges TRANSIENT raises GoalTransientError."""
        mock_subprocess.return_value = MagicMock(
            returncode=2, stdout="", stderr="psql: could not connect to server: Connection refused"
        )
        goal = self._make_goal(verification_commands=["npm test"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        with patch.object(self.bot, "_goal_classify_command_failure", return_value=True):
            with self.assertRaises(self.bot.GoalTransientError):
                self.bot._verify_milestone(goal, milestone)

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_command_infra_failure_classified_real_hard_fails(self, mock_claude, mock_subprocess):
        """Same infra-looking output, but classifier says REAL → milestone fails (no raise)."""
        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="", stderr="could not connect to server: Connection refused"
        )
        goal = self._make_goal(verification_commands=["npm test"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        with patch.object(self.bot, "_goal_classify_command_failure", return_value=False):
            result = self.bot._verify_milestone(goal, milestone)
        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failed"]), 1)

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_test_report_bypasses_classifier(self, mock_claude, mock_subprocess):
        """A pytest-style failure containing infra words is a REAL failure via the fast path —
        the classifier is NOT even consulted (regression for the goal_7611e829 false positive)."""
        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="collected 40 items\ntest_x.py::t FAILED\nE ConnectionError: Connection refused\nassert r.status_code == 503\n1 failed, 39 passed",
            stderr=""
        )
        goal = self._make_goal(verification_commands=["python3 -m pytest -q"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        with patch.object(self.bot, "_goal_classify_command_failure") as mc:
            result = self.bot._verify_milestone(goal, milestone)
            mc.assert_not_called()  # fast path: test report -> REAL without a model call
        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failed"]), 1)

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_verification_command_real_failure_still_hard_fails(self, mock_claude, mock_subprocess):
        """A plain non-infra command failure hard-fails without a classifier call (fast path)."""
        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="", stderr="AssertionError: expected 3 got 2"
        )
        goal = self._make_goal(verification_commands=["npm test"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        with patch.object(self.bot, "_goal_classify_command_failure") as mc:
            result = self.bot._verify_milestone(goal, milestone)
            mc.assert_not_called()  # no infra signal -> no model call
        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failed"]), 1)

    @patch("bot.subprocess.run")
    def test_verification_commands_run_with_no_criteria(self, mock_subprocess):
        """Commands run even when acceptance_criteria is empty; failures block."""
        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="", stderr="lint errors"
        )
        goal = self._make_goal(verification_commands=["npm run lint"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failed"]), 1)
        # Claude should NOT be called since there are no acceptance criteria
        # (subprocess.run is the only mock, no run_claude mock needed)

    @patch("bot.subprocess.run")
    def test_verification_commands_pass_with_no_criteria(self, mock_subprocess):
        """Passing commands with no criteria -> all_passed=True."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        goal = self._make_goal(verification_commands=["npm run lint"])
        milestone = {"title": "Test", "acceptance_criteria": []}
        result = self.bot._verify_milestone(goal, milestone)

        self.assertTrue(result["all_passed"])
        self.assertEqual(len(result["passed"]), 1)

    @patch("bot.subprocess.run")
    def test_unsafe_verification_command_is_rejected(self, mock_subprocess):
        """Unsafe commands are a hard failure and are never executed."""
        goal = self._make_goal(verification_commands=["npm test; rm -rf /"])
        milestone = {"title": "Test", "acceptance_criteria": []}

        result = self.bot._verify_milestone(goal, milestone)

        self.assertFalse(result["all_passed"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("Unsafe verification command rejected", result["failed"][0]["evidence"])
        mock_subprocess.assert_not_called()

    @patch("bot.subprocess.run")
    @patch("bot.run_claude")
    def test_missing_npm_script_command_is_skipped(self, mock_claude, mock_subprocess):
        """Generated stale npm script commands do not block when the package lacks that script."""
        relay = self.cwd / "relay-server"
        relay.mkdir()
        (relay / "package.json").write_text(json.dumps({
            "scripts": {"build": "tsc", "test": "jest"}
        }))
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        mock_claude.return_value = (json.dumps({
            "results": [
                {"criterion": "build passes", "satisfied": True, "evidence": "build ok"},
                {"criterion": "tests pass", "satisfied": True, "evidence": "tests ok"},
            ]
        }), [])

        goal = {"cwd": str(self.cwd), "config": {"verification_commands": []}}
        milestone = {
            "title": "Final verification",
            "acceptance_criteria": ["build passes", "tests pass"],
            "verification_commands": [
                "npm --prefix relay-server run typecheck",
                "npm --prefix relay-server run build",
                "npm --prefix relay-server test",
            ],
        }

        result = self.bot._verify_milestone(goal, milestone)

        self.assertTrue(result["all_passed"])
        self.assertEqual(mock_subprocess.call_count, 2)
        called = [call.args[0] for call in mock_subprocess.call_args_list]
        self.assertNotIn(["npm", "--prefix", "relay-server", "run", "typecheck"], called)


class TestGoalVerificationSuggestions(unittest.TestCase):
    """Test deterministic verification command suggestions."""

    def setUp(self):
        import bot
        self.bot = bot
        self.tmpdir = tempfile.mkdtemp()
        self.cwd = Path(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_package_json_code_milestone_suggests_npm_test(self):
        (self.cwd / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"}
        }))
        milestone = {
            "title": "Add validator unit tests",
            "description": "Implement TypeScript code and tests",
            "acceptance_criteria": ["npm test passes"],
        }

        commands = self.bot._goal_suggest_verification_commands(str(self.cwd), milestone)

        self.assertIn("npm test", commands)

    def test_monorepo_package_milestone_targets_subdir_npm(self):
        relay = self.cwd / "relay-server"
        relay.mkdir()
        (relay / "package.json").write_text(json.dumps({
            "scripts": {"build": "tsc", "test": "jest"}
        }))
        (self.cwd / "tests").mkdir()
        milestone = {
            "title": "Final relay-server verification",
            "description": "Run relay-server TypeScript build and tests",
            "acceptance_criteria": ["npm test passes all suites in relay-server/"],
        }

        commands = self.bot._goal_suggest_verification_commands(str(self.cwd), milestone)

        self.assertIn("npm --prefix relay-server test", commands)
        self.assertNotIn("python3 -m pytest -q", commands)

    def test_monorepo_missing_typecheck_script_is_not_suggested(self):
        relay = self.cwd / "relay-server"
        relay.mkdir()
        (relay / "package.json").write_text(json.dumps({
            "scripts": {"build": "tsc", "test": "jest"}
        }))
        milestone = {
            "title": "Final relay-server verification",
            "description": "Run relay-server TypeScript build and tests. Do NOT use npm run typecheck — it does not exist.",
            "acceptance_criteria": [
                "npm run build completes with exit code 0",
                "npm test completes with all tests passing",
            ],
        }

        commands = self.bot._goal_normalize_verification_commands(str(self.cwd), milestone)

        self.assertIn("npm --prefix relay-server run build", commands)
        self.assertIn("npm --prefix relay-server test", commands)
        self.assertNotIn("npm --prefix relay-server run typecheck", commands)

    def test_natural_language_npm_phrase_without_package_is_not_command(self):
        milestone = {
            "title": "Final verification",
            "description": "Check that the work is complete",
            "acceptance_criteria": ["npm test passes all suites"],
        }

        commands = self.bot._goal_suggest_verification_commands(str(self.cwd), milestone)

        self.assertEqual(commands, [])

    def test_explicit_safe_command_is_preserved(self):
        milestone = {
            "title": "Run Python tests",
            "description": "Verify with `python3 -m pytest -q`",
            "acceptance_criteria": [],
        }

        commands = self.bot._goal_suggest_verification_commands(str(self.cwd), milestone)

        self.assertEqual(commands, ["python3 -m pytest -q"])

    def test_unsafe_explicit_command_is_ignored(self):
        milestone = {
            "title": "Bad command",
            "description": "Do not run `rm -rf /`",
            "acceptance_criteria": [],
        }

        commands = self.bot._goal_suggest_verification_commands(str(self.cwd), milestone)

        self.assertEqual(commands, [])

    def test_model_supplied_commands_are_normalized_to_safe_allow_list(self):
        milestone = {
            "title": "Run tests",
            "description": "Run test verification",
            "acceptance_criteria": [],
            "verification_commands": ["python3 -m pytest -q", "npm test; rm -rf /"],
        }

        commands = self.bot._goal_normalize_verification_commands(str(self.cwd), milestone)

        self.assertEqual(commands, ["python3 -m pytest -q"])


class TestAssessGoalState(unittest.TestCase):
    """Test _assess_goal_state with mocked Claude calls."""

    def setUp(self):
        import bot
        self.bot = bot

    def _make_goal(self):
        return {
            "description": "Add tests",
            "cwd": "/tmp",
            "config": {"auto_replan_threshold": 3},
            "milestones": [
                {"id": "m1", "title": "Write tests", "description": "Write unit tests",
                 "status": "completed", "order": 1, "depends_on": [], "attempts": 1},
                {"id": "m2", "title": "Fix failures", "description": "Fix failing tests",
                 "status": "pending", "order": 2, "depends_on": ["m1"], "attempts": 0},
            ],
            "iterations": [],
            "learnings": [],
        }

    @patch("bot.run_claude")
    def test_assessment_returns_next_milestone(self, mock_claude):
        mock_claude.return_value = (json.dumps({
            "current_state_summary": "m1 done, m2 next",
            "next_milestone_id": "m2",
            "recommended_action": "Fix the failing tests",
            "risk_factors": [],
            "should_replan": False,
            "replan_reason": None,
        }), [])
        result = self.bot._assess_goal_state(self._make_goal())
        self.assertEqual(result["next_milestone_id"], "m2")
        self.assertFalse(result["should_replan"])

    @patch("bot.run_claude")
    def test_assessment_fallback_on_parse_failure(self, mock_claude):
        mock_claude.return_value = ("Can't produce JSON", [])
        result = self.bot._assess_goal_state(self._make_goal())
        # Should fallback to first pending milestone
        self.assertEqual(result["next_milestone_id"], "m2")

    @patch("bot.run_claude")
    def test_assessment_all_done(self, mock_claude):
        mock_claude.return_value = ("Not JSON", [])
        goal = self._make_goal()
        goal["milestones"][1]["status"] = "completed"
        result = self.bot._assess_goal_state(goal)
        self.assertIsNone(result["next_milestone_id"])


class TestExtractLearnings(unittest.TestCase):
    """Test _extract_learnings with mocked Claude calls."""

    def setUp(self):
        import bot
        self.bot = bot

    @patch("bot.run_claude")
    def test_extracts_learnings(self, mock_claude):
        mock_claude.return_value = (json.dumps([
            {"category": "technical", "insight": "Need to install deps first"},
        ]), [])
        result = self.bot._extract_learnings(
            {"cwd": "/tmp"},
            {"action": "run tests", "outcome": "failure",
             "verification": {"passed": [], "failed": []}, "error_log": "module not found"},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["category"], "technical")

    @patch("bot.run_claude")
    def test_returns_empty_on_parse_failure(self, mock_claude):
        mock_claude.return_value = ("No learnings", [])
        result = self.bot._extract_learnings(
            {"cwd": "/tmp"},
            {"action": "test", "outcome": "success",
             "verification": {"passed": [], "failed": []}, "error_log": ""},
        )
        self.assertEqual(result, [])


class TestGoalCommands(unittest.TestCase):
    """Test /goal command handling."""

    def setUp(self):
        import bot
        self.bot = bot
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

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

        # Mock send_message
        self._orig_send = bot.send_message
        bot.send_message = MagicMock()

        # Mock get_active_session
        self._orig_get_session = bot.get_active_session
        self.mock_session = {"name": "test", "cwd": "/tmp",
                             "claude_session_ids": {}, "id": "sess_1"}
        bot.get_active_session = MagicMock(return_value=self.mock_session)

        self._orig_get_session_id = bot.get_session_id
        bot.get_session_id = MagicMock(return_value="sess_1")

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GOALS_INDEX_FILE = self._orig_index_file
        self.bot.goal_active = self._orig_goal_active
        self.bot.goal_state = self._orig_goal_state
        self.bot._goal_lock = self._orig_goal_lock
        self.bot.send_message = self._orig_send
        self.bot.get_active_session = self._orig_get_session
        self.bot.get_session_id = self._orig_get_session_id
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_goal_no_args_shows_help(self):
        result = self.bot.handle_command(12345, "/goal")
        self.assertTrue(result)
        call_text = self.bot.send_message.call_args[0][1]
        self.assertIn("Goal Mode", call_text)

    def test_goal_list_empty(self):
        result = self.bot.handle_command(12345, "/goal list")
        self.assertTrue(result)
        call_text = self.bot.send_message.call_args[0][1]
        self.assertIn("No goals", call_text)

    def test_goal_status_no_goals(self):
        result = self.bot.handle_command(12345, "/goal status")
        self.assertTrue(result)
        call_text = self.bot.send_message.call_args[0][1]
        self.assertIn("No active goals", call_text)

    def test_goal_status_with_goal(self):
        goal = self.bot._create_goal("12345", "sess_1", "/tmp", "test goal")
        goal["title"] = "Test Goal"
        goal["status"] = "active"
        goal["milestones"] = [
            {"id": "m1", "title": "Step 1", "status": "completed", "order": 1},
            {"id": "m2", "title": "Step 2", "status": "in_progress", "order": 2},
        ]
        self.bot._save_goal(goal)

        result = self.bot.handle_command(12345, "/goal status")
        self.assertTrue(result)
        call_text = self.bot.send_message.call_args[0][1]
        self.assertIn("Test Goal", call_text)
        self.assertIn("1/2", call_text)

    def test_goal_plan_shows_milestones(self):
        goal = self.bot._create_goal("12345", "sess_1", "/tmp", "test")
        goal["title"] = "My Goal"
        goal["status"] = "active"
        goal["milestones"] = [
            {"id": "m1", "title": "Step A", "status": "completed", "order": 1, "attempts": 1},
            {"id": "m2", "title": "Step B", "status": "pending", "order": 2, "attempts": 0},
        ]
        self.bot._save_goal(goal)

        result = self.bot.handle_command(12345, "/goal plan")
        self.assertTrue(result)
        call_text = self.bot.send_message.call_args[0][1]
        self.assertIn("Step A", call_text)
        self.assertIn("Step B", call_text)
        self.assertIn("✅", call_text)

    def test_goal_list_shows_goals(self):
        goal = self.bot._create_goal("12345", "sess_1", "/tmp", "test")
        goal["title"] = "Listed Goal"
        goal["status"] = "active"
        goal["milestones"] = []
        self.bot._save_goal(goal)

        result = self.bot.handle_command(12345, "/goal list")
        self.assertTrue(result)
        call_text = self.bot.send_message.call_args[0][1]
        self.assertIn("Listed Goal", call_text)

    @patch("bot._decompose_goal")
    def test_goal_create_decomposes_and_shows_approval(self, mock_decompose):
        mock_decompose.return_value = ("Test Goal", [
            {"id": "m1", "title": "Step 1", "status": "pending",
             "acceptance_criteria": ["check"], "order": 1, "depends_on": [],
             "description": "", "attempts": 0, "completed_at": None},
        ])

        result = self.bot.handle_command(12345, "/goal Add a health endpoint")
        self.assertTrue(result)
        mock_decompose.assert_called_once()

        # Goal should exist on disk in planning status (awaiting approval)
        goals = self.bot._list_goals("12345")
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["title"], "Test Goal")
        self.assertEqual(goals[0]["status"], "planning")

        # Inline keyboard should have been sent
        last_call = self.bot.send_message.call_args
        self.assertIn("reply_markup", last_call[1] if last_call[1] else {})
        keyboard = last_call[1]["reply_markup"]
        self.assertIn("inline_keyboard", keyboard)
        # First row should have Approve and Cancel buttons
        buttons = keyboard["inline_keyboard"][0]
        self.assertTrue(any("goal_approve_" in b["callback_data"] for b in buttons))
        self.assertTrue(any("goal_cancel_plan_" in b["callback_data"] for b in buttons))


class TestCircuitBreakers(unittest.TestCase):
    """Test Phase 2e circuit breakers: max-total-time, stuck detection, force_replan."""

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

    def _make_goal(self, learnings=None, config_overrides=None):
        """Create a test goal and return it."""
        config = {
            "max_iterations": 50,
            "max_consecutive_failures": 5,
            "execution_mode": "justdoit",
            "auto_replan_threshold": 3,
            "max_total_time": 28800,
            "verification_commands": [],
            "pause_between_iterations": False,
            "model": "opus",
        }
        if config_overrides:
            config.update(config_overrides)
        goal = {
            "id": "goal_test123",
            "chat_id": "12345",
            "session_id": "sid1",
            "cwd": "/tmp/test",
            "title": "Test Goal",
            "description": "Do something",
            "status": "active",
            "created_at": "2026-06-19T10:00:00",
            "updated_at": "2026-06-19T10:00:00",
            "completed_at": None,
            "milestones": [
                {"id": "m1", "title": "Step 1", "status": "pending",
                 "acceptance_criteria": ["check1"], "attempts": 0},
            ],
            "iterations": [],
            "learnings": learnings or [],
            "config": config,
        }
        self.bot._save_goal(goal)
        return goal

    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot.get_session_by_id")
    def test_max_total_time_pauses_goal(self, mock_get_session, mock_ws,
                                         mock_save, mock_send):
        """Goal pauses when max_total_time is exceeded."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        goal = self._make_goal(config_overrides={"max_total_time": 1})  # 1 second

        call_count = [0]
        def fake_time():
            call_count[0] += 1
            # First few calls (setup): return 0
            # Later calls (elapsed check in loop): return 100 to exceed budget
            if call_count[0] <= 2:
                return 0
            return 100

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", side_effect=fake_time):
            self.bot._run_goal_loop(12345, "sid1", "goal_test123")

        goal = self.bot._load_goal("goal_test123")
        self.assertEqual(goal["status"], "paused")
        time_msgs = [c for c in mock_send.call_args_list
                     if "time budget" in str(c).lower()]
        self.assertTrue(len(time_msgs) > 0)

    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot.get_session_by_id")
    def test_repeated_learning_stuck_detection(self, mock_get_session, mock_ws,
                                                mock_save, mock_send):
        """Goal pauses when the same learning appears 3+ times."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}
        repeated_learnings = [
            {"insight": "Same thing keeps happening", "iteration": i, "category": "technical"}
            for i in range(3)
        ]
        goal = self._make_goal(learnings=repeated_learnings)

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", return_value=0):
            self.bot._run_goal_loop(12345, "sid1", "goal_test123")

        goal = self.bot._load_goal("goal_test123")
        self.assertEqual(goal["status"], "paused")
        stuck_msgs = [c for c in mock_send.call_args_list
                      if "stuck" in str(c).lower() or "recurring" in str(c).lower()]
        self.assertTrue(len(stuck_msgs) > 0)

    def test_default_config_includes_max_total_time(self):
        """_create_goal includes max_total_time in default config."""
        goal = self.bot._create_goal(12345, "sid1", "/tmp", "test goal")
        self.assertIn("max_total_time", goal["config"])
        self.assertEqual(goal["config"]["max_total_time"], 28800)

    @patch("bot.send_message")
    @patch("bot.save_active_tasks")
    @patch("bot._ws_broadcast_status")
    @patch("bot.get_session_by_id")
    @patch("bot._assess_goal_state")
    @patch("bot._execute_goal_action")
    @patch("bot._verify_milestone")
    @patch("bot._extract_learnings")
    @patch("bot._replan_goal")
    def test_force_replan_on_auto_replan_threshold(self, mock_replan, mock_learn,
                                                     mock_verify, mock_execute,
                                                     mock_assess, mock_get_session,
                                                     mock_ws, mock_save, mock_send):
        """After auto_replan_threshold failures on a milestone, force replan next iteration."""
        mock_get_session.return_value = {"id": "sid1", "name": "default", "cwd": "/tmp/test"}

        # Milestone with attempts already at threshold - 1 (will reach threshold after this attempt)
        goal = self._make_goal(config_overrides={"auto_replan_threshold": 2})
        goal["milestones"][0]["attempts"] = 1  # Will become 2 (= threshold) after increment
        self.bot._save_goal(goal)

        # Iteration 1: assess returns m1 -> execute -> fail -> force_replan set
        # Iteration 2: assess -> force_replan applied -> replan -> continue (back to top)
        # Iteration 3: execute the replanned milestone
        # Iteration 4: assess returns None -> done
        mock_assess.side_effect = [
            {"next_milestone_id": "m1", "recommended_action": "Fix it"},
            {"next_milestone_id": "m1", "recommended_action": "Try again"},  # Will get force_replan
            {"next_milestone_id": "m2", "recommended_action": "New step"},
            {"next_milestone_id": None},  # Done after replan
        ]
        mock_execute.return_value = "Failed to fix"
        fail_result = {
            "all_passed": False,
            "passed": [],
            "failed": [{"criterion": "check1", "evidence": "failed"}],
        }
        pass_result = {
            "all_passed": True,
            "passed": [{"criterion": "new", "evidence": "ok"}],
            "failed": [],
        }
        mock_verify.side_effect = [fail_result, pass_result]
        mock_learn.return_value = []
        mock_replan.return_value = (
            [{"id": "m2", "title": "New step", "status": "pending", "acceptance_criteria": []}],
            "Replanned"
        )

        # time.time must increment so the inter-iteration sleep loop exits
        time_counter = [0]
        def advancing_time():
            time_counter[0] += 1
            return time_counter[0]

        with patch("bot._check_pause", return_value=True), \
             patch("time.time", side_effect=advancing_time), \
             patch("time.sleep"):
            self.bot._run_goal_loop(12345, "sid1", "goal_test123")

        # Verify replan was triggered via the force_replan path
        mock_replan.assert_called_once()
        goal = self.bot._load_goal("goal_test123")
        self.assertEqual(goal["status"], "completed")


class TestGoalCheckins(unittest.TestCase):
    """Test scheduled check-in creation/cancellation for paused goals (Phase 7b)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

        import bot
        self.bot = bot
        self._orig_goals_dir = bot.GOALS_DIR
        self._orig_index_file = bot.GOALS_INDEX_FILE
        self._orig_sched_tasks = bot.scheduled_tasks
        self._orig_sched_lock = bot._scheduled_tasks_lock

        bot.GOALS_DIR = self.goals_dir
        bot.GOALS_INDEX_FILE = self.goals_dir / "index.json"
        bot.scheduled_tasks = {}
        bot._scheduled_tasks_lock = threading.Lock()

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GOALS_INDEX_FILE = self._orig_index_file
        self.bot.scheduled_tasks = self._orig_sched_tasks
        self.bot._scheduled_tasks_lock = self._orig_sched_lock
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_goal(self, checkin_schedule=None):
        goal = {
            "id": "goal_checkin1",
            "chat_id": "12345",
            "session_id": "sess1",
            "cwd": "/tmp/test",
            "title": "Test Goal",
            "description": "Test",
            "status": "paused",
            "config": {"checkin_schedule": checkin_schedule},
            "milestones": [],
            "iterations": [],
            "learnings": [],
        }
        self.bot._save_goal(goal)
        return goal

    @patch("bot.save_scheduled_tasks")
    @patch("bot._ws_broadcast_schedule")
    def test_schedule_checkin_creates_task(self, mock_ws, mock_save):
        """Scheduling a check-in creates a cron scheduled task."""
        goal = self._make_goal(checkin_schedule="0 9 * * *")
        task_id = self.bot._schedule_goal_checkin(goal)
        self.assertIsNotNone(task_id)
        self.assertIn(task_id, self.bot.scheduled_tasks)
        task = self.bot.scheduled_tasks[task_id]
        self.assertEqual(task["cron_expr"], "0 9 * * *")
        self.assertIn("goal_checkin1", task["prompt"])
        # Goal should have the task ID stored
        reloaded = self.bot._load_goal("goal_checkin1")
        self.assertEqual(reloaded["_checkin_task_id"], task_id)

    def test_schedule_checkin_no_schedule_returns_none(self):
        """If no checkin_schedule in config, no task is created."""
        goal = self._make_goal(checkin_schedule=None)
        result = self.bot._schedule_goal_checkin(goal)
        self.assertIsNone(result)
        self.assertEqual(len(self.bot.scheduled_tasks), 0)

    @patch("bot.save_scheduled_tasks")
    @patch("bot._ws_broadcast_schedule")
    def test_cancel_checkin_removes_task(self, mock_ws, mock_save):
        """Cancelling a check-in removes the scheduled task."""
        goal = self._make_goal(checkin_schedule="0 9 * * *")
        task_id = self.bot._schedule_goal_checkin(goal)
        self.assertIn(task_id, self.bot.scheduled_tasks)

        # Cancel it
        goal = self.bot._load_goal("goal_checkin1")
        self.bot._cancel_goal_checkin(goal)
        self.assertNotIn(task_id, self.bot.scheduled_tasks)
        reloaded = self.bot._load_goal("goal_checkin1")
        self.assertNotIn("_checkin_task_id", reloaded)

    def test_cancel_checkin_no_task_id_noop(self):
        """Cancelling when no check-in exists is a no-op."""
        goal = self._make_goal()
        self.bot._cancel_goal_checkin(goal)  # Should not raise


class TestGlobalLearnings(unittest.TestCase):
    """Test cross-goal learning store (Phase 8a-8c)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

        import bot
        self.bot = bot
        self._orig_goals_dir = bot.GOALS_DIR
        self._orig_global_file = bot.GLOBAL_LEARNINGS_FILE

        bot.GOALS_DIR = self.goals_dir
        bot.GLOBAL_LEARNINGS_FILE = self.goals_dir / "global_learnings.json"

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GLOBAL_LEARNINGS_FILE = self._orig_global_file
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_empty_global_learnings(self):
        """Loading when no file exists returns empty list."""
        result = self.bot._load_global_learnings()
        self.assertEqual(result, [])

    def test_save_and_load_global_learnings(self):
        """Save and reload global learnings round-trips correctly."""
        learnings = [
            {"insight": "Always set DATABASE_URL", "category": "environment",
             "tags": ["python", "postgres"], "problem_type": "deployment",
             "confirmations": 1, "pinned": False,
             "created_at": "2026-01-01", "last_confirmed": "2026-01-01",
             "source_goal_id": "g1", "source_project": "/tmp/proj"},
        ]
        self.bot._save_global_learnings(learnings)
        reloaded = self.bot._load_global_learnings()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["insight"], "Always set DATABASE_URL")

    @patch("bot.run_claude")
    def test_promote_learnings_adds_to_global(self, mock_claude):
        """Promoting learnings from a completed goal adds to global store."""
        mock_claude.return_value = (json.dumps({
            "promotable": [
                {"insight": "Use --no-cache for docker builds in CI",
                 "category": "process", "tags": ["docker", "ci"],
                 "problem_type": "deployment"}
            ]
        }), None)

        goal = {
            "id": "goal_promo1",
            "title": "Set up CI pipeline",
            "cwd": "/tmp/ci-project",
            "description": "Set up CI",
            "learnings": [
                {"category": "process", "insight": "Use --no-cache for docker builds in CI"},
                {"category": "technical", "insight": "Very specific to this goal only"},
            ],
            "config": {},
        }
        self.bot._promote_learnings(goal)
        global_l = self.bot._load_global_learnings()
        self.assertEqual(len(global_l), 1)
        self.assertEqual(global_l[0]["insight"], "Use --no-cache for docker builds in CI")
        self.assertEqual(global_l[0]["confirmations"], 1)

    @patch("bot.run_claude")
    def test_promote_learnings_deduplicates(self, mock_claude):
        """Promoting a duplicate learning bumps confirmations instead of adding."""
        # Pre-populate
        self.bot._save_global_learnings([{
            "insight": "Always run migrations before tests",
            "category": "process", "tags": [], "problem_type": "testing",
            "confirmations": 1, "pinned": False,
            "created_at": "2026-01-01", "last_confirmed": "2026-01-01",
            "source_goal_id": "g1", "source_project": "/tmp/p1",
        }])

        mock_claude.return_value = (json.dumps({
            "promotable": [
                {"insight": "Always run migrations before tests",
                 "category": "process", "tags": ["testing"],
                 "problem_type": "testing"}
            ]
        }), None)

        goal = {
            "id": "goal_promo2", "title": "Test goal", "cwd": "/tmp/p2",
            "description": "Test", "config": {},
            "learnings": [{"category": "process", "insight": "Always run migrations before tests"}],
        }
        self.bot._promote_learnings(goal)
        global_l = self.bot._load_global_learnings()
        self.assertEqual(len(global_l), 1)  # No duplicate
        self.assertEqual(global_l[0]["confirmations"], 2)  # Bumped

    @patch("bot.run_claude")
    def test_promote_learnings_empty_promotable(self, mock_claude):
        """If Claude says nothing is promotable, global store stays empty."""
        mock_claude.return_value = (json.dumps({"promotable": []}), None)
        goal = {
            "id": "g1", "title": "T", "cwd": "/tmp", "description": "D", "config": {},
            "learnings": [{"category": "technical", "insight": "very specific"}],
        }
        self.bot._promote_learnings(goal)
        self.assertEqual(self.bot._load_global_learnings(), [])

    def test_retrieve_relevant_learnings_by_project(self):
        """Learnings from the same project path score higher."""
        self.bot._save_global_learnings([
            {"insight": "A", "category": "technical", "tags": [],
             "problem_type": "general", "confirmations": 1, "pinned": False,
             "created_at": "2026-06-01", "last_confirmed": "2026-06-01",
             "source_goal_id": "g1", "source_project": "/tmp/my-project"},
            {"insight": "B", "category": "technical", "tags": [],
             "problem_type": "general", "confirmations": 1, "pinned": False,
             "created_at": "2026-06-01", "last_confirmed": "2026-06-01",
             "source_goal_id": "g2", "source_project": "/tmp/other-project"},
        ])
        results = self.bot._retrieve_relevant_learnings("/tmp/my-project", "some goal")
        self.assertTrue(len(results) >= 1)
        # Project-matching learning should be first
        self.assertEqual(results[0]["insight"], "A")

    def test_retrieve_relevant_learnings_by_tags(self):
        """Learnings with matching tags score higher."""
        self.bot._save_global_learnings([
            {"insight": "Use pytest fixtures", "category": "process",
             "tags": ["python", "testing"], "problem_type": "testing",
             "confirmations": 1, "pinned": False,
             "created_at": "2026-06-01", "last_confirmed": "2026-06-01",
             "source_goal_id": "g1", "source_project": "/tmp/other"},
            {"insight": "Docker multistage", "category": "process",
             "tags": ["docker"], "problem_type": "deployment",
             "confirmations": 1, "pinned": False,
             "created_at": "2026-06-01", "last_confirmed": "2026-06-01",
             "source_goal_id": "g2", "source_project": "/tmp/other"},
        ])
        results = self.bot._retrieve_relevant_learnings("/tmp/new", "Set up python testing framework")
        # Python/testing learning should rank higher
        self.assertTrue(any(r["insight"] == "Use pytest fixtures" for r in results))

    def test_retrieve_relevant_learnings_empty_store(self):
        """Returns empty list when no global learnings exist."""
        results = self.bot._retrieve_relevant_learnings("/tmp/proj", "anything")
        self.assertEqual(results, [])

    def test_decay_removes_old_unconfirmed(self):
        """Decay prunes learnings older than 90 days with 1 confirmation."""
        self.bot._save_global_learnings([
            {"insight": "Old stale", "category": "technical", "tags": [],
             "problem_type": "general", "confirmations": 1, "pinned": False,
             "created_at": "2025-01-01", "last_confirmed": "2025-01-01",
             "source_goal_id": "g1", "source_project": "/tmp"},
            {"insight": "Recent fresh", "category": "technical", "tags": [],
             "problem_type": "general", "confirmations": 1, "pinned": False,
             "created_at": "2026-06-15", "last_confirmed": "2026-06-15",
             "source_goal_id": "g2", "source_project": "/tmp"},
        ])
        pruned = self.bot._decay_global_learnings()
        self.assertEqual(pruned, 1)
        remaining = self.bot._load_global_learnings()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["insight"], "Recent fresh")

    def test_decay_keeps_pinned(self):
        """Decay does not remove pinned learnings even if old."""
        self.bot._save_global_learnings([
            {"insight": "Important pinned", "category": "technical", "tags": [],
             "problem_type": "general", "confirmations": 1, "pinned": True,
             "created_at": "2024-01-01", "last_confirmed": "2024-01-01",
             "source_goal_id": "g1", "source_project": "/tmp"},
        ])
        pruned = self.bot._decay_global_learnings()
        self.assertEqual(pruned, 0)
        remaining = self.bot._load_global_learnings()
        self.assertEqual(len(remaining), 1)

    def test_decay_keeps_well_confirmed(self):
        """Decay keeps old learnings with multiple confirmations."""
        self.bot._save_global_learnings([
            {"insight": "Well confirmed", "category": "technical", "tags": [],
             "problem_type": "general", "confirmations": 3, "pinned": False,
             "created_at": "2025-01-01", "last_confirmed": "2025-01-01",
             "source_goal_id": "g1", "source_project": "/tmp"},
        ])
        pruned = self.bot._decay_global_learnings()
        self.assertEqual(pruned, 0)
        remaining = self.bot._load_global_learnings()
        self.assertEqual(len(remaining), 1)

    def test_decay_empty_store_noop(self):
        """Decay on empty store returns 0 and doesn't crash."""
        pruned = self.bot._decay_global_learnings()
        self.assertEqual(pruned, 0)


class TestGlobalLearningsIntegration(unittest.TestCase):
    """Test that global learnings are injected into decomposition and assessment prompts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.goals_dir = Path(self.tmpdir) / "goals"
        self.goals_dir.mkdir()

        import bot
        self.bot = bot
        self._orig_goals_dir = bot.GOALS_DIR
        self._orig_global_file = bot.GLOBAL_LEARNINGS_FILE

        bot.GOALS_DIR = self.goals_dir
        bot.GLOBAL_LEARNINGS_FILE = self.goals_dir / "global_learnings.json"

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GLOBAL_LEARNINGS_FILE = self._orig_global_file
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("bot.run_claude")
    def test_decompose_includes_global_learnings(self, mock_claude):
        """Decomposition prompt includes relevant global learnings."""
        self.bot._save_global_learnings([
            {"insight": "Always check for null pointers", "category": "technical",
             "tags": ["python"], "problem_type": "debugging",
             "confirmations": 2, "pinned": False,
             "created_at": "2026-06-01", "last_confirmed": "2026-06-01",
             "source_goal_id": "g1", "source_project": "/tmp/proj"},
        ])

        mock_claude.return_value = (json.dumps({
            "title": "Test Goal",
            "milestones": [
                {"id": "m1", "title": "Step 1", "description": "Do step 1",
                 "acceptance_criteria": ["works"], "order": 1, "depends_on": []}
            ]
        }), None)

        title, milestones = self.bot._decompose_goal("Fix python bugs", "/tmp/proj")
        # Verify global learnings were in the prompt
        call_args = mock_claude.call_args
        prompt = call_args[0][0]
        self.assertIn("LEARNINGS FROM PAST GOALS", prompt)
        self.assertIn("null pointers", prompt)

    @patch("bot.run_claude")
    def test_assess_includes_global_learnings(self, mock_claude):
        """Assessment prompt includes relevant global learnings."""
        self.bot._save_global_learnings([
            {"insight": "CI tests need DATABASE_URL", "category": "environment",
             "tags": ["testing", "postgres"], "problem_type": "testing",
             "confirmations": 3, "pinned": False,
             "created_at": "2026-06-01", "last_confirmed": "2026-06-01",
             "source_goal_id": "g1", "source_project": "/tmp/proj"},
        ])

        mock_claude.return_value = (json.dumps({
            "current_state_summary": "ok",
            "next_milestone_id": "m1",
            "recommended_action": "do it",
            "risk_factors": [],
            "should_replan": False,
            "replan_reason": None,
        }), None)

        goal = {
            "description": "Set up testing pipeline",
            "cwd": "/tmp/proj",
            "milestones": [{"id": "m1", "title": "M1", "status": "pending",
                           "attempts": 0, "order": 1}],
            "iterations": [],
            "learnings": [],
            "config": {"auto_replan_threshold": 3},
        }
        self.bot._assess_goal_state(goal)
        call_args = mock_claude.call_args
        prompt = call_args[0][0]
        self.assertIn("LEARNINGS FROM PAST GOALS", prompt)
        self.assertIn("DATABASE_URL", prompt)


class TestExecutionModeDispatch(unittest.TestCase):
    """Test that _execute_goal_action honors goal.config.execution_mode (Phase 2c)."""

    def setUp(self):
        import bot
        self.bot = bot

    def _make_goal(self, execution_mode="claude-only"):
        return {
            "id": "goal_exec1",
            "title": "Test Goal",
            "description": "Test",
            "cwd": "/tmp/test",
            "config": {"execution_mode": execution_mode},
            "milestones": [],
            "iterations": [],
            "learnings": [],
        }

    def _make_milestone(self):
        return {"id": "m1", "title": "Step 1", "description": "Do it",
                "acceptance_criteria": ["works"], "status": "in_progress", "attempts": 1}

    def _make_code_milestone(self):
        return {
            "id": "m1",
            "title": "Add regression tests",
            "description": "Implement code changes and run npm test",
            "acceptance_criteria": ["unit tests pass", "bug is fixed"],
            "status": "in_progress",
            "attempts": 1,
        }

    def _make_session(self):
        return {"name": "test", "cwd": "/tmp/test", "history": [],
                "claude_session_id": None, "id": "sess_test"}

    @patch("bot.update_claude_session_id")
    @patch("bot.run_claude_streaming", return_value=("done", [], None, "csid1", None))
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_claude_only_mode(self, mock_gsid, mock_fb, mock_stream, mock_update):
        """claude-only mode uses run_claude_streaming."""
        goal = self._make_goal("claude-only")
        result = self.bot._execute_goal_action(goal, self._make_milestone(), "do it", 123, self._make_session())
        self.assertEqual(result, "done")
        mock_stream.assert_called_once()

    @patch("bot.update_claude_session_id")
    @patch("bot.run_claude_streaming", return_value=("done", [], None, "csid1", None))
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_justdoit_mode_uses_claude(self, mock_gsid, mock_fb, mock_stream, mock_update):
        """justdoit mode also uses run_claude_streaming (goal mode IS the iterating pattern)."""
        goal = self._make_goal("justdoit")
        result = self.bot._execute_goal_action(goal, self._make_milestone(), "do it", 123, self._make_session())
        self.assertEqual(result, "done")
        mock_stream.assert_called_once()

    @patch("bot.update_claude_session_id")
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_executor_declares_scoped_verification(self, mock_gsid, mock_fb, mock_update):
        """The executor's VERIFY: lines become the milestone's scoped verification commands,
        replacing coarse decompose-time guesses; unsafe declarations are dropped."""
        resp = ("Implemented and ran the scoped test.\n"
                "VERIFY: npm --prefix relay-server test -- src/alerts.test.ts\n"
                "VERIFY: flutter test test/alerts_test.dart\n"
                "VERIFY: rm -rf /\n")
        milestone = self._make_milestone()
        milestone["verification_commands"] = ["python3 -m pytest -q"]  # stale whole-suite guess
        with patch("bot.run_claude_streaming", return_value=(resp, [], None, "csid1", None)):
            self.bot._execute_goal_action(self._make_goal("claude-only"), milestone, "do it", 123, self._make_session())
        self.assertEqual(milestone["verification_commands"],
                         ["npm --prefix relay-server test -- src/alerts.test.ts",
                          "flutter test test/alerts_test.dart"])  # rm -rf dropped, whole-suite replaced

    @patch("bot.update_claude_session_id")
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_executor_verify_none_clears_commands(self, mock_gsid, mock_fb, mock_update):
        """`VERIFY: none` clears stale whole-suite commands for docs-only milestones."""
        milestone = self._make_milestone()
        milestone["verification_commands"] = ["python3 -m pytest -q"]
        with patch("bot.run_claude_streaming", return_value=("Docs written.\nVERIFY: none", [], None, "c", None)):
            self.bot._execute_goal_action(self._make_goal("claude-only"), milestone, "do it", 123, self._make_session())
        self.assertEqual(milestone["verification_commands"], [])

    @patch("bot.run_codex", return_value="codex output")
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_omni_mode_uses_codex(self, mock_gsid, mock_fb, mock_codex):
        """omni mode delegates to run_codex."""
        goal = self._make_goal("omni")
        result = self.bot._execute_goal_action(goal, self._make_milestone(), "do it", 123, self._make_session())
        self.assertEqual(result, "codex output")
        mock_codex.assert_called_once()
        self.assertEqual(mock_codex.call_args.kwargs.get("process_key"), "sess_test")

    @patch("bot.run_codex", side_effect=["codex output", "review output"])
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_codex_reviewed_mode_runs_fresh_review(self, mock_gsid, mock_fb, mock_codex):
        """codex_reviewed executes with Codex, then runs a fresh Codex review pass."""
        goal = self._make_goal("codex_reviewed")
        result = self.bot._execute_goal_action(goal, self._make_code_milestone(), "implement tests", 123, self._make_session())

        self.assertIn("codex output", result)
        self.assertIn("FRESH CODEX REVIEW", result)
        self.assertIn("review output", result)
        self.assertEqual(mock_codex.call_count, 2)
        self.assertIsNone(mock_codex.call_args_list[1].kwargs.get("session"))
        self.assertEqual(goal["last_execution_strategy"]["effective_mode"], "codex_reviewed")

    @patch("bot.run_codex", side_effect=["codex output", "review output"])
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_auto_code_milestone_uses_codex_reviewed(self, mock_gsid, mock_fb, mock_codex):
        """auto routes code/test milestones to Codex execution plus fresh review."""
        goal = self._make_goal("auto")
        result = self.bot._execute_goal_action(goal, self._make_code_milestone(), "fix the validator bug", 123, self._make_session())

        self.assertIn("review output", result)
        self.assertEqual(mock_codex.call_count, 2)
        self.assertEqual(goal["last_execution_strategy"]["executor"], "codex")
        self.assertEqual(goal["last_execution_strategy"]["reviewer"], "codex")

    @patch("bot.update_claude_session_id")
    @patch("bot.run_claude_streaming", return_value=("claude output", [], None, None, None))
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_auto_non_code_milestone_uses_claude(self, mock_gsid, mock_fb, mock_stream, mock_update):
        """auto routes non-code milestones to Claude execution."""
        goal = self._make_goal("auto")
        milestone = {"id": "m1", "title": "Draft launch notes", "description": "Write a concise summary",
                     "acceptance_criteria": ["summary exists"], "status": "in_progress", "attempts": 1}

        result = self.bot._execute_goal_action(goal, milestone, "write the summary", 123, self._make_session())

        self.assertEqual(result, "claude output")
        mock_stream.assert_called_once()
        self.assertEqual(goal["last_execution_strategy"]["executor"], "claude")

    @patch("bot.update_claude_session_id")
    @patch("bot.run_claude_streaming", return_value=("done", [], None, None, None))
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_goal_model_passed_to_claude_execution(self, mock_gsid, mock_fb, mock_stream, mock_update):
        """goal.config.model is used for Claude-backed execution."""
        goal = self._make_goal("claude-only")
        goal["config"]["model"] = "haiku"
        self.bot._execute_goal_action(goal, self._make_milestone(), "do it", 123, self._make_session())
        self.assertEqual(mock_stream.call_args.kwargs.get("model"), "haiku")

    @patch("bot.update_claude_session_id")
    @patch("bot.run_claude_streaming", return_value=("default", [], None, None, None))
    @patch("bot.drain_user_feedback", return_value="")
    @patch("bot.get_session_id", return_value="sess_test")
    def test_missing_execution_mode_defaults_to_auto(self, mock_gsid, mock_fb, mock_stream, mock_update):
        """Missing execution_mode defaults to auto, which uses Claude for non-code work."""
        goal = self._make_goal()
        goal["config"].pop("execution_mode", None)
        result = self.bot._execute_goal_action(goal, self._make_milestone(), "do it", 123, self._make_session())
        mock_stream.assert_called_once()


class TestGoalRateLimitHandling(unittest.TestCase):
    """Test Goal Mode rate-limit parsing and fallback behavior."""

    def setUp(self):
        import bot
        self.bot = bot

    def test_parse_reset_wait_duration_minutes(self):
        wait_seconds, reset_hint = self.bot._parse_reset_wait(
            "429 Too Many Requests. Try again in 7 minutes."
        )

        self.assertEqual(wait_seconds, 7 * 60)
        self.assertIn("7 minutes", reset_hint)

    def test_parse_reset_wait_retry_after_seconds_minimum(self):
        wait_seconds, reset_hint = self.bot._parse_reset_wait(
            "Retry-After: 42"
        )

        self.assertEqual(wait_seconds, 60)
        self.assertIn("42", reset_hint)

    def test_unknown_rate_limit_uses_goal_configured_wait(self):
        with self.assertRaises(self.bot.GoalRateLimitError) as ctx:
            self.bot._goal_detect_model_issue(
                "Error: rate limit exceeded.",
                context="goal test",
                goal_or_config={"rate_limit_max_wait": 180},
            )

        self.assertEqual(ctx.exception.wait_seconds, 180)
        self.assertIsNone(ctx.exception.reset_time)

    def test_hit_limit_resets_time_is_rate_limit(self):
        message = "You've hit your limit · resets 8:40pm (Asia/Taipei)"

        self.assertTrue(self.bot.QUOTA_REGEX.search(message))
        wait_seconds, reset_hint = self.bot._parse_reset_wait(message)

        self.assertGreaterEqual(wait_seconds, 60)
        self.assertIn("8:40pm", reset_hint)

    def test_model_output_infra_words_not_treated_as_transient(self):
        """Model OUTPUT is content, not a provider error: infra phrases like '503 Service
        Unavailable' / 'connection refused' in the model's own text must NOT raise
        GoalTransientError (this false-fired 3× and aborted decomposition). Genuine model-call
        transients are surfaced by rate-limit/timeout detection or raised exceptions instead."""
        for content in (
            "Error running Claude: 503 Service Unavailable, try again later.",
            "m3: Replace the placeholder endpoint that returns 503 with a real handler.",
            "Handle connection refused and internal server error states gracefully.",
        ):
            # Must not raise.
            self.bot._goal_detect_model_issue(content, context="goal test")

    def test_rate_limit_and_timeout_still_detected(self):
        """Specific, valuable signatures are still caught on model output."""
        with self.assertRaises(self.bot.GoalRateLimitError):
            self.bot._goal_detect_model_issue("You've hit your limit · resets 8:40pm", context="x")
        with self.assertRaises(self.bot.GoalModelTimeoutError):
            self.bot._goal_detect_model_issue("Claude process timed out after 900s", context="x")

    def test_model_content_describing_infra_is_not_transient(self):
        """Decomposition/assessment text that DESCRIBES infra work must not be
        mistaken for a provider failure (regression: broad infra phrases in model
        content falsely aborted goal creation)."""
        decomposition = json.dumps({
            "title": "Support all tab-1 scenarios with real-data verification",
            "milestones": [
                {"id": "m1", "title": "Verify diet_risk against real RDS",
                 "acceptance_criteria": [
                     "Handle the case where the connection times out gracefully",
                     "Retry when the operation timed out or the database is starting up",
                     "If we could not connect to server, log and retry",
                 ]},
            ],
        })
        # Should NOT raise — this is legitimate model content, not a provider error.
        self.bot._goal_detect_model_issue(decomposition, context="goal decomposition")

    def test_verification_command_output_infra_phrases_are_transient(self):
        """The broad command-output classifier DOES flag SSH/DB infra phrases."""
        self.assertTrue(self.bot._goal_is_transient_text(
            "ssh: connect to host db-prod port 22: Connection timed out"))
        self.assertTrue(self.bot._goal_is_transient_text(
            "psql: could not connect to server: Connection refused"))
        # Plain prose without infra failure signatures is not transient
        self.assertFalse(self.bot._goal_is_transient_text(
            "All acceptance criteria satisfied; tests passed."))


class TestExplicitPauseResumeCancel(unittest.TestCase):
    """Test explicit /goal pause, resume, cancel handlers persist state (Phase 3d)."""

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
        self._orig_justdoit_active = bot.justdoit_active
        self._orig_active_processes = bot.active_processes
        self._orig_cancelled_sessions = bot.cancelled_sessions

        bot.GOALS_DIR = self.goals_dir
        bot.GOALS_INDEX_FILE = self.goals_dir / "index.json"
        bot.goal_active = {}
        bot.goal_state = {}
        bot._goal_lock = threading.Lock()
        bot.justdoit_active = {}
        bot.active_processes = {}
        bot.cancelled_sessions = set()

    def tearDown(self):
        self.bot.GOALS_DIR = self._orig_goals_dir
        self.bot.GOALS_INDEX_FILE = self._orig_index_file
        self.bot.goal_active = self._orig_goal_active
        self.bot.goal_state = self._orig_goal_state
        self.bot._goal_lock = self._orig_goal_lock
        self.bot.justdoit_active = self._orig_justdoit_active
        self.bot.active_processes = self._orig_active_processes
        self.bot.cancelled_sessions = self._orig_cancelled_sessions
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_running_goal(self, goal_id="goal_prc1"):
        """Create a running goal with active state."""
        goal = {
            "id": goal_id,
            "chat_id": "12345",
            "session_id": "sess1",
            "cwd": "/tmp/test",
            "title": "Test Goal",
            "description": "Test",
            "status": "active",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "milestones": [],
            "iterations": [],
            "learnings": [],
            "config": {"checkin_schedule": None},
        }
        self.bot._save_goal(goal)
        chat_key = "12345:sess1"
        self.bot.goal_active[chat_key] = goal_id
        self.bot.goal_state[chat_key] = {
            "active": True,
            "paused": False,
            "resume_event": threading.Event(),
            "goal_id": goal_id,
            "task": "Test Goal",
            "phase": "goal",
            "step": 0,
            "chat_id": "12345",
            "session_name": "test",
        }
        self.bot.goal_state[chat_key]["resume_event"].set()
        return goal, chat_key

    @patch("bot._schedule_goal_checkin")
    @patch("bot._ws_broadcast_goal")
    @patch("bot.save_active_tasks")
    @patch("bot.send_message")
    def test_explicit_pause_persists_status(self, mock_msg, mock_save, mock_ws, mock_checkin):
        """Explicit /goal pause persists goal.status = 'paused' to disk."""
        goal, chat_key = self._setup_running_goal()

        # Simulate /goal pause
        state = self.bot.goal_state[chat_key]
        active_goal_id = self.bot.goal_active[chat_key]
        state["paused"] = True
        state.get("resume_event").clear()
        # This is what the handler now does:
        loaded = self.bot._load_goal(active_goal_id)
        loaded["status"] = "paused"
        self.bot._save_goal(loaded)

        # Verify disk state
        reloaded = self.bot._load_goal("goal_prc1")
        self.assertEqual(reloaded["status"], "paused")

    @patch("bot._ws_broadcast_goal")
    @patch("bot.save_active_tasks")
    @patch("bot.send_message")
    def test_explicit_pause_emits_ws_event(self, mock_msg, mock_save, mock_ws):
        """Explicit /goal pause emits a WS 'paused' event."""
        goal, chat_key = self._setup_running_goal()

        # Call the actual handler by simulating the command
        with patch("bot._schedule_goal_checkin"):
            with patch("bot.get_active_session", return_value={"name": "test", "id": "sess1"}):
                with patch("bot.get_session_id", return_value="sess1"):
                    self.bot.handle_command(12345, "/goal pause")

        mock_ws.assert_called_once()
        args = mock_ws.call_args
        self.assertEqual(args[0][1], "paused")  # event name

    @patch("bot._cancel_goal_checkin")
    @patch("bot.send_message")
    def test_in_memory_resume_cancels_checkin(self, mock_msg, mock_checkin):
        """In-memory resume cancels the check-in task."""
        goal, chat_key = self._setup_running_goal()
        # Pause first
        self.bot.goal_state[chat_key]["paused"] = True
        self.bot.goal_state[chat_key]["resume_event"].clear()
        goal_on_disk = self.bot._load_goal("goal_prc1")
        goal_on_disk["status"] = "paused"
        self.bot._save_goal(goal_on_disk)

        # Resume via handler
        with patch("bot.get_active_session", return_value={"name": "test", "id": "sess1"}):
            with patch("bot.get_session_id", return_value="sess1"):
                self.bot.handle_command(12345, "/goal resume")

        mock_checkin.assert_called_once()
        reloaded = self.bot._load_goal("goal_prc1")
        self.assertEqual(reloaded["status"], "active")

    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot._cancel_goal_checkin")
    @patch("bot.save_active_tasks")
    @patch("bot.send_message")
    def test_explicit_cancel_persists_abandoned(self, mock_msg, mock_save, mock_checkin, mock_ws_goal, mock_ws_status):
        """Explicit /goal cancel persists goal.status = 'abandoned'."""
        goal, chat_key = self._setup_running_goal()

        with patch("bot.get_active_session", return_value={"name": "test", "id": "sess1"}):
            with patch("bot.get_session_id", return_value="sess1"):
                self.bot.handle_command(12345, "/goal cancel")

        reloaded = self.bot._load_goal("goal_prc1")
        self.assertEqual(reloaded["status"], "abandoned")
        mock_ws_goal.assert_called()
        mock_checkin.assert_called_once()

    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot._cancel_goal_checkin")
    @patch("bot.save_active_tasks")
    @patch("bot.send_message")
    def test_explicit_cancel_kills_subprocess(self, mock_msg, mock_save, mock_checkin, mock_ws_goal, mock_ws_status):
        """Explicit /goal cancel kills the active Claude subprocess."""
        goal, chat_key = self._setup_running_goal()

        # Set up a mock subprocess
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = MagicMock()
        self.bot.active_processes["sess1"] = mock_proc

        with patch("os.getpgid", return_value=12345), \
             patch("os.killpg") as mock_killpg, \
             patch("bot.get_active_session", return_value={"name": "test", "id": "sess1"}), \
             patch("bot.get_session_id", return_value="sess1"):
            self.bot.handle_command(12345, "/goal cancel")

        mock_killpg.assert_called_once()
        self.assertNotIn("sess1", self.bot.active_processes)
        self.assertIn("sess1", self.bot.cancelled_sessions)

    @patch("bot._ws_broadcast_status")
    @patch("bot._ws_broadcast_goal")
    @patch("bot._cancel_goal_checkin")
    @patch("bot.save_active_tasks")
    @patch("bot.send_message")
    def test_omni_goal_cancel_kills_codex_subprocess(self, mock_msg, mock_save,
                                                      mock_checkin, mock_ws_goal,
                                                      mock_ws_status):
        """Cancel of an omni-mode goal kills the Codex subprocess registered via process_key."""
        goal, chat_key = self._setup_running_goal()
        # Mark goal as omni execution mode
        goal["config"] = {"execution_mode": "omni"}
        self.bot._save_goal(goal)

        # Simulate Codex subprocess registered by run_codex(process_key="sess1")
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.stdout = MagicMock()
        self.bot.active_processes["sess1"] = mock_proc

        with patch("os.getpgid", return_value=99999), \
             patch("os.killpg") as mock_killpg, \
             patch("bot.get_active_session", return_value={"name": "test", "id": "sess1"}), \
             patch("bot.get_session_id", return_value="sess1"):
            self.bot.handle_command(12345, "/goal cancel")

        # Verify Codex subprocess was killed and cleaned up
        mock_killpg.assert_called_once_with(99999, ANY)
        self.assertNotIn("sess1", self.bot.active_processes)
        self.assertIn("sess1", self.bot.cancelled_sessions)
        # Verify goal persisted as abandoned
        reloaded = self.bot._load_goal("goal_prc1")
        self.assertEqual(reloaded["status"], "abandoned")


if __name__ == "__main__":
    unittest.main()
