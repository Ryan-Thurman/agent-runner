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

from agent_runner.config import SAMPLE_CONFIG, load_config
from agent_runner.errors import ConfigError


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
    if overrides:
        data.update(overrides)
    (repo / ".agent-runner.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


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
            self.assertEqual(load_config(repo).roles["coder"], "claude")

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

    def test_empty_checks_are_accepted_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo, {"checks": []})

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
            locks = home / "locks"
            locks.mkdir(parents=True)
            lock_path = locks / "repo.lock"
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
            locks = home / "locks"
            locks.mkdir(parents=True)
            lock_path = locks / "repo.lock"
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

    def test_sigint_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            repo.mkdir()
            git_init(repo)
            write_config(repo)
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
            lock_path = home / "locks" / "repo.lock"
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
