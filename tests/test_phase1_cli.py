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

from agent_runner.config import SAMPLE_CONFIG, load_config, project_slug
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
    data["autoCommit"] = False
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
            self.assertEqual(config.roles["coder"], "claude")
            self.assertEqual(config.agents["claude"].prompt_prefix, "")
            self.assertFalse(config.auto_merge)

            second = run_cli(repo, home, "init")

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already exists", second.stderr)

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
            del data["agents"]["claude"]["readOnlyFlags"]
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "missing required field"):
                load_config(repo)

            data = json.loads(_strip_sample_comments(SAMPLE_CONFIG))
            data["agents"]["claude"]["promptPrefix"] = ["not", "a", "string"]
            (repo / ".agent-runner.json").write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "promptPrefix"):
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
