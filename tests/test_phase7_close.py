import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from agent_runner.config import SAMPLE_CONFIG, project_slug, strip_json_comments
from agent_runner.plan import parse_plan_file
from agent_runner.storage import (
    connect_db,
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
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def commit_all(repo: Path, message: str = "baseline") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)


def add_origin(repo: Path, remote: Path) -> None:
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)


def write_plan(repo: Path, *, phase_count: int = 1, status: str = "PENDING") -> None:
    second_phase = ""
    if phase_count > 1:
        second_phase = (
            "\n## Phase 2: Second phase\n"
            "Status: PENDING\n\n"
            "Create phase2.txt.\n\n"
            "Acceptance Criteria:\n"
            "- phase2.txt exists.\n"
        )
    plan_path = repo / "docs" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "## Phase 1: First phase\n"
        f"Status: {status}\n\n"
        "Create generated.txt.\n\n"
        "Acceptance Criteria:\n"
        "- generated.txt exists.\n"
        f"{second_phase}",
        encoding="utf-8",
    )


def write_config(
    repo: Path,
    agent_script: Path,
    *,
    auto_commit: bool = True,
    auto_merge: bool = False,
) -> None:
    data = json.loads(strip_json_comments(SAMPLE_CONFIG))
    data["agents"] = {
        "fake": {
            "command": sys.executable,
            "promptArgs": [str(agent_script)],
            "writeFlags": ["--write-flag"],
            "readOnlyFlags": ["--read-only-flag"],
            "outputCapture": "stdout",
        }
    }
    data["roles"] = {"coder": "fake", "reviewer": "fake"}
    data["checks"] = [
        f"{shlex.quote(sys.executable)} -c "
        "\"from pathlib import Path; assert Path('generated.txt').exists()\""
    ]
    data["autoCommit"] = auto_commit
    data["autoMerge"] = auto_merge
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_phase7_agent(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

prompt = sys.argv[-1]
trace = Path(os.environ["TRACE_DIR"])
trace.mkdir(parents=True, exist_ok=True)

if "Review the published phase PR independently" in prompt:
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "blockingIssues": [],
        "nonBlockingIssues": [],
        "recommendedFixPrompt": ""
    }))
    raise SystemExit(0)

if "Close the accepted phase" in prompt:
    (trace / "close-argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    if os.environ.get("CLOSE_FAIL") == "1":
        print("closer failed")
        raise SystemExit(9)
    if os.environ.get("CLOSE_INVALID_PLAN") == "1":
        plan = Path("docs/plan.md")
        text = plan.read_text(encoding="utf-8")
        text = re.sub(
            r"(## Phase 1: [^\n]+\n)(?:Status: [A-Z_]+\n)?",
            r"\1Status: BOGUS_STATUS\n",
            text,
            count=1,
        )
        plan.write_text(text, encoding="utf-8")
        print("wrote invalid plan")
        raise SystemExit(0)
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
    Path("docs/usage.md").write_text("Close phase docs updated.\n", encoding="utf-8")
    handoff = Path(f".acc/phases/docs-plan.md/phase-{phase_number:02d}-handoff.md")
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "# Phase handoff\n\n"
        "## Completed Work\nClosed the phase.\n\n"
        "## Decisions\nUsed scripted closer.\n\n"
        "## Files Changed\ndocs/plan.md, docs/usage.md\n\n"
        "## Checks Run\nconfigured checks passed\n\n"
        "## Open Risks\nNone.\n\n"
        "## Next-Phase Context\nContinue with the next pending phase.\n",
        encoding="utf-8",
    )
    print("closed phase")
    raise SystemExit(0)

if "Phase 2: Second phase" in prompt:
    Path("phase2-started.txt").write_text("started\n", encoding="utf-8")
    print("phase 2 intentionally blocked")
    raise SystemExit(7)

