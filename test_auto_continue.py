"""Auto-continue resume-delay parsing.

Regression: the `resume in <N><unit>` pattern required a word boundary immediately after the
unit, so a COMPOUND duration like "5h45m" did not match ('h' is followed by '4'). The parse
returned 0, which means "resume immediately" — so a task that asked to wait ~6 hours for a
deploy window resumed seconds later and burned its entire auto-continue budget re-checking
unchanged state.
"""
import sys
import unittest

sys.argv = ["bot.py"]
import bot


def delay_for(marker):
    _, delay = bot._incomplete_signal(f"some work\n\n⏳ INCOMPLETE — resume in {marker} — reason")
    return delay


class TestResumeDelayParsing(unittest.TestCase):

    def test_compound_durations(self):
        """The case that regressed: hours+minutes in one token."""
        self.assertEqual(delay_for("5h45m"), 5 * 3600 + 45 * 60)
        self.assertEqual(delay_for("1h30m"), 5400)
        self.assertEqual(delay_for("2h 30m"), 9000)

    def test_single_unit_durations_still_work(self):
        self.assertEqual(delay_for("15m"), 900)
        self.assertEqual(delay_for("5h"), 18000)
        self.assertEqual(delay_for("300s"), 300)
        self.assertEqual(delay_for("90s"), 90)

    def test_long_waits_are_clamped_not_zeroed(self):
        """A too-long request must clamp to the ceiling — never fall back to 0 (immediate)."""
        self.assertEqual(delay_for("24h"), min(86400, bot.CLAUDE_RESUME_DELAY_MAX))
        self.assertEqual(delay_for("99h"), bot.CLAUDE_RESUME_DELAY_MAX)

    def test_compound_duration_never_yields_immediate_resume(self):
        """Guards the actual failure mode: any hour-scale wait must not parse as 0."""
        for marker in ("5h45m", "5h40m", "5h30m", "1h05m", "3h15m"):
            self.assertGreater(delay_for(marker), 3600,
                               f"{marker} must be an hour-scale wait, not an immediate resume")

    def test_marker_without_delay_means_immediate(self):
        incomplete, delay = bot._incomplete_signal("work\n\n⏳ INCOMPLETE — still going")
        self.assertTrue(incomplete)
        self.assertEqual(delay, 0)


if __name__ == "__main__":
    unittest.main()
