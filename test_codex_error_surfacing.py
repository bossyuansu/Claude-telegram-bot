"""A failed Codex turn must say why, not return an empty message.

2026-09-09 23:24: a `/codex` turn was blocked server-side —
  task_complete.error = {"message": "This request was blocked by our safety systems.
                         Reason: Potentially unintended activity.",
                         "codex_error_info": "misalignment_policy_violation"}
  last_agent_message  = null
The user saw a blank reply. run_codex derived failures only from STDERR
(_codex_stderr_reason), but this class of error arrives solely on the --json event stream as
`turn.failed` / `error`, so nothing was logged and nothing was shown.
"""
import json
import subprocess
import sys
import unittest
from unittest import mock

sys.argv = ["bot.py"]
import bot


class TestCodexErrorText(unittest.TestCase):

    def test_safety_block_payload_is_readable(self):
        """The exact payload from the 23:24 incident."""
        text = bot._codex_error_text({
            "message": "This request was blocked by our safety systems. "
                       "Reason: Potentially unintended activity.",
            "codex_error_info": "misalignment_policy_violation",
        })
        self.assertIn("blocked by our safety systems", text)
        self.assertIn("misalignment_policy_violation", text,
                      "the refusal class names the problem — keep it")

    def test_nested_json_envelope_is_unwrapped(self):
        """turn.failed.message is often an escaped JSON envelope; show the reason, not the JSON."""
        text = bot._codex_error_text({"message": json.dumps({
            "type": "error", "status": 400,
            "error": {"type": "invalid_request_error",
                      "message": "The 'gpt-9-nonexistent' model is not supported."},
        })})
        self.assertIn("not supported", text)
        self.assertNotIn("{", text, "the raw JSON envelope must not reach the user")
        self.assertIn("invalid_request_error", text)

    def test_degenerate_payloads_do_not_raise(self):
        for payload in (None, {}, "", {"message": ""}, {"codex_error_info": "x"}):
            self.assertIsInstance(bot._codex_error_text(payload), str)

    def test_empty_payload_is_falsy_so_it_never_masks_a_good_reply(self):
        """A blank error must not trigger the 'no response' notice on a successful turn."""
        self.assertFalse(bot._codex_error_text({}))
        self.assertFalse(bot._codex_error_text(None))


class TestCodexJsonStreamShape(unittest.TestCase):
    """Pin the real event shapes this depends on — codex is upgraded often."""

    @classmethod
    def setUpClass(cls):
        cls.events = []
        try:
            out = subprocess.run(
                ["codex", "exec", "-m", "gpt-9-nonexistent",
                 "--dangerously-bypass-approvals-and-sandbox", "--json", "hi"],
                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
            ).stdout
        except Exception as e:
            raise unittest.SkipTest(f"codex CLI unavailable: {e}")
        for line in out.splitlines():
            try:
                cls.events.append(json.loads(line))
            except Exception:
                pass
        if not cls.events:
            raise unittest.SkipTest("codex produced no JSON events")

    def test_failure_emits_turn_failed_with_an_error_object(self):
        failed = [e for e in self.events if e.get("type") == "turn.failed"]
        self.assertTrue(failed, f"expected turn.failed, got {[e.get('type') for e in self.events]}")
        self.assertIsInstance(failed[0].get("error"), dict)

    def test_the_parser_extracts_a_reason_from_the_real_event(self):
        for e in self.events:
            if e.get("type") in ("turn.failed", "error"):
                err = e.get("error") if isinstance(e.get("error"), dict) else e
                self.assertTrue(bot._codex_error_text(err).strip(),
                                "a real failure event must yield a non-empty reason")
                return
        self.fail("no failure event found")

    def test_non_fatal_item_errors_are_not_treated_as_turn_failures(self):
        """`item.completed` with item.type == "error" can be a WARNING (e.g. 'defaulting to
        fallback metadata'). Only turn.failed/error are fatal, or a warning would blank a good
        reply."""
        item_errors = [e for e in self.events
                       if e.get("type", "").startswith("item.")
                       and (e.get("item") or {}).get("type") == "error"]
        for e in item_errors:
            self.assertNotIn(e.get("type"), ("turn.failed", "error"))




