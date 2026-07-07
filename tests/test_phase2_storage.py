import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import SAMPLE_CONFIG, project_slug, strip_json_comments
from agent_runner.storage import (
    connect_db,
    create_job,
    create_phase,
    create_plan,
    get_job,
    get_phase,
    get_or_create_project,
    phase_log_dir,
    reap_orphaned_jobs,
    record_event,
    storage_paths,
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


def write_config(repo: Path) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


class Phase2StorageTests(unittest.TestCase):
    def test_fresh_db_is_created_lazily_with_pragmas_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            paths = storage_paths(home)

            with connect_db(home) as db:
                journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
                tables = {
                    row[0]
                    for row in db.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }

            self.assertTrue(paths.db_path.exists())
            self.assertEqual(journal_mode, "wal")
            self.assertEqual(busy_timeout, 10000)
            self.assertEqual(tables, {"projects", "plans", "phases", "jobs", "events"})

            with connect_db(home) as db:
                project = get_or_create_project(
                    db, slug="repo-abc123", repo_path=Path(tmp) / "repo"
                )

            self.assertEqual(project["slug"], "repo-abc123")

    def test_connect_db_migrates_jobs_type_check_for_autofix(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            paths = storage_paths(home)
            paths.db_path.parent.mkdir(parents=True, exist_ok=True)
            raw = sqlite3.connect(paths.db_path)
            raw.executescript(
                """
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    repo_path TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE plans (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    content_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, path)
                );
                CREATE TABLE phases (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                    phase_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    publish_mode TEXT,
                    branch_name TEXT,
                    pr_url TEXT,
                    published_sha TEXT,
                    log_dir TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, phase_number)
                );
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
                    phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    trigger TEXT,
                    prompt_path TEXT,
                    log_path TEXT,
                    output_path TEXT,
                    error TEXT,
                    pid INTEGER,
                    started_sha TEXT,
                    finished_sha TEXT,
                    exit_code INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(type IN ('IMPLEMENT', 'RUN_CHECKS', 'REVIEW', 'FIX', 'CLOSE_PHASE')),
                    CHECK(status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
                    CHECK(trigger IS NULL OR trigger IN ('checks', 'review'))
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
                    phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
                    job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO projects (id, slug, repo_path, created_at, updated_at)
                VALUES (1, 'repo', '/tmp/repo', 'now', 'now');
                INSERT INTO plans (id, project_id, path, created_at, updated_at)
                VALUES (1, 1, 'docs/plan.md', 'now', 'now');
                INSERT INTO phases (
                    id, project_id, plan_id, phase_number, title, content_hash,
                    created_at, updated_at
                )
                VALUES (1, 1, 1, 1, 'Old phase', 'hash', 'now', 'now');
                INSERT INTO jobs (
                    id, project_id, plan_id, phase_id, type, status,
                    created_at, updated_at
                )
                VALUES (1, 1, 1, 1, 'IMPLEMENT', 'SUCCEEDED', 'now', 'now');
                """
            )
            raw.commit()
            raw.close()

            with connect_db(home) as db:
                prior = db.execute("SELECT * FROM jobs WHERE id = 1").fetchone()
                project = db.execute("SELECT * FROM projects WHERE id = 1").fetchone()
                create_job(
                    db,
                    project_id=project["id"],
                    plan_id=1,
                    phase_id=1,
                    job_type="AUTOFIX",
                )
                types = [
                    row["type"]
                    for row in db.execute("SELECT type FROM jobs ORDER BY id")
                ]

            self.assertEqual(prior["type"], "IMPLEMENT")
            self.assertEqual(types, ["IMPLEMENT", "AUTOFIX"])

    def test_unique_constraints_fire_for_plans_and_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with connect_db(Path(tmp) / "home") as db:
                project = get_or_create_project(db, slug="repo", repo_path=repo)
                plan = create_plan(
                    db,
                    project_id=project["id"],
                    path="docs/plan.md",
                    content_hash="plan-hash",
                )
                create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=2,
                    title="Storage",
                    content_hash="phase-hash",
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    create_plan(
                        db,
                        project_id=project["id"],
                        path="docs/plan.md",
                        content_hash="other",
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    create_phase(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_number=2,
                        title="Duplicate",
                        content_hash="other",
                    )

    def test_orphan_reap_marks_running_job_failed_and_resets_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with connect_db(Path(tmp) / "home") as db:
                project = get_or_create_project(db, slug="repo", repo_path=repo)
                plan = create_plan(db, project_id=project["id"], path="docs/plan.md")
                phase = create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=2,
                    title="Storage",
                    content_hash="phase-hash",
                    status="CHECKING",
                )
                job = create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="RUN_CHECKS",
                    status="RUNNING",
                    started_sha="abc123",
                )

                reaped = reap_orphaned_jobs(db, project["id"])

                self.assertEqual(reaped, [job["id"]])
                failed_job = get_job(db, job["id"])
                reset_phase = get_phase(db, phase["id"])
                event = db.execute(
                    """
                    SELECT * FROM events
                    WHERE project_id = ? AND plan_id = ? AND phase_id = ? AND job_id = ?
                    """,
                    (project["id"], plan["id"], phase["id"], job["id"]),
                ).fetchone()

            self.assertEqual(failed_job["status"], "FAILED")
            self.assertEqual(failed_job["error"], "orphaned")
            self.assertEqual(reset_phase["status"], "CHECKING")
            self.assertIsNotNone(event)
            self.assertEqual(event["event_type"], "job.orphaned")

    def test_events_store_project_plan_phase_job_linkage(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with connect_db(Path(tmp) / "home") as db:
                project = get_or_create_project(db, slug="repo", repo_path=repo)
                plan = create_plan(db, project_id=project["id"], path="docs/plan.md")
                phase = create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=2,
                    title="Storage",
                    content_hash="phase-hash",
                )
                job = create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="REVIEW",
                )

                event = record_event(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_id=job["id"],
                    event_type="review.started",
                    message="review started",
                    data={"phase": 2},
                )

            self.assertEqual(event["project_id"], project["id"])
            self.assertEqual(event["plan_id"], plan["id"])
            self.assertEqual(event["phase_id"], phase["id"])
            self.assertEqual(event["job_id"], job["id"])
            self.assertEqual(json.loads(event["data_json"]), {"phase": 2})

    def test_phase_log_dir_uses_project_plan_and_phase(self):
        path = phase_log_dir(
            Path("/tmp/logs"),
            project_slug="agent-runner-abc123",
            plan_path="docs/plan.md",
            phase_number=2,
        )

        self.assertEqual(
            path,
            Path("/tmp/logs/agent-runner-abc123/docs-plan.md/phase-2"),
        )

    def test_status_with_no_plan_reports_cleanly_and_outputs_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)

            result = run_cli(repo, home, "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no plan registered yet", result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["project"]["repo_path"], str(repo.resolve()))
            self.assertEqual(payload["plans"], [])

    def test_status_reaps_orphaned_running_jobs_before_display_without_live_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            slug = project_slug(repo)

            with connect_db(home) as db:
                project = get_or_create_project(db, slug=slug, repo_path=repo)
                plan = create_plan(db, project_id=project["id"], path="docs/plan.md")
                phase = create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=2,
                    title="Storage",
                    content_hash="phase-hash",
                    status="REVIEWING",
                )
                create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="REVIEW",
                    status="RUNNING",
                    log_path=Path("/tmp/review.log"),
                    started_at="2026-07-06T00:00:00+00:00",
                )

            result = run_cli(repo, home, "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("reaped 1 orphaned job(s)", result.stderr)
            self.assertNotIn("running jobs:", result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["runningJobs"], [])
            self.assertEqual(payload["plans"][0]["phases"][0]["status"], "REVIEWING")

    def test_status_preserves_running_jobs_when_project_lock_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            slug = project_slug(repo)

            with connect_db(home) as db:
                project = get_or_create_project(db, slug=slug, repo_path=repo)
                plan = create_plan(db, project_id=project["id"], path="docs/plan.md")
                phase = create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=2,
                    title="Storage",
                    content_hash="phase-hash",
                    status="REVIEWING",
                )
                create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="REVIEW",
                    status="RUNNING",
                    log_path=Path("/tmp/review.log"),
                    started_at="2026-07-06T00:00:00+00:00",
                )
            locks_dir = home / "locks"
            locks_dir.mkdir(parents=True)
            (locks_dir / f"{slug}.lock").write_text(
                json.dumps({"pid": os.getpid(), "repoPath": str(repo.resolve())}),
                encoding="utf-8",
            )

            result = run_cli(repo, home, "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("reaped 1 orphaned job(s)", result.stderr)
            self.assertIn("running jobs:", result.stderr)
            self.assertIn("job 1: REVIEW phase=2", result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["runningJobs"][0]["type"], "REVIEW")
            self.assertEqual(payload["runningJobs"][0]["phase_number"], 2)

    def test_status_lists_registered_phases_publish_state_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            slug = project_slug(repo)

            with connect_db(home) as db:
                project = get_or_create_project(db, slug=slug, repo_path=repo)
                plan = create_plan(
                    db,
                    project_id=project["id"],
                    path="docs/plan.md",
                    content_hash="plan-hash",
                )
                phase = create_phase(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_number=2,
                    title="Storage",
                    content_hash="phase-hash",
                    status="REVIEWING",
                    retry_count=1,
                    publish_mode="work-branch",
                    branch_name="phase-2-sqlite-state",
                    pr_url="https://github.com/example/project/pull/12",
                    published_sha="abc123",
                )
                record_event(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    event_type="phase.reviewing",
                    message="phase is ready for review",
                )

            result = run_cli(repo, home, "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("running jobs:", result.stderr)
            self.assertIn("phase 2: REVIEWING retries=1", result.stderr)
            self.assertIn("branch_name=phase-2-sqlite-state", result.stderr)
            self.assertIn(
                "pr=#12 (https://github.com/example/project/pull/12)",
                result.stderr,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["runningJobs"], [])
            self.assertEqual(payload["plans"][0]["path"], "docs/plan.md")
            self.assertEqual(payload["plans"][0]["phases"][0]["status"], "REVIEWING")
            self.assertEqual(
                payload["recentEvents"][0]["event_type"], "phase.reviewing"
            )


if __name__ == "__main__":
    unittest.main()
