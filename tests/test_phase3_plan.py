import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import SAMPLE_CONFIG, project_slug, strip_json_comments
from agent_runner.errors import PlanError
from agent_runner.plan import (
    parse_plan_file,
    parse_plan_markdown,
    register_or_resume_plan,
)
from agent_runner.storage import connect_db, list_phases_for_plan, list_plans_for_project


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


def write_config(repo: Path, plan_path: str = "docs/plan.md") -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    agent_script = repo / "fake_agent.py"
    agent_script.write_text(
        r"""
import json
import re
import sys
from pathlib import Path

prompt = sys.argv[-1]
if "Review the staged phase work independently" in prompt:
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "blockingIssues": [],
        "nonBlockingIssues": [],
        "recommendedFixPrompt": ""
    }))
elif "Close the accepted phase" in prompt:
    phase_number = int(re.search(r"Phase (\d+):", prompt).group(1))
    plan = Path("docs/plan.md")
    text = plan.read_text(encoding="utf-8")
    text = re.sub(
        rf"(## Phase {phase_number}: [^\n]+\n)(?:Status: [A-Z_]+\n)?",
        rf"\1Status: COMPLETE\nEvidence: commit pending; checks passed\n",
        text,
        count=1,
    )
    plan.write_text(text, encoding="utf-8")
    handoff = Path(f".acc/phases/docs-plan.md/phase-{phase_number:02d}-handoff.md")
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
else:
    print("fake agent completed")
""".lstrip(),
        encoding="utf-8",
    )
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
    data["planPath"] = plan_path
    data["autoCommit"] = False
    data["allowDirty"] = True
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_plan(repo: Path, text: str, plan_path: str = "docs/plan.md") -> None:
    path = repo / plan_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sample_plan(
    phase_1_body: str = "Build CLI.\n",
    phase_3_body: str = "Parse plan.\n",
    phase_1_status: str = "PENDING",
) -> str:
    return (
        "# Build Plan\n\n"
        "Ignored preamble.\n\n"
        "## Phase 1: CLI\n"
        f"Status: {phase_1_status}\n\n"
        f"{phase_1_body}"
        "\n"
        "## Phase 3: Plan parsing\n"
        f"{phase_3_body}"
    )


