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


if __name__ == "__main__":
    unittest.main()
