"""Goal delivery gate: a goal is not done until its work is merged via a PR.

Every milestone can pass while the work is still invisible to everyone else — uncommitted, on a
branch nobody merged, or committed straight to the base branch with no review. Completing on that
basis reports a success nobody can use.

The subtle case is the squash merge: it rewrites SHAs, so a *merged* branch still reports commits
"ahead" of base. Git alone would call delivered work unmerged and wedge the goal in delivery
attempts forever, so the gate consults the PR state before deciding.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.argv = ["bot.py"]
import bot


def git(cwd, *args):
    subprocess.run(("git",) + args, cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo():
    """A repo with an `origin` remote and one commit on main, checked out clean."""
    d = tempfile.mkdtemp()
    remote, work = os.path.join(d, "remote.git"), os.path.join(d, "work")
    subprocess.run(["git", "init", "--bare", "-b", "main", remote], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "init", "-b", "main", work], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git(work, "config", "user.email", "t@t.t")
    git(work, "config", "user.name", "t")
    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("base\n")
    git(work, "add", "-A")
    git(work, "commit", "-m", "base")
    git(work, "remote", "add", "origin", remote)
    git(work, "push", "-u", "origin", "main")
    return d, work


def commit(work, text, name="a.txt"):
    with open(os.path.join(work, name), "w") as f:
        f.write(text)
    git(work, "add", "-A")
    git(work, "commit", "-m", text)


class TestGoalDeliveryGaps(unittest.TestCase):

    def setUp(self):
        self.dirs = []
        self._real_gh = bot._gh_json
        self.addCleanup(lambda: setattr(bot, "_gh_json", self._real_gh))
        self.addCleanup(lambda: [shutil.rmtree(d, ignore_errors=True) for d in self.dirs])

    def repo(self):
        d, work = make_repo()
        self.dirs.append(d)
        return work

    def stub_gh(self, prs):
        bot._gh_json = lambda *a, **k: prs

    def test_non_git_directory_is_not_gated(self):
        """Nothing to enforce — must not block a goal that never touched a repo."""
        d = tempfile.mkdtemp()
        self.dirs.append(d)
        gaps, probe, _ = bot._goal_delivery_gaps(d)
        self.assertEqual(gaps, [])
        self.assertIsNone(probe)

    def test_clean_repo_with_nothing_to_deliver_passes(self):
        gaps, _, _ = bot._goal_delivery_gaps(self.repo())
        self.assertEqual(gaps, [])

    def test_uncommitted_changes_are_a_gap(self):
        work = self.repo()
        with open(os.path.join(work, "b.txt"), "w") as f:
            f.write("wip\n")
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("uncommitted" in g for g in gaps), gaps)

    def test_commits_straight_on_base_branch_are_a_gap(self):
        """Work may be 'in', but it bypassed review entirely — the case this gate exists for."""
        work = self.repo()
        commit(work, "sneaky")
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("no PR was opened" in g for g in gaps), gaps)

    def test_feature_branch_with_no_pr_is_a_gap(self):
        work = self.repo()
        git(work, "checkout", "-b", "feat")
        commit(work, "work")
        self.stub_gh([])
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("no PR opened" in g for g in gaps), gaps)

    def test_open_pr_is_not_yet_delivered(self):
        work = self.repo()
        git(work, "checkout", "-b", "feat")
        commit(work, "work")
        self.stub_gh([{"number": 7, "state": "OPEN", "url": "u"}])
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("still OPEN" in g for g in gaps), gaps)

    def test_squash_merged_branch_counts_as_delivered(self):
        """The regression this guards: squash merge rewrites SHAs, so the branch still reads
        'ahead' of base. Git alone would loop delivery attempts on already-merged work."""
        work = self.repo()
        git(work, "checkout", "-b", "feat")
        commit(work, "work")
        probe = bot._workspace_probe(work)
        self.assertGreater(probe["ahead"], 0, "precondition: branch reads as ahead of base")
        self.stub_gh([{"number": 9, "state": "MERGED", "url": "u"}])
        gaps, _, detail = bot._goal_delivery_gaps(work)
        self.assertEqual(gaps, [])
        self.assertIn("#9", detail)

    def test_unverifiable_pr_state_is_reported_not_assumed(self):
        """gh missing/unauthenticated must not silently pass work off as delivered."""
        work = self.repo()
        git(work, "checkout", "-b", "feat")
        commit(work, "work")
        self.stub_gh(None)
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("could not be verified" in g for g in gaps), gaps)

    def test_detached_head_is_a_gap(self):
        work = self.repo()
        commit(work, "second")
        sha = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=work,
                             capture_output=True, text=True).stdout.strip()
        git(work, "checkout", sha)
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("detached HEAD" in g for g in gaps), gaps)

    def test_dirty_and_unmerged_report_together(self):
        """Gaps accumulate — the operator should see everything blocking delivery at once."""
        work = self.repo()
        git(work, "checkout", "-b", "feat")
        commit(work, "work")
        with open(os.path.join(work, "c.txt"), "w") as f:
            f.write("wip\n")
        self.stub_gh([])
        gaps, _, _ = bot._goal_delivery_gaps(work)
        self.assertTrue(any("uncommitted" in g for g in gaps), gaps)
        self.assertTrue(any("no PR opened" in g for g in gaps), gaps)


class TestDeliveryPrompt(unittest.TestCase):

    def test_prompt_names_the_gaps_and_forbids_shortcuts(self):
        probe = {"base": "origin/main", "branch": "feat", "head": "abc1234"}
        p = bot._goal_delivery_prompt({"title": "G"}, ["2 uncommitted file(s)"], probe)
        self.assertIn("2 uncommitted file(s)", p)
        self.assertIn("origin/main", p)
        self.assertIn("DELIVERY:", p)
        for forbidden in ("weaken tests", "bypass branch protection"):
            self.assertIn(forbidden, p)


if __name__ == "__main__":
    unittest.main()