class Phase3PlanTests(unittest.TestCase):
    def test_parser_handles_gaps_missing_status_preamble_and_trailing_phase(self):
        parsed = parse_plan_markdown(sample_plan(), path="docs/plan.md")

        self.assertEqual([phase.phase_number for phase in parsed.phases], [1, 3])
        self.assertEqual(parsed.phases[0].title, "CLI")
        self.assertEqual(parsed.phases[0].status, "PENDING")
        self.assertEqual(parsed.phases[1].title, "Plan parsing")
        self.assertEqual(parsed.phases[1].status, "PENDING")
        self.assertIn("Build CLI.", parsed.phases[0].content)
        self.assertIn("Parse plan.", parsed.phases[1].content)
        self.assertNotIn("Ignored preamble", parsed.phases[0].content)
        self.assertNotIn("Ignored preamble", parsed.phases[1].content)

    def test_parser_rejects_duplicate_phase_numbers(self):
        text = (
            "## Phase 1: First\n"
            "Do first.\n"
            "## Phase 1: Duplicate\n"
            "Do duplicate.\n"
        )

        with self.assertRaisesRegex(PlanError, "duplicate phase number"):
            parse_plan_markdown(text, path="docs/plan.md")

    def test_parser_rejects_invalid_status_marker(self):
        text = (
            "## Phase 1: Bad status\n"
            "Status: NOT_A_REAL_STATUS\n"
            "Do work.\n"
        )

        with self.assertRaisesRegex(PlanError, "invalid phase status marker"):
            parse_plan_markdown(text, path="docs/plan.md")

    def test_status_line_changes_do_not_change_phase_hash(self):
        pending = parse_plan_markdown(
            sample_plan(phase_1_status="PENDING"), path="docs/plan.md"
        )
        complete = parse_plan_markdown(
            sample_plan(phase_1_status="COMPLETE"), path="docs/plan.md"
        )

        self.assertEqual(pending.phases[0].content_hash, complete.phases[0].content_hash)
        self.assertEqual(pending.content_hash, complete.content_hash)

    def test_evidence_line_after_status_does_not_change_phase_hash(self):
        without_evidence = parse_plan_markdown(
            "## Phase 1: CLI\n"
            "Status: COMPLETE\n\n"
            "Build CLI.\n",
            path="docs/plan.md",
        )
        with_evidence = parse_plan_markdown(
            "## Phase 1: CLI\n"
            "Status: COMPLETE\n"
            "Evidence: commit abc123; checks passed\n\n"
            "Build CLI.\n",
            path="docs/plan.md",
        )

        self.assertEqual(
            without_evidence.phases[0].content_hash,
            with_evidence.phases[0].content_hash,
        )
        self.assertEqual(without_evidence.content_hash, with_evidence.content_hash)

    def test_status_marker_can_follow_blank_lines(self):
        direct = parse_plan_markdown(
            "## Phase 1: CLI\n"
            "Status: COMPLETE\n\n"
            "Build CLI.\n",
            path="docs/plan.md",
        )
        spaced = parse_plan_markdown(
            "## Phase 1: CLI\n\n"
            "Status: COMPLETE\n\n"
            "Build CLI.\n",
            path="docs/plan.md",
        )

        self.assertEqual(spaced.phases[0].status, "COMPLETE")
        self.assertEqual(spaced.phases[0].content_hash, direct.phases[0].content_hash)
        self.assertEqual(spaced.content_hash, direct.content_hash)

    def test_invalid_status_marker_after_blank_lines_is_rejected(self):
        text = (
            "## Phase 1: Bad status\n\n"
            "Status: NOT_A_REAL_STATUS\n"
            "Do work.\n"
        )

        with self.assertRaisesRegex(PlanError, "invalid phase status marker"):
            parse_plan_markdown(text, path="docs/plan.md")

    def test_parse_plan_file_rejects_paths_outside_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with self.assertRaisesRegex(PlanError, "escapes repository"):
                parse_plan_file(repo, "../outside-plan.md")

    def test_run_reports_missing_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)

            result = run_cli(repo, home, "run")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing plan file docs/plan.md", result.stderr)

    def test_run_rejects_duplicate_phase_numbers_without_registering_partial_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(
                repo,
                "## Phase 1: First\nDo first.\n## Phase 1: Duplicate\nDo duplicate.\n",
            )

            result = run_cli(repo, home, "run")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate phase number", result.stderr)
            with connect_db(home) as db:
                phase_count = db.execute("SELECT COUNT(*) FROM phases").fetchone()[0]
            self.assertEqual(phase_count, 0)

    def test_run_rejects_invalid_status_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(
                repo,
                "## Phase 1: Bad status\nStatus: NOT_A_REAL_STATUS\nDo work.\n",
            )

            result = run_cli(repo, home, "run")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid phase status marker", result.stderr)

    def test_run_registers_plan_and_phase_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, sample_plan(phase_1_status="REVIEWING"))

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("registered plan docs/plan.md with 2 phase(s)", result.stderr)
            with connect_db(home) as db:
                project = db.execute(
                    "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
                ).fetchone()
                plans = list_plans_for_project(db, project["id"])
                phases = list_phases_for_plan(db, plans[0]["id"])
            self.assertEqual(plans[0]["path"], "docs/plan.md")
            self.assertEqual([phase["phase_number"] for phase in phases], [1, 3])
            self.assertEqual(phases[0]["status"], "COMPLETE")
            self.assertEqual(phases[1]["status"], "PENDING")
            self.assertTrue((home / "logs" / project_slug(repo) / "docs-plan.md" / "phase-1").is_dir())

    def test_pending_phase_body_change_updates_only_that_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, sample_plan(phase_1_status="REVIEWING"))
            first = run_cli(repo, home, "run")
            self.assertEqual(first.returncode, 0, first.stderr)

            with connect_db(home) as db:
                project = db.execute(
                    "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
                ).fetchone()
                plan = list_plans_for_project(db, project["id"])[0]
                before = {
                    phase["phase_number"]: phase["content_hash"]
                    for phase in list_phases_for_plan(db, plan["id"])
                }

            write_plan(
                repo,
                sample_plan(
                    phase_1_status="REVIEWING",
                    phase_3_body="Parse plan with changes.\n",
                ),
            )
            with connect_db(home) as db:
                parsed_plan = parse_plan_file(repo, "docs/plan.md")
                result = register_or_resume_plan(
                    db,
                    project_id=project["id"],
                    project_slug=project_slug(repo),
                    logs_dir=home / "logs",
                    parsed_plan=parsed_plan,
                )
                after = {
                    phase["phase_number"]: phase["content_hash"]
                    for phase in list_phases_for_plan(db, plan["id"])
                }
                event = db.execute(
                    """
                    SELECT * FROM events
                    WHERE event_type = 'phase.plan_change_updated'
                    """
                ).fetchone()
            self.assertEqual(result.changed_phase_numbers, [3])
            self.assertEqual(after[1], before[1])
            self.assertNotEqual(after[3], before[3])
            self.assertIsNotNone(event)

    def test_in_progress_phase_body_change_blocks_without_accept_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, sample_plan(phase_1_status="REVIEWING"))
            first = run_cli(repo, home, "run")
            self.assertEqual(first.returncode, 0, first.stderr)

            with connect_db(home) as db:
                project = db.execute(
                    "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
                ).fetchone()
                plan = list_plans_for_project(db, project["id"])[0]
                phase = db.execute(
                    """
                    SELECT * FROM phases
                    WHERE plan_id = ? AND phase_number = 3
                    """,
                    (plan["id"],),
                ).fetchone()
                db.execute(
                    "UPDATE phases SET status = 'IMPLEMENTING' WHERE id = ?",
                    (phase["id"],),
                )
                db.commit()
                original_hash = phase["content_hash"]

            write_plan(
                repo,
                sample_plan(
                    phase_1_status="REVIEWING",
                    phase_3_body="Changed protected phase.\n",
                ),
            )
            blocked = run_cli(repo, home, "run")

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("plan changed for phase 3", blocked.stderr)
            self.assertIn("--accept-plan-change", blocked.stderr)
            with connect_db(home) as db:
                unchanged = db.execute(
                    """
                    SELECT * FROM phases
                    WHERE plan_id = ? AND phase_number = 3
                    """,
                    (plan["id"],),
                ).fetchone()
            self.assertEqual(unchanged["content_hash"], original_hash)

    def test_accept_plan_change_updates_protected_phase_and_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, sample_plan(phase_1_status="REVIEWING"))
            first = run_cli(repo, home, "run")
            self.assertEqual(first.returncode, 0, first.stderr)

            with connect_db(home) as db:
                project = db.execute(
                    "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
                ).fetchone()
                plan = list_plans_for_project(db, project["id"])[0]
                phase = db.execute(
                    """
                    SELECT * FROM phases
                    WHERE plan_id = ? AND phase_number = 3
                    """,
                    (plan["id"],),
                ).fetchone()
                db.execute(
                    "UPDATE phases SET status = 'COMPLETE' WHERE id = ?",
                    (phase["id"],),
                )
                db.commit()
                original_hash = phase["content_hash"]

            write_plan(
                repo,
                sample_plan(
                    phase_1_status="REVIEWING",
                    phase_3_body="Accepted protected change.\n",
                ),
            )
            accepted = run_cli(repo, home, "run", "--accept-plan-change")

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("accepted protected plan change(s): 3", accepted.stderr)
            self.assertNotIn("updated changed phase(s): 3", accepted.stderr)
            with connect_db(home) as db:
                updated = db.execute(
                    """
                    SELECT * FROM phases
                    WHERE plan_id = ? AND phase_number = 3
                    """,
                    (plan["id"],),
                ).fetchone()
                event = db.execute(
                    """
                    SELECT * FROM events
                    WHERE event_type = 'phase.plan_change_accepted'
                    """
                ).fetchone()
            self.assertNotEqual(updated["content_hash"], original_hash)
            self.assertIsNotNone(event)

    def test_blocked_phase_body_change_includes_accept_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, sample_plan(phase_1_status="REVIEWING"))
            first = run_cli(repo, home, "run")
            self.assertEqual(first.returncode, 0, first.stderr)

            with connect_db(home) as db:
                project = db.execute(
                    "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
                ).fetchone()
                plan = list_plans_for_project(db, project["id"])[0]
                phase = db.execute(
                    """
                    SELECT * FROM phases
                    WHERE plan_id = ? AND phase_number = 3
                    """,
                    (plan["id"],),
                ).fetchone()
                db.execute(
                    "UPDATE phases SET status = 'BLOCKED' WHERE id = ?",
                    (phase["id"],),
                )
                db.commit()

            write_plan(
                repo,
                sample_plan(
                    phase_1_status="REVIEWING",
                    phase_3_body="Changed blocked phase.\n",
                ),
            )
            blocked = run_cli(repo, home, "run")

            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("plan changed for phase 3", blocked.stderr)
            self.assertIn("--accept-plan-change", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
