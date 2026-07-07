import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import SAMPLE_CONFIG, project_slug, strip_json_comments
from agent_runner.plan import parse_plan_file
from agent_runner.storage import (
    connect_db,
    create_job,
    create_phase,
    create_plan,
    get_or_create_project,
    phase_log_dir,
)


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
        "## Phase 5: Test implementation\n"
        f"Status: {status}\n\n"
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
    auto_commit: bool = False,
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
    data["roleFallbacks"] = {}
    data["autoFixAttempts"] = 0
    data["checks"] = checks
    data["allowDirty"] = allow_dirty
    data["autoCommit"] = auto_commit
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_fake_agent(path: Path, *, exit_code: int = 0, create_file: bool = True) -> None:
    create_line = ""
    if create_file:
        create_line = "Path('generated.txt').write_text('created by fake coder\\n')"
    path.write_text(
        fr"""
import json
import re
import sys
from pathlib import Path

prompt = sys.argv[-1]
if "Review the staged phase work independently" in prompt:
    print(json.dumps({{
        "status": "PASS",
        "summary": "accepted",
        "blockingIssues": [],
        "nonBlockingIssues": [],
        "recommendedFixPrompt": ""
    }}))
    raise SystemExit(0)
if "Close the accepted phase" in prompt:
    phase_number = int(re.search(r"Phase (\d+):", prompt).group(1))
    plan = Path("docs/plan.md")
    text = plan.read_text(encoding="utf-8")
    text = re.sub(
        rf"(## Phase {{phase_number}}: [^\n]+\n)(?:Status: [A-Z_]+\n)?",
        rf"\1Status: COMPLETE\nEvidence: commit pending; checks passed\n",
        text,
        count=1,
    )
    plan.write_text(text, encoding="utf-8")
    handoff = Path(f".acc/phases/docs-plan.md/phase-{{phase_number:02d}}-handoff.md")
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "## Completed Work\nDone.\n\n"
        "## Decisions\nNone.\n\n"
        "## Files Changed\ndocs/plan.md\n\n"
        "## Checks Run\nConfigured checks passed.\n\n"
        "## Open Risks\nNone.\n\n"
        "## Next-Phase Context\nContinue.\n",
        encoding="utf-8",
    )
    print("fake closer completed")
    raise SystemExit(0)
if "Phase 5: Test implementation" not in prompt:
    print("missing phase prompt", file=sys.stderr)
    raise SystemExit(12)
{create_line}
print("fake coder completed")
raise SystemExit({exit_code})
""".lstrip(),
        encoding="utf-8",
    )


