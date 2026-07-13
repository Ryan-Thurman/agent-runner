import tempfile
import unittest
from pathlib import Path

from agent_runner.errors import JobError
from agent_runner.jobs import JobResult
from agent_runner.phase_loop import (
    _checks_fix_prompt,
    _implement_prompt,
    _review_fix_prompt,
    _validate_close_phase_outputs,
)
from agent_runner.plan import ParsedPhase


PLAN_PATH = "docs/plan.md"
REGISTERED_HASH = "aaaaaaaaaaaa1111"
DRIFTED_HASH = "bbbbbbbbbbbb2222"


def _phase(status: str = "COMPLETE", content_hash: str = DRIFTED_HASH) -> ParsedPhase:
    return ParsedPhase(
        phase_number=1,
        title="First phase",
        status=status,
        content="Create generated.txt.\n",
        content_hash=content_hash,
    )


class PlanOwnershipPromptTests(unittest.TestCase):
    """Writer prompts must preserve agent-runner ownership of lifecycle state."""

    def _assert_rule(self, prompt: str) -> None:
        self.assertIn(f"Do not edit `{PLAN_PATH}`", prompt)
        self.assertIn("If a project command tells you to update the plan", prompt)

    def test_implement_prompt_keeps_review_orchestration_with_runner(self):
        prompt = _implement_prompt(
            _phase(), require_publish=False, plan_path=PLAN_PATH
        )
        self.assertNotIn("/dev-implement-task", prompt)
        self._assert_rule(prompt)
        self.assertIn("IMPLEMENT worker in agent-runner's state machine", prompt)
        self.assertIn("do not run a phase or PR review", prompt)
        self.assertIn("Do not invoke `/dev-phase-review`, `/dev-pr-review`, or `pr-review`", prompt)
        self.assertIn("do not spawn reviewer subagents", prompt)
        self.assertIn("CHECKING, REVIEW, FIX, and CLOSE_PHASE", prompt)

    def test_checks_fix_prompt_forbids_plan_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            checks = JobResult(
                job_id=1,
                status="FAILED",
                exit_code=1,
                error="check failed",
                log_path=Path(tmp) / "missing.log",
                prompt_path=Path(tmp) / "prompt.md",
                output_path=Path(tmp) / "output.txt",
            )
            prompt = _checks_fix_prompt(
                _phase(), checks, require_publish=False, plan_path=PLAN_PATH
            )
        self._assert_rule(prompt)

    def test_review_fix_prompt_forbids_plan_edits(self):
        review = {
            "status": "CHANGES_REQUESTED",
            "summary": "Needs fixes.",
            "findings": {"blocking": ["Fix it"], "shouldFix": [], "nitpick": []},
        }
        prompt = _review_fix_prompt(
            _phase(),
            review,
            pr_url=None,
            require_publish=False,
            plan_path=PLAN_PATH,
        )
        self._assert_rule(prompt)


class ClosePhaseBlameTests(unittest.TestCase):
    """A drifted body must be blamed on the job that actually edited it."""

    def _validate(self, *, pre_close_hash):
        _validate_close_phase_outputs(
            repo_root=Path("."),
            plan_path=PLAN_PATH,
            phase={"content_hash": REGISTERED_HASH, "phase_number": 1},
            fresh_phase=_phase(),
            pre_close_hash=pre_close_hash,
        )

    def test_body_intact_before_close_blames_the_closer(self):
        with self.assertRaises(JobError) as ctx:
            self._validate(pre_close_hash=REGISTERED_HASH)
        self.assertIn("closer changed the protected phase body", str(ctx.exception))

    def test_body_already_drifted_before_close_blames_an_earlier_job(self):
        with self.assertRaises(JobError) as ctx:
            self._validate(pre_close_hash=DRIFTED_HASH)
        message = str(ctx.exception)
        self.assertIn("an earlier job edited it", message)
        self.assertIn("the closer is not the cause", message)
        self.assertNotIn("closer changed the protected phase body", message)

    def test_unknown_pre_close_hash_falls_back_to_closer_blame(self):
        # The manual-merge repair path validates without a pre-close snapshot.
        with self.assertRaises(JobError) as ctx:
            self._validate(pre_close_hash=None)
        self.assertIn("closer changed the protected phase body", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
