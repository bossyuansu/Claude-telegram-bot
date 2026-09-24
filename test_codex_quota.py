"""Quota-aware Codex model selection.

The Codex CLI exposes no usage/quota command (`codex login status` prints only "Logged in using
ChatGPT"), but each session rollout under ~/.codex/sessions records a `rate_limits` payload. We
read the newest one so a long autonomous loop can de-escalate from the premium model to the
cheaper fallback before it hits a hard rate limit mid-run.

The dangerous failure mode is a *silent* downgrade: if the usage probe returns nothing (no rollout
yet, unreadable file, schema change), we must keep using the model the user asked for rather than
quietly serving every future run from the fallback.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.argv = ["bot.py"]
import bot


def with_usage(snap):
    """Pin the usage cache so no disk read happens."""
    bot._codex_model_ctx._usage_cache = (time.time(), snap)


def window(pct, **kw):
    return {"primary": {"used_percent": pct, "window_minutes": 10080}, "secondary": None,
            "plan_type": "pro", "rate_limit_reached_type": None, **kw}


class TestCodexQuotaFallback(unittest.TestCase):

    def setUp(self):
        bot._codex_model_ctx.model = None
        self.addCleanup(lambda: setattr(bot._codex_model_ctx, "_usage_cache", None))
        self.addCleanup(lambda: setattr(bot._codex_model_ctx, "model", None))

    def test_below_threshold_keeps_premium_model(self):
        with_usage(window(4.0))
        self.assertEqual(bot._codex_model(), bot.CODEX_MODEL)

    def test_at_or_above_threshold_falls_back(self):
        for pct in (bot.CODEX_USAGE_FALLBACK_PERCENT, 92.0, 100.0):
            with_usage(window(pct))
            self.assertEqual(bot._codex_model(), bot.CODEX_FALLBACK_MODEL, f"at {pct}%")

    def test_hard_rate_limit_falls_back_regardless_of_percent(self):
        """`rate_limit_reached_type` is authoritative even if used_percent looks low."""
        with_usage(window(10.0, rate_limit_reached_type="primary"))
        self.assertEqual(bot._codex_model(), bot.CODEX_FALLBACK_MODEL)

    def test_secondary_window_counts(self):
        """A near-full short window must trigger even when the weekly window is idle."""
        snap = window(2.0)
        snap["secondary"] = {"used_percent": 95.0, "window_minutes": 300}
        with_usage(snap)
        self.assertEqual(bot._codex_model(), bot.CODEX_FALLBACK_MODEL)

    def test_unknown_usage_never_silently_downgrades(self):
        """No data must mean 'use what was asked for', not 'assume the worst'."""
        for snap in (None, {}, {"primary": None, "secondary": None}, {"primary": {}}):
            with_usage(snap)
            self.assertIsNone(bot._codex_usage_percent(snap))
            self.assertEqual(bot._codex_model(), bot.CODEX_MODEL, f"snap={snap}")

    def test_non_premium_selection_is_never_rewritten(self):
        """Only premium models de-escalate; a cheap model must not be swapped for another."""
        with_usage(window(99.0))
        for model in ("gpt-5.5", bot.CODEX_FALLBACK_MODEL):
            bot._codex_model_ctx.model = model
            self.assertEqual(bot._codex_model(), model)

    def test_usage_line_is_empty_when_unknown(self):
        with_usage(None)
        self.assertEqual(bot._codex_usage_line(), "")

    def test_usage_line_reports_percent_and_warns_on_fallback(self):
        with_usage(window(4.0))
        self.assertIn("4%", bot._codex_usage_line())
        self.assertNotIn("falling back", bot._codex_usage_line())
        with_usage(window(92.0))
        self.assertIn("falling back", bot._codex_usage_line())

    def test_probe_reads_real_rollout_without_raising(self):
        """End-to-end against the live ~/.codex/sessions tree; absent data is a valid answer."""
        bot._codex_model_ctx._usage_cache = None
        snap = bot._codex_usage()
        self.assertTrue(snap is None or isinstance(snap, dict))
        if snap:
            self.assertIsInstance(bot._codex_usage_percent(snap), float)


if __name__ == "__main__":
    unittest.main()


class TestUsageProbeSurvivesRolloutsWithoutRateLimits(unittest.TestCase):
    """The probe must not go blind because the newest rollout happens to lack a snapshot.

    2026-09-24: usage read 86% one minute and None the next. Cause: _codex_usage read ONLY the
    newest rollout, and short/failed/probe runs produce rollouts with no rate_limits event.
    _codex_usage_percent() then returns None, _codex_model() keeps the premium model, and the
    quota fallback is silently off on a window that is 86% spent — the exact thing it exists to
    prevent.
    """

    def setUp(self):
        import tempfile
        bot._codex_model_ctx._usage_cache = None
        self.addCleanup(lambda: setattr(bot._codex_model_ctx, "_usage_cache", None))
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self._real_expand = os.path.expanduser

    def _write(self, name, lines, mtime):
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")
        os.utime(p, (mtime, mtime))
        return p

    def _rate_limits_line(self, used, resets_in=3600):
        return {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": used, "window_minutes": 10080,
                        "resets_at": time.time() + resets_in},
            "plan_type": "pro"}}}

    def _probe(self):
        """Run _codex_usage against our fake sessions dir."""
        with mock.patch.object(bot.os.path, "expanduser",
                               side_effect=lambda p: self.dir if p.endswith(".codex/sessions")
                               else self._real_expand(p)):
            return bot._codex_usage(max_age=0)

    def test_falls_back_to_an_older_rollout_when_the_newest_has_no_snapshot(self):
        now = time.time()
        self._write("rollout-old.jsonl", [self._rate_limits_line(86.0)], now - 600)
        self._write("rollout-new.jsonl", [{"type": "event_msg",
                                           "payload": {"type": "task_started"}}], now)
        snap = self._probe()
        self.assertIsNotNone(snap, "a probe run without rate_limits must not blind the reader")
        self.assertEqual(bot._codex_usage_percent(snap), 86.0)

    def test_the_newest_snapshot_wins(self):
        now = time.time()
        self._write("rollout-a.jsonl", [self._rate_limits_line(20.0)], now - 600)
        self._write("rollout-b.jsonl", [self._rate_limits_line(86.0)], now)
        self.assertEqual(bot._codex_usage_percent(self._probe()), 86.0)

    def test_an_already_reset_window_reports_unknown_not_a_stale_high_reading(self):
        """Quota refilled — forcing the fallback on the dead reading would waste the premium
        model for the whole next window."""
        now = time.time()
        self._write("rollout-spent.jsonl", [self._rate_limits_line(97.0, resets_in=-60)], now)
        self.assertIsNone(bot._codex_usage_percent(self._probe()))

    def test_no_rollouts_at_all_is_unknown_not_an_exception(self):
        self.assertIsNone(self._probe())

    def test_the_scan_is_bounded(self):
        """Never walk the whole history; it grows without limit."""
        self.assertIsInstance(bot._CODEX_USAGE_SCAN_ROLLOUTS, int)
        self.assertLessEqual(bot._CODEX_USAGE_SCAN_ROLLOUTS, 50)
        now = time.time()
        for i in range(bot._CODEX_USAGE_SCAN_ROLLOUTS + 5):
            self._write(f"rollout-{i:03d}.jsonl",
                        [{"type": "event_msg", "payload": {"type": "task_started"}}], now - i)
        self._write("rollout-999-ancient.jsonl", [self._rate_limits_line(50.0)], now - 10_000)
        self.assertIsNone(self._probe(),
                          "a snapshot beyond the scan window must not be found")
