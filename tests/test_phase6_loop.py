import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import SAMPLE_CONFIG, strip_json_comments
from agent_runner.storage import connect_db


ROOT = Path(__file__).resolve().parents[1]


def run_cli(cwd: Path, home: Path, *args: str, extra_env: Optional[dict[str, str]] = None):
    env = os.environ.copy()
    env["AGENT_RUNNER_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agent_runner", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def commit_all(repo: Path, message: str = "baseline") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-qm",
            message,
        ],
        cwd=repo,
        check=True,
    )


def write_plan(repo: Path, *, status: str = "PENDING") -> None:
    plan_path = repo / "docs" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "## Phase 6: REVIEW and FIX convergence loop\n"
        f"Status: {status}\n\n"
        "Create generated.txt and converge through review.\n\n"
        "Acceptance Criteria:\n"
        "- generated.txt exists.\n",
        encoding="utf-8",
    )


def write_config(
    repo: Path,
    agent_script: Path,
    *,
    checks: list[str],
    max_retries: int = 3,
) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    data["agents"] = {
        "fake": {
            "command": sys.executable,
            "promptArgs": [str(agent_script)],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
    }
    data["roles"] = {"coder": "fake", "reviewer": "fake"}
    data["checks"] = checks
    data["maxRetriesPerPhase"] = max_retries
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_phase6_agent(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
import sys
from pathlib import Path

prompt = sys.argv[-1]
mode = os.environ.get("AGENT_MODE", "PASS")
trace_dir = Path(os.environ["TRACE_DIR"])
trace_dir.mkdir(parents=True, exist_ok=True)

if "Review the staged phase work independently" in prompt:
    review_number = len(list(trace_dir.glob("review-*.md"))) + 1
    (trace_dir / f"review-{review_number}.md").write_text(prompt, encoding="utf-8")
    if mode == "GARBAGE":
        print("not json from reviewer")
        raise SystemExit(0)
    if mode == "BLOCKED":
        print(json.dumps({
            "status": "BLOCKED",
            "summary": "reviewer cannot proceed",
            "blockingIssues": ["external blocker"],
            "nonBlockingIssues": ["non-gating note"],
            "recommendedFixPrompt": ""
        }))
        raise SystemExit(0)
    if mode == "REVIEW_FIX" and not Path("fix-marker.txt").exists():
        print(json.dumps({
            "status": "CHANGES_REQUESTED",
            "summary": "fix marker is missing",
            "blockingIssues": ["Create fix-marker.txt"],
            "nonBlockingIssues": ["Should Fix: tidy wording"],
            "recommendedFixPrompt": "Create the marker"
        }))
        raise SystemExit(0)
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "blockingIssues": [],
        "nonBlockingIssues": ["Nice to Have: optional cleanup"],
        "recommendedFixPrompt": ""
    }))
    raise SystemExit(0)

if "Fix only" in prompt:
    (trace_dir / f"fix-{len(list(trace_dir.glob('fix-*.md'))) + 1}.md").write_text(
        prompt,
        encoding="utf-8",
    )
    Path("fix-marker.txt").write_text("fixed\n", encoding="utf-8")
    print("fake fixer completed")
    raise SystemExit(0)

Path("generated.txt").write_text("created\n", encoding="utf-8")
print("fake coder completed")
""".lstrip(),
        encoding="utf-8",
    )


def phase_row(home: Path, repo: Path):
    with connect_db(home) as db:
        return db.execute(
            """
            SELECT phases.*
            FROM phases
            JOIN projects ON projects.id = phases.project_id
            WHERE projects.repo_path = ?
            """,
            (str(repo.resolve()),),
        ).fetchone()


def jobs(home: Path, phase_id: int):
    with connect_db(home) as db:
        return db.execute(
            "SELECT * FROM jobs WHERE phase_id = ? ORDER BY id", (phase_id,)
        ).fetchall()


class Phase6LoopTests(unittest.TestCase):
    def test_review_pass_advances_to_closing_without_coder_output_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; assert Path('generated.txt').exists()\""
                ],
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("review passed", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "CLOSING")
            self.assertEqual(phase["retry_count"], 0)
            review_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            self.assertIn("git diff --staged", review_prompt)
            self.assertNotIn("fake coder completed", review_prompt)

    def test_review_changes_requested_runs_fix_then_reruns_checks_and_rereview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; assert Path('generated.txt').exists()\""
                ],
            )
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "REVIEW_FIX"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "CLOSING")
            self.assertEqual(phase["retry_count"], 1)
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual(
                [(job["type"], job["trigger"]) for job in phase_jobs],
                [
                    ("IMPLEMENT", None),
                    ("RUN_CHECKS", None),
                    ("REVIEW", None),
                    ("FIX", "review"),
                    ("RUN_CHECKS", None),
                    ("REVIEW", None),
                ],
            )
            first_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            second_prompt = (trace / "review-2.md").read_text(encoding="utf-8")
            fix_prompt = (trace / "fix-1.md").read_text(encoding="utf-8")
            self.assertNotIn("fake coder completed", first_prompt)
            self.assertIn("Previous review.json", second_prompt)
            self.assertIn(
                "Verify these blocking issues are resolved; only new Blocking findings may block.",
                second_prompt,
            )
            self.assertIn("Create fix-marker.txt", second_prompt)
            self.assertIn("Create fix-marker.txt", fix_prompt)
            self.assertNotIn("Should Fix: tidy wording", fix_prompt)

    def test_review_blocked_stops_without_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "BLOCKED"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(phase_row(home, repo)["status"], "BLOCKED")
            phase_jobs = jobs(home, phase_row(home, repo)["id"])
            self.assertNotIn("FIX", [job["type"] for job in phase_jobs])

    def test_checks_fix_cycle_exhausts_retries_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"import sys; print('still failing'); sys.exit(2)\""
                ],
                max_retries=2,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 1)
            self.assertIn("retries exhausted", result.stderr)
            self.assertIn("outstanding checks blockers", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertEqual(phase["retry_count"], 2)
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("FIX"), 2)
            self.assertEqual([job["type"] for job in phase_jobs].count("RUN_CHECKS"), 3)

    def test_reviewer_non_json_blocks_and_preserves_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "GARBAGE"},
            )

            self.assertEqual(result.returncode, 1)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            review_log = Path(phase["log_dir"]) / "review.log"
            self.assertIn("not json from reviewer", review_log.read_text(encoding="utf-8"))
            self.assertFalse((Path(phase["log_dir"]) / "review.json").exists())


if __name__ == "__main__":
    unittest.main()
