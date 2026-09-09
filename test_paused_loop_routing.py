"""A paused autonomous loop must stop capturing messages.

`/goal pause` sets paused=True but deliberately leaves active=True — `active` doubles as the
not-cancelled flag the loops poll to decide whether to keep iterating, so clearing it would look
like a cancel. handle_message's router gated on `active` alone, so a PAUSED goal kept swallowing
every message into a feedback queue nothing was running to drain. The user pauses precisely in
order to talk to the bot normally, and instead got silence.
"""
import sys
import unittest
from unittest import mock

sys.argv = ["bot.py"]
import bot

CHAT = 999
SESSION = {"id": "sess1", "name": "s", "cwd": "/tmp"}
KEY = f"{CHAT}:sess1"

LOOPS = [
    ("goal_state", "Goal"),
    ("justdoit_active", "JustDoIt"),
    ("ralph_active", "Ralph"),
    ("omni_active", "Omni"),
    ("deepreview_active", "Deep review"),
]


class TestPausedLoopRouting(unittest.TestCase):

    def setUp(self):
        for attr, _ in LOOPS:
            getattr(bot, attr).pop(KEY, None)
        bot.user_feedback_queue.pop(KEY, None)
        self.addCleanup(lambda: bot.user_feedback_queue.pop(KEY, None))
        self.addCleanup(lambda: [getattr(bot, a).pop(KEY, None) for a, _ in LOOPS])

    def send(self, text):
        """Deliver a message; returns True if it was captured as loop feedback."""
        bot.active_processes.pop("sess1", None)
        with mock.patch.object(bot, "send_message"), \
             mock.patch.object(bot, "get_active_session", return_value=SESSION), \
             mock.patch.object(bot, "run_claude_in_thread") as run, \
             mock.patch.object(bot, "save_active_tasks"), \
             mock.patch.object(bot, "check_memory_pressure", return_value=(True, 8000)), \
             mock.patch.object(bot, "_ws_broadcast"), \
             mock.patch.object(bot, "_ws_broadcast_status"):
            bot.handle_message(CHAT, text, session=SESSION)
            captured = bool(bot.user_feedback_queue.get(KEY))
            bot.active_processes.pop("sess1", None)
            return captured, run.called

    def test_running_loop_captures_feedback(self):
        """Baseline: an actually-running loop must still capture messages."""
        for attr, mode in LOOPS:
            getattr(bot, attr)[KEY] = {"active": True, "paused": False}
            captured, _ = self.send("some feedback")
            self.assertTrue(captured, f"{mode}: running loop should capture feedback")
            bot.user_feedback_queue.pop(KEY, None)
            getattr(bot, attr).pop(KEY, None)

    def test_paused_loop_does_not_capture(self):
        """The reported bug: paused loops kept swallowing messages."""
        for attr, mode in LOOPS:
            getattr(bot, attr)[KEY] = {"active": True, "paused": True}
            captured, _ = self.send("hello, are you there")
            self.assertFalse(captured, f"{mode}: paused loop must NOT capture feedback")
            getattr(bot, attr).pop(KEY, None)

    def test_paused_loop_message_reaches_claude(self):
        """Not capturing is only half of it — the message must actually be handled."""
        bot.goal_state[KEY] = {"active": True, "paused": True}
        captured, ran = self.send("what is the status")
        self.assertFalse(captured)
        self.assertTrue(ran, "a message sent while paused must be handled normally")

    def test_pause_keeps_active_true(self):
        """Guards the reason the bug existed: pause must not clear `active`, since the loops
        poll `active` as their not-cancelled flag. If this ever changes, the router gate can
        be simplified — but until then both fields must be consulted."""
        bot.goal_state[KEY] = {"active": True, "paused": False, "resume_event": mock.MagicMock()}
        bot.goal_active[f"{CHAT}:sess1"] = "goal_x"
        self.addCleanup(lambda: bot.goal_active.pop(f"{CHAT}:sess1", None))
        with mock.patch.object(bot, "send_message"), \
             mock.patch.object(bot, "get_active_session", return_value=SESSION), \
             mock.patch.object(bot, "_load_goal", return_value=None), \
             mock.patch.object(bot, "save_active_tasks"), \
             mock.patch.object(bot, "_ws_broadcast_goal"):
            bot.handle_command(CHAT, "/goal pause")
        self.assertTrue(bot.goal_state[KEY].get("active"), "pause must leave active=True")
        self.assertTrue(bot.goal_state[KEY].get("paused"), "pause must set paused=True")

    def test_interrupt_prefix_while_paused_is_not_captured(self):
        """`!` interrupts a RUNNING step; with nothing running there is nothing to interrupt."""
        bot.goal_state[KEY] = {"active": True, "paused": True}
        captured, _ = self.send("!urgent")
        self.assertFalse(captured)

    def test_resume_discards_feedback_queued_while_paused(self):
        """Messages typed while paused were addressed to the bot, not the loop — resuming must
        not inject them as goal feedback and steer the goal with side conversation."""
        bot.goal_state[KEY] = {"active": True, "paused": True, "resume_event": mock.MagicMock()}
        bot.goal_active[KEY] = "goal_x"
        bot.user_feedback_queue[KEY] = ["what is the status", "are you there"]
        self.addCleanup(lambda: bot.goal_active.pop(KEY, None))
        with mock.patch.object(bot, "send_message") as sm, \
             mock.patch.object(bot, "get_active_session", return_value=SESSION), \
             mock.patch.object(bot, "_load_goal", return_value=None), \
             mock.patch.object(bot, "_goal_rate_limit_resume_delay", return_value=(0, None)), \
             mock.patch.object(bot, "save_active_tasks"), \
             mock.patch.object(bot, "_ws_broadcast_goal"):
            bot.handle_command(CHAT, "/goal resume")
        self.assertNotIn(KEY, bot.user_feedback_queue, "stale feedback must be dropped on resume")
        self.assertFalse(bot.goal_state[KEY]["paused"], "resume must unpause")
        said = " ".join(c.args[1] for c in sm.call_args_list)
        self.assertIn("Discarded 2", said, "the user must be told what was dropped")

    def test_resume_says_nothing_when_queue_is_empty(self):
        bot.goal_state[KEY] = {"active": True, "paused": True, "resume_event": mock.MagicMock()}
        bot.goal_active[KEY] = "goal_x"
        self.addCleanup(lambda: bot.goal_active.pop(KEY, None))
        with mock.patch.object(bot, "send_message") as sm, \
             mock.patch.object(bot, "get_active_session", return_value=SESSION), \
             mock.patch.object(bot, "_load_goal", return_value=None), \
             mock.patch.object(bot, "_goal_rate_limit_resume_delay", return_value=(0, None)), \
             mock.patch.object(bot, "save_active_tasks"), \
             mock.patch.object(bot, "_ws_broadcast_goal"):
            bot.handle_command(CHAT, "/goal resume")
        self.assertNotIn("Discarded", " ".join(c.args[1] for c in sm.call_args_list))

    def test_no_loop_at_all_is_unaffected(self):
        captured, ran = self.send("plain message")
        self.assertFalse(captured)
        self.assertTrue(ran)