class _FakeProc:
    """Minimal stand-in for the codex subprocess: yields a scripted JSON event stream."""

    def __init__(self, events):
        import io
        self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in events))
        self.stderr = io.StringIO("")
        self.returncode = 0
        self.pid = 4242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class TestPoisonedThreadAutoReset(unittest.TestCase):
    """A policy-blocked thread must be dropped and retried once on a fresh thread.

    Resuming re-sends the whole history, so the block re-trips forever. Observed on
    life-companion (2): 5 consecutive blocks on one thread, then 2 more on a fresh one.
    """

    BLOCK = [
        {"type": "thread.started", "thread_id": "poisoned-thread"},
        {"type": "turn.failed", "error": {
            "message": "This request was blocked by our safety systems. "
                       "Reason: Potentially unintended activity.",
            "codex_error_info": "misalignment_policy_violation"}},
    ]
    GOOD = [
        {"type": "thread.started", "thread_id": "fresh-thread"},
        {"type": "item.completed",
         "item": {"id": "i1", "type": "agent_message", "text": "recovered"}},
    ]

    def _run(self, scripts, session):
        """Run run_codex with Popen scripted per invocation; returns (result, commands)."""
        cmds = []

        def fake_popen(cmd, *a, **kw):
            cmds.append(cmd)
            return _FakeProc(scripts[len(cmds) - 1])

        with mock.patch("subprocess.Popen", side_effect=fake_popen), \
             mock.patch.object(bot, "save_sessions"), \
             mock.patch.object(bot, "save_cli_last_response"):
            out = bot.run_codex("do the thing", cwd="/tmp", session=session)
        return out, cmds

    def test_blocked_resume_resets_and_retries_on_a_fresh_thread(self):
        session = {"name": "s", "codex_session_id": "poisoned-thread"}
        out, cmds = self._run([self.BLOCK, self.GOOD], session)
        self.assertEqual(len(cmds), 2, "must retry exactly once")
        self.assertIn("resume", cmds[0], "first attempt resumes the existing thread")
        self.assertNotIn("resume", cmds[1], "retry must start a FRESH thread")
        self.assertEqual(out, "recovered", "the retry's answer is what the user gets")

    def test_retry_is_bounded_when_the_request_itself_is_refused(self):
        """If a fresh thread is blocked too, stop — do not loop resetting."""
        session = {"name": "s", "codex_session_id": "poisoned-thread"}
        out, cmds = self._run([self.BLOCK, self.BLOCK], session)
        self.assertEqual(len(cmds), 2, "exactly two attempts, then give up")
        self.assertIn("blocked by our safety systems", out)
        self.assertIn("fresh thread was already tried", out,
                      "the user must be told the thread was not the problem")

    def test_quota_failure_does_not_discard_a_working_thread(self):
        """Only policy blocks are poison. A quota/model error must keep the thread."""
        # Real codex reports the SAME thread id when resuming, so the id being unchanged
        # afterwards proves no reset happened (rather than merely being overwritten).
        quota = [
            {"type": "thread.started", "thread_id": "keep-me"},
            {"type": "turn.failed", "error": {"message": "You have hit your usage limit."}},
        ]
        session = {"name": "s", "codex_session_id": "keep-me"}
        out, cmds = self._run([quota], session)
        self.assertEqual(len(cmds), 1, "no retry for a non-policy failure")
        self.assertEqual(session["codex_session_id"], "keep-me", "thread must be preserved")
        self.assertIn("usage limit", out)

    def test_fresh_thread_block_does_not_attempt_a_reset(self):
        """Nothing to reset when there was no thread to begin with."""
        session = {"name": "s"}  # no codex_session_id
        out, cmds = self._run([self.BLOCK], session)
        self.assertEqual(len(cmds), 1)
        self.assertIn("blocked by our safety systems", out)


if __name__ == "__main__":
    unittest.main()
