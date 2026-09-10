"""JustDoIt must start from the discussion that preceded it.

Reported 2026-09-10: `/go` → `/justdoit` on life-companion (3) planned as if the prior Codex
discussion had never happened. The log shows why — the bridge landed on the wrong turn:

  20:16:48  [Claude] Context bridge injected (3096 chars)
  20:17:05  [Claude] No context bridge ...      <- /go's plan-relevance check
  20:17:18  [Claude] No context bridge ...      <- JustDoIt Step 0
  20:17:24  [Claude] No context bridge ...      <- JustDoIt Step 1

get_context_bridge() only reports activity SINCE the last Claude call, so the first Claude turn
consumes it. /go's own relevance check is a Claude call, which makes that near-certain.

Second, independent gap: Step 0 runs on CLAUDE_PLANNING_MODEL and the implementation steps on the
general model, each with its own per-model Claude session. The implementation step never witnessed
the planning conversation, so the plan FILE is the only handoff — and it was not told to read it.
"""
import sys
import unittest

sys.argv = ["bot.py"]
import bot


class TestContextBridgeIsConsumed(unittest.TestCase):
    """Pins the flaw that makes the explicit injection necessary."""

    def test_bridge_is_empty_once_claude_has_run(self):
        session = {"cwd": "/tmp/proj", "activity_log": [
            {"cli": "Codex", "time": "2026-09-10T20:13:11"},
            {"cli": "Claude", "time": "2026-09-10T20:16:48"},   # consumes it
        ]}
        self.assertFalse(bot.get_context_bridge(session, "Claude").strip(),
                         "a preceding Claude turn spends the bridge — this is the bug")

    def test_session_context_survives_an_intervening_claude_turn(self):
        """_goal_session_context is order-independent, which is why the loop uses it instead."""
        session = {"cwd": "/tmp/proj", "activity_log": [
            {"cli": "Codex", "time": "2026-09-10T20:13:11"},
            {"cli": "Claude", "time": "2026-09-10T20:16:48"},
        ]}
        ctx = bot._goal_session_context(session)
        self.assertIn("Codex", ctx)
        self.assertIn("SESSION CONTEXT", ctx)

    def test_no_other_cli_means_no_context_block(self):
        """Claude-only sessions must not get a pointless header."""
        session = {"cwd": "/tmp/proj", "activity_log": [
            {"cli": "Claude", "time": "2026-09-10T20:16:48"},
        ]}
        self.assertEqual(bot._goal_session_context(session), "")

    def test_empty_activity_log_is_safe(self):
        self.assertEqual(bot._goal_session_context({"cwd": "/tmp/p", "activity_log": []}), "")


class TestJustDoItPromptWiring(unittest.TestCase):
    """The prompts are built inline in run_justdoit_loop, so assert on its source."""

    @classmethod
    def setUpClass(cls):
        import inspect
        cls.src = inspect.getsource(bot.run_justdoit_loop)

    def test_step0_injects_prior_cli_context(self):
        self.assertIn("_goal_session_context(session)", self.src,
                      "Step 0 must pull prior-CLI context independently of bridge ordering")
        self.assertIn("prior_cli_context", self.src)

    def test_step0_tells_claude_to_read_the_other_cli_log(self):
        self.assertIn("READ its session log first", self.src,
                      "naming the log is useless unless it is told to read it")

    def test_implementation_step_is_told_to_read_the_plan_file(self):
        """Step 1 runs on a different model/session than Step 0 — the file is the only handoff."""
        self.assertIn("FIRST: read", self.src)
        self.assertIn("you did not take part in", self.src,
                      "the prompt should say why the plan is not already in context")

    def test_planning_and_implementation_still_use_different_models(self):
        """Not a bug — a cheap planner and a strong implementer is deliberate. Pinned so the
        plan-file handoff is not quietly removed on the assumption they share a session."""
        self.assertIn("model=CLAUDE_PLANNING_MODEL", self.src)


if __name__ == "__main__":
    unittest.main()
