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
        "## Phase 6: REVIEW and FIX convergence loop\n"
        f"Status: {status}\n\n"
        "Create generated.txt and converge through review.\n\n"
        "Acceptance Criteria:\n"
        "- generated.txt exists.\n",
        encoding="utf-8",
    )


def write_config(
    repo: Path,
    agent_script: Path,
    *,
    checks: list[str],
    max_retries: int = 3,
    auto_commit: bool = False,
    coder_args: Optional[list[str]] = None,
    coder_fallback: bool = False,
    reviewer_args: Optional[list[str]] = None,
    reviewer_fallback: bool = False,
    review_triage: bool = False,
    triage_args: Optional[list[str]] = None,
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
    data.pop("reviewTriage", None)
    data["autoFixAttempts"] = 0
    if coder_args is not None:
        data["agents"]["special-coder"] = {
            "command": sys.executable,
            "promptArgs": [str(agent_script), *coder_args],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
        data["roles"]["coder"] = "special-coder"
    if reviewer_args is not None:
        data["agents"]["special-reviewer"] = {
            "command": sys.executable,
            "promptArgs": [str(agent_script), *reviewer_args],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
        data["roles"]["reviewer"] = "special-reviewer"
    if review_triage:
        data["agents"]["simple-reviewer"] = {
            "command": sys.executable,
            "promptArgs": [
                str(agent_script),
                "--review-profile",
                "simple",
                *(triage_args or []),
            ],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
        data["agents"]["complex-reviewer"] = {
            "command": sys.executable,
            "promptArgs": [str(agent_script), "--review-profile", "complex"],
            "writeFlags": [],
            "readOnlyFlags": [],
            "outputCapture": "stdout",
        }
        data["reviewTriage"] = {
            "simple": "simple-reviewer",
            "complex": "complex-reviewer",
        }
        data["roles"]["reviewer"] = "complex-reviewer"
    role_fallbacks = {}
    if coder_fallback:
        role_fallbacks["coder"] = ["fake"]
    if reviewer_fallback:
        role_fallbacks["reviewer"] = ["fake"]
    if role_fallbacks:
        data["roleFallbacks"] = role_fallbacks
    data["checks"] = checks
    data["maxRetriesPerPhase"] = max_retries
    data["autoCommit"] = auto_commit
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_phase6_agent(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

prompt = sys.argv[-1]
mode = os.environ.get("AGENT_MODE", "PASS")
trace_dir = Path(os.environ["TRACE_DIR"])
trace_dir.mkdir(parents=True, exist_ok=True)

if "Classify this phase review into one reviewer tier" in prompt:
    (trace_dir / f"triage-{len(list(trace_dir.glob('triage-*.md'))) + 1}.md").write_text(
        prompt,
        encoding="utf-8",
    )
    if "--triage-fail" in sys.argv:
        print("triage timeout or crash", file=sys.stderr)
        raise SystemExit(1)
    if "--triage-garbage" in sys.argv:
        print("not json from triage")
        raise SystemExit(0)
    print(json.dumps({"tier": os.environ.get("TRIAGE_TIER", "simple")}))
    raise SystemExit(0)

if (
    "Review the staged phase work independently" in prompt
    or "Review the published phase PR independently" in prompt
):
    review_number = len(list(trace_dir.glob("review-*.md"))) + 1
    (trace_dir / f"review-{review_number}.md").write_text(prompt, encoding="utf-8")
    if "--review-profile" in sys.argv:
        profile = sys.argv[sys.argv.index("--review-profile") + 1]
        (trace_dir / f"review-profile-{review_number}.txt").write_text(
            profile,
            encoding="utf-8",
        )
    if "--quota-fail" in sys.argv:
        print("codex quota exceeded: 429 Too Many Requests", file=sys.stderr)
        raise SystemExit(1)
    if "--hard-fail" in sys.argv:
        print("reviewer crashed for unrelated reasons", file=sys.stderr)
        raise SystemExit(1)
    if mode == "GARBAGE":
        print("not json from reviewer")
        raise SystemExit(0)
    if mode == "BLOCKED":
        print(json.dumps({
            "status": "BLOCKED",
            "summary": "reviewer cannot proceed",
            "blockingIssues": ["external blocker"],
            "nonBlockingIssues": ["non-gating note"],
            "recommendedFixPrompt": ""
        }))
        raise SystemExit(0)
    if mode == "FENCED_PASS":
        print("```json")
        print(json.dumps({
            "status": "PASS",
            "summary": "accepted in markdown",
            "blockingIssues": [],
            "nonBlockingIssues": ["Nice to Have: optional cleanup"],
            "recommendedFixPrompt": ""
        }))
        print("```")
        raise SystemExit(0)
    if mode == "REVIEW_FIX" and not Path("fix-marker.txt").exists():
        print(json.dumps({
            "status": "CHANGES_REQUESTED",
            "summary": "fix marker is missing",
            "blockingIssues": ["Create fix-marker.txt"],
            "nonBlockingIssues": ["Should Fix: tidy wording"],
            "recommendedFixPrompt": "Create the marker"
        }))
        raise SystemExit(0)
    if mode == "REVIEW_DRIP_FEED":
        if not Path("fix-marker.txt").exists():
            print(json.dumps({
                "status": "CHANGES_REQUESTED",
                "summary": "fix marker is missing",
                "blockingIssues": ["Create fix-marker.txt"],
                "nonBlockingIssues": [],
                "recommendedFixPrompt": "Create the marker"
            }))
            raise SystemExit(0)
        print(json.dumps({
            "status": "CHANGES_REQUESTED",
            "summary": "second review found a new blocker",
            "blockingIssues": ["Create second-marker.txt"],
            "nonBlockingIssues": [],
            "recommendedFixPrompt": "Create the second marker"
        }))
        raise SystemExit(0)
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "blockingIssues": [],
        "nonBlockingIssues": ["Nice to Have: optional cleanup"],
        "recommendedFixPrompt": ""
    }))
    raise SystemExit(0)

if "Fix only" in prompt:
    (trace_dir / f"fix-{len(list(trace_dir.glob('fix-*.md'))) + 1}.md").write_text(
        prompt,
        encoding="utf-8",
    )
    if "--quota-fail-fix" in sys.argv:
        print("coder quota exceeded: 429 Too Many Requests", file=sys.stderr)
        raise SystemExit(1)
    Path("fix-marker.txt").write_text("fixed\n", encoding="utf-8")
    if os.environ.get("AGENT_PUBLISH") == "1":
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run([
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-qm",
            "fix phase",
        ], check=True)
    print("fake fixer completed")
    raise SystemExit(0)

if "Close the accepted phase" in prompt:
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
    raise SystemExit(0)

if "--quota-fail-implement" in sys.argv:
    print("coder quota exceeded: 429 Too Many Requests", file=sys.stderr)
    raise SystemExit(1)
Path("generated.txt").write_text("created\n", encoding="utf-8")
if os.environ.get("AGENT_PUBLISH") == "1":
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run([
        "git",
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test User",
        "commit",
        "-qm",
        "implement phase",
    ], check=True)
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
    }))
    raise SystemExit(0)

