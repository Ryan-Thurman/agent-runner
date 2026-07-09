import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_runner.diffs import MARKER, elide_diff
from agent_runner.jobs import MAX_PROMPT_BYTES, _bounded_prompt
from agent_runner.phase_loop import _review_prompt
from agent_runner.plan import ParsedPhase


def _section(path: str, body_lines: int, line: str = "+x") -> str:
    body = "\n".join(f"{line}{index}" for index in range(body_lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{body_lines} +1,{body_lines} @@\n"
        f"{body}\n"
    )


class ElideDiffTest(unittest.TestCase):
    def test_empty_and_blank_inputs_pass_through(self) -> None:
        self.assertEqual(elide_diff(""), "")
        self.assertEqual(elide_diff("\n\n"), "\n\n")

    def test_small_diff_is_unchanged(self) -> None:
        diff = _section("src/app.py", 3)
        self.assertEqual(elide_diff(diff), diff)

    def test_oversized_section_keeps_header_and_elides_body(self) -> None:
        diff = _section("bundle.js", 500) + _section("src/app.py", 3)
        result = elide_diff(diff, max_file_bytes=1024)

        # Every touched path survives, so the reviewer still sees the shape.
        self.assertIn("diff --git a/bundle.js b/bundle.js", result)
        self.assertIn("--- a/bundle.js", result)
        self.assertIn("+++ b/bundle.js", result)
        self.assertIn("@@ elided @@", result)
        self.assertIn(f"{MARKER} elided 501 diff lines", result)

        # The bundle body is gone; the hand-written file is untouched.
        self.assertNotIn("+x499", result)
        self.assertIn("+x2\n", result)
        self.assertIn("diff --git a/src/app.py b/src/app.py", result)

    def test_oversized_section_without_a_hunk_is_kept(self) -> None:
        # A rename or "Binary files differ" section has no body to drop, so
        # eliding it would lose the only information it carries.
        diff = (
            "diff --git a/img.png b/img.png\n"
            "index 1111111..2222222 100644\n"
            "Binary files a/img.png and b/img.png differ\n"
        )
        self.assertEqual(elide_diff(diff, max_file_bytes=1), diff)

    def test_body_line_resembling_a_file_header_does_not_split(self) -> None:
        # A patch that patches a patch: the inner header is prefixed with '+'.
        diff = (
            "diff --git a/fixture.patch b/fixture.patch\n"
            "--- a/fixture.patch\n"
            "+++ b/fixture.patch\n"
            "@@ -1,1 +1,2 @@\n"
            "+diff --git a/inner.py b/inner.py\n"
            " diff --git a/other.py b/other.py\n"
        )
        self.assertEqual(elide_diff(diff), diff)

    def test_total_budget_drops_trailing_sections_and_says_so(self) -> None:
        diff = "".join(_section(f"file{index}.py", 4) for index in range(20))
        result = elide_diff(diff, max_file_bytes=10_000, max_total_bytes=600)

        self.assertLessEqual(len(result.encode("utf-8")), 800)
        self.assertIn("diff --git a/file0.py b/file0.py", result)
        self.assertNotIn("diff --git a/file19.py b/file19.py", result)
        self.assertIn(f"{MARKER} dropped the last", result)
        self.assertIn("of 20 file sections", result)

    def test_total_budget_keeps_at_least_one_section(self) -> None:
        diff = _section("a.py", 4) + _section("b.py", 4)
        result = elide_diff(diff, max_file_bytes=10_000, max_total_bytes=1)

        self.assertIn("diff --git a/a.py b/a.py", result)
        self.assertIn(f"{MARKER} dropped the last 1 of 2 file sections", result)

    def test_realistic_bundle_diff_lands_under_argv_budget(self) -> None:
        # The failure this module exists for: one rebuilt minified bundle on a
        # single ~420 KB line, next to the change a reviewer actually reads.
        bundle = (
            "diff --git a/dist/index.js b/dist/index.js\n"
            "--- a/dist/index.js\n"
            "+++ b/dist/index.js\n"
            "@@ -1 +1 @@\n"
            f"-{'a' * 420_000}\n"
            f"+{'b' * 420_000}\n"
        )
        result = elide_diff(bundle + _section("src/app.py", 20))

        self.assertLess(len(result.encode("utf-8")), MAX_PROMPT_BYTES)
        self.assertNotIn("aaaa", result)
        self.assertIn("@@ elided @@", result)
        self.assertIn("+x19", result)


class BoundedPromptTest(unittest.TestCase):
    def test_small_prompt_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-prompt.md"
            self.assertEqual(_bounded_prompt("hello", path), "hello")

    def test_oversized_prompt_is_truncated_and_points_at_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-prompt.md"
            result = _bounded_prompt("z" * (MAX_PROMPT_BYTES + 5_000), path)

            self.assertIn("prompt truncated", result)
            self.assertIn(str(path), result)
            self.assertLess(len(result.encode("utf-8")), MAX_PROMPT_BYTES + 500)

    def test_truncation_does_not_split_a_multibyte_character(self) -> None:
        # A 3-byte char straddling the cut must be dropped, not corrupted.
        prompt = "世" * MAX_PROMPT_BYTES
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-prompt.md"
            result = _bounded_prompt(prompt, path)

            self.assertNotIn("�", result)
            result.encode("utf-8").decode("utf-8")


class ReviewPromptTest(unittest.TestCase):
    def test_review_prompt_bounds_a_staged_minified_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for command in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
                subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
            (repo / "bundle.js").write_text("x" * 600_000, encoding="utf-8")
            (repo / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)

            phase = ParsedPhase(
                phase_number=7,
                title="Quantitative architecture metrics",
                status="TODO",
                content="Add Lakos depth.",
                content_hash="abc",
            )
            prompt = _review_prompt(repo, phase, repo, phase={}, use_published_diff=False)

            self.assertLess(len(prompt.encode("utf-8")), MAX_PROMPT_BYTES)
            self.assertIn("diff --git a/bundle.js b/bundle.js", prompt)
            self.assertIn("@@ elided @@", prompt)
            self.assertIn("def handler():", prompt)


if __name__ == "__main__":
    unittest.main()
