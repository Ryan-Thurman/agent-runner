import json
import unittest

from agent_runner.phase_loop import _review_json_document, _unwrap_agent_envelope

REVIEW_JSON = json.dumps(
    {
        "status": "PASS",
        "summary": "looks good",
        "findings": {"blocking": [], "shouldFix": [], "nitpick": []},
        "recommendedFixPrompt": "",
    },
    indent=2,
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


if __name__ == "__main__":
    unittest.main()
