"""Reconnect catch-up must not replay a stream that can no longer be live.

api.py re-sends `start` + the full running text for every entry in _active_streams on EVERY
WebSocket connect. Entries are removed only on 'done', so a stream that never sends one —
cancelled, errored, or interrupted by a hot reload (loader.py preserves _active_streams across
reloads) — is replayed forever. Symptom: the last message reappears on every refresh.
"""
import sys
import time
import unittest

sys.argv = ["bot.py"]
import api


class TestPruneStaleActiveStreams(unittest.TestCase):

    def setUp(self):
        self._saved = dict(api._active_streams)
        api._active_streams.clear()
        self.addCleanup(lambda: (api._active_streams.clear(),
                                 api._active_streams.update(self._saved)))

    def add(self, mid, age_sec, text="hello"):
        api._active_streams[mid] = {
            "chat_id": 1, "session": "s", "text": text,
            "created_at": int((time.time() - age_sec) * 1000),
        }

    def test_stale_entry_is_dropped(self):
        """A stream older than the cap cannot still be running."""
        self.add(1, api._ACTIVE_STREAM_MAX_AGE_MS / 1000 + 60)
        api._prune_stale_active_streams()
        self.assertNotIn(1, api._active_streams)

    def test_live_entry_is_kept(self):
        """A genuinely in-flight stream must still be caught up on reconnect."""
        self.add(2, 30)
        api._prune_stale_active_streams()
        self.assertIn(2, api._active_streams, "a 30s-old stream is live — do not drop it")

    def test_long_running_goal_stream_is_kept(self):
        """The goal loop's execution_stale_timeout is 1200s; such a stream is still legitimate."""
        self.add(3, 1200)
        api._prune_stale_active_streams()
        self.assertIn(3, api._active_streams,
                      "20min is within the longest legitimate stream — dropping it would lose "
                      "catch-up for a running goal")

    def test_prune_reports_what_it_dropped(self):
        self.add(4, 9999)
        self.add(5, 10)
        dropped = api._prune_stale_active_streams()
        self.assertEqual(dropped, [4])

    def test_missing_created_at_is_treated_as_stale(self):
        """A malformed entry must not become immortal."""
        api._active_streams[6] = {"chat_id": 1, "session": "s", "text": "x"}
        api._prune_stale_active_streams()
        self.assertNotIn(6, api._active_streams)

    def test_empty_is_a_no_op(self):
        self.assertEqual(api._prune_stale_active_streams(), [])


if __name__ == "__main__":
    unittest.main()
