import json
import tempfile
import unittest
from pathlib import Path

from agent_runner.config import strip_json_comments
from agent_runner.storage import connect_db, list_phases_for_plan, list_plans_for_project

from test_phase3_plan import git_init, run_cli, sample_plan, write_config, write_plan


def use_trivial_checks(repo: Path) -> None:
    # The shared fixture inherits the sample config's real checks, which cannot
    # pass in a bare temp repo. These tests are about which phase runs, not what
    # the checks find.
    path = repo / ".agent-runner.json"
    data = json.loads(strip_json_comments(path.read_text(encoding="utf-8")))
    data["checks"] = ["python3 -c pass"]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def phases_for(home: Path, repo: Path):
    with connect_db(home) as db:
        project = db.execute(
            "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
        ).fetchone()
        plans = list_plans_for_project(db, project["id"])
        return list_phases_for_plan(db, plans[0]["id"])


def jobs_for(home: Path, repo: Path):
    with connect_db(home) as db:
        project = db.execute(
            "SELECT * FROM projects WHERE repo_path = ?", (str(repo.resolve()),)
        ).fetchone()
        return db.execute(
            """
            SELECT jobs.type, phases.phase_number
            FROM jobs JOIN phases ON phases.id = jobs.phase_id
            WHERE jobs.project_id = ?
            """,
            (project["id"],),
        ).fetchall()


class MarkPhaseCommandTests(unittest.TestCase):
    def test_mark_phase_registers_an_unrun_plan_and_skips_landed_work(self):
        # The phases worth marking COMPLETE are usually the ones that landed
        # before the plan reached the runner, so `mark-phase` has to register the
        # plan itself -- requiring a prior `run` would execute the very phase we
        # are trying to skip.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            use_trivial_checks(repo)
            write_plan(repo, sample_plan())

            marked = run_cli(repo, home, "mark-phase", "1", "--status", "COMPLETE")

            self.assertEqual(marked.returncode, 0, marked.stderr)
            self.assertIn("phase 1 marked COMPLETE (was PENDING)", marked.stderr)
            phases = phases_for(home, repo)
            self.assertEqual(phases[0]["status"], "COMPLETE")
            self.assertEqual(phases[1]["status"], "PENDING")

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            # Phase 1 stayed COMPLETE and the runner never opened a job for it.
            phases = phases_for(home, repo)
            self.assertEqual(phases[0]["status"], "COMPLETE")
            worked = {job["phase_number"] for job in jobs_for(home, repo)}
            self.assertNotIn(1, worked)
            self.assertIn(3, worked)

    def test_mark_phase_rejects_unknown_status_and_unknown_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, sample_plan())

            bad_status = run_cli(repo, home, "mark-phase", "1", "--status", "DONE")
            bad_phase = run_cli(repo, home, "mark-phase", "9", "--status", "COMPLETE")

            self.assertEqual(bad_status.returncode, 1)
            self.assertIn("invalid status DONE", bad_status.stderr)
            self.assertEqual(bad_phase.returncode, 1)
            self.assertIn("has no phase 9", bad_phase.stderr)

    def test_run_refuses_a_plan_whose_phases_have_no_acceptance_criteria(self):
        # The failure this guards: a historical tracking document, pointed at the
        # runner, whose phases describe features but name no command that decides
        # them.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(
                repo,
                "# Build Plan\n\n"
                "## Phase 1: CLI\nStatus: PENDING\n\nMake the CLI nicer.\n",
            )

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 1)
            self.assertIn("plan is not executable", result.stderr)
            self.assertIn("Acceptance Criteria", result.stderr)

    def test_run_refuses_a_plan_with_a_prose_status_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(
                repo,
                "# Build Plan\n\n"
                "## Phase 1: CLI\nStatus: completed on 2026-07-09.\n\nBuild CLI.\n",
            )

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 1)
            self.assertIn("unrecognized status marker", result.stderr)

    def test_run_warns_when_plan_context_exceeds_the_configured_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo, ("A" * 13000) + "\n\n" + sample_plan())

            result = run_cli(repo, home, "run")

            self.assertIn("plan-level context exceeds", result.stderr)
            self.assertIn("planContextCharLimit (12000 characters)", result.stderr)


if __name__ == "__main__":
    unittest.main()
