import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_runner.cli import _exec_self_restart
from agent_runner.lock import ProjectLock
from agent_runner.phase_loop import (
    MAX_SELF_RESTARTS,
    NO_SELF_RESTART_ENV,
    RESTART_COUNT_ENV,
    _should_self_restart,
    runner_is_self_hosted,
)
from test_phase7_close import (
    ROOT,
    add_origin_remote,
    commit_all,
    git_init,
    phase_rows,
    run_cli,
    seed_closing_published_phase,
    write_config,
    write_fake_gh,
    write_phase7_agent,
    write_plan,
)
from test_phase6_loop import (
    write_config as write_phase6_config,
    write_phase6_agent,
    write_plan as write_phase6_plan,
)

from agent_runner.storage import connect_db


class SelfHostedDetectionTests(unittest.TestCase):
    def test_own_checkout_is_self_hosted(self):
        self.assertTrue(runner_is_self_hosted(ROOT))

    def test_other_repo_is_not_self_hosted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(runner_is_self_hosted(Path(tmp)))


class ShouldSelfRestartTests(unittest.TestCase):
    def _patch_env(self, **values: str):
        env = {key: value for key, value in values.items()}
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (RESTART_COUNT_ENV, NO_SELF_RESTART_ENV):
            if name not in env:
                os.environ.pop(name, None)

    def test_true_for_self_hosted_repo(self):
        self._patch_env()
        self.assertTrue(_should_self_restart(ROOT))

    def test_false_when_disabled_by_env(self):
        self._patch_env(**{NO_SELF_RESTART_ENV: "1"})
        self.assertFalse(_should_self_restart(ROOT))

    def test_false_when_restart_cap_reached(self):
        self._patch_env(**{RESTART_COUNT_ENV: str(MAX_SELF_RESTARTS)})
        self.assertFalse(_should_self_restart(ROOT))

    def test_false_for_non_self_hosted_repo(self):
        self._patch_env()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(_should_self_restart(Path(tmp)))

    def test_false_on_non_posix(self):
        self._patch_env()
        with mock.patch("agent_runner.phase_loop.os.name", "nt"):
            self.assertFalse(_should_self_restart(ROOT))


class ExecSelfRestartTests(unittest.TestCase):
    def test_releases_lock_bumps_count_and_execs_repo_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            locks = root / "locks"
            repo.mkdir()
            lock = ProjectLock(locks, "test-project", repo)
            lock.acquire()
            self.assertTrue(lock.path.exists())

            with mock.patch.dict(os.environ, {RESTART_COUNT_ENV: "3"}), mock.patch(
                "agent_runner.cli.os.execv"
            ) as execv:
                _exec_self_restart(lock, repo)
                self.assertEqual(os.environ[RESTART_COUNT_ENV], "4")

            self.assertFalse(lock.path.exists())
            execv.assert_called_once()
            executable, argv = execv.call_args.args
            self.assertEqual(argv, [executable, str(repo / "agent-runner"), "run"])
            self.assertNotIn("--accept-plan-change", argv)


class SelfRestartEndToEndTests(unittest.TestCase):
    def test_implement_in_self_hosted_repo_restarts_before_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            trace = root / "trace"
            script = root / "phase6_agent.py"
            repo.mkdir()
            git_init(repo)
            write_phase6_agent(script)
            write_phase6_plan(repo)
            write_phase6_config(repo, script, checks=[])
            shutil.copytree(
                ROOT / "agent_runner",
                repo / "agent_runner",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copy2(ROOT / "agent-runner", repo / "agent-runner")
            shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
            commit_all(repo)

            result = run_cli(
                repo,
                home,
                "run",
                extra_env={
                    "TRACE_DIR": str(trace),
                    NO_SELF_RESTART_ENV: "",
                    RESTART_COUNT_ENV: "0",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "IMPLEMENT complete; restarting to load updated runner code before checks",
                result.stderr,
            )
            self.assertEqual(
                result.stderr.count("acquired lock"),
                2,
                "expected the restarted process to re-acquire the lock",
            )
            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            with connect_db(home) as db:
                events = db.execute(
                    "SELECT event_type, message FROM events ORDER BY id"
                ).fetchall()
            self.assertIn("runner.restart", [row["event_type"] for row in events])
            restart_messages = [
                row["message"]
                for row in events
                if row["event_type"] == "runner.restart"
            ]
            self.assertIn("IMPLEMENT complete", restart_messages[0])

    def test_merge_in_self_hosted_repo_restarts_and_continues(self):
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
            # Make the temp repo genuinely self-hosted: the subprocess runs
            # with cwd=repo, so this copy of the package shadows PYTHONPATH
            # and runner_is_self_hosted() sees the package inside repo_root.
            shutil.copytree(
                ROOT / "agent_runner",
                repo / "agent_runner",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copy2(ROOT / "agent-runner", repo / "agent-runner")
            # Without this, the subprocess's own imports drop __pycache__
            # into the worktree and the close preflight rejects it as
            # unreviewed changes — same as the real checkout's .gitignore.
            shutil.copy2(ROOT / ".gitignore", repo / ".gitignore")
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
                    NO_SELF_RESTART_ENV: "",
                    RESTART_COUNT_ENV: "0",
                },
            )

            # exec preserves the PID, so subprocess.run waits through the
            # restart and sees the second invocation's exit code and output.
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("restarting to load updated runner code", result.stderr)
            self.assertIn("BLOCKED after IMPLEMENT failure", result.stderr)
            self.assertEqual(
                result.stderr.count("acquired lock"),
                2,
                "expected the restarted process to re-acquire the lock",
            )

            rows = phase_rows(home, repo)
            self.assertEqual(rows[0]["status"], "COMPLETE")
            self.assertEqual(rows[1]["status"], "BLOCKED")
            self.assertTrue((repo / "phase2-started.txt").exists())
            with connect_db(home) as db:
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertIn("phase.merged", event_types)
            self.assertIn("runner.restart", event_types)

    def test_non_self_hosted_repo_does_not_restart(self):
        # The pre-existing merge-on-close test covers the in-process
        # auto-advance; this asserts the restart event never fires there.
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

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn("restarting to load updated runner code", result.stderr)
            with connect_db(home) as db:
                event_types = [
                    row["event_type"]
                    for row in db.execute(
                        "SELECT event_type FROM events ORDER BY id"
                    ).fetchall()
                ]
            self.assertNotIn("runner.restart", event_types)
            self.assertIn("phase.auto_advance", event_types)


if __name__ == "__main__":
    unittest.main()
