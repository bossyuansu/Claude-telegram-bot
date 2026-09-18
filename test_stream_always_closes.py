"""Every WS stream `start` must be followed by a `done`, on the error paths too.

An unclosed stream is what makes an older message reappear at the bottom of the app's list on
every refresh: the app only drops a message from streamingMessageIds on 'done', so without one it
carries the message forward forever and re-appends it on each reload (ChatViewModel.swapMessages).
The server half of the same leak is capped by _prune_stale_active_streams (test_ws_active_streams).

run_codex_task was the outlier — its `except FileNotFoundError` / `except Exception` handlers
returned without emitting 'done', while run_claude_streaming and run_codex both emit one from
theirs. This pins the invariant as source structure, because reproducing a mid-turn codex crash
end-to-end would need the real CLI.
"""
import ast
import re
import sys
import unittest

sys.argv = ["bot.py"]

SRC = open("bot.py").read()
TREE = ast.parse(SRC)

STREAMING_FUNCS = ("run_claude_streaming", "run_codex", "run_codex_task")


def _func(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in bot.py — was it renamed?")


def _stream_ops(node):
    """Every literal op passed to _ws_stream(...) inside `node`."""
    ops = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_ws_stream" and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)):
            ops.append((n.args[1].value, n.lineno))
    return ops


class TestEveryStarterAlsoCloses(unittest.TestCase):

    def test_the_streaming_functions_still_exist(self):
        """Guard the rest of this file against a silent rename."""
        for name in STREAMING_FUNCS:
            self.assertTrue(_stream_ops(_func(name)),
                            f"{name} emits no _ws_stream events any more")

    def test_each_function_that_opens_a_stream_also_closes_it(self):
        for name in STREAMING_FUNCS:
            ops = [op for op, _ in _stream_ops(_func(name))]
            self.assertIn("start", ops, f"{name} no longer opens a stream")
            self.assertIn("done", ops, f"{name} opens a stream but never closes it")

    def test_run_codex_task_closes_the_stream_from_its_finally(self):
        """The handler that catches a mid-turn crash must close the stream.

        A 'done' only in the happy path leaves the app streaming forever when codex throws.
        """
        node = _func("run_codex_task")
        finallies = [t for t in ast.walk(node) if isinstance(t, ast.Try) and t.finalbody]
        self.assertTrue(finallies, "run_codex_task lost its finally block")
        closed = any(
            op == "done"
            for t in finallies
            for stmt in t.finalbody
            for op, _ in _stream_ops(stmt)
        )
        self.assertTrue(closed,
                        "run_codex_task's finally must emit 'done' — its except handlers return "
                        "without one, so the app is left with a stream that never ends")

    def test_the_close_is_guarded_so_a_normal_turn_sends_exactly_one_done(self):
        """The finally must not fire a second 'done' after the happy path already sent one."""
        node = _func("run_codex_task")
        finallies = [t for t in ast.walk(node) if isinstance(t, ast.Try) and t.finalbody]
        guarded = False
        for t in finallies:
            for stmt in t.finalbody:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.If) and any(
                            op == "done" for op, _ in _stream_ops(n)):
                        names = {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
                        if "_ws_stream_open" in names:
                            guarded = True
        self.assertTrue(guarded,
                        "the finally's 'done' must be behind the _ws_stream_open flag, or a "
                        "successful turn emits two terminal events")

    def test_the_flag_is_cleared_on_the_happy_path(self):
        """Otherwise the guard above is always true and every turn double-fires."""
        body = SRC[SRC.index("def run_codex_task"):]
        body = body[:body.index("\ndef run_gemini_task")]
        self.assertRegex(body, r"_ws_stream_open\s*=\s*True",
                         "the flag is never set when the stream opens")
        self.assertRegex(body, r"_ws_stream_open\s*=\s*False",
                         "the flag is never cleared, so the finally would re-close a closed stream")

    def test_the_flag_is_initialised_before_the_try(self):
        """It is read in the finally; if start raised before assigning it, that is a NameError
        inside a cleanup handler — which would mask the original exception."""
        node = _func("run_codex_task")
        inner = next(n for n in ast.walk(node)
                     if isinstance(n, ast.FunctionDef) and n.name == "codex_thread")
        first_try = next(s for s in inner.body if isinstance(s, ast.Try))
        pre = [s for s in inner.body if s.lineno < first_try.lineno]
        assigned = {t.id for s in pre if isinstance(s, ast.Assign)
                    for t in s.targets if isinstance(t, ast.Name)}
        for var in ("_ws_stream_open", "_ws_stream_mid", "_ws_stream_label"):
            self.assertIn(var, assigned,
                          f"{var} must be initialised before the try that the finally guards")


class TestClientSideExpiryIsWiredUp(unittest.TestCase):
    """The app is the half that matters: its process outlives bot restarts, so a stream left open
    by a restart is never closed by anything on the server."""

    PATH = "android/app/src/main/java/com/claudebot/app/ChatViewModel.kt"

    @classmethod
    def setUpClass(cls):
        cls.src = open(cls.PATH).read()

    def test_stale_streams_are_expired(self):
        self.assertIn("private fun expireStaleStreams()", self.src)
        self.assertRegex(self.src, r"streamingSnapshot\(\)[^{]*\{\s*\n\s*expireStaleStreams\(\)",
                         "the snapshot handed to swapMessages must be pruned first, or a dead "
                         "stream is re-appended on every refresh")

    def test_activity_is_recorded_on_every_live_event(self):
        self.assertRegex(
            self.src,
            r'if \(msg\.op != "done"\) lastStreamActivity\[mid\] = System\.currentTimeMillis\(\)',
            "without a liveness stamp, expiry would kill long-running but healthy streams")

    def test_expiry_window_is_not_shorter_than_the_servers(self):
        """30 min matches _ACTIVE_STREAM_MAX_AGE_MS. A shorter client window would drop the carry
        for a stream the server still considers live."""
        m = re.search(r"STREAM_STALE_AFTER_MS\s*=\s*(\d+)\s*\*\s*(\d+)\s*\*\s*(\d+)L", self.src)
        self.assertIsNotNone(m, "STREAM_STALE_AFTER_MS not found")
        client_ms = int(m.group(1)) * int(m.group(2)) * int(m.group(3))
        import api
        self.assertGreaterEqual(client_ms, api._ACTIVE_STREAM_MAX_AGE_MS)

    def test_a_carried_stream_is_placed_by_timestamp_not_appended(self):
        """An off-page carry appended blindly lands below newer messages — the reported symptom."""
        self.assertIn("messages.indexOfFirst { it.timestamp > s.timestamp }", self.src)


if __name__ == "__main__":
    unittest.main()
