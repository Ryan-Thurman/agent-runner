import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import time
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


def popen_cli(
    cwd: Path, home: Path, *args: str, extra_env: Optional[dict[str, str]] = None
):
    env = os.environ.copy()
    env["AGENT_RUNNER_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "agent_runner", *args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def commit_all(repo: Path, message: str = "baseline") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)


def write_plan(repo: Path) -> None:
    plan_path = repo / "docs" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "## Phase 1: Operator surface\n"
        "Status: PENDING\n\n"
        "Create generated.txt and close the phase.\n\n"
        "Acceptance Criteria:\n"
        "- generated.txt exists.\n",
        encoding="utf-8",
    )


def write_config(
    repo: Path,
    agent_script: Path,
    *,
    swapped_roles: bool = False,
) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    agents = {
        "coderish": {
            "command": sys.executable,
            "promptArgs": [str(agent_script)],
            "writeFlags": ["--coderish-write"],
            "readOnlyFlags": ["--coderish-readonly"],
            "outputCapture": "stdout",
        },
        "reviewish": {
            "command": sys.executable,
            "promptArgs": [str(agent_script)],
            "writeFlags": ["--reviewish-write"],
            "readOnlyFlags": ["--reviewish-readonly"],
            "outputCapture": "stdout",
        },
    }
    data["agents"] = agents
    if swapped_roles:
        data["roles"] = {"coder": "reviewish", "reviewer": "coderish"}
    else:
        data["roles"] = {"coder": "coderish", "reviewer": "reviewish"}
    data["roleFallbacks"] = {}
    data.pop("reviewTriage", None)
    data.pop("presets", None)
    data["autoFixAttempts"] = 0
    data["checks"] = [
        f"{shlex.quote(sys.executable)} -c "
        "\"from pathlib import Path; assert Path('generated.txt').exists()\""
    ]
    data["autoCommit"] = False
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_phase8_agent(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
import re
import sys
import time
from pathlib import Path

prompt = sys.argv[-1]
trace = Path(os.environ["TRACE_DIR"])
trace.mkdir(parents=True, exist_ok=True)

if (
    "Review the staged phase work independently" in prompt
    or "Review the published phase PR independently" in prompt
):
    (trace / "review-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "findings": {"blocking": [], "shouldFix": [], "nitpick": []}
    }))
    raise SystemExit(0)

if "Close the accepted phase" in prompt:
    (trace / "close-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    phase_number = int(re.search(r"Phase (\d+):", prompt).group(1))
    plan = Path("docs/plan.md")
    text = plan.read_text(encoding="utf-8")
    text = re.sub(
        rf"(## Phase {phase_number}: [^\n]+\n)(?:Status: [A-Z_]+\n)?",
        rf"\1Status: COMPLETE\nEvidence: operator tests passed\n",
        text,
        count=1,
    )
    plan.write_text(text, encoding="utf-8")
    handoff = Path(f".acc/phases/docs-plan.md/phase-{phase_number:02d}-handoff.md")
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "## Completed Work\nDone.\n\n"
        "## Decisions\nNone.\n\n"
        "## Files Changed\ngenerated.txt\n\n"
        "## Checks Run\nConfigured checks passed.\n\n"
        "## Open Risks\nNone.\n\n"
        "## Next-Phase Context\nContinue.\n",
        encoding="utf-8",
    )
    print("closed phase")
    raise SystemExit(0)

