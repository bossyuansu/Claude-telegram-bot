"""Review scoping: a follow-up /deepreview must review what the loop actually changed.

/deepreview scoped itself from Claude's conversational memory ("the code you've been working on in
this session") plus session.last_prompt. A goal running in Codex mode never enters that memory, so
the review looked at the wrong thing — or nothing. Goals now record a git-derived file list on exit
and deepreview anchors to it.

Also covers a truncation bug this uncovered: _git_out() stripped stdout, and `git status
--porcelain` encodes "unstaged" as a LEADING SPACE, so the first filename lost its first character
(`bot.py` -> `ot.py`) in every workspace scope block shown to reviewers.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.argv = ["bot.py"]
import bot

from test_goal_delivery import commit, git, make_repo


class TestPorcelainTruncation(unittest.TestCase):
    """The first dirty file must keep its first character."""

    def setUp(self):
        self.d, self.work = make_repo()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_unstaged_first_file_is_not_truncated(self):
        """`git status --porcelain` writes ' M bot.py' — a leading space that strip() ate."""
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("modified\n")
        probe = bot._workspace_probe(self.work)
        self.assertEqual(probe["dirty"], ["a.txt"])

    def test_staged_and_untracked_files_still_parse(self):
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("modified\n")
        git(self.work, "add", "a.txt")           # 'M  a.txt' — no leading space
        with open(os.path.join(self.work, "zz.txt"), "w") as f:
            f.write("new\n")                      # '?? zz.txt'
        self.assertEqual(sorted(bot._workspace_probe(self.work)["dirty"]), ["a.txt", "zz.txt"])

    def test_goal_scope_files_agree_with_probe(self):
        """Both porcelain readers must parse identically — they feed the same review scope."""
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("modified\n")
        self.assertIn("a.txt", bot._goal_scope_files(self.work, None))


class TestGoalScopeFiles(unittest.TestCase):

    def setUp(self):
        self.d, self.work = make_repo()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_committed_work_since_base_is_captured(self):
        """The point of the baseline: work COMMITTED during the goal is still its work."""
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work,
                              capture_output=True, text=True).stdout.strip()
        commit(self.work, "goal change", name="feature.py")
        files = bot._goal_scope_files(self.work, base)
        self.assertIn("feature.py", files)

    def test_uncommitted_and_committed_work_combine(self):
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.work,
                              capture_output=True, text=True).stdout.strip()
        commit(self.work, "goal change", name="feature.py")
        with open(os.path.join(self.work, "wip.py"), "w") as f:
            f.write("wip\n")
        files = bot._goal_scope_files(self.work, base)
        self.assertIn("feature.py", files)
        self.assertIn("wip.py", files)

    def test_no_baseline_still_reports_uncommitted(self):
        """A goal that never recorded a base SHA must not silently report an empty scope."""
        with open(os.path.join(self.work, "wip.py"), "w") as f:
            f.write("wip\n")
        self.assertEqual(bot._goal_scope_files(self.work, None), ["wip.py"])


class TestDeepreviewScopeContext(unittest.TestCase):

    def setUp(self):
        self.d, self.work = make_repo()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def handoff(self, **kw):
        base = {"title": "Ship the thing", "cwd": self.work, "files": ["alpha.py", "beta.py"],
                "file_count": 2, "ended_at": datetime.now().isoformat()}
        base.update(kw)
        return {"last_goal_scope": base}

    def test_goal_files_are_named_in_the_review_scope(self):
        ctx = bot._deepreview_scope_context(self.work, self.handoff())
        self.assertIn("alpha.py", ctx)
        self.assertIn("beta.py", ctx)
        self.assertIn("Ship the thing", ctx)

    def test_scope_warns_the_work_may_be_outside_claude_context(self):
        """The whole point: Codex-run goal work is not in Claude's history."""
        ctx = bot._deepreview_scope_context(self.work, self.handoff())
        self.assertIn("Codex", ctx)
        self.assertIn("Read these files from disk", ctx)

    def test_stale_handoff_is_ignored(self):
        """A goal from last week is not what /deepreview means today."""
        old = (datetime.now() - timedelta(days=3)).isoformat()
        ctx = bot._deepreview_scope_context(self.work, self.handoff(ended_at=old))
        self.assertNotIn("alpha.py", ctx)

    def test_handoff_from_a_different_repo_is_ignored(self):
        """Sessions can change cwd; a file list from another checkout would misdirect the review."""
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        ctx = bot._deepreview_scope_context(self.work, self.handoff(cwd=other))
        self.assertNotIn("alpha.py", ctx)

    def test_falls_back_to_workspace_scope_without_a_handoff(self):
        with open(os.path.join(self.work, "loose.py"), "w") as f:
            f.write("x\n")
        ctx = bot._deepreview_scope_context(self.work, {})
        self.assertIn("WORKSPACE SCOPE", ctx)
        self.assertIn("loose.py", ctx)

    def test_non_git_directory_yields_no_scope_rather_than_crashing(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.assertEqual(bot._deepreview_scope_context(d, {}), "")

    def test_malformed_handoff_timestamp_does_not_drop_the_scope(self):
        """An unparseable date must not silently discard the file list."""
        ctx = bot._deepreview_scope_context(self.work, self.handoff(ended_at="not-a-date"))
        self.assertIn("alpha.py", ctx)


if __name__ == "__main__":
    unittest.main()