Path("generated.txt").write_text("created\n", encoding="utf-8")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-qm", "implement phase"], check=True)
print("https://example.test/pull/1")
print("fake coder completed")
""".lstrip(),
        encoding="utf-8",
    )


def write_fake_gh(path: Path) -> None:
    path.write_text(
        r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
branch = subprocess.check_output(
    ["git", "branch", "--show-current"], text=True
).strip()
sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
branch = os.environ.get("GH_HEAD_REF_NAME", branch)
sha = os.environ.get("GH_HEAD_REF_OID", sha)

if args[:2] == ["pr", "view"]:
    pr_url = "https://example.test/pull/1"
    if len(args) > 2 and not args[2].startswith("--"):
        pr_url = args[2]
    print(json.dumps({
        "url": pr_url,
        "headRefName": branch,
        "headRefOid": sha,
        "state": os.environ.get("GH_PR_STATE", "OPEN"),
        "mergeable": os.environ.get("GH_PR_MERGEABLE", "MERGEABLE"),
        "isDraft": os.environ.get("GH_PR_DRAFT") == "1",
    }))
    raise SystemExit(0)

if args[:2] == ["pr", "merge"]:
    merge_log = os.environ.get("GH_MERGE_LOG")
    if merge_log:
        with open(merge_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args) + "\n")
    if os.environ.get("GH_PR_MERGE_FAIL") == "1":
        print("merge failed", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)

if args[:2] == ["pr", "diff"]:
    subprocess.run(["git", "show", "--format=", "--patch", "HEAD"], check=True)
    raise SystemExit(0)

print(f"unsupported gh args: {args}", file=sys.stderr)
raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


def seed_closing_published_phase(repo: Path, home: Path) -> str:
    published_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    parsed_plan = parse_plan_file(repo, "docs/plan.md")
    parsed_phase = parsed_plan.phases[0]

    with connect_db(home) as db:
        project = get_or_create_project(db, slug=project_slug(repo), repo_path=repo)
        plan = create_plan(
            db,
            project_id=project["id"],
            path=parsed_plan.path,
            content_hash=parsed_plan.content_hash,
        )
        create_phase(
            db,
            project_id=project["id"],
            plan_id=plan["id"],
            phase_number=parsed_phase.phase_number,
            title=parsed_phase.title,
            content_hash=parsed_phase.content_hash,
            status="CLOSING",
            publish_mode="pr",
            branch_name="dev/test-phase",
            pr_url="https://example.test/pull/1",
            published_sha=published_sha,
            log_dir=phase_log_dir(
                home / "logs",
                project_slug=project_slug(repo),
                plan_path=parsed_plan.path,
                phase_number=parsed_phase.phase_number,
            ),
        )
    return published_sha


def phase_rows(home: Path, repo: Path):
    with connect_db(home) as db:
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


class Phase7CloseTests(unittest.TestCase):
    def test_close_phase_writes_plan_handoff_commits_and_completes_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            subprocess.run(["git", "checkout", "-q", "-b", "dev/test-phase"], cwd=repo, check=True)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo)
            write_config(repo, script)
            before_phase_hash = parse_plan_file(repo, "docs/plan.md").phases[0].content_hash
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("plan complete", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            parsed = parse_plan_file(repo, "docs/plan.md")
            self.assertEqual(parsed.phases[0].status, "COMPLETE")
            self.assertEqual(parsed.phases[0].content_hash, before_phase_hash)
            handoff = repo / ".acc/phases/docs-plan.md/phase-01-handoff.md"
            self.assertTrue(handoff.exists())
            for section in (
                "Completed Work",
                "Decisions",
                "Files Changed",
                "Checks Run",
                "Open Risks",
                "Next-Phase Context",
            ):
                self.assertIn(f"## {section}", handoff.read_text(encoding="utf-8"))
            close_argv = json.loads((trace / "close-argv.json").read_text(encoding="utf-8"))
            self.assertIn("--write-flag", close_argv)
            self.assertNotIn("--read-only-flag", close_argv)
            commit_files = subprocess.check_output(
                ["git", "show", "--format=", "--name-only", "HEAD"],
                cwd=repo,
                text=True,
            ).splitlines()
            self.assertIn("docs/plan.md", commit_files)
            self.assertIn("docs/usage.md", commit_files)
            self.assertIn(".acc/phases/docs-plan.md/phase-01-handoff.md", commit_files)
            with connect_db(home) as db:
                project = db.execute(
                    "SELECT * FROM projects WHERE repo_path = ?",
                    (str(repo.resolve()),),
                ).fetchone()
                plan = db.execute("SELECT * FROM plans").fetchone()
                jobs = db.execute("SELECT type FROM jobs ORDER BY id").fetchall()
            self.assertEqual(project["status"], "COMPLETE")
            self.assertEqual(plan["status"], "COMPLETE")
            self.assertEqual([job["type"] for job in jobs][-1], "CLOSE_PHASE")

    def test_auto_merge_merges_pr_after_successful_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            remote = root / "origin.git"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            merge_log = root / "merge.log"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            add_origin(repo, remote)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo)
            write_config(repo, script, auto_merge=True)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_MERGE_LOG": str(merge_log),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            self.assertTrue(merge_log.exists())
            self.assertIn(
                ["pr", "merge", "https://example.test/pull/1", "--merge"],
                [
                    json.loads(line)
                    for line in merge_log.read_text(encoding="utf-8").splitlines()
                ],
            )
            with connect_db(home) as db:
                events = db.execute(
                    "SELECT event_type, message FROM events ORDER BY id"
                ).fetchall()
            self.assertIn(
                ("phase.merged", "phase 1 PR merged"),
                [(event["event_type"], event["message"]) for event in events],
            )

    def test_auto_merge_blocks_when_pr_is_not_mergeable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            remote = root / "origin.git"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            merge_log = root / "merge.log"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            add_origin(repo, remote)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo)
            write_config(repo, script, auto_merge=True)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_MERGE_LOG": str(merge_log),
                    "GH_PR_MERGEABLE": "CONFLICTING",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED after CLOSE_PHASE publish/merge", result.stderr)
            self.assertIn("phase PR is not mergeable", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertFalse(merge_log.exists())

    def test_closer_failure_blocks_without_marking_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase7_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_plan(repo, status="CLOSING")
            write_config(repo, script, auto_commit=False)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "CLOSE_FAIL": "1"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED after CLOSE_PHASE failure", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertNotIn("Status: COMPLETE", (repo / "docs/plan.md").read_text())

    def test_invalid_closer_plan_write_back_blocks_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase7_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_plan(repo, status="CLOSING")
            write_config(repo, script, auto_commit=False)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "CLOSE_INVALID_PLAN": "1"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED after CLOSE_PHASE validation", result.stderr)
            self.assertIn("invalid phase status marker: BOGUS_STATUS", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            with connect_db(home) as db:
                events = db.execute(
                    "SELECT event_type, message FROM events ORDER BY id"
                ).fetchall()
                jobs = db.execute("SELECT type FROM jobs ORDER BY id").fetchall()
            self.assertIn(
                (
                    "phase.blocked",
                    "CLOSE_PHASE validation failed for phase 1: "
                    "invalid phase status marker: BOGUS_STATUS",
                ),
                [(event["event_type"], event["message"]) for event in events],
            )
            self.assertEqual([job["type"] for job in jobs], ["CLOSE_PHASE"])

            write_plan(repo, status="CLOSING")
            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 1)
            self.assertIn("phase 1 is BLOCKED", result.stderr)
            with connect_db(home) as db:
                jobs = db.execute("SELECT type FROM jobs ORDER BY id").fetchall()
            self.assertEqual([job["type"] for job in jobs], ["CLOSE_PHASE"])

    def test_completing_phase_auto_starts_next_pending_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=2, status="CLOSING")
            write_config(repo, script, auto_commit=True)
            commit_all(repo)
            seed_closing_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED after IMPLEMENT failure", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            self.assertEqual(rows[1]["status"], "BLOCKED")
            self.assertTrue((repo / "phase2-started.txt").exists())
            with connect_db(home) as db:
                jobs = db.execute(
                    """
                    SELECT phases.phase_number, jobs.type
                    FROM jobs
                    JOIN phases ON phases.id = jobs.phase_id
                    ORDER BY jobs.id
                    """
            ).fetchall()
            self.assertIn((2, "IMPLEMENT"), [(row["phase_number"], row["type"]) for row in jobs])

    def test_auto_commit_blocks_close_when_head_moved_after_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, status="CLOSING")
            write_config(repo, script, auto_commit=True)
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            published_sha = seed_closing_published_phase(repo, home)
            (repo / "unreviewed.txt").write_text("unreviewed\n", encoding="utf-8")
            commit_all(repo, "unreviewed change")
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_HEAD_REF_OID": published_sha,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED before CLOSE_PHASE", result.stderr)
            self.assertIn(head_sha[:12], result.stderr)
            self.assertIn(published_sha[:12], result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertFalse((trace / "close-argv.json").exists())
            with connect_db(home) as db:
                jobs = db.execute("SELECT type FROM jobs ORDER BY id").fetchall()
            self.assertEqual([job["type"] for job in jobs], [])

    def test_auto_commit_blocks_close_on_wrong_local_branch_at_reviewed_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, status="CLOSING")
            write_config(repo, script, auto_commit=True)
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            published_sha = seed_closing_published_phase(repo, home)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/wrong-phase"],
                cwd=repo,
                check=True,
            )

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_HEAD_REF_NAME": "dev/test-phase",
                    "GH_HEAD_REF_OID": published_sha,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED before CLOSE_PHASE", result.stderr)
            self.assertIn("current branch 'dev/wrong-phase'", result.stderr)
            self.assertIn("reviewed published branch 'dev/test-phase'", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertFalse((trace / "close-argv.json").exists())
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertEqual(head_sha, published_sha)
            with connect_db(home) as db:
                jobs = db.execute("SELECT type FROM jobs ORDER BY id").fetchall()
            self.assertEqual([job["type"] for job in jobs], [])


if __name__ == "__main__":
    unittest.main()