def write_sleeping_agent(path: Path) -> None:
    path.write_text(
        """
import signal
import sys
import time
from pathlib import Path

def handle_term(signum, frame):
    Path("agent-terminated.txt").write_text("terminated\\n", encoding="utf-8")
    raise SystemExit(143)

signal.signal(signal.SIGTERM, handle_term)
Path("agent-started.txt").write_text("started\\n", encoding="utf-8")
while True:
    print("tick", flush=True)
    time.sleep(0.1)
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
    def test_implement_stages_untracked_file_and_passes_review_to_closing(self):
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
            self.assertIn("plan complete", result.stderr)
            staged = subprocess.run(
                ["git", "diff", "--staged", "--name-only"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.splitlines()
            self.assertIn("generated.txt", staged)
            self.assertEqual(phase_row(home, repo)["status"], "COMPLETE")

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

            self.assertEqual(result.returncode, 1, result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertEqual(phase["retry_count"], 3)
            with connect_db(home) as db:
                fix_job = db.execute(
                    """
                    SELECT * FROM jobs
                    WHERE phase_id = ? AND type = 'FIX'
                    ORDER BY id
                    LIMIT 1
                    """,
                    (phase["id"],),
                ).fetchone()
            self.assertIsNotNone(fix_job)
            self.assertEqual(fix_job["status"], "SUCCEEDED")
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

    def test_sigint_during_agent_job_marks_failed_without_thread_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "sleeping_agent.py"
            repo.mkdir()
            git_init(repo)
            write_sleeping_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)
            env = os.environ.copy()
            env["AGENT_RUNNER_HOME"] = str(home)
            env["PYTHONPATH"] = str(ROOT)

            proc = subprocess.Popen(
                [sys.executable, "-m", "agent_runner", "run"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.time() + 10
                while time.time() < deadline and not (repo / "agent-started.txt").exists():
                    time.sleep(0.05)
                self.assertTrue((repo / "agent-started.txt").exists())

                proc.send_signal(signal.SIGINT)
                stdout, stderr = proc.communicate(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)

            self.assertEqual(proc.returncode, 130, stderr + stdout)
            self.assertIn("interrupted; lock released", stderr)
            self.assertNotIn("Exception in thread", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertFalse(
                (home / "locks" / f"{project_slug(repo)}.lock").exists()
            )
            self.assertTrue((repo / "agent-terminated.txt").exists())
            with connect_db(home) as db:
                job = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(job["status"], "FAILED")
            self.assertEqual(job["error"], "interrupted")

    def test_implementing_resume_skips_dirty_gate_after_orphan_reap(self):
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
            parsed_plan = parse_plan_file(repo, "docs/plan.md")
            parsed_phase = parsed_plan.phases[0]
            log_dir = phase_log_dir(
                home / "logs",
                project_slug=project_slug(repo),
                plan_path="docs/plan.md",
                phase_number=parsed_phase.phase_number,
            )
            with connect_db(home) as db:
                project = get_or_create_project(
                    db, slug=project_slug(repo), repo_path=repo
                )
                plan = create_plan(
                    db,
                    project_id=project["id"],
                    path="docs/plan.md",
                    content_hash=parsed_plan.content_hash,
                )
                phase = create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=parsed_phase.phase_number,
                    title=parsed_phase.title,
                    content_hash=parsed_phase.content_hash,
                    log_dir=log_dir,
                )
                create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="IMPLEMENT",
                    status="RUNNING",
                )
            (repo / "leftover-from-crash.txt").write_text("partial\n", encoding="utf-8")

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("reaped 1 orphaned job", result.stderr)
            self.assertEqual(phase_row(home, repo)["status"], "COMPLETE")

    def test_checking_resume_runs_checks_without_implement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "fake_agent.py"
            repo.mkdir()
            git_init(repo)
            write_fake_agent(script)
            write_plan(repo, status="CHECKING")
            write_config(repo, script, checks=[f"{shlex.quote(sys.executable)} -c \"print('checks only')\""])
            commit_all(repo)

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("plan complete", result.stderr)
            self.assertFalse((repo / "generated.txt").exists())
            with connect_db(home) as db:
                job_types = [
                    row["type"]
                    for row in db.execute("SELECT type FROM jobs ORDER BY id").fetchall()
                ]
            self.assertEqual(job_types, ["RUN_CHECKS", "REVIEW", "CLOSE_PHASE"])
            self.assertEqual(phase_row(home, repo)["status"], "COMPLETE")

    def test_allow_dirty_warns_and_stages_only_new_implementation_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "fake_agent.py"
            repo.mkdir()
            git_init(repo)
            write_fake_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[], allow_dirty=True)
            commit_all(repo)
            (repo / "dirty.txt").write_text("pre-existing\n", encoding="utf-8")

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning: worktree is dirty; continuing", result.stderr)
            staged = subprocess.run(
                ["git", "diff", "--staged", "--name-only"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.splitlines()
            self.assertIn("generated.txt", staged)
            self.assertNotIn("dirty.txt", staged)
            porcelain = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertIn("?? dirty.txt", porcelain)

    def test_terminal_status_branches_have_expected_exit_codes(self):
        cases = [
            ("CLOSING", 0, "plan complete"),
            ("BLOCKED", 1, "inspect status before rerunning"),
        ]
        for phase_status, expected_code, expected_message in cases:
            with self.subTest(phase_status=phase_status):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = root / "repo"
                    home = root / "home"
                    script = root / "fake_agent.py"
                    repo.mkdir()
                    git_init(repo)
                    write_fake_agent(script)
                    write_plan(repo, status=phase_status)
                    write_config(repo, script, checks=[])
                    commit_all(repo)

                    result = run_cli(repo, home, "run")

                    self.assertEqual(result.returncode, expected_code, result.stderr)
                    self.assertIn(expected_message, result.stderr)
                    with connect_db(home) as db:
                        job_count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                    expected_jobs = 1 if phase_status == "CLOSING" else 0
                    self.assertEqual(job_count, expected_jobs)


if __name__ == "__main__":
    unittest.main()
