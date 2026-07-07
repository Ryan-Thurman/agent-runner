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
    update_phase_status,
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
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)


def commit_all(repo: Path, message: str = "baseline") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)


def write_plan(repo: Path, *, phase_count: int = 1, status: str = "PENDING") -> None:
    write_custom_plan(repo, phase_count=phase_count, status=status)


def write_custom_plan(
    repo: Path,
    *,
    phase_count: int = 1,
    status: str = "PENDING",
    phase_1_body: str = "Create generated.txt.",
) -> None:
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
        f"{phase_1_body}\n\n"
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
    merge_on_close: bool = False,
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
    data["roleFallbacks"] = {}
    data.pop("reviewTriage", None)
    data["autoFixAttempts"] = 0
    data["checks"] = [
        f"{shlex.quote(sys.executable)} -c "
        "\"from pathlib import Path; assert Path('generated.txt').exists()\""
    ]
    data["autoCommit"] = auto_commit
    data["mergeOnClose"] = merge_on_close
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
import re
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
branch = subprocess.check_output(
    ["git", "branch", "--show-current"], text=True
).strip()
sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
branch = os.environ.get("GH_HEAD_REF_NAME", branch)
sha = os.environ.get("GH_HEAD_REF_OID", sha)
state_dir = os.environ.get("GH_STATE_DIR")


def merge_marker(pr_url):
    safe = re.sub(r"[^A-Za-z0-9]+", "-", pr_url)
    return Path(state_dir) / f"merged-{safe}"


if args[:2] == ["pr", "merge"] and state_dir:
    pr_url = args[2]
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], check=True)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    merge_marker(pr_url).write_text("merged\n", encoding="utf-8")
    raise SystemExit(0)

if args[:2] == ["pr", "view"]:
    pr_url = "https://example.test/pull/1"
    if len(args) > 2 and not args[2].startswith("--"):
        pr_url = args[2]
    state = os.environ.get("GH_PR_STATE", "OPEN")
    if state_dir and merge_marker(pr_url).exists():
        state = "MERGED"
    # Simulate GitHub API propagation lag: view calls 2..N+1 report the sha
    # seen at the first view call instead of the live branch head.
    stale_views = int(os.environ.get("GH_STALE_OID_VIEWS", "0"))
    if stale_views and state_dir:
        sd = Path(state_dir)
        sd.mkdir(parents=True, exist_ok=True)
        counter = sd / "view-count"
        count = (int(counter.read_text()) if counter.exists() else 0) + 1
        counter.write_text(str(count))
        first_sha_file = sd / "first-view-sha"
        if count == 1:
            first_sha_file.write_text(sha)
        elif count <= 1 + stale_views:
            sha = first_sha_file.read_text().strip()
    print(json.dumps({
        "url": pr_url,
        "headRefName": branch,
        "headRefOid": sha,
        "state": state,
        "mergeable": os.environ.get("GH_PR_MERGEABLE", "MERGEABLE"),
        "isDraft": os.environ.get("GH_PR_DRAFT") == "1",
        "mergeCommit": {"oid": os.environ.get("GH_MERGE_COMMIT", sha)},
    }))
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


def add_origin_remote(repo: Path, root: Path) -> Path:
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True)
    return origin


def seed_closing_published_phase(
    repo: Path, home: Path, *, status: str = "CLOSING"
) -> str:
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
            status=status,
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


def seed_complete_published_phase(repo: Path, home: Path) -> str:
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
            status="COMPLETE",
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


