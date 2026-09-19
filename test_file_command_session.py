"""`/file` must label its result with the session the command ran in.

The download card broadcast by /file carried `get_session_id(session)` — an 8-char uuid — while
every other WS event carries the session NAME and the app matches on the name:

    ChatViewModel: matchesSessionFilter(msg.session)          // msg.session == sessionFilter
                   msg.session.isEmpty() || msg.session == effectiveSession

A uuid matches no session, so the card never appeared in the session the command was typed in,
and `availableSessions.add(msg.session)` added the raw uuid to the session picker.
"""
import sys
import unittest
from unittest import mock

sys.argv = ["bot.py"]
import bot


class TestFileEventCarriesTheSessionName(unittest.TestCase):

    SESSION = {"id": "a1b2c3d4", "name": "life-companion (4)", "cwd": "/tmp"}

    def _run_file_command(self, path, session=SESSION, origin_is_app=True):
        """Run `/file <path>` and return the broadcast 'file' payloads."""
        events = []

        def capture(chat_id, event_type, data):
            if event_type == "file":
                events.append(data)

        with mock.patch.object(bot, "get_active_session", return_value=session), \
             mock.patch.object(bot, "_ws_broadcast", side_effect=capture), \
             mock.patch.object(bot, "send_message"), \
             mock.patch.object(bot, "send_document", return_value=True), \
             mock.patch.object(bot, "send_photo", return_value=True), \
             mock.patch.object(bot, "_origin_is_app", return_value=origin_is_app):
            bot.handle_command(1, f"/file {path}")
        return events

    def setUp(self):
        import tempfile
        fd, self.path = tempfile.mkstemp(suffix=".txt")
        with open(fd, "w") as f:
            f.write("hello")
        self.addCleanup(lambda: __import__("os").unlink(self.path))

    def test_the_file_card_is_tagged_with_the_session_name(self):
        events = self._run_file_command(self.path)
        self.assertEqual(len(events), 1, "expected exactly one file event")
        self.assertEqual(events[0]["session"], "life-companion (4)")

    def test_it_is_not_tagged_with_the_session_id(self):
        """The id is what shipped; it matches no session name in the app."""
        events = self._run_file_command(self.path)
        self.assertNotEqual(events[0]["session"], "a1b2c3d4",
                            "a uuid here means the card lands outside the session, and the uuid "
                            "itself shows up as a phantom entry in the session picker")

    def test_no_active_session_sends_an_empty_label_not_a_crash(self):
        """Empty means 'whatever the user is viewing', which is the right fallback."""
        events = self._run_file_command(self.path, session=None)
        self.assertEqual(events[0]["session"], "")

    def test_the_telegram_path_tags_it_the_same_way(self):
        """Attribution must not depend on whether the app or Telegram asked."""
        events = self._run_file_command(self.path, origin_is_app=False)
        self.assertEqual(events[0]["session"], "life-companion (4)")


class TestEveryBroadcastUsesNames(unittest.TestCase):
    """One site out of eleven disagreed. Pin the convention so it stays one."""

    def test_no_broadcast_passes_a_session_id_as_the_session(self):
        src = open("bot.py").read()
        self.assertNotIn('"session": get_session_id(', src,
                         "WS events are matched by session NAME in the app — never send the id")


class TestTargetedCommandsLabelTheirOutput(unittest.TestCase):
    """handle_command_for_session runs a command against a session the user is NOT switched to.

    It pinned which session the command operates on (_active_session_override) but not how its
    replies are labelled. send_message checks _ws_session_override FIRST, so a leftover value on
    the thread would attribute the reply to a different session than the command ran against.
    """

    SESSION = {"id": "x", "name": "target-session", "cwd": "/tmp"}

    def test_the_ws_label_is_pinned_to_the_target_session(self):
        seen = {}

        def fake_handle_command(chat_id, text):
            seen["ws"] = getattr(bot._ws_session_override, "name", None)
            seen["active"] = getattr(bot._active_session_override, "session", None)
            return True

        bot._ws_session_override.name = "some-other-session"
        with mock.patch.object(bot, "handle_command", side_effect=fake_handle_command):
            bot.handle_command_for_session(1, "/file x", self.SESSION)
        self.assertEqual(seen["ws"], "target-session",
                         "a stale override would mislabel the reply")
        self.assertEqual(seen["active"], self.SESSION)

    def test_both_overrides_are_restored_afterwards(self):
        bot._ws_session_override.name = "outer"
        bot._active_session_override.session = {"name": "outer"}
        with mock.patch.object(bot, "handle_command", return_value=True):
            bot.handle_command_for_session(1, "/file x", self.SESSION)
        self.assertEqual(getattr(bot._ws_session_override, "name", None), "outer")
        self.assertEqual(getattr(bot._active_session_override, "session", None), {"name": "outer"})

    def test_restored_even_when_the_command_raises(self):
        bot._ws_session_override.name = "outer"
        with mock.patch.object(bot, "handle_command", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                bot.handle_command_for_session(1, "/file x", self.SESSION)
        self.assertEqual(getattr(bot._ws_session_override, "name", None), "outer",
                         "a leaked override would mislabel every later message on this thread")

    def tearDown(self):
        bot._ws_session_override.name = None
        bot._active_session_override.session = None


if __name__ == "__main__":
    unittest.main()
