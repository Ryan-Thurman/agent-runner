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


def write_config(repo: Path, *, plan_verify: Optional[list[str]] = None) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    data["planPath"] = "docs/plan.md"
    data["checks"] = []
    data["agents"] = {
        "fake": {
            "command": sys.executable,
            "promptArgs": ["-c", "print('unused')"],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
    }
    data["roles"] = {"coder": "fake", "reviewer": "fake"}
    data["roleFallbacks"] = {}
    data.pop("reviewTriage", None)
    data.pop("presets", None)
    data["autoFixAttempts"] = 0
    data["autoCommit"] = False
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    if plan_verify is None:
        data.pop("planVerify", None)
    else:
        data["planVerify"] = plan_verify
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_plan(repo: Path, *, status_line: str = "Status: PENDING\n") -> None:
    plan = repo / "docs" / "plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "# Test Plan\n\n"
        "Context for the plan.\n\n"
        "## Phase 1: Validate something\n"
        f"{status_line}"
        "\n"
        "Acceptance Criteria:\n"
        "- The verifier accepts this plan.\n",
        encoding="utf-8",
    )


def write_verify_script(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
from pathlib import Path

keys = [
    "AGENT_RUNNER_REPO_ROOT",
    "AGENT_RUNNER_PLAN_PATH",
    "AGENT_RUNNER_PLAN_ABS_PATH",
    "AGENT_RUNNER_PLAN_PHASE_COUNT",
    "AGENT_RUNNER_PLAN_HASH",
]
payload = {key: os.environ[key] for key in keys}
plan = Path(payload["AGENT_RUNNER_PLAN_ABS_PATH"])
if not plan.is_file():
    raise SystemExit("plan file env did not point at a file")
Path(os.environ["TRACE_PATH"]).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
print("verified plan")
""".lstrip(),
        encoding="utf-8",
    )


class PlanValidateCommandTests(unittest.TestCase):
    def test_plan_validate_runs_configured_verify_without_registering_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "verify_plan.py"
            trace = root / "trace.json"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_verify_script(script)
            write_config(
                repo,
                plan_verify=[
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
                ],
            )

            result = run_cli(
                repo,
                home,
                "plan-validate",
                extra_env={"TRACE_PATH": str(trace), "AGENT_RUNNER_LIVE_LOGS": "0"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("starting PLAN_VERIFY job", result.stderr)
            self.assertIn("plan validated: docs/plan.md with 1 phase(s)", result.stderr)
            payload = json.loads(trace.read_text(encoding="utf-8"))
            self.assertEqual(payload["AGENT_RUNNER_PLAN_PATH"], "docs/plan.md")
            self.assertEqual(payload["AGENT_RUNNER_PLAN_PHASE_COUNT"], "1")
            self.assertTrue(payload["AGENT_RUNNER_PLAN_HASH"])
            self.assertEqual(Path(payload["AGENT_RUNNER_REPO_ROOT"]), repo.resolve())

            with connect_db(home) as db:
                jobs = db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
                plans = db.execute("SELECT * FROM plans ORDER BY id").fetchall()
                events = db.execute("SELECT * FROM events ORDER BY id").fetchall()

            self.assertEqual([job["type"] for job in jobs], ["PLAN_VERIFY"])
            self.assertEqual(jobs[0]["phase_id"], None)
            self.assertEqual(plans, [])
            self.assertEqual(events[-1]["event_type"], "plan.validated")

    def test_plan_validate_accepts_one_shot_verify_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "verify_plan.py"
            trace = root / "trace.json"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_verify_script(script)
            write_config(repo, plan_verify=None)

            result = run_cli(
                repo,
                home,
                "plan-validate",
                "--verify",
                f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
                extra_env={"TRACE_PATH": str(trace), "AGENT_RUNNER_LIVE_LOGS": "0"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(trace.exists())
            self.assertIn("plan validated: docs/plan.md with 1 phase(s)", result.stderr)

    def test_plan_validate_reports_verify_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_config(
                repo,
                plan_verify=[
                    f"{shlex.quote(sys.executable)} -c "
                    f"{shlex.quote('import sys; print(\"bad plan\", file=sys.stderr); sys.exit(7)')}"
                ],
            )

            result = run_cli(
                repo,
                home,
                "plan-validate",
                extra_env={"AGENT_RUNNER_LIVE_LOGS": "0"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("PLAN_VERIFY job", result.stderr)
            self.assertIn("plan verification failed", result.stderr)
            with connect_db(home) as db:
                jobs = db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
                events = db.execute("SELECT * FROM events ORDER BY id").fetchall()

            self.assertEqual(jobs[0]["status"], "FAILED")
            self.assertEqual(jobs[0]["exit_code"], 7)
            self.assertEqual(events[-1]["event_type"], "plan_verify.failed")

    def test_plan_validate_without_verify_runs_structural_validation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_config(repo, plan_verify=None)

            result = run_cli(repo, home, "plan-validate")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("plan parsed: docs/plan.md with 1 phase(s)", result.stderr)
            self.assertIn("only structural validation ran", result.stderr)

    def test_plan_validate_rejects_malformed_or_escaping_plan_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_plan(repo, status_line="")
            write_config(repo, plan_verify=[])

            result = run_cli(repo, home, "plan-validate")

            self.assertEqual(result.returncode, 1)
            self.assertIn("phase 1 is missing a Status marker", result.stderr)

            escaping = run_cli(repo, home, "plan-validate", "--plan", "../outside.md")

            self.assertEqual(escaping.returncode, 1)
            self.assertIn("plan path escapes repository", escaping.stderr)


if __name__ == "__main__":
    unittest.main()