(trace / "implement-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
(trace / "implement-started").write_text(str(os.getpid()), encoding="utf-8")
wait_file = os.environ.get("WAIT_FILE")
if wait_file:
    while not Path(wait_file).exists():
        time.sleep(0.05)
if os.environ.get("SLOW_IMPLEMENT") == "1":
    time.sleep(60)
Path("generated.txt").write_text("created\n", encoding="utf-8")
count_path = trace / "implement-count.txt"
count = int(count_path.read_text(encoding="utf-8")) if count_path.exists() else 0
count_path.write_text(str(count + 1), encoding="utf-8")
print("implemented phase")
""".lstrip(),
        encoding="utf-8",
    )


def phase_rows(home: Path, repo: Path):
    db = connect_db(home)
    try:
        return db.execute(
            """
            SELECT phases.*
            FROM phases
            JOIN projects ON projects.id = phases.project_id
            WHERE projects.repo_path = ?
            ORDER BY phases.phase_number
            """,
            (str(repo.resolve()),),
        ).fetchall()
    finally:
        db.close()


def jobs(home: Path, repo: Path):
    db = connect_db(home)
    try:
        return db.execute(
            """
            SELECT jobs.*
            FROM jobs
            JOIN projects ON projects.id = jobs.project_id
            WHERE projects.repo_path = ?
            ORDER BY jobs.id
            """,
            (str(repo.resolve()),),
        ).fetchall()
    finally:
        db.close()


def wait_for_path(path: Path, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


class Phase8OperatorTests(unittest.TestCase):
    def test_kill9_mid_implement_reaps_orphan_and_reruns_implement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase8_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase8_agent(script)
            write_plan(repo)
            write_config(repo, script)
            commit_all(repo)

            proc = popen_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "SLOW_IMPLEMENT": "1"},
            )
            wait_for_path(trace / "implement-started")
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            proc.communicate(timeout=5)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("reaped 1 orphaned job", result.stderr)
            self.assertEqual(phase_rows(home, repo)[0]["status"], "COMPLETE")
            phase_jobs = jobs(home, repo)
            self.assertEqual(phase_jobs[0]["type"], "IMPLEMENT")
            self.assertEqual(phase_jobs[0]["status"], "FAILED")
            self.assertEqual(phase_jobs[0]["error"], "orphaned")
            self.assertEqual(phase_jobs[1]["type"], "IMPLEMENT")
            self.assertEqual(phase_jobs[1]["status"], "SUCCEEDED")

    def test_pause_during_running_job_stops_before_next_job_then_resume_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            release = root / "release"
            script = root / "phase8_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase8_agent(script)
            write_plan(repo)
            write_config(repo, script)
            commit_all(repo)

            proc = popen_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "WAIT_FILE": str(release)},
            )
            wait_for_path(trace / "implement-started")
            pause = run_cli(repo, home, "pause")
            release.write_text("go\n", encoding="utf-8")
            stdout, stderr = proc.communicate(timeout=10)

            self.assertEqual(pause.returncode, 0, pause.stderr)
            self.assertEqual(proc.returncode, 0, stderr + stdout)
            self.assertIn("project paused at a job boundary", stderr)
            self.assertEqual(phase_rows(home, repo)[0]["status"], "CHECKING")
            self.assertEqual([job["type"] for job in jobs(home, repo)], ["IMPLEMENT"])

            paused_run = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})
            resume = run_cli(repo, home, "resume")
            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(paused_run.returncode, 0, paused_run.stderr)
            self.assertIn("project is PAUSED", paused_run.stderr)
            self.assertIn("agent-runner resume", paused_run.stderr)
            self.assertEqual(resume.returncode, 0, resume.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("plan complete", result.stderr)
            self.assertEqual(phase_rows(home, repo)[0]["status"], "COMPLETE")
            self.assertEqual(
                [job["type"] for job in jobs(home, repo)],
                ["IMPLEMENT", "RUN_CHECKS", "REVIEW", "CLOSE_PHASE"],
            )

    def test_logs_prints_latest_phase_log_dir_and_tails_newest_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase8_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase8_agent(script)
            write_plan(repo)
            write_config(repo, script)
            commit_all(repo)
            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})
            self.assertEqual(result.returncode, 0, result.stderr)

            logs = run_cli(repo, home, "logs", "-n", "5")

            log_dir = phase_rows(home, repo)[0]["log_dir"]
            self.assertEqual(logs.returncode, 0, logs.stderr)
            self.assertEqual(logs.stdout.splitlines()[0], log_dir)
            self.assertIn("closed phase", logs.stdout)
            self.assertIn("tailing:", logs.stderr)

    def test_swapped_roles_still_complete_with_role_specific_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase8_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase8_agent(script)
            write_plan(repo)
            write_config(repo, script, swapped_roles=True)
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            implement_argv = json.loads((trace / "implement-argv.json").read_text())
            review_argv = json.loads((trace / "review-argv.json").read_text())
            close_argv = json.loads((trace / "close-argv.json").read_text())
            self.assertIn("--reviewish-write", implement_argv)
            self.assertIn("--coderish-readonly", review_argv)
            self.assertIn("--reviewish-write", close_argv)
            self.assertEqual(phase_rows(home, repo)[0]["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
