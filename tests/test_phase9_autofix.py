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
    auto_commit: bool = False,
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
    data.pop("reviewTriage", None)
    data.pop("presets", None)
    data["checks"] = [
        f"{shlex.quote(sys.executable)} -c "
        "\"from pathlib import Path; assert Path('fixed.txt').exists()\""
    ]
    data["maxRetriesPerPhase"] = 0
    if auto_fix_attempts is None:
        data.pop("autoFixAttempts", None)
    else:
        data["autoFixAttempts"] = auto_fix_attempts
    data["autoCommit"] = auto_commit
    data["mergeOnClose"] = False
    data["timeoutMinutes"] = 1
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_autofix_agent(path: Path) -> None:
    path.write_text(
        r"""
import json
import os
import re
import subprocess
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
    if os.environ.get("AUTOFIX_MODE") == "CRASH":
        print("auto-fix crashed for test", file=sys.stderr)
        raise SystemExit(1)
    Path("fixed.txt").write_text("fixed by auto-fix\n", encoding="utf-8")
    if "Publish requirements before you finish" in prompt:
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run([
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-qm",
            "auto-fix phase",
        ], check=True)
    print("auto-fix completed")
    raise SystemExit(0)

if (
    "Review the staged phase work independently" in prompt
    or "Review the published phase PR independently" in prompt
):
    review_attempt = len(list(trace_dir.glob("review-*.md"))) + 1
    (trace_dir / f"review-{review_attempt}.md").write_text(prompt, encoding="utf-8")
    if os.environ.get("REVIEW_BLOCK_ONCE") and not (trace_dir / "review-blocked").exists():
        (trace_dir / "review-blocked").write_text("blocked\n", encoding="utf-8")
        print(json.dumps({
            "status": "BLOCKED",
            "summary": "published review needs auto-fix",
            "findings": {
                "blocking": ["fixed.txt is missing from the published diff"],
                "shouldFix": [],
                "nitpick": []
            }
        }))
        raise SystemExit(0)
    print(json.dumps({
        "status": "PASS",
        "summary": "accepted",
        "findings": {"blocking": [], "shouldFix": [], "nitpick": []}
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
if "Publish requirements before you finish" in prompt:
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
print("fake implement completed")
raise SystemExit(0)
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

def write_post(kind, pr_url, action, body_file):
    state_dir = os.environ.get("GH_STATE_DIR")
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, f"github-{kind}.json"), "w", encoding="utf-8") as fh:
            json.dump({"kind": kind, "prUrl": pr_url, "action": action}, fh)
        with open(body_file, encoding="utf-8") as fh:
            body = fh.read()
        with open(os.path.join(state_dir, f"github-{kind}-body.md"), "w", encoding="utf-8") as fh:
            fh.write(body)
    raise SystemExit(0)

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
    subprocess.run(["git", "show", "--format=", "--patch", "HEAD"], check=True)
    raise SystemExit(0)

if args[:2] == ["pr", "review"]:
    action = "--approve" if "--approve" in args else "--request-changes"
    body_file = args[args.index("--body-file") + 1]
    write_post("review", args[2], action, body_file)

if args[:2] == ["pr", "comment"]:
    body_file = args[args.index("--body-file") + 1]
    write_post("comment", args[2], "comment", body_file)

if args[:2] == ["issue", "create"]:
    if os.environ.get("GH_ISSUE_FAIL"):
        print("issue create failed for test", file=sys.stderr)
        raise SystemExit(1)
    title = args[args.index("--title") + 1]
    with open(args[args.index("--body-file") + 1]) as fh:
        body = fh.read()
    trace_dir = os.environ.get("TRACE_DIR")
    if trace_dir:
        os.makedirs(trace_dir, exist_ok=True)
        count = len([n for n in os.listdir(trace_dir) if n.startswith("issue-")]) + 1
        with open(os.path.join(trace_dir, f"issue-{count}.md"), "w") as fh:
            fh.write(title + "\n---\n" + body)
    print("https://example.test/issues/1")
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
            self.assertNotIn("Phase PR URL:", autofix_prompt)
            self.assertNotIn("Review JSON path:", autofix_prompt)
            review_prompt = (trace / "review-1.md").read_text(encoding="utf-8")
            self.assertIn("fixed.txt", review_prompt)
            self.assertIn("git diff --staged", review_prompt)
            self.assertIn("Checks log path:", review_prompt)
            self.assertNotIn("fixed by auto-fix", review_prompt)
            phase_events = events(home, phase["id"])
            autofix_events = [
                event for event in phase_events if event["event_type"] == "phase.autofix"
            ]
            self.assertEqual(len(autofix_events), 1)
            unblocked_events = [
                event for event in phase_events if event["event_type"] == "phase.unblocked"
            ]
            self.assertEqual(json.loads(unblocked_events[0]["data_json"])["to"], "CHECKING")

    def test_autofix_publish_prompt_allows_autocommit_run_to_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "autofix_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_config(repo, script, auto_fix_attempts=2, auto_commit=True)
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-phase"],
                cwd=repo,
                check=True,
            )

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
            self.assertNotIn("worktree is dirty", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            self.assertEqual(phase["publish_mode"], "pr")
            self.assertEqual(phase["branch_name"], "dev/test-phase")
            self.assertEqual(phase["pr_url"], "https://example.test/pull/1")
            autofix_prompt = (trace / "autofix-1.md").read_text(encoding="utf-8")
            self.assertIn("Publish requirements before you finish", autofix_prompt)
            self.assertIn("update the existing PR", autofix_prompt)
            self.assertNotIn("Phase PR URL:", autofix_prompt)
            self.assertNotIn("- Do not commit anything.", autofix_prompt)
            status = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=repo,
                text=True,
            )
            self.assertEqual(status, "")

    def test_autofix_refreshes_published_metadata_before_review_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "autofix_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_config(repo, script, auto_fix_attempts=2, auto_commit=True)
            config_path = repo / ".agent-runner.json"
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["checks"] = [
                f"{shlex.quote(sys.executable)} -c "
                "\"from pathlib import Path; assert Path('generated.txt').exists()\""
            ]
            config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            commit_all(repo)
            subprocess.run(
                ["git", "checkout", "-q", "-b", "dev/test-review-autofix"],
                cwd=repo,
                check=True,
            )

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "REVIEW_BLOCK_ONCE": "1",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("stored phase PR head changed", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("REVIEW"), 2)
            self.assertEqual([job["type"] for job in phase_jobs].count("AUTOFIX"), 1)
            autofix_prompt = (trace / "autofix-1.md").read_text(encoding="utf-8")
            self.assertIn("Phase PR URL: https://example.test/pull/1", autofix_prompt)
            self.assertIn("Review JSON path:", autofix_prompt)
            second_review = (trace / "review-2.md").read_text(encoding="utf-8")
            self.assertIn(f"Reviewed SHA: {phase['published_sha']}", second_review)
            self.assertIn("Previous review.json path:", second_review)
            published_files = subprocess.check_output(
                ["git", "show", "--format=", "--name-only", phase["published_sha"]],
                cwd=repo,
                text=True,
            )
            self.assertIn("fixed.txt", published_files)

    def test_autofix_budget_carries_across_separate_blocking_episodes(self):
        # The budget is cumulative for the phase's lifetime: an attempt spent
        # unblocking a checks failure must count against a later, unrelated
        # review block (episode two resumes at attempt 2/2, not 1/2).
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
                extra_env={"TRACE_DIR": str(trace), "REVIEW_BLOCK_ONCE": "1"},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("auto-fix attempt 1/2 with profile fake", result.stderr)
            self.assertIn("auto-fix attempt 2/2 with profile fake", result.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "COMPLETE")
            phase_jobs = jobs(home, phase["id"])
            job_types = [job["type"] for job in phase_jobs]
            self.assertEqual(job_types.count("AUTOFIX"), 2)
            self.assertEqual(job_types.count("REVIEW"), 2)

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

    def test_autofix_attempt_cap_persists_across_runner_restarts(self):
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
            extra_env = {"TRACE_DIR": str(trace), "AUTOFIX_MODE": "NOOP"}

            first = run_cli(repo, home, "run", extra_env=extra_env)
            self.assertEqual(first.returncode, 1)
            self.assertIn("auto-fix attempt 2/2 with profile fake", first.stderr)

            second = run_cli(repo, home, "run", extra_env=extra_env)
            self.assertEqual(second.returncode, 1)
            self.assertNotIn("auto-fix attempt", second.stderr)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("AUTOFIX"), 2)

    def test_autofix_exhausted_budget_posts_escalation_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "autofix_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_config(repo, script, auto_fix_attempts=2)
            commit_all(repo)
            extra_env = {
                "TRACE_DIR": str(trace),
                "AUTOFIX_MODE": "NOOP",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            }

            first = run_cli(repo, home, "run", extra_env=extra_env)

            self.assertEqual(first.returncode, 1)
            self.assertIn(
                "posted blocked-phase issue: https://example.test/issues/1",
                first.stderr,
            )
            issue = (trace / "issue-1.md").read_text(encoding="utf-8")
            self.assertIn("[agent-runner] phase 9 blocked:", issue)
            self.assertIn("auto-fix budget exhausted (2/2 attempts used)", issue)
            self.assertIn("Blocking event message:", issue)
            self.assertIn("Newest phase log tail", issue)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            escalated = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "phase.autofix_escalated"
            ]
            self.assertEqual(len(escalated), 1)
            self.assertEqual(
                json.loads(escalated[0]["data_json"])["issueUrl"],
                "https://example.test/issues/1",
            )

            # A restart sees the same exhausted budget and blocking message;
            # the recorded event must prevent a duplicate issue.
            second = run_cli(repo, home, "run", extra_env=extra_env)
            self.assertEqual(second.returncode, 1)
            self.assertNotIn("posted blocked-phase issue", second.stderr)
            self.assertFalse((trace / "issue-2.md").exists())

    def test_failed_autofix_job_posts_escalation_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "autofix_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_config(repo, script, auto_fix_attempts=2)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "AUTOFIX_MODE": "CRASH",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("posted blocked-phase issue", result.stderr)
            issue = (trace / "issue-1.md").read_text(encoding="utf-8")
            self.assertIn("auto-fix attempt 1/2", issue)
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            phase_jobs = jobs(home, phase["id"])
            self.assertEqual([job["type"] for job in phase_jobs].count("AUTOFIX"), 1)

    def test_escalation_post_failure_warns_without_recording_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            bin_dir = root / "bin"
            script = root / "autofix_agent.py"
            repo.mkdir()
            bin_dir.mkdir()
            git_init(repo)
            write_plan(repo)
            write_autofix_agent(script)
            write_fake_gh(bin_dir / "gh")
            write_config(repo, script, auto_fix_attempts=1)
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    "AUTOFIX_MODE": "NOOP",
                    "GH_ISSUE_FAIL": "1",
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "warning: could not post blocked-phase issue", result.stderr
            )
            phase = phase_row(home, repo)
            self.assertEqual(phase["status"], "BLOCKED")
            escalated = [
                event
                for event in events(home, phase["id"])
                if event["event_type"] == "phase.autofix_escalated"
            ]
            self.assertEqual(escalated, [])

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