if __name__ == "__main__":
    unittest.main()


class TestDelayedResumeSessionTag(unittest.TestCase):
    """A delayed auto-continue must be attributed to the session the TASK belongs to.

    _fire_delayed_resume runs on a one-shot threading.Timer, so _ws_session_override is unset and
    send_message falls back to get_active_session() — tagging "Resuming task…" with whichever
    session the user happens to be looking at, often a different project entirely.
    """

    TASK_SESSION = {"id": "taskS", "name": "life-companion", "cwd": "/tmp"}
    OTHER_SESSION = {"id": "otherS", "name": "some-other-project", "cwd": "/tmp"}

    def setUp(self):
        bot.claude_autocontinue_count.pop("taskS", None)
        bot.active_processes.pop("taskS", None)
        bot.message_queue.pop("taskS", None)
        bot.cancelled_sessions.discard("taskS")
        self.addCleanup(lambda: bot.active_processes.pop("taskS", None))
        self.addCleanup(lambda: bot.claude_autocontinue_count.pop("taskS", None))
        self.addCleanup(lambda: setattr(bot._ws_session_override, "name", None))

    def fire(self):
        """Fire the timer callback; return the session tag in force when the message was sent."""
        seen = {}

        def capture(chat_id, text, *a, **kw):
            seen["override"] = getattr(bot._ws_session_override, "name", None)
            seen["text"] = text
            return 1

        bot._ws_session_override.name = None  # a fresh timer thread has none
        with mock.patch.object(bot, "send_message", side_effect=capture), \
             mock.patch.object(bot, "get_session_by_id", return_value=self.TASK_SESSION), \
             mock.patch.object(bot, "get_active_session", return_value=self.OTHER_SESSION), \
             mock.patch.object(bot, "run_claude_in_thread"), \
             mock.patch.object(bot, "_clear_pending_resume"), \
             mock.patch.object(bot, "_ws_broadcast"):
            bot._fire_delayed_resume(CHAT, "taskS")
        return seen

    def test_resume_is_tagged_with_the_tasks_session(self):
        seen = self.fire()
        self.assertIn("Resuming task", seen.get("text", ""))
        self.assertEqual(seen.get("override"), "life-companion",
                         "resume must be attributed to the task's session, not the active one")

    def test_resume_tag_is_not_the_currently_active_session(self):
        """The actual regression: the message landed in whatever session was open at the time."""
        seen = self.fire()
        self.assertNotEqual(seen.get("override"), "some-other-project")
