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
import os
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


class TestRolloutIsTheAuthoritativeFailureRecord(unittest.TestCase):
    """The JSON stream is not a reliable channel for a server-side refusal.

    2026-09-19, life-companion: two turns ("next" at 01:19:53 and 01:20:42) each produced a blank
    message. The bot logged no error and broadcast only stream start + done. The rollout held the
    only record:

        payload = {"type": "task_complete", "last_agent_message": null,
                   "error": {"message": "This request was blocked by our safety systems. ...",
                             "codex_error_info": "misalignment_policy_violation"}}
    """

    REAL_ERROR = {
        "message": "This request was blocked by our safety systems. "
                   "Reason: Potentially unintended activity.",
        "codex_error_info": "misalignment_policy_violation",
    }

    def _rollout(self, payloads):
        """Write a rollout file and point the reader at it. Returns the extracted reason."""
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for p in payloads:
                f.write(json.dumps({"type": "event_msg", "payload": p}) + "\n")
        self.addCleanup(os.unlink, path)
        with mock.patch.object(bot, "_codex_rollout_path", return_value=path):
            return bot._codex_rollout_error("tid")

    def test_reads_the_reason_from_a_failed_turn(self):
        reason = self._rollout([
            {"type": "task_complete", "last_agent_message": None, "error": self.REAL_ERROR},
        ])
        self.assertIn("blocked by our safety systems", reason)
        self.assertIn("misalignment_policy_violation", reason)

    def test_a_turn_that_answered_is_not_a_failure(self):
        """However it ended, a turn with an assistant message must not raise a failure notice."""
        self.assertEqual(self._rollout([
            {"type": "task_complete", "last_agent_message": "here you go", "error": None},
        ]), "")

    def test_only_the_most_recent_turn_counts(self):
        """A rollout accumulates every turn. An old failure must not be reported as today's."""
        self.assertEqual(self._rollout([
            {"type": "task_complete", "last_agent_message": None, "error": self.REAL_ERROR},
            {"type": "task_complete", "last_agent_message": "recovered", "error": None},
        ]), "", "the thread recovered — reporting the earlier block would be wrong")

    def test_a_later_failure_after_an_earlier_success_is_reported(self):
        reason = self._rollout([
            {"type": "task_complete", "last_agent_message": "fine", "error": None},
            {"type": "task_complete", "last_agent_message": None, "error": self.REAL_ERROR},
        ])
        self.assertIn("blocked by our safety systems", reason)

    def test_missing_or_unreadable_rollout_is_not_an_error(self):
        with mock.patch.object(bot, "_codex_rollout_path", return_value=None):
            self.assertEqual(bot._codex_rollout_error("tid"), "")
        with mock.patch.object(bot, "_codex_rollout_path", return_value="/nope/missing.jsonl"):
            self.assertEqual(bot._codex_rollout_error("tid"), "")
        self.assertEqual(bot._codex_rollout_error(None), "")

    def test_garbage_lines_are_skipped(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write("not json\n{broken\n")
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "last_agent_message": None,
                "error": self.REAL_ERROR}}) + "\n")
        self.addCleanup(os.unlink, path)
        with mock.patch.object(bot, "_codex_rollout_path", return_value=path):
            self.assertIn("safety systems", bot._codex_rollout_error("tid"))


class TestInteractivePathSurfacesErrorsToo(unittest.TestCase):
    """run_codex_task is the path a plain message to a Codex session takes.

    Every piece of error surfacing built in b33885d / 4f5d51a went into run_codex only, so this
    path returned a blank message with no reason for months. Same shape of omission as the missing
    'done' fixed in 83b0ef2 — two of the three streaming functions were updated, not the third.
    """

    @classmethod
    def setUpClass(cls):
        src = open("bot.py").read()
        start = src.index("def run_codex_task(")
        cls.body = src[start:src.index("\ndef run_gemini_task")]

    def test_it_captures_stream_errors(self):
        self.assertIn("turn.failed", self.body,
                      "run_codex_task must watch the JSON stream for a failed turn")
        self.assertIn("_codex_error_text", self.body)

    def test_it_falls_back_to_the_rollout(self):
        self.assertIn("_codex_rollout_error", self.body,
                      "the stream carried no error in the 2026-09-19 incident — the rollout did")

    def test_an_empty_turn_produces_a_notice(self):
        self.assertRegex(self.body, r"Codex returned no response",
                         "an empty turn must tell the user why")

    def test_the_notice_reaches_the_app_not_just_telegram(self):
        """The app renders the WS 'done' text, so a notice only in final_chunk is invisible there."""
        self.assertRegex(self.body, r"_ws_done_text\s*=\s*f?\"?.*notice|notice.*_ws_done_text",
                         "the failure notice must be included in the WS done payload")

    def test_it_does_not_discard_the_thread(self):
        """2026-09-19: the same thread resumed fine minutes after two consecutive blocks, so the
        block is often transient and dropping the thread would destroy the conversation."""
        self.assertNotRegex(
            self.body, r'session\["codex_session_id"\]\s*=\s*None',
            "run_codex_task must not reset the thread on a policy block — it is usually transient")


if __name__ == "__main__":
    unittest.main()
