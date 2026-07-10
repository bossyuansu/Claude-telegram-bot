"""Tests for deepreview clean-verdict gates."""
import unittest
from unittest.mock import MagicMock, patch

import bot


class TestDeepreviewCleanSignals(unittest.TestCase):
    def test_clean_signal_must_be_a_verdict_line(self):
        self.assertTrue(bot._deepreview_has_clean_signal("notes\nALL_CLEAN\nverified", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("**ALL_CLEAN** \u2014 verified", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("**ALL_CLEAN.** 55 analytics-worker tests passed", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("`ALL_CLEAN`: verified", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("All clean. Verified files and tests.", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("Verdict: **ALL CLEAN** - verified", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("## ALL-CLEAN: verified", "ALL_CLEAN"))
        self.assertTrue(bot._deepreview_has_clean_signal("Result: CLEAN.", "CLEAN"))
        self.assertFalse(bot._deepreview_has_clean_signal("I will not say ALL_CLEAN yet", "ALL_CLEAN"))
        self.assertFalse(bot._deepreview_has_clean_signal("ALL_CLEAN-ish", "ALL_CLEAN"))
        self.assertFalse(bot._deepreview_has_clean_signal("This is all clean now", "ALL_CLEAN"))
        self.assertFalse(bot._deepreview_has_clean_signal("ALL_CLEAN", "CLEAN"))

    def test_first_clean_iteration_is_not_accepted(self):
        self.assertFalse(bot._deepreview_can_accept_clean(1))
        self.assertTrue(bot._deepreview_can_accept_clean(bot.DEEPREVIEW_MIN_CLEAN_ITERATIONS))


class TestCodexStderrClassification(unittest.TestCase):
    def test_non_quota_tool_error_on_success_is_ignored(self):
        stderr = "ERROR: file or directory not found: cloud-analytics-worker/tests/test_conversation_analyzer.py"
        self.assertIsNone(bot._codex_stderr_reason(stderr, 0))

    def test_non_quota_error_on_failure_is_not_quota(self):
        stderr = "ERROR: file or directory not found: cloud-analytics-worker/tests/test_conversation_analyzer.py"
        reason = bot._codex_stderr_reason(stderr, 1)
        self.assertTrue(reason.startswith("Codex error"))
        self.assertIn(stderr, reason)

    def test_quota_error_triggers_wait(self):
        reason = bot._codex_stderr_reason("ERROR: rate limited. Try again later.", 0)
        self.assertTrue(reason.startswith("QUOTA:60 Codex error"))


class TestCodexCleanVerification(unittest.TestCase):
    @patch("bot.subprocess.Popen")
    def test_clean_verification_uses_read_only_sandbox(self, mock_popen):
        process = MagicMock()
        process.communicate.return_value = ("ALL_CLEAN", "")
        process.returncode = 0
        mock_popen.return_value = process

        output, is_clean, reasoning = bot.run_codex_deepreview_clean_verification(
            "history", 7, "/tmp", claude_feedback="ALL_CLEAN"
        )

        self.assertEqual(output, "ALL_CLEAN")
        self.assertTrue(is_clean)
        self.assertEqual(reasoning, "No issues found")

        cmd = mock_popen.call_args.args[0]
        self.assertEqual(cmd[:4], ["codex", "-a", "never", "exec"])
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)


class TestCodexCompactionContext(unittest.TestCase):
    def test_compacted_continuation_prompt_includes_summary_last_response_and_task(self):
        prompt = bot.build_compacted_continuation_prompt(
            "summary facts",
            "continue",
            "Codex",
            "last answer with the immediate next step",
        )

        self.assertIn("summary facts", prompt)
        self.assertIn("last answer with the immediate next step", prompt)
        self.assertIn("[New task:]\ncontinue", prompt)

    @patch("bot.reset_message_count")
    @patch("bot.update_cli_session_id")
    @patch("bot.save_session_summary")
    @patch("bot.send_message")
    @patch("bot.run_codex", return_value="summary with last answer and enough detail to pass the minimum persistence length check")
    def test_codex_compaction_summary_prompt_includes_latest_response(
        self,
        mock_run_codex,
        mock_send,
        mock_save_summary,
        mock_update_sid,
        mock_reset_count,
    ):
        session = {
            "id": "sid1",
            "cwd": "/tmp",
            "last_responses": {"codex": "LATEST FINAL RESPONSE"},
        }

        summary = bot.perform_proactive_compaction(123, session, "Codex")

        self.assertIn("summary with last answer", summary)
        prompt = mock_run_codex.call_args.args[0]
        self.assertIn("LATEST CODEX RESPONSE BEFORE COMPACTION", prompt)
        self.assertIn("LATEST FINAL RESPONSE", prompt)


class TestDeepreviewLoopGates(unittest.TestCase):
    @patch("bot.time.sleep", return_value=None)
    @patch("bot._ws_broadcast_status")
    @patch("bot.save_active_tasks")
    @patch("bot.update_claude_session_id")
    @patch("bot.get_session_by_id")
    @patch("bot.increment_message_count", return_value=False)
    @patch("bot._check_pause", return_value=True)
    @patch("bot.run_codex_deepreview_clean_verification")
    @patch("bot.run_codex_deepreview_fix")
    @patch("bot.run_codex_deepreview")
    @patch("bot.run_claude_streaming")
    @patch("bot.send_message")
    @patch("bot.get_session_id", return_value="sid1")
    def test_deepreview_requires_second_clean_iteration(
        self,
        mock_get_session_id,
        mock_send,
        mock_claude,
        mock_codex_review,
        mock_codex_fix,
        mock_codex_verify,
        mock_check_pause,
        mock_increment,
        mock_get_session_by_id,
        mock_update_sid,
        mock_save_tasks,
        mock_ws_status,
        mock_sleep,
    ):
        session = {"id": "sid1", "name": "test-session", "cwd": "/tmp"}
        mock_get_session_by_id.return_value = session
        mock_claude.side_effect = [
            ("Claude first pass", [], None, "claude-1", False),
            ("Claude second pass", [], None, "claude-2", False),
            ("ALL_CLEAN\nVerified first pass", [], None, "claude-3", False),
            ("ALL_CLEAN\nVerified second pass", [], None, "claude-4", False),
        ]
        mock_codex_review.side_effect = [
            (None, True, "No issues found"),
            (None, True, "No issues found"),
        ]
        mock_codex_fix.side_effect = [
            ("ALL_CLEAN", True, "No issues found"),
        ]
        mock_codex_verify.return_value = ("ALL_CLEAN", True, "No issues found")

        bot.run_deepreview_loop(123, session)

        self.assertEqual(mock_codex_review.call_count, 2)
        self.assertEqual(mock_codex_fix.call_count, 1)
        self.assertEqual(mock_codex_verify.call_count, 1)

        sent = "\n".join(str(call.args[1]) for call in mock_send.call_args_list)
        self.assertIn("Codex reported CLEAN at iteration 1", sent)
        self.assertIn("Claude reported ALL_CLEAN at iteration 1", sent)
        self.assertIn("Codex is satisfied with Claude's work after 2 iterations", sent)
        self.assertIn("Codex independently verified Claude's ALL_CLEAN verdict after 2 iterations", sent)
        self.assertNotIn("Claude is satisfied with Codex's work after 1 iterations", sent)


if __name__ == "__main__":
    unittest.main()
