import unittest

from agent_runner.phase_loop import extract_pr_number, format_pr_url


class PhaseLoopPrFormattingTests(unittest.TestCase):
    def test_extract_pr_number_from_github_pull_url(self):
        self.assertEqual(
            extract_pr_number("https://github.com/example/project/pull/12"),
            "12",
        )
        self.assertEqual(
            format_pr_url("https://github.com/example/project/pull/12"),
            "PR #12 (https://github.com/example/project/pull/12)",
        )

    def test_extract_pr_number_returns_none_without_trailing_number(self):
        self.assertIsNone(
            extract_pr_number("https://github.com/example/project/pull/not-a-number")
        )
        self.assertEqual(
            format_pr_url("https://github.com/example/project/pull/not-a-number"),
            "https://github.com/example/project/pull/not-a-number",
        )

    def test_extract_pr_number_returns_none_for_empty_or_none_url(self):
        self.assertIsNone(extract_pr_number(""))
        self.assertIsNone(extract_pr_number(None))


if __name__ == "__main__":
    unittest.main()
