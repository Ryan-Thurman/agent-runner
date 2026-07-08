import json
import tempfile
import unittest
from pathlib import Path

from agent_runner.config import ReviewTriageConfig, RunnerConfig
from agent_runner.jobs import JobResult
from agent_runner.phase_loop import (
    _interpret_review_triage,
    _last_json_object,
    _parse_review_document,
    _review_json_document,
    _unwrap_agent_envelope,
)

REVIEW_JSON = json.dumps(
    {
        "status": "PASS",
        "summary": "looks good",
        "findings": {"blocking": [], "shouldFix": [], "nitpick": []},
        "recommendedFixPrompt": "",
    },
    indent=2,
)

# Matches the format _extract_review_json persists to review.json, which the
# next review prompt embeds verbatim inside a ```json fence.
PREVIOUS_REVIEW = (
    json.dumps(
        {
            "status": "CHANGES_REQUESTED",
            "summary": "fix the bug",
            "findings": {"blocking": ["bug"], "shouldFix": [], "nitpick": []},
            "recommendedFixPrompt": "fix it",
        },
        indent=2,
        sort_keys=True,
    )
    + "\n"
)


class ReviewJsonDocumentTests(unittest.TestCase):
    def test_bare_json(self):
        self.assertEqual(json.loads(_review_json_document(REVIEW_JSON))["status"], "PASS")

    def test_fence_only(self):
        raw = f"```json\n{REVIEW_JSON}\n```"
        self.assertEqual(json.loads(_review_json_document(raw))["status"], "PASS")

    def test_plain_fence_without_language(self):
        raw = f"```\n{REVIEW_JSON}\n```"
        self.assertEqual(json.loads(_review_json_document(raw))["status"], "PASS")

    def test_prose_before_fence(self):
        # Regression: reviewers narrate before the fenced JSON; the extractor
        # previously required the fence to open the output and failed to parse.
        raw = (
            "My review is complete. **Bug confirmed real.** The fix binds only\n"
            "the local name into `names`, keeping the origin in `aliases`.\n\n"
            f"```json\n{REVIEW_JSON}\n```"
        )
        self.assertEqual(json.loads(_review_json_document(raw))["status"], "PASS")

    def test_prose_before_bare_json(self):
        raw = f"Review done, verdict below.\n\n{REVIEW_JSON}\n"
        self.assertEqual(json.loads(_review_json_document(raw))["status"], "PASS")

    def test_last_fenced_block_wins(self):
        draft = REVIEW_JSON.replace("PASS", "BLOCKED")
        raw = f"Draft:\n```json\n{draft}\n```\nFinal:\n```json\n{REVIEW_JSON}\n```"
        self.assertEqual(json.loads(_review_json_document(raw))["status"], "PASS")

    def test_nested_objects_do_not_shadow_document(self):
        raw = f"Note the findings map is empty.\n\n{REVIEW_JSON}"
        parsed = json.loads(_review_json_document(raw))
        self.assertIn("status", parsed)

    def test_invalid_fenced_content_still_returned_for_error_reporting(self):
        raw = "```json\nnot json at all\n```"
        self.assertEqual(_review_json_document(raw), "not json at all")

    def test_no_json_returns_stripped_output(self):
        self.assertEqual(_review_json_document("  nothing here  "), "nothing here")

    def test_unparseable_brace_run_before_bare_document(self):
        # Exercises _last_json_object's raw_decode-failure retry: the scan must
        # skip past "{a lot}" and still find the real document after it.
        raw = f"Costs rose {{a lot}} this quarter.\n\n{REVIEW_JSON}"
        self.assertEqual(json.loads(_review_json_document(raw))["status"], "PASS")

    def test_last_json_object_recovers_after_unparseable_brace(self):
        raw = '{not json} trailing prose {"tier": "simple"}'
        self.assertEqual(_last_json_object(raw), '{"tier": "simple"}')


