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
        "## Phase 9: Auto-fix blocked phase\n"
        "Status: PENDING\n\n"
        "Create generated.txt and make checks pass.\n\n"
        "Acceptance Criteria:\n"
        "- fixed.txt exists.\n",
        encoding="utf-8",
    )


def write_config(
    repo: Path,
    agent_script: Path,
    *,
    auto_fix_attempts: Optional[int],
    include_fixer: bool = True,
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
    if include_fixer:
        data["roles"]["fixer"] = "fake"
    data["roleFallbacks"] = {}
    data["checks"] = [
        f"{shlex.quote(sys.executable)} -c "
        "\"from pathlib import Path; assert Path('fixed.txt').exists()\""
    ]
    data["maxRetriesPerPhase"] = 0
    if auto_fix_attempts is None:
        data.pop("autoFixAttempts", None)
    else:
        data["autoFixAttempts"] = auto_fix_attempts
    data["autoCommit"] = False
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_autofix_agent(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
import re
import sys
from pathlib import Path

prompt = sys.argv[-1]
trace_dir = Path(os.environ["TRACE_DIR"])
trace_dir.mkdir(parents=True, exist_ok=True)

if "Fix the underlying problem that blocked this phase" in prompt:
    attempt = len(list(trace_dir.glob("autofix-*.md"))) + 1
    (trace_dir / f"autofix-{attempt}.md").write_text(prompt, encoding="utf-8")
    if os.environ.get("AUTOFIX_MODE") == "NOOP":
        print("auto-fix intentionally did nothing")
        raise SystemExit(0)
    Path("fixed.txt").write_text("fixed by auto-fix\n", encoding="utf-8")
    print("auto-fix completed")
    raise SystemExit(0)

if "Review the staged phase work independently" in prompt:
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "blockingIssues": [],
        "nonBlockingIssues": [],
        "recommendedFixPrompt": ""
    }))
    raise SystemExit(0)

if "Close the accepted phase" in prompt:
    phase_number = int(re.search(r"Phase (\d+):", prompt).group(1))
    plan = Path("docs/plan.md")
    text = plan.read_text(encoding="utf-8")
    text = re.sub(
        rf"(## Phase {phase_number}: [^\n]+\n)(?:Status: [A-Z_]+\n)?",
        rf"\1Status: COMPLETE\nEvidence: auto-fix checks passed\n",
        text,
        count=1,
    )
    plan.write_text(text, encoding="utf-8")
    handoff = Path(f".acc/phases/docs-plan.md/phase-{phase_number:02d}-handoff.md")
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "## Completed Work\nDone.\n\n"
        "## Decisions\nNone.\n\n"
        "## Files Changed\nfixed.txt\n\n"
        "## Checks Run\nConfigured checks passed.\n\n"
        "## Open Risks\nNone.\n\n"
        "## Next-Phase Context\nContinue.\n",
        encoding="utf-8",
    )
    print("fake closer completed")
    raise SystemExit(0)

Path("generated.txt").write_text("created by implement\n", encoding="utf-8")
print("fake implement completed")
raise SystemExit(0)
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


def events(home: Path, phase_id: int):
    with connect_db(home) as db:
        return db.execute(
            "SELECT * FROM events WHERE phase_id = ? ORDER BY id", (phase_id,)
        ).fetchall()


class AutofixLoopTests(unittest.TestCase):
    def test_autofix_unblocks_blocked_phase_and_continues_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "autofix_agent.py"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_config(repo, script, auto_fix_attempts=2)
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto-fix attempt 1/2 with profile fake", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual(
                [job["type"] for job in phase_jobs],
                [
                    "IMPLEMENT",
                    "RUN_CHECKS",
                    "AUTOFIX",
                    "RUN_CHECKS",
                    "REVIEW",
                    "CLOSE_PHASE",
                ],
            )
            autofix_prompt = (trace / "autofix-1.md").read_text(encoding="utf-8")
            self.assertIn("Phase 9: Auto-fix blocked phase", autofix_prompt)
            self.assertIn("retries exhausted", autofix_prompt)
            self.assertIn("Newest phase log tail", autofix_prompt)
            self.assertIn("Never invoke `autorun`, `agent-runner`", autofix_prompt)
            phase_events = events(home, phase["id"])
            autofix_events = [
                event for event in phase_events if event["event_type"] == "phase.autofix"
            ]
            self.assertEqual(len(autofix_events), 1)
            unblocked_events = [
                event for event in phase_events if event["event_type"] == "phase.unblocked"
            ]
            self.assertEqual(json.loads(unblocked_events[0]["data_json"])["to"], "CHECKING")

    def test_autofix_attempt_cap_leaves_phase_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "autofix_agent.py"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_config(repo, script, auto_fix_attempts=2)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AUTOFIX_MODE": "NOOP"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("auto-fix attempt 1/2 with profile fake", result.stderr)
            self.assertIn("auto-fix attempt 2/2 with profile fake", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("AUTOFIX"), 2)

    def test_autofix_disabled_or_missing_fixer_keeps_blocking_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "autofix_agent.py"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_config(
                repo,
                script,
                auto_fix_attempts=0,
                include_fixer=False,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 1)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertNotIn("auto-fix attempt", result.stderr)
            self.assertNotIn("AUTOFIX", [job["type"] for job in jobs(home, phase["id"])])

    def test_autofix_attempts_require_fixer_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "autofix_agent.py"
            repo.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_config(
                repo,
                script,
                auto_fix_attempts=1,
                include_fixer=False,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(root / "trace")})

            self.assertEqual(result.returncode, 1)
            self.assertIn("autoFixAttempts > 0 requires roles.fixer", result.stderr)


if __name__ == "__main__":
    unittest.main()
