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


def write_plan(repo: Path) -> None:
    plan_path = repo / "docs" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "## Phase 5: Test implementation\n"
        "Status: PENDING\n\n"
        "Create the generated marker file.\n\n"
        "Acceptance Criteria:\n"
        "- Marker file exists.\n",
        encoding="utf-8",
    )


def write_config(
    repo: Path,
    agent_script: Path,
    *,
    checks: list[str],
    allow_dirty: bool = False,
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
    data["allowDirty"] = allow_dirty
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_fake_agent(path: Path, *, exit_code: int = 0, create_file: bool = True) -> None:
    create_line = ""
    if create_file:
        create_line = "Path('generated.txt').write_text('created by fake coder\\n')"
    path.write_text(
        f"""
import sys
from pathlib import Path

prompt = sys.argv[-1]
if "Phase 5: Test implementation" not in prompt:
    print("missing phase prompt", file=sys.stderr)
    raise SystemExit(12)
{create_line}
print("fake coder completed")
raise SystemExit({exit_code})
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


class Phase5LoopTests(unittest.TestCase):
    def test_implement_stages_untracked_file_and_passes_checks_to_reviewing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "fake_agent.py"
            repo.mkdir()
            git_init(repo)
            write_fake_agent(script)
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

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("checks passed", result.stderr)
            staged = subprocess.run(
                ["git", "diff", "--staged", "--name-only"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.splitlines()
            self.assertIn("generated.txt", staged)
            self.assertEqual(phase_row(home, repo)["status"], "REVIEWING")

    def test_coder_failure_blocks_phase_and_status_explains_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "fake_agent.py"
            repo.mkdir()
            git_init(repo)
            write_fake_agent(script, exit_code=7, create_file=False)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(repo, home, "run")
            status = run_cli(repo, home, "status")

            self.assertEqual(result.returncode, 1)
            self.assertEqual(phase_row(home, repo)["status"], "BLOCKED")
            self.assertIn("IMPLEMENT failed", status.stderr)
            self.assertIn("exit code 7", status.stderr)

    def test_checks_failure_enqueues_fix_with_check_output_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "fake_agent.py"
            repo.mkdir()
            git_init(repo)
            write_fake_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"import sys; print('bad check output'); sys.exit(3)\""
                ],
            )
            commit_all(repo)

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "FIXING")
            self.assertEqual(phase["retry_count"], 1)
            with connect_db(home) as db:
                fix_job = db.execute(
                    """
                    SELECT * FROM jobs
                    WHERE phase_id = ? AND type = 'FIX'
                    """,
                    (phase["id"],),
                ).fetchone()
            self.assertIsNotNone(fix_job)
            self.assertEqual(fix_job["status"], "PENDING")
            self.assertEqual(fix_job["trigger"], "checks")
            prompt_text = Path(fix_job["prompt_path"]).read_text(encoding="utf-8")
            self.assertIn("bad check output", prompt_text)

    def test_dirty_repo_blocks_before_any_job_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "fake_agent.py"
            repo.mkdir()
            git_init(repo)
            write_fake_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 1)
            self.assertIn("dirty worktree", result.stderr)
            with connect_db(home) as db:
                job_count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            self.assertEqual(job_count, 0)
            self.assertEqual(phase_row(home, repo)["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