if args[:2] == ["pr", "diff"]:
    if "--stat" in args:
        subprocess.run(["git", "show", "--format=", "--stat", "HEAD"], check=True)
    else:
        subprocess.run(["git", "show", "--format=", "--patch", "HEAD"], check=True)
    raise SystemExit(0)

print(f"unsupported gh args: {args}", file=sys.stderr)
raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


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


def seed_reviewing_published_phase(repo: Path, home: Path) -> str:
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
            status="REVIEWING",
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


class Phase6LoopTests(unittest.TestCase):
    def test_review_triage_simple_routes_review_to_simple_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[], review_triage=True)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "TRIAGE_TIER": "simple"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "review triage: phase 6 tier=simple; reviewing with profile simple-reviewer",
                result.stderr,
            )
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual(
                [job["type"] for job in phase_jobs],
                ["IMPLEMENT", "RUN_CHECKS", "TRIAGE", "REVIEW", "CLOSE_PHASE"],
            )
            self.assertEqual(
                (trace / "review-profile-1.txt").read_text(encoding="utf-8"),
                "simple",
            )
            triage_events = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "review.triage"
            ]
            self.assertEqual(len(triage_events), 1)
            event_data = json.loads(triage_events[0]["data_json"])
            self.assertEqual(event_data["tier"], "simple")
            self.assertEqual(event_data["profile"], "simple-reviewer")

    def test_review_triage_complex_routes_review_to_complex_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[], review_triage=True)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "TRIAGE_TIER": "complex"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "review triage: phase 6 tier=complex; reviewing with profile complex-reviewer",
                result.stderr,
            )
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertEqual(
                (trace / "review-profile-1.txt").read_text(encoding="utf-8"),
                "complex",
            )

    def test_review_triage_garbage_routes_to_complex_and_phase_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[],
                review_triage=True,
                triage_args=["--triage-garbage"],
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "review triage: phase 6 tier=complex; reviewing with profile complex-reviewer",
                result.stderr,
            )
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertEqual(
                (trace / "review-profile-1.txt").read_text(encoding="utf-8"),
                "complex",
            )
            triage_events = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "review.triage"
            ]
            event_data = json.loads(triage_events[0]["data_json"])
            self.assertEqual(event_data["tier"], "complex")
            self.assertIn("invalid triage JSON", event_data["reason"])

    def test_review_without_triage_does_not_spawn_triage_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertNotIn("TRIAGE", [job["type"] for job in jobs(home, phase["id"])])

    def test_review_pass_advances_to_closing_without_coder_output_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
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

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("plan complete", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertEqual(phase["retry_count"], 0)
            review_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            self.assertIn("git diff --staged", review_prompt)
            self.assertIn("If a `pr-review` skill or workflow is available", review_prompt)
            self.assertIn("Verify the phase acceptance criteria", review_prompt)
            self.assertIn("severity, affected file/line", review_prompt)
            self.assertNotIn("fake coder completed", review_prompt)

    def test_reviewer_markdown_fenced_json_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "FENCED_PASS"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            review = json.loads(
                (Path(phase["log_dir"]) / "review.json").read_text(encoding="utf-8")
            )
            self.assertEqual(review["status"], "PASS")
            self.assertEqual(review["summary"], "accepted in markdown")

    def test_auto_commit_requires_published_pr_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase6_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; assert Path('generated.txt').exists()\""
                ],
                auto_commit=True,
            )
            commit_all(repo)
            subprocess.run(["git", "checkout", "-q", "-b", "dev/test-phase"], cwd=repo, check=True)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "AGENT_PUBLISH": "1",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("plan complete", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertEqual(phase["publish_mode"], "pr")
            self.assertEqual(phase["branch_name"], "dev/test-phase")
            self.assertEqual(phase["pr_url"], "https://example.test/pull/1")
            self.assertEqual(len(phase["published_sha"]), 40)
            review_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            self.assertIn("Review the published phase PR independently", review_prompt)
            self.assertIn("Published PR: https://example.test/pull/1", review_prompt)
            self.assertIn("published PR diff", review_prompt)
            self.assertIn("generated.txt", review_prompt)
            self.assertNotIn("fake coder completed", review_prompt)

    def test_auto_commit_blocks_unpublished_work_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; assert Path('generated.txt').exists()\""
                ],
                auto_commit=True,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 1)
            self.assertIn("BLOCKED before REVIEW", result.stderr)
            self.assertIn("worktree is dirty", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            phase_jobs = jobs(home, phase["id"])
            self.assertNotIn("REVIEW", [job["type"] for job in phase_jobs])

    def test_auto_commit_blocks_merged_stored_pr_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase6_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, status="REVIEWING")
            write_config(repo, script, checks=[], auto_commit=True)
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_reviewing_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_PR_STATE": "MERGED",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("phase PR is MERGED", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            phase_jobs = jobs(home, phase["id"])
            self.assertNotIn("REVIEW", [job["type"] for job in phase_jobs])

    def test_auto_commit_blocks_stored_pr_branch_mismatch_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase6_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, status="REVIEWING")
            write_config(repo, script, checks=[], auto_commit=True)
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_reviewing_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_HEAD_REF_NAME": "dev/other-phase",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("stored phase PR branch changed", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertNotIn(
                "REVIEW", [job["type"] for job in jobs(home, phase["id"])]
            )

    def test_auto_commit_blocks_stored_pr_sha_mismatch_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "phase6_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_plan(repo, status="REVIEWING")
            write_config(repo, script, checks=[], auto_commit=True)
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )
            seed_reviewing_published_phase(repo, home)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "GH_HEAD_REF_OID": "abc123456789",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("stored phase PR head changed", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertNotIn(
                "REVIEW", [job["type"] for job in jobs(home, phase["id"])]
            )

    def test_review_changes_requested_runs_fix_then_reruns_checks_and_rereview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
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

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "REVIEW_FIX"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertEqual(phase["retry_count"], 1)
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual(
                [(job["type"], job["trigger"]) for job in phase_jobs],
                [
                    ("IMPLEMENT", None),
                    ("RUN_CHECKS", None),
                    ("REVIEW", None),
                    ("FIX", "review"),
                    ("RUN_CHECKS", None),
                    ("REVIEW", None),
                    ("CLOSE_PHASE", None),
                ],
            )
            first_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            second_prompt = (trace / "review-2.md").read_text(encoding="utf-8")
            fix_prompt = (trace / "fix-1.md").read_text(encoding="utf-8")
            self.assertNotIn("fake coder completed", first_prompt)
            self.assertIn("Previous review.json", second_prompt)
            self.assertIn(
                "Verify these blocking issues are resolved; only new Blocking findings may block.",
                second_prompt,
            )
            self.assertIn("Create fix-marker.txt", second_prompt)
            self.assertIn("Create fix-marker.txt", fix_prompt)
            self.assertNotIn("Should Fix: tidy wording", fix_prompt)

    def test_review_fix_limit_blocks_after_one_rereview_without_second_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[], max_retries=3)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "REVIEW_DRIP_FEED"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("review fix limit exhausted", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertEqual(phase["retry_count"], 1)
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("REVIEW"), 2)
            self.assertEqual([job["type"] for job in phase_jobs].count("FIX"), 1)
            first_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            self.assertIn("Make one comprehensive pass", first_prompt)

    def test_review_blocked_stops_without_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "BLOCKED"},
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(phase_row(home, repo)["status"], "BLOCKED")
            phase_jobs = jobs(home, phase_row(home, repo)["id"])
            self.assertNotIn("FIX", [job["type"] for job in phase_jobs])

    def test_checks_fix_cycle_exhausts_retries_and_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"import sys; print('still failing'); sys.exit(2)\""
                ],
                max_retries=2,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 1)
            self.assertIn("retries exhausted", result.stderr)
            self.assertIn("outstanding checks blockers", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            self.assertEqual(phase["retry_count"], 2)
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("FIX"), 2)
            self.assertEqual([job["type"] for job in phase_jobs].count("RUN_CHECKS"), 3)

    def test_reviewer_non_json_blocks_and_preserves_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(repo, script, checks=[])
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "GARBAGE"},
            )

            self.assertEqual(result.returncode, 1)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            review_log = Path(phase["log_dir"]) / "review.log"
            self.assertIn("not json from reviewer", review_log.read_text(encoding="utf-8"))
            self.assertFalse((Path(phase["log_dir"]) / "review.json").exists())

    def test_review_quota_failure_falls_back_to_fallback_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[],
                reviewer_args=["--quota-fail"],
                reviewer_fallback=True,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("falling back to profile 'fake'", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            review_jobs = [
                job for job in jobs(home, phase["id"]) if job["type"] == "REVIEW"
            ]
            self.assertEqual(
                [job["status"] for job in review_jobs], ["FAILED", "SUCCEEDED"]
            )
            fallback_events = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "review.fallback"
            ]
            self.assertEqual(len(fallback_events), 1)
            self.assertIn("quota/rate limit", fallback_events[0]["message"])

    def test_implement_quota_failure_falls_back_to_coder_fallback_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[],
                coder_args=["--quota-fail-implement"],
                coder_fallback=True,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("falling back to profile 'fake'", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertTrue((repo / "generated.txt").exists())
            implement_jobs = [
                job for job in jobs(home, phase["id"]) if job["type"] == "IMPLEMENT"
            ]
            self.assertEqual(
                [job["status"] for job in implement_jobs], ["FAILED", "SUCCEEDED"]
            )
            fallback_events = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "implement.fallback"
            ]
            self.assertEqual(len(fallback_events), 1)
            self.assertIn("quota/rate limit", fallback_events[0]["message"])

    def test_fix_quota_failure_falls_back_to_coder_fallback_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; assert Path('generated.txt').exists()\""
                ],
                coder_args=["--quota-fail-fix"],
                coder_fallback=True,
            )
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={"TRACE_DIR": str(trace), "AGENT_MODE": "REVIEW_FIX"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("falling back to profile 'fake'", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertTrue((repo / "fix-marker.txt").exists())
            fix_jobs = [job for job in jobs(home, phase["id"]) if job["type"] == "FIX"]
            self.assertEqual(
                [job["status"] for job in fix_jobs], ["FAILED", "SUCCEEDED"]
            )
            fallback_events = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "fix.fallback"
            ]
            self.assertEqual(len(fallback_events), 1)
            self.assertIn("quota/rate limit", fallback_events[0]["message"])

    def test_review_non_quota_failure_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_plan(repo)
            write_config(
                repo,
                script,
                checks=[],
                reviewer_args=["--hard-fail"],
                reviewer_fallback=True,
            )
            commit_all(repo)

            result = run_cli(repo, home, "run", extra_env={"TRACE_DIR": str(trace)})

            self.assertEqual(result.returncode, 1)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            review_jobs = [
                job for job in jobs(home, phase["id"]) if job["type"] == "REVIEW"
            ]
            self.assertEqual([job["status"] for job in review_jobs], ["FAILED"])
            fallback_events = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "review.fallback"
            ]
            self.assertEqual(fallback_events, [])


if __name__ == "__main__":
    unittest.main()
