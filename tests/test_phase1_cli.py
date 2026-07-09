import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from agent_runner.config import (
    DEFAULT_CHECKS,
    NODE_CHECKS,
    PLACEHOLDER_CHECKS,
    SAMPLE_CONFIG,
    claude_read_only_allowed_tools,
    load_config,
    project_slug,
)
from agent_runner.errors import ConfigError, GitRepoError, LockError
from agent_runner.git import find_git_root
from agent_runner.lock import ProjectLock


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


def write_config(repo: Path, overrides: Optional[dict] = None) -> None:
    data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
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
    data["roleFallbacks"] = {}
    data.pop("reviewTriage", None)
    data["autoFixAttempts"] = 0
    data["autoCommit"] = False
    data["mergeOnClose"] = False
    if overrides:
        data.update(overrides)
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_plan(repo: Path) -> None:
    plan_path = repo / "docs" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "## Phase 1: Test phase\nStatus: REVIEWING\n\nAcceptance Criteria:\n- Pass.\n",
        encoding="utf-8",
    )


def _strip_sample_comments(text: str) -> str:
    from agent_runner.config import strip_json_comments

    return strip_json_comments(text)


class Phase1CliTests(unittest.TestCase):
    def test_version_flag_prints_package_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "cwd"
            home = Path(tmp) / "home"
            cwd.mkdir()

            result = run_cli(cwd, home, "--version")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "agent-runner 0.1.0\n")
            self.assertEqual(result.stderr, "")

    def test_init_creates_home_layout_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)

            result = run_cli(repo, home, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((home / "locks").is_dir())
            self.assertTrue((home / "logs").is_dir())
            config_path = repo / ".agent-runner.json"
            self.assertTrue(config_path.exists())
            self.assertIn("// Path to the markdown plan", config_path.read_text())
            config = load_config(repo)
            self.assertEqual(config.roles["coder"], "codex")
            self.assertEqual(config.roles["reviewer"], "claude-opus")
            self.assertEqual(config.roles["fixer"], "claude-opus")
            self.assertEqual(config.auto_fix_attempts, 2)
            self.assertEqual(config.agents["claude-opus"].prompt_prefix, "")
            self.assertIsNotNone(config.review_triage)
            self.assertEqual(config.review_triage.simple, "claude-sonnet")
            self.assertEqual(config.review_triage.complex, "claude-opus")
            self.assertEqual(config.checks, PLACEHOLDER_CHECKS)
            self.assertEqual(config.warnings, [])
            self.assertIn(
                "checks must be replaced before the first run",
                result.stderr,
            )
            self.assertIn(
                "next: review planPath/checks in .agent-runner.json",
                result.stderr,
            )
            self.assertIn("next: write docs/plan.md", result.stderr)
            self.assertIn("next: run `autorun run`", result.stderr)

            second = run_cli(repo, home, "init")

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already exists", second.stderr)

    def test_autorun_symlink_works_from_another_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_repo = Path(tmp) / "target"
            home = Path(tmp) / "home"
            bin_dir = Path(tmp) / "bin"
            target_repo.mkdir()
            bin_dir.mkdir()
            git_init(target_repo)
            symlink = bin_dir / "autorun"
            symlink.symlink_to(ROOT / "autorun")
            env = os.environ.copy()
            env["AGENT_RUNNER_HOME"] = str(home)

            result = subprocess.run(
                [str(symlink), "status"],
                cwd=target_repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[agent-runner] project:", result.stderr)
            self.assertIn("no plan registered yet", result.stderr)

    def test_init_detects_python_package_and_loads_without_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

            result = run_cli(repo, home, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(repo)
            self.assertEqual(config.checks, DEFAULT_CHECKS)
            self.assertEqual(config.warnings, [])
            self.assertNotIn("placeholder", result.stderr)

    def test_init_detects_package_json_and_loads_without_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            (repo / "package.json").write_text('{"scripts":{"test":"node --test"}}\n')

            result = run_cli(repo, home, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(repo)
            self.assertEqual(config.checks, NODE_CHECKS)
            self.assertEqual(config.warnings, [])

    def test_init_detects_tests_directory_with_python_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            tests_dir = repo / "tests"
            tests_dir.mkdir(parents=True)
            git_init(repo)
            (tests_dir / "test_demo.py").write_text("def test_placeholder():\n    pass\n")

            result = run_cli(repo, home, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(repo)
            self.assertEqual(config.checks, DEFAULT_CHECKS)
            self.assertEqual(config.warnings, [])

    def test_init_placeholder_check_loads_and_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            env = os.environ.copy()
            env["AGENT_RUNNER_HOME"] = str(home)

            result = subprocess.run(
                [str(ROOT / "autorun"), "init"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = load_config(repo)
            self.assertEqual(config.checks, PLACEHOLDER_CHECKS)
            self.assertEqual(config.warnings, [])
            check = subprocess.run(
                config.checks[0],
                cwd=repo,
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(check.returncode, 0)
            self.assertIn("replace the placeholder checks entry", check.stderr)

    def test_generated_claude_profiles_pin_models(self):
        data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))

        claude_profiles = [
            profile
            for profile in data["agents"].values()
            if profile["command"] == "claude"
        ]

        self.assertTrue(claude_profiles)
        for profile in claude_profiles:
            self.assertIn("--model", profile["promptArgs"])

    def test_claude_read_only_allowed_tools_stay_read_only(self):
        allowed_tools = claude_read_only_allowed_tools()
        tools = allowed_tools.split(",")

        self.assertIn("Bash(gh pr diff:*)", tools)
        self.assertIn("Bash(gh pr view:*)", tools)
        self.assertIn("Bash(gh pr checks:*)", tools)
        self.assertIn("Bash(gh api:*)", tools)
        self.assertIn("Bash(git diff:*)", tools)
        self.assertIn("Bash(git log:*)", tools)
        self.assertIn("Bash(git show:*)", tools)
        self.assertNotIn("Bash(gh:*)", tools)
        for forbidden in ("gh pr merge", "gh pr comment", "git push"):
            self.assertNotIn(forbidden, allowed_tools)

    def test_generated_claude_reviewer_flags_use_joined_allowed_and_disallowed(self):
        data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))

        for name in ("claude-opus", "claude-sonnet"):
            read_only_flags = data["agents"][name]["readOnlyFlags"]
            allowed = [
                flag for flag in read_only_flags if flag.startswith("--allowedTools=")
            ]
            disallowed = [
                flag for flag in read_only_flags if flag.startswith("--disallowedTools=")
            ]

            self.assertEqual(allowed, [f"--allowedTools={claude_read_only_allowed_tools()}"])
            self.assertEqual(disallowed, ["--disallowedTools=Edit,Write,NotebookEdit"])
            self.assertNotIn("--allowedTools", read_only_flags)
            self.assertNotIn("--disallowedTools", read_only_flags)

    def test_generated_antigravity_places_print_flag_after_role_flags(self):
        data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
        antigravity = data["agents"]["antigravity"]

        self.assertNotIn("-p", antigravity["promptArgs"])
        self.assertEqual(antigravity["writeFlags"][-1], "-p")
        self.assertEqual(antigravity["readOnlyFlags"][-1], "-p")

    def test_run_outside_git_and_missing_config_fail_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            home = Path(tmp) / "home"
            outside.mkdir()

            outside_result = run_cli(outside, home, "run")

            self.assertNotEqual(outside_result.returncode, 0)
            self.assertIn("not inside a git repository", outside_result.stderr)

            repo = Path(tmp) / "repo"
            repo.mkdir()
            git_init(repo)

            missing_config = run_cli(repo, home, "run")

            self.assertNotEqual(missing_config.returncode, 0)
            self.assertIn("missing .agent-runner.json", missing_config.stderr)

    def test_find_git_root_reports_missing_git_without_traceback(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(GitRepoError, "git executable was not found"):
                find_git_root()

    def test_project_slug_includes_absolute_repo_path_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first" / "backend"
            second = Path(tmp) / "second" / "backend"
            first.mkdir(parents=True)
            second.mkdir(parents=True)

            first_slug = project_slug(first)
            second_slug = project_slug(second)

            self.assertRegex(first_slug, r"^backend-[0-9a-f]{12}$")
            self.assertRegex(second_slug, r"^backend-[0-9a-f]{12}$")
            self.assertNotEqual(first_slug, second_slug)

    def test_live_lock_for_different_repo_path_reports_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            other_repo = Path(tmp) / "other" / "repo"
            locks = Path(tmp) / "locks"
            repo.mkdir()
            other_repo.mkdir(parents=True)
            locks.mkdir()
            lock_path = locks / "shared.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "repoPath": str(other_repo),
                        "startedAt": "2026-07-06T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            lock = ProjectLock(locks, "shared", repo)

            with self.assertRaisesRegex(LockError, "project lock collision"):
                lock.acquire()
            self.assertTrue(lock_path.exists())

    def test_config_validation_rejects_bad_roles_and_missing_profile_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git_init(repo)
            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["roles"]["reviewer"] = "missing-agent"
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "unknown agent profile"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            del data["agents"]["claude-opus"]["readOnlyFlags"]
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "missing required field"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["agents"]["claude-opus"]["promptPrefix"] = ["not", "a", "string"]
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "promptPrefix"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["roleFallbacks"] = {"reviewer": ["missing-agent"]}
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "unknown agent profile"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            self.assertEqual(
                data["reviewTriage"],
                {"simple": "claude-sonnet", "complex": "claude-opus"},
            )
            data["reviewTriage"]["simple"] = "missing-agent"
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "reviewTriage.simple"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["roleFallbacks"] = {"unknown-role": ["antigravity"]}
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "configured role"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["roleFallbacks"] = {"reviewer": ["antigravity"]}
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            config = load_config(repo)
            self.assertEqual(config.role_fallbacks, {"reviewer": ["antigravity"]})
            self.assertEqual(config.warnings, [])
            self.assertEqual(config.base_branch, "main")
            self.assertTrue(config.merge_on_close)
            self.assertEqual(config.merge_strategy, "squash")

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["roleFallbacks"] = {"coder": ["antigravity"]}
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            config = load_config(repo)
            self.assertEqual(config.role_fallbacks, {"coder": ["antigravity"]})
            self.assertEqual(config.warnings, [])

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["roles"]["planner"] = "claude-opus"
            data["roleFallbacks"] = {"planner": ["antigravity"]}
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            config = load_config(repo)
            self.assertEqual(config.role_fallbacks, {"planner": ["antigravity"]})
            self.assertEqual(config.warnings, [])

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["mergeStrategy"] = "octopus"
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "mergeStrategy"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["mergeOnClose"] = True
            data["autoCommit"] = False
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "mergeOnClose requires autoCommit"):
                load_config(repo)

    def test_empty_checks_are_accepted_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo, {"checks": []})
            write_plan(repo)

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("warning: config checks is empty", result.stderr)

    def test_concurrent_run_refuses_live_lock_and_reset_lock_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo)
            locks = home / "locks"
            locks.mkdir(parents=True)
            lock_path = locks / f"{project_slug(repo)}.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "repoPath": str(repo),
                        "startedAt": "2026-07-06T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(repo, home, "run")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already locked", result.stderr)
            self.assertTrue(lock_path.exists())

            reset = run_cli(repo, home, "reset-lock")

            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertFalse(lock_path.exists())

    def test_dead_pid_lock_is_reaped_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo)
            locks = home / "locks"
            locks.mkdir(parents=True)
            lock_path = locks / f"{project_slug(repo)}.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "repoPath": str(repo),
                        "startedAt": "2026-07-06T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(lock_path.exists())

    def test_non_object_lock_payload_is_reaped_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo)
            locks = home / "locks"
            locks.mkdir(parents=True)
            lock_path = locks / f"{project_slug(repo)}.lock"
            lock_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

            result = run_cli(repo, home, "run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(lock_path.exists())

    def test_sigint_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
            write_plan(repo)
            env = os.environ.copy()
            env["AGENT_RUNNER_HOME"] = str(home)
            env["AGENT_RUNNER_HOLD_SECONDS"] = "20"
            env["PYTHONPATH"] = str(ROOT)

            proc = subprocess.Popen(
                [sys.executable, "-m", "agent_runner", "run"],
                cwd=repo,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            lock_path = home / "locks" / f"{project_slug(repo)}.lock"
            deadline = time.time() + 5
            while time.time() < deadline and not lock_path.exists():
                time.sleep(0.05)
            self.assertTrue(lock_path.exists())

            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=5)

            self.assertEqual(proc.returncode, 130, stderr + stdout)
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