def seed_blocked_published_phase(
    repo: Path, home: Path, *, content_hash: Optional[str] = None
) -> str:
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
        phase = create_phase(
            db,
            project_id=project["id"],
            plan_id=plan["id"],
            phase_number=parsed_phase.phase_number,
            title=parsed_phase.title,
            content_hash=content_hash or parsed_phase.content_hash,
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
        update_phase_status(db, phase["id"], "BLOCKED")
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
            self.assertIn(
                "[agent-runner] phase 1 PR #1 opened: "
                "https://example.test/pull/1",
                result.stderr,
            )
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
                events = db.execute(
                    "SELECT event_type, message FROM events ORDER BY id"
                ).fetchall()
            self.assertEqual(project["status"], "COMPLETE")
            self.assertEqual(plan["status"], "COMPLETE")
            self.assertEqual([job["type"] for job in jobs][-1], "CLOSE_PHASE")
            self.assertIn(
                (
                    "phase.published",
                    "phase 1 published to PR #1 (https://example.test/pull/1)",
                ),
                [(event["event_type"], event["message"]) for event in events],
            )

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

    def test_merge_on_close_merges_and_starts_next_phase_on_fresh_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            gh_state = root / "gh-state"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=2, status="CLOSING")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_closing_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_STATE_DIR": str(gh_state),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("[agent-runner] phase 1 PR #1 merged (squash)", result.stderr)
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
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
                event_messages = [
                    row["message"]
                    for row in db.execute(
                        "SELECT message FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertIn(
                (2, "IMPLEMENT"), [(row["phase_number"], row["type"]) for row in jobs]
            )
            self.assertIn("phase.merged", event_types)
            self.assertIn(
                "merged phase 1 PR #1 (https://example.test/pull/1) (squash)",
                event_messages,
            )
            self.assertIn("phase.branch_created", event_types)

            current_branch = subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=repo, text=True
            ).strip()
            self.assertEqual(current_branch, "dev/phase-02-second-phase")
            origin_plan = subprocess.check_output(
                ["git", "show", "origin/main:docs/plan.md"], cwd=repo, text=True
            )
            self.assertIn("## Phase 1: First phase\nStatus: COMPLETE", origin_plan)

    def test_run_reconciles_manually_merged_blocked_phase_and_starts_next_phase(self):
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
            write_plan(repo, phase_count=2, status="COMPLETE")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            merge_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            seed_blocked_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_PR_STATE": "MERGED",
                    "GH_MERGE_COMMIT": merge_commit,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(
                "reconciled phase 1 from manually merged PR #1", result.stderr
            )
            self.assertIn("BLOCKED after IMPLEMENT failure", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            self.assertIsNone(rows[0]["blocked_from"])
            self.assertEqual(rows[0]["published_sha"], merge_commit)
            self.assertEqual(rows[1]["status"], "BLOCKED")
            self.assertTrue((repo / "phase2-started.txt").exists())
            with connect_db(home) as db:
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertIn("phase.reconciled", event_types)

    def test_manually_merged_phase_blocks_when_plan_marker_is_missing(self):
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
            write_plan(repo, status="PENDING")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            merge_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            seed_blocked_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_PR_STATE": "MERGED",
                    "GH_MERGE_COMMIT": merge_commit,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "plan marker does not prove completion: phase status is PENDING",
                result.stderr,
            )
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            with connect_db(home) as db:
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertNotIn("phase.reconciled", event_types)

    def test_manually_merged_phase_blocks_when_plan_body_hash_mismatches(self):
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
            write_plan(repo, status="PENDING")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            stale_hash = parse_plan_file(repo, "docs/plan.md").phases[0].content_hash
            write_custom_plan(
                repo,
                status="COMPLETE",
                phase_1_body="Create a different generated marker.",
            )
            commit_all(repo, "manual merge changed plan body")
            add_origin_remote(repo, root)
            merge_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            seed_blocked_published_phase(repo, home, content_hash=stale_hash)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_PR_STATE": "MERGED",
                    "GH_MERGE_COMMIT": merge_commit,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("plan body hash does not match registered phase", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertEqual(rows[0]["content_hash"], stale_hash)

    def test_run_does_not_reconcile_non_merged_phase_pr(self):
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
            write_plan(repo, status="COMPLETE")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            seed_blocked_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_PR_STATE": "OPEN",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("phase 1 is BLOCKED", result.stderr)
            self.assertNotIn("reconciled phase 1", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            with connect_db(home) as db:
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertNotIn("phase.reconciled", event_types)

    def _run_merge_preflight_case(self, extra_env: dict[str, str]):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            gh_state = root / "gh-state"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=1, status="CLOSING")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_closing_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_STATE_DIR": str(gh_state),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    **extra_env,
                },
            )
            rows = phase_rows(home, repo)
            with connect_db(home) as db:
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            return result, rows, event_types

    def test_draft_pr_blocks_merge_on_close(self):
        result, rows, event_types = self._run_merge_preflight_case(
            {"GH_PR_DRAFT": "1"}
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("phase PR is a draft", result.stderr)
        self.assertEqual(rows[0]["status"], "BLOCKED")
        self.assertNotIn("phase.merged", event_types)

    def test_conflicting_pr_blocks_merge_on_close(self):
        result, rows, event_types = self._run_merge_preflight_case(
            {"GH_PR_MERGEABLE": "CONFLICTING"}
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("merge conflicts with the base branch", result.stderr)
        self.assertEqual(rows[0]["status"], "BLOCKED")
        self.assertNotIn("phase.merged", event_types)

    def test_stale_pr_head_retries_then_merges(self):
        # The first merge-preflight views see the pre-close sha (GitHub API
        # lag); the retry loop should absorb it and still merge.
        result, rows, event_types = self._run_merge_preflight_case(
            {
                "GH_STALE_OID_VIEWS": "2",
                "AGENT_RUNNER_MERGE_RETRY_SECONDS": "0",
            }
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retrying in 0s", result.stderr)
        self.assertEqual(rows[0]["status"], "COMPLETE")
        self.assertIn("phase.merged", event_types)

    def test_stale_pr_head_blocks_after_retries_exhausted_then_unblocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            gh_state = root / "gh-state"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=1, status="CLOSING")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_closing_published_phase(repo, home)
            base_env = {
                "TRACE_DIR": str(trace),
                "GH_STATE_DIR": str(gh_state),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "AGENT_RUNNER_MERGE_RETRY_SECONDS": "0",
            }

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={**base_env, "GH_STALE_OID_VIEWS": "10"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("after 5 attempts", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertEqual(rows[0]["blocked_from"], "MERGING")

            unblock = run_cli(repo, home, "unblock", extra_env=base_env)
            self.assertEqual(unblock.returncode, 0, unblock.stderr)
            self.assertIn("unblocked to MERGING", unblock.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "MERGING")
            self.assertIsNone(rows[0]["blocked_from"])

            # The stale window has passed; resuming should merge without
            # re-running the closer.
            resume = run_cli(repo, home, "run", extra_env=base_env)

            self.assertEqual(resume.returncode, 0, resume.stderr)
            self.assertIn("resuming merge", resume.stderr)
            self.assertIn("plan complete", resume.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            with connect_db(home) as db:
                close_jobs = db.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE type = 'CLOSE_PHASE'"
                ).fetchone()["n"]
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertEqual(close_jobs, 1)
            self.assertIn("phase.unblocked", event_types)
            self.assertIn("phase.merged", event_types)

    def test_already_merged_pr_completes_on_merging_resume(self):
        # An operator can merge the phase PR by hand while the phase is
        # blocked; resuming from MERGING should accept that and complete
        # without invoking gh pr merge.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            gh_state = root / "gh-state"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=1, status="COMPLETE")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_closing_published_phase(repo, home, status="MERGING")

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_STATE_DIR": str(gh_state),
                    "GH_PR_STATE": "MERGED",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "[agent-runner] phase PR #1 already merged; skipping merge",
                result.stderr,
            )
            self.assertIn("plan complete", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            # gh pr merge writes a marker file; it must not have run.
            self.assertFalse(list(gh_state.glob("merged-*")) if gh_state.exists() else [])
            with connect_db(home) as db:
                close_jobs = db.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE type = 'CLOSE_PHASE'"
                ).fetchone()["n"]
            self.assertEqual(close_jobs, 0)

    def test_unblock_without_blocked_phase_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            script = root / "phase7_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_plan(repo, phase_count=1, status="CLOSING")
            write_config(repo, script)
            commit_all(repo)
            seed_closing_published_phase(repo, home)

            result = run_cli(repo, home, "unblock")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no BLOCKED phase", result.stderr)

    def test_close_without_merge_on_close_stops_before_next_phase(self):
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

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("merge PR", result.stderr)
            self.assertIn("merge PR #1 (https://example.test/pull/1)", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            self.assertEqual(rows[1]["status"], "PENDING")
            with connect_db(home) as db:
                phase2_jobs = db.execute(
                    """
                    SELECT jobs.id
                    FROM jobs
                    JOIN phases ON phases.id = jobs.phase_id
                    WHERE phases.phase_number = 2
                    """
                ).fetchall()
            self.assertEqual(phase2_jobs, [])

    def test_pending_phase_blocked_when_previous_pr_unmerged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            gh_state = root / "gh-state"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=2, status="COMPLETE")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            seed_complete_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_STATE_DIR": str(gh_state),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("not MERGED", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            self.assertEqual(rows[1]["status"], "BLOCKED")

    def test_existing_phase_branch_with_unique_commits_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            gh_state = root / "gh-state"
            bin_dir = root / "bin"
            script = root / "phase7_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase7_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, phase_count=1, status="PENDING")
            write_config(repo, script, auto_commit=True, merge_on_close=True)
            commit_all(repo)
            add_origin_remote(repo, root)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/phase-01-first-phase"],
                cwd=repo,
                check=True,
            )
            (repo / "stray.txt").write_text("stray work\n", encoding="utf-8")
            commit_all(repo, "stray commit not on main")
            subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_STATE_DIR": str(gh_state),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("already exists with commits", result.stderr)
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "BLOCKED")

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
