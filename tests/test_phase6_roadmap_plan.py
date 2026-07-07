import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import SAMPLE_CONFIG, strip_json_comments
from agent_runner.plan import parse_plan_file
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


def write_config(repo: Path, agent_script: Path) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    data["planPath"] = "docs/plan-roadmap.md"
    data["checks"] = []
    data["agents"] = {
        "fake": {
            "command": sys.executable,
            "promptArgs": [str(agent_script)],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
    }
    data["roles"] = {
        "coder": "fake",
        "reviewer": "fake",
        "planner": "fake",
    }
    data["roleFallbacks"] = {}
    data.pop("reviewTriage", None)
    data["autoFixAttempts"] = 0
    data["autoCommit"] = False
    data["allowDirty"] = True
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_roadmap(repo: Path) -> None:
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "roadmap.md").write_text(
        "# Roadmap\n\n"
        "## Completed Capability Areas\n\n"
        "- Already done.\n\n"
        "## Recommended Next Roadmap\n\n"
        "### 1. Unfinished Alpha\n\n"
        "Problem: alpha is not done.\n\n"
        "Plan:\n"
        "- Build alpha carefully.\n",
        encoding="utf-8",
    )


def write_fake_planner(path: Path) -> None:
    path.write_text(
        r"""
import os
import sys
from pathlib import Path

prompt = sys.argv[-1]
trace_dir = Path(os.environ["TRACE_DIR"])
trace_dir.mkdir(parents=True, exist_ok=True)
(trace_dir / "prompt.md").write_text(prompt, encoding="utf-8")

roadmap = Path("docs/roadmap.md").read_text(encoding="utf-8")
if "Unfinished Alpha" not in roadmap:
    raise SystemExit("roadmap was not readable")

output = Path("docs/plan-roadmap.md")
output.parent.mkdir(parents=True, exist_ok=True)
if os.environ.get("BAD_PLAN") == "1":
    output.write_text(
        "# Generated Plan\n\n"
        "## Phase 1: Unfinished Alpha\n\n"
        "Acceptance Criteria:\n"
        "- Alpha is planned.\n",
        encoding="utf-8",
    )
else:
    output.write_text(
        "# Generated Plan\n\n"
        "Plan-level context for later runner prompts.\n\n"
        "## Phase 1: Unfinished Alpha\n"
        "Status: PENDING\n\n"
        "Conservatively plan alpha from the unfinished roadmap item.\n\n"
        "Acceptance Criteria:\n"
        "- Alpha has a scoped implementation phase.\n"
        "- The phase can be executed by agent-runner later.\n",
        encoding="utf-8",
    )
print("planned roadmap")
""".lstrip(),
        encoding="utf-8",
    )


class RoadmapPlanCommandTests(unittest.TestCase):
    def test_plan_roadmap_generates_executable_plan_without_registering_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "fake_planner.py"
            repo.mkdir()
            git_init(repo)
            write_roadmap(repo)
            write_fake_planner(script)
            write_config(repo, script)

            result = run_cli(repo, home, "plan-roadmap", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("roadmap plan ready: docs/plan-roadmap.md", result.stderr)
            self.assertIn("run `agent-runner run` later", result.stderr)
            prompt = (trace / "prompt.md").read_text(encoding="utf-8")
            self.assertIn("Roadmap path: `docs/roadmap.md`", prompt)
            self.assertIn("Output plan path: `docs/plan-roadmap.md`", prompt)
            self.assertIn("Do not implement roadmap items", prompt)
            parsed = parse_plan_file(repo, "docs/plan-roadmap.md")
            self.assertEqual(len(parsed.phases), 1)
            self.assertEqual(parsed.phases[0].status, "PENDING")
            self.assertEqual(parsed.phases[0].title, "Unfinished Alpha")
            self.assertIn("Acceptance Criteria:", parsed.phases[0].content)

            with connect_db(home) as db:
                jobs = db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
                plans = db.execute("SELECT * FROM plans ORDER BY id").fetchall()
                events = db.execute("SELECT * FROM events ORDER BY id").fetchall()
            self.assertEqual([job["type"] for job in jobs], ["ROADMAP_PLAN"])
            self.assertEqual(jobs[0]["plan_id"], None)
            self.assertEqual(jobs[0]["phase_id"], None)
            self.assertEqual(plans, [])
            self.assertEqual(events[-1]["event_type"], "roadmap.plan_generated")

    def test_plan_roadmap_rejects_generated_phase_without_status_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "fake_planner.py"
            repo.mkdir()
            git_init(repo)
            write_roadmap(repo)
            write_fake_planner(script)
            write_config(repo, script)

            result = run_cli(
                repo,
                home,
                "plan-roadmap",
                extra_env={"TRACE_DIR": str(trace), "BAD_PLAN": "1"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("phase 1 is missing a Status marker", result.stderr)


if __name__ == "__main__":
    unittest.main()
