import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_runner.cli import (
    _autofix_attempt_count,
    _autofix_resume_status,
    _job_landed_commits,
    _requires_human_intent,
    _reviewed_head_moved,
)
from agent_runner.storage import connect_db, create_job, create_phase, create_plan, get_or_create_project


def _phase_row(**overrides):
    row = {"branch_name": None, "published_sha": None, "blocked_from": None}
    row.update(overrides)
    return row


class RequiresHumanIntentTests(unittest.TestCase):
    def test_plan_drift_messages_require_human(self):
        for message in (
            "plan changed for phase 2",
            "closer changed the protected phase body; only status/evidence",
            "registered phase 2 body differs",
            "phase body on origin/main differs",
        ):
            with self.subTest(message=message):
                self.assertTrue(_requires_human_intent(message))

    def test_publish_and_close_preflight_mismatches_require_human(self):
        # These are bookkeeping mismatches, not code defects. A fixer agent has
        # nothing to fix, and a "successful" autofix would launder the guard.
        for message in (
            "close preflight failed for phase 2: current HEAD 9a676441e201 does "
            "not match reviewed published SHA cec78187d48f; rerun review",
            "current branch 'main' does not match reviewed published branch "
            "'dev/phase-02'; check out the reviewed branch before closing",
            "publish required before review, but the PR head is cec78187d48f "
            "while local HEAD is 9a676441e201; push the branch before review",
        ):
            with self.subTest(message=message):
                self.assertTrue(_requires_human_intent(message))

    def test_ordinary_failures_still_autofix(self):
        for message in (
            "phase 2 BLOCKED after IMPLEMENT failure",
            "checks failed for phase 2: pnpm test exited 1",
            "phase 2 BLOCKED by reviewer: missing test coverage",
        ):
            with self.subTest(message=message):
                self.assertFalse(_requires_human_intent(message))


class AutofixResumeStatusTests(unittest.TestCase):
    def test_new_commits_reroute_post_review_statuses_through_checking(self):
        for blocked_from in ("CLOSING", "MERGING"):
            with self.subTest(blocked_from=blocked_from):
                self.assertEqual(
                    _autofix_resume_status(
                        blocked_from=blocked_from, landed_commits=True
                    ),
                    "CHECKING",
                )

    def test_new_commits_leave_pre_review_statuses_alone(self):
        for blocked_from in ("IMPLEMENTING", "CHECKING", "REVIEWING", "FIXING"):
            with self.subTest(blocked_from=blocked_from):
                self.assertEqual(
                    _autofix_resume_status(
                        blocked_from=blocked_from, landed_commits=True
                    ),
                    blocked_from,
                )

    def test_no_new_commits_resumes_where_it_blocked(self):
        self.assertEqual(
            _autofix_resume_status(blocked_from="CLOSING", landed_commits=False),
            "CLOSING",
        )


class DbBackedGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self.db = connect_db(home)
        project = get_or_create_project(self.db, slug="demo", repo_path=home)
        plan = create_plan(self.db, project_id=project["id"], path="docs/plan.md")
        phase = create_phase(
            self.db,
            project_id=project["id"],
            plan_id=plan["id"],
            phase_number=1,
            title="Demo",
            content_hash="abc123",
        )
        self.project_id = project["id"]
        self.plan_id = plan["id"]
        self.phase_id = phase["id"]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _autofix(self, **overrides):
        kwargs = {
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "phase_id": self.phase_id,
            "job_type": "AUTOFIX",
            "status": "FAILED",
        }
        kwargs.update(overrides)
        return create_job(self.db, **kwargs)

    def test_interrupted_autofix_does_not_spend_an_attempt(self):
        self._autofix(exit_code=None, error="interrupted")
        self._autofix(exit_code=None, error="interrupted")
        self.assertEqual(_autofix_attempt_count(self.db, self.phase_id), 0)

    def test_real_failures_and_successes_spend_attempts(self):
        self._autofix(exit_code=1, error="agent job failed")
        self._autofix(exit_code=None, error="timeout after 900s; killed with SIGKILL")
        self._autofix(status="SUCCEEDED", exit_code=0)
        self.assertEqual(_autofix_attempt_count(self.db, self.phase_id), 3)

    def test_job_landed_commits_compares_start_and_finish_shas(self):
        moved = self._autofix(started_sha="a" * 40, finished_sha="b" * 40)
        still = self._autofix(started_sha="a" * 40, finished_sha="a" * 40)
        unknown = self._autofix(started_sha=None, finished_sha=None)
        self.assertTrue(_job_landed_commits(self.db, moved["id"]))
        self.assertFalse(_job_landed_commits(self.db, still["id"]))
        self.assertFalse(_job_landed_commits(self.db, unknown["id"]))
        self.assertFalse(_job_landed_commits(self.db, 9999))


class ReviewedHeadMovedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "work"], cwd=self.repo, check=True)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        self.reviewed_sha = self._commit("first")

    def tearDown(self):
        self._tmp.cleanup()

    def _commit(self, message: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
             "commit", "-q", "-m", message],
            cwd=self.repo,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()

    def test_head_at_reviewed_sha_has_not_moved(self):
        phase = _phase_row(branch_name="work", published_sha=self.reviewed_sha)
        self.assertFalse(_reviewed_head_moved(self.repo, phase))

    def test_commit_after_review_moves_head(self):
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        self._commit("second")
        phase = _phase_row(branch_name="work", published_sha=self.reviewed_sha)
        self.assertTrue(_reviewed_head_moved(self.repo, phase))

    def test_other_branch_defers_to_the_preflight(self):
        subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=self.repo, check=True)
        phase = _phase_row(branch_name="work", published_sha=self.reviewed_sha)
        self.assertFalse(_reviewed_head_moved(self.repo, phase))

    def test_missing_publish_metadata_is_not_a_move(self):
        self.assertFalse(_reviewed_head_moved(self.repo, _phase_row()))
        self.assertFalse(_reviewed_head_moved(None, _phase_row(branch_name="work")))


if __name__ == "__main__":
    unittest.main()
