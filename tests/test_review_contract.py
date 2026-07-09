import tempfile
import unittest
from pathlib import Path

from agent_runner.errors import JobError
from agent_runner.phase_loop import (
    _render_github_review_body,
    _review_fix_prompt,
    _review_prompt,
    _validate_review_payload,
)
from agent_runner.plan import ParsedPhase


def _phase() -> ParsedPhase:
    return ParsedPhase(
        phase_number=2,
        title="Review contract",
        status="REVIEWING",
        content="Replace the inlined diff with reviewer-fetched context.",
        content_hash="abc123",
    )


def _empty_findings() -> dict[str, list[str]]:
    return {"blocking": [], "shouldFix": [], "nitpick": []}


class ReviewContractTests(unittest.TestCase):
    def test_review_prompt_uses_pr_url_and_does_not_inline_diff(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            log_dir = tmp / "logs"
            log_dir.mkdir()
            (log_dir / "checks.log").write_text(
                "diff --git a/secret b/secret\n+not in prompt\n",
                encoding="utf-8",
            )
            phase_row = {
                "pr_url": "https://github.com/example/repo/pull/36",
                "branch_name": "dev/review-contract",
                "published_sha": "1234567890abcdef",
            }

            prompt = _review_prompt(
                tmp,
                _phase(),
                log_dir,
                phase=phase_row,
                base_branch="main",
                use_published_diff=True,
            )

            self.assertIn("PR #36 (https://github.com/example/repo/pull/36)", prompt)
            self.assertIn("gh pr diff https://github.com/example/repo/pull/36", prompt)
            self.assertIn("Checks log path:", prompt)
            self.assertIn("nitpick is advisory", prompt)
            self.assertIn("Return PASS only when blocking and shouldFix are empty", prompt)
            self.assertNotIn("diff --git", prompt)
            self.assertNotIn("recommendedFixPrompt", prompt)

    def test_validate_review_payload_accepts_new_contract_and_rejects_legacy_keys(self):
        review = _validate_review_payload(
            {"status": "PASS", "summary": "ok", "findings": _empty_findings()}
        )

        self.assertEqual(set(review), {"status", "summary", "findings"})
        self.assertEqual(review["status"], "PASS")

        coerced = _validate_review_payload(
            {
                "status": "CHANGES_REQUESTED",
                "summary": "object finding",
                "findings": {
                    "blocking": [],
                    "shouldFix": [{"path": "agent_runner/phase_loop.py"}],
                    "nitpick": [],
                },
            }
        )
        self.assertEqual(
            coerced["findings"]["shouldFix"],
            ['{"path": "agent_runner/phase_loop.py"}'],
        )

        for legacy_key, value in (
            ("recommendedFixPrompt", "fix it"),
            ("blockingIssues", ["legacy blocker"]),
        ):
            payload = {
                "status": "PASS",
                "summary": "legacy",
                "findings": _empty_findings(),
                legacy_key: value,
            }
            with self.assertRaisesRegex(JobError, "unknown top-level"):
                _validate_review_payload(payload)

    def test_render_github_body_omits_legacy_sections_and_empty_buckets(self):
        review = _validate_review_payload(
            {
                "status": "CHANGES_REQUESTED",
                "summary": "Needs one change.",
                "findings": {
                    "blocking": ["agent_runner/phase_loop.py:1 - fix the contract"],
                    "shouldFix": [],
                    "nitpick": [],
                },
            }
        )

        body = _render_github_review_body(
            plan_path="docs/plan.md",
            phase_number=2,
            review_job_id=9,
            reviewed_sha="abcdef123456",
            review=review,
        )

        self.assertIn("<!-- agent-runner-review", body)
        self.assertIn("plan=docs/plan.md", body)
        self.assertIn("phase=2", body)
        self.assertIn("review_job=9", body)
        self.assertIn("reviewed_sha=abcdef123456", body)
        self.assertIn("**Phase 2 review - CHANGES_REQUESTED**", body)
        self.assertIn("**Blocking**", body)
        self.assertNotIn("Recommended Fix Prompt", body)
        self.assertNotIn("## Reviewed SHA", body)
        self.assertNotIn("- None", body)
        self.assertNotIn("**Should fix**", body)

    def test_review_fix_prompt_leads_with_pr_and_markdown_checklist(self):
        review = _validate_review_payload(
            {
                "status": "CHANGES_REQUESTED",
                "summary": "Needs fixes.",
                "findings": {
                    "blocking": ["Create fix-marker.txt"],
                    "shouldFix": ["Tidy the generated text"],
                    "nitpick": ["Shorten the marker comment"],
                },
            }
        )

        prompt = _review_fix_prompt(
            _phase(),
            review,
            pr_url="https://github.com/example/repo/pull/36",
            require_publish=True,
            reviewed_sha="abcdef1234567890",
        )

        self.assertTrue(
            prompt.startswith(
                "These are the findings from the code review of "
                "PR #36 (https://github.com/example/repo/pull/36). "
                "Fix all blocking and should-fix findings."
            )
        )
        self.assertIn("gh pr diff https://github.com/example/repo/pull/36", prompt)
        self.assertIn("Must-fix review findings", prompt)
        self.assertIn("### Blocking", prompt)
        self.assertIn("- [ ] Create fix-marker.txt", prompt)
        self.assertIn("### Should fix", prompt)
        self.assertIn("- [ ] Tidy the generated text", prompt)
        self.assertIn("Optional nitpicks, only if trivial", prompt)
        self.assertIn("- [ ] Shorten the marker comment", prompt)
        self.assertNotIn("recommendedFixPrompt", prompt)
        self.assertNotIn("```json", prompt)


if __name__ == "__main__":
    unittest.main()
