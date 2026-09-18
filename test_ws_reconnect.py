"""A live view must recover from a close it did not ask for.

2026-09-18: the streaming message froze after switching out of the app and back. Server log:

    18:15:37 [WS] Client connected (last_seq=2047, replaying 0)
    18:15:57 [WS] Client disconnected
    18:16:03 [STREAM] Line #150: total_bytes_read=126436     <- bot still working
    18:17:10 [STREAM] _process_text: 95 chars                <- and still producing
    (no reconnect, no broadcast, for minutes)

WebSocketManager.onClosed only called scheduleReconnect() when code != 1000, so a NORMAL close
from the server or a relay left the app DISCONNECTED for good. Live stream deltas are transient
(seq=0) and dropped server-side when no client is attached, so nothing accumulates in the replay
buffer — the message just stops updating.

Intent to stay down is already held in `shouldReconnect` (only disconnect() clears it), and stale
sockets are already filtered by isStale(), so the close code should not gate reconnection at all.
"""
import re
import sys
import time
import unittest

sys.argv = ["bot.py"]
import api


WS_MANAGER = "android/app/src/main/java/com/claudebot/app/network/WebSocketManager.kt"


class TestReconnectsOnAnyUnrequestedClose(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(WS_MANAGER) as f:
            cls.src = f.read()
        m = re.search(r"override fun onClosed\(.*?\n            \}", cls.src, re.S)
        assert m, "onClosed not found — was WebSocketManager restructured?"
        cls.on_closed = m.group(0)

    def test_close_code_does_not_gate_the_reconnect(self):
        """The failure was `if (code != 1000) { ... scheduleReconnect() }`."""
        self.assertNotRegex(
            self.on_closed,
            r"if \(code != 1000\)\s*\{[^}]*scheduleReconnect\(\)",
            "scheduleReconnect() must not sit inside the code != 1000 branch — a clean close "
            "from the server would again leave the app down while the bot keeps streaming")

    def test_on_closed_always_schedules_a_reconnect(self):
        self.assertIn("scheduleReconnect()", self.on_closed,
                      "onClosed must attempt to reconnect")
        # ...and not by falling into a DISCONNECTED dead end instead.
        self.assertNotRegex(
            self.on_closed, r"else\s+onStateChange\(ConnectionState\.DISCONNECTED\)",
            "the 1000 branch must no longer terminate in DISCONNECTED")

    def test_a_user_initiated_disconnect_still_stays_down(self):
        """scheduleReconnect() is only safe to call unconditionally because it self-guards."""
        m = re.search(r"private fun scheduleReconnect\(\).*?\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "scheduleReconnect not found")
        body = m.group(0)
        self.assertRegex(
            body, r"if \(!shouldReconnect\)\s*\{\s*\n\s*onStateChange\(ConnectionState\.DISCONNECTED\)",
            "scheduleReconnect must bail out when shouldReconnect is false, or ON_STOP would "
            "immediately reopen the socket it just closed")

    def test_disconnect_clears_the_intent_flag(self):
        m = re.search(r"fun disconnect\(\).*?\n    \}", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertRegex(m.group(0), r"shouldReconnect\s*=\s*false")

    def test_stale_sockets_are_still_ignored(self):
        """Without this, reconnecting on every close would resurrect replaced sockets."""
        self.assertIn("if (isStale(webSocket)) return", self.on_closed)


class TestPruneUsesLastActivity(unittest.TestCase):
    """The catch-up snapshot is the only way a reconnect recovers text dropped while the app was
    away. Pruning it while the stream is still running re-creates the frozen view."""

    def setUp(self):
        self._saved = dict(api._active_streams)
        api._active_streams.clear()
        self.addCleanup(lambda: (api._active_streams.clear(),
                                 api._active_streams.update(self._saved)))

    def add(self, mid, started_sec_ago, idle_sec, text="hello"):
        now = time.time() * 1000
        api._active_streams[mid] = {
            "chat_id": 1, "session": "s", "text": text,
            "created_at": int(now - started_sec_ago * 1000),
            "updated_at": int(now - idle_sec * 1000),
        }

    def test_a_long_stream_that_is_still_appending_is_kept(self):
        """The regression: a 90-minute goal run appending right now was pruned on start time."""
        self.add(1, started_sec_ago=5400, idle_sec=5)
        api._prune_stale_active_streams()
        self.assertIn(1, api._active_streams,
                      "a stream that appended 5s ago is live no matter when it started")

    def test_an_idle_stream_is_still_dropped(self):
        self.add(2, started_sec_ago=5400, idle_sec=3600)
        api._prune_stale_active_streams()
        self.assertNotIn(2, api._active_streams)

    def test_entries_without_updated_at_fall_back_to_created_at(self):
        """Snapshots carried across a hot reload predate the updated_at field."""
        api._active_streams[3] = {"chat_id": 1, "session": "s", "text": "x",
                                  "created_at": int(time.time() * 1000)}
        api._prune_stale_active_streams()
        self.assertIn(3, api._active_streams, "a fresh legacy entry must survive")

        api._active_streams[4] = {"chat_id": 1, "session": "s", "text": "x",
                                  "created_at": int((time.time() - 9999) * 1000)}
        api._prune_stale_active_streams()
        self.assertNotIn(4, api._active_streams, "an old legacy entry must still be dropped")


class TestBroadcastMaintainsLiveness(unittest.TestCase):

    def setUp(self):
        self._saved = dict(api._active_streams)
        api._active_streams.clear()
        self.addCleanup(lambda: (api._active_streams.clear(),
                                 api._active_streams.update(self._saved)))

    def test_append_advances_updated_at(self):
        api.broadcast_ws(1, "stream", {"op": "start", "message_id": 9, "session": "s"})
        api._active_streams[9]["updated_at"] = 0
        api.broadcast_ws(1, "stream", {"op": "append", "message_id": 9, "text": "hi"})
        self.assertGreater(api._active_streams[9]["updated_at"], 0)
        self.assertEqual(api._active_streams[9]["text"], "hi")

    def test_tool_events_count_as_liveness_without_adding_text(self):
        """A turn can spend half an hour in tool calls and emit no text at all."""
        api.broadcast_ws(1, "stream", {"op": "start", "message_id": 10, "session": "s"})
        api._active_streams[10]["updated_at"] = 0
        api.broadcast_ws(1, "stream", {"op": "tool", "message_id": 10, "tool": "bash",
                                       "path": "ls"})
        self.assertGreater(api._active_streams[10]["updated_at"], 0,
                           "tool activity must keep the snapshot alive")
        self.assertEqual(api._active_streams[10]["text"], "",
                         "a tool event carries no user-visible text")

    def test_done_still_removes_the_entry(self):
        api.broadcast_ws(1, "stream", {"op": "start", "message_id": 11, "session": "s"})
        api.broadcast_ws(1, "stream", {"op": "done", "message_id": 11, "text": "x"})
        self.assertNotIn(11, api._active_streams)


if __name__ == "__main__":
    unittest.main()