class StaleReviewEchoTests(unittest.TestCase):
    # The review prompt quotes the previous review.json; a candidate
    # byte-identical to that quote is an echo, not the reviewer's new verdict.

    def test_fenced_echo_after_new_verdict_is_ignored(self):
        raw = (
            f"```json\n{REVIEW_JSON}\n```\n\n"
            "For reference, the previous review was:\n"
            f"```json\n{PREVIOUS_REVIEW}```"
        )
        doc = _review_json_document(raw, stale_document=PREVIOUS_REVIEW)
        self.assertEqual(json.loads(doc)["status"], "PASS")

    def test_bare_echo_after_new_verdict_is_ignored(self):
        raw = (
            f"{REVIEW_JSON}\n\n"
            f"Previous review for reference:\n{PREVIOUS_REVIEW}"
        )
        doc = _review_json_document(raw, stale_document=PREVIOUS_REVIEW)
        self.assertEqual(json.loads(doc)["status"], "PASS")

    def test_echo_only_output_raises_instead_of_returning_stale_verdict(self):
        raw = f"```json\n{PREVIOUS_REVIEW}```"
        with self.assertRaises(json.JSONDecodeError):
            _review_json_document(raw, stale_document=PREVIOUS_REVIEW)

    def test_reissued_verdict_with_different_formatting_is_accepted(self):
        # Only a byte-identical echo is rejected; a reviewer genuinely
        # re-issuing the same verdict in its own formatting must still count.
        reissued = json.dumps(json.loads(PREVIOUS_REVIEW))
        doc = _review_json_document(
            f"```json\n{reissued}\n```", stale_document=PREVIOUS_REVIEW
        )
        self.assertEqual(json.loads(doc)["status"], "CHANGES_REQUESTED")

    def test_envelope_result_echoing_previous_review_fails_closed(self):
        # The echo inside the envelope is rejected, so the unparsed envelope
        # passes through and fails review validation upstream instead of
        # silently reviving the stale verdict.
        envelope = {"type": "result", "result": f"```json\n{PREVIOUS_REVIEW}```"}
        payload = _parse_review_document(
            json.dumps(envelope), stale_document=PREVIOUS_REVIEW
        )
        self.assertEqual(payload.get("type"), "result")
        self.assertNotIn("status", payload)


class UnwrapAgentEnvelopeTests(unittest.TestCase):
    # `claude -p --output-format json` emits {"type": "result", "result": "<text>", ...}

    def test_unwraps_claude_json_envelope(self):
        envelope = {"type": "result", "result": f"prose first\n```json\n{REVIEW_JSON}\n```"}
        raw = json.dumps(envelope)
        payload = _unwrap_agent_envelope(json.loads(_review_json_document(raw)))
        self.assertEqual(payload["status"], "PASS")

    def test_review_document_passes_through(self):
        payload = json.loads(REVIEW_JSON)
        self.assertIs(_unwrap_agent_envelope(payload), payload)

    def test_envelope_with_non_json_result_passes_through(self):
        envelope = {"type": "result", "result": "no json here"}
        self.assertEqual(_unwrap_agent_envelope(envelope), envelope)


class ReviewTriageInterpretationTests(unittest.TestCase):
    # Regression guard: triage must keep flowing through the same tolerant
    # extraction pipeline as reviews (prose framing, fences, claude envelope),
    # not a bare json.loads.

    def _triage_config(self, tmp: Path) -> RunnerConfig:
        return RunnerConfig(
            path=tmp / ".agent-runner.json",
            data={},
            agents={},
            roles={},
            role_fallbacks={},
            review_triage=ReviewTriageConfig(
                simple="simple-reviewer", complex="complex-reviewer"
            ),
            plan_path="docs/plan.md",
            checks=[],
            max_retries_per_phase=0,
            auto_fix_attempts=0,
            timeout_minutes=1,
            auto_commit=False,
            allow_dirty=False,
            base_branch="main",
            merge_on_close=False,
            merge_strategy="squash",
            warnings=[],
        )

    def _triage_result(self, tmp: Path, output: str) -> JobResult:
        output_path = tmp / "triage-output.txt"
        output_path.write_text(output, encoding="utf-8")
        return JobResult(
            job_id=1,
            status="SUCCEEDED",
            exit_code=0,
            log_path=tmp / "triage.log",
            prompt_path=None,
            output_path=output_path,
            error=None,
        )

    def _interpret(self, output: str):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            return _interpret_review_triage(
                self._triage_config(tmp), self._triage_result(tmp, output)
            )

    def test_prose_framed_triage_json(self):
        triage = self._interpret(
            'The diff is a one-line rename, so:\n\n{"tier": "simple"}\n'
        )
        self.assertEqual(triage.tier, "simple")
        self.assertEqual(triage.profile_name, "simple-reviewer")

    def test_fenced_triage_json(self):
        triage = self._interpret(
            'Verdict below.\n```json\n{"tier": "complex"}\n```'
        )
        self.assertEqual(triage.tier, "complex")
        self.assertEqual(triage.profile_name, "complex-reviewer")

    def test_enveloped_triage_json(self):
        envelope = {"type": "result", "result": '{"tier": "simple"}'}
        triage = self._interpret(json.dumps(envelope))
        self.assertEqual(triage.tier, "simple")
        self.assertEqual(triage.profile_name, "simple-reviewer")

    def test_unparseable_triage_output_falls_back_to_complex(self):
        triage = self._interpret("no json at all")
        self.assertEqual(triage.tier, "complex")
        self.assertEqual(triage.profile_name, "complex-reviewer")
        self.assertIn("invalid triage JSON", triage.reason)


if __name__ == "__main__":
    unittest.main()
