"""Creating a session must tell the app the active session moved.

2026-09-19: `/new life-companion` created "life-companion (4)" and made it active server-side.
The next message typed in the app went to the ORIGINAL "life-companion" instead:

    12:45:12  [API] message: /new life-companion
    12:54:05  [API] message: i need to update the chinese bp stack...
    12:54:05  [handle_message] session=life-companion, id=128636496114048      <- wrong session
    12:55:45  [handle_message] session=life-companion (4), id=128636375052736  <- a Telegram
                                                                                  upload, correct

Telegram input reads the active session directly, so it was unaffected — which is what made this
look like the app was "sending to the wrong place". The app targets `sessionFilter ?:
currentSession`, and nothing had told it either value changed: /switch and the session picker both
broadcast "active_session", but session CREATION never did.
"""
import sys
import unittest
from unittest import mock

sys.argv = ["bot.py"]
import bot


class TestCreateSessionAnnouncesItself(unittest.TestCase):

    def setUp(self):
        self._saved = dict(bot.user_sessions)
        bot.user_sessions.clear()
        self.addCleanup(lambda: (bot.user_sessions.clear(),
                                 bot.user_sessions.update(self._saved)))

    def _create(self, name, chat_id=1):
        with mock.patch.object(bot, "save_sessions"), \
             mock.patch.object(bot, "_ws_broadcast") as bcast:
            session = bot.create_session(chat_id, name, "/tmp")
        return session, bcast

    def test_creation_broadcasts_the_new_active_session(self):
        session, bcast = self._create("life-companion")
        bcast.assert_called_once()
        chat_id, event, data = bcast.call_args[0]
        self.assertEqual(event, "active_session")
        self.assertEqual(data["session"], session["name"])

    def test_the_broadcast_carries_the_disambiguated_name(self):
        """The app routes by NAME. Announcing the base name would point it at session 1."""
        self._create("life-companion")
        self._create("life-companion")
        self._create("life-companion")
        session, bcast = self._create("life-companion")
        self.assertEqual(session["name"], "life-companion (4)")
        self.assertEqual(bcast.call_args[0][2]["session"], "life-companion (4)",
                         "broadcasting 'life-companion' would send the app to the ORIGINAL "
                         "session — exactly the reported bug")

    def test_the_session_is_actually_active_when_the_broadcast_goes_out(self):
        """A client that reacts by re-reading state must not see a stale active id."""
        seen = {}

        def capture(chat_id, event, data):
            seen["active"] = bot.user_sessions["1"]["active"]

        with mock.patch.object(bot, "save_sessions"), \
             mock.patch.object(bot, "_ws_broadcast", side_effect=capture):
            session = bot.create_session(1, "proj", "/tmp")
        self.assertEqual(seen["active"], bot.get_session_id(session))

    def test_a_broadcast_failure_does_not_break_session_creation(self):
        with mock.patch.object(bot, "save_sessions"), \
             mock.patch.object(bot, "_ws_broadcast", side_effect=RuntimeError("no ws")):
            session = bot.create_session(1, "proj", "/tmp")
        self.assertEqual(session["name"], "proj")
        self.assertEqual(bot.user_sessions["1"]["active"], bot.get_session_id(session))


class TestEverySwitchPointAnnounces(unittest.TestCase):
    """The three places that move the active session must all tell the app.

    Two of them did; creation was the odd one out. Same shape as run_codex vs run_codex_task.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = open("bot.py").read()

    def test_create_session_broadcasts(self):
        start = self.src.index("\ndef create_session(")
        body = self.src[start:self.src.index("\ndef ", start + 10)]
        self.assertIn('"active_session"', body,
                      "create_session sets user_sessions[...]['active'] — it must announce it")

    def test_switch_command_still_broadcasts(self):
        self.assertIn('_ws_broadcast(chat_id, "active_session"', self.src)
        self.assertGreaterEqual(self.src.count('"active_session"'), 3,
                                "expected /switch, the session picker, and create_session")


class TestAppFollowsTheSwitch(unittest.TestCase):
    """A stale filter pins sends to the old session even once the broadcast arrives."""

    PATH = "android/app/src/main/java/com/claudebot/app/ChatViewModel.kt"

    @classmethod
    def setUpClass(cls):
        with open(cls.PATH) as f:
            cls.src = f.read()

    def test_sends_target_the_filter_before_the_active_session(self):
        """Pins why the filter has to move; if this changes, the handler below is wrong."""
        self.assertIn("get() = sessionFilter.value ?: currentSession.value", self.src)

    def test_active_session_event_repoints_a_stale_filter(self):
        start = self.src.index('"active_session" ->')
        block = self.src[start:start + 900]
        self.assertIn("currentSession.value = msg.session", block)
        self.assertIn("setSessionFilter(msg.session)", block,
                      "without this the user keeps typing into the session they just left")

    def test_it_never_widens_the_filter_to_all(self):
        """Clearing the filter would dump every session into the view unasked."""
        start = self.src.index('"active_session" ->')
        block = self.src[start:start + 900]
        self.assertIn("filter != null && filter != msg.session", block,
                      "only repoint an EXISTING filter, and only when it actually differs")
        self.assertNotIn("setSessionFilter(null)", block)


if __name__ == "__main__":
    unittest.main()
