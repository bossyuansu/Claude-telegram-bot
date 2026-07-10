"""Tests for interrupting autonomous deepreview runs."""
import signal
import unittest
from unittest.mock import MagicMock, patch

import bot


class TestDeepreviewInterruptHandler(unittest.TestCase):
    def test_bang_interrupt_marks_deepreview_and_kills_process(self):
        session = {"id": "sid1", "name": "life-companion (2)"}
        state = {"active": True, "chat_id": 123, "session_name": session["name"]}
        proc = MagicMock()
        proc.pid = 4242
        proc.stdout = MagicMock()
        old_cancelled = set(bot.cancelled_sessions)

        try:
            with patch.dict(bot.justdoit_active, {}, clear=True), \
                 patch.dict(bot.omni_active, {}, clear=True), \
                 patch.dict(bot.ralph_active, {}, clear=True), \
                 patch.dict(bot.deepreview_active, {"123:sid1": state}, clear=True), \
                 patch.dict(bot.active_processes, {"sid1": proc}, clear=True), \
                 patch.dict(bot.user_feedback_queue, {}, clear=True), \
                 patch.object(bot, "send_message") as mock_send, \
                 patch("os.getpgid", return_value=4242), \
                 patch("os.killpg") as mock_killpg:

                bot.handle_message(123, "!restart with this constraint", session=session)

                self.assertTrue(state["interrupted"])
                self.assertEqual(bot.user_feedback_queue["123:sid1"], ["restart with this constraint"])
                self.assertIn("sid1", bot.cancelled_sessions)
                mock_killpg.assert_called_once_with(4242, signal.SIGKILL)
                mock_send.assert_called_once()
        finally:
            bot.cancelled_sessions.clear()
            bot.cancelled_sessions.update(old_cancelled)


if __name__ == "__main__":
    unittest.main()
