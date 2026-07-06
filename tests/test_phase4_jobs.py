import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_runner.config import AgentProfile
from agent_runner.errors import JobError
from agent_runner.jobs import run_agent_job, run_checks_job
from agent_runner.storage import (
    connect_db,
    create_job,
    create_phase,
    create_plan,
    get_job,
    get_or_create_project,
)


def git_init_with_commit(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test User",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=path,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def write_fake_agent(path: Path) -> None:
    path.write_text(
        """
import json
import os
import signal
import sys
import time

if os.environ.get("FAKE_AGENT_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

argv_path = os.environ.get("FAKE_AGENT_ARGV")
if argv_path:
    with open(argv_path, "w", encoding="utf-8") as handle:
        json.dump(sys.argv[1:], handle)

if os.environ.get("FAKE_AGENT_SLEEP"):
    time.sleep(float(os.environ["FAKE_AGENT_SLEEP"]))

if "--output-last-message" in sys.argv:
    index = sys.argv.index("--output-last-message")
    with open(sys.argv[index + 1], "w", encoding="utf-8") as handle:
        handle.write("last message\\n")

print("fake stdout")
print("fake stderr", file=sys.stderr)
raise SystemExit(int(os.environ.get("FAKE_AGENT_EXIT", "0")))
""".lstrip(),
        encoding="utf-8",
    )


def make_profile(script: Path, *, output_capture: str = "stdout") -> AgentProfile:
    return AgentProfile(
        name="fake",
        command=sys.executable,
        prompt_args=[str(script)],
        write_flags=["--write-flag"],
        read_only_flags=["--read-only-flag"],
        output_capture=output_capture,
    )


def setup_state(home: Path, repo: Path):
    with connect_db(home) as db:
        project = get_or_create_project(db, slug="repo", repo_path=repo)
        plan = create_plan(db, project_id=project["id"], path="docs/plan.md")
        phase = create_phase(
            db,
            project_id=project["id"],
            plan_id=plan["id"],
            phase_number=4,
            title="Job engine",
            content_hash="hash",
        )
    return project, plan, phase


class Phase4JobTests(unittest.TestCase):
    def test_agent_job_success_writes_prompt_logs_output_and_shas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            expected_sha = git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)

            with connect_db(home) as db:
                result = run_agent_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="IMPLEMENT",
                    role="coder",
                    profile=make_profile(script),
                    prompt="Implement the phase.",
                    repo_root=repo,
                    log_dir=home / "logs" / "phase-4",
                    timeout_seconds=5,
                )
                row = get_job(db, result.job_id)

            self.assertEqual(result.status, "SUCCEEDED")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.prompt_path.read_text(encoding="utf-8"), "Implement the phase.")
            self.assertIn("fake stdout", result.log_path.read_text(encoding="utf-8"))
            self.assertIn("fake stderr", result.log_path.read_text(encoding="utf-8"))
            self.assertEqual(result.output_path.read_text(encoding="utf-8"), "fake stdout\n")
            self.assertEqual(row["started_sha"], expected_sha)
            self.assertEqual(row["finished_sha"], expected_sha)

    def test_agent_receives_prompt_text_not_prompt_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            argv_path = root / "argv.json"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            old_environ = os.environ.copy()
            os.environ["FAKE_AGENT_ARGV"] = str(argv_path)
            prompt = "Do the actual phase work."

            try:
                with connect_db(home) as db:
                    result = run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="IMPLEMENT",
                        role="coder",
                        profile=make_profile(script),
                        prompt=prompt,
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )
            finally:
                os.environ.clear()
                os.environ.update(old_environ)

            argv = json.loads(argv_path.read_text(encoding="utf-8"))
            self.assertIn(prompt, argv)
            self.assertNotIn(str(result.prompt_path), argv)

    def test_agent_job_nonzero_exit_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            old_environ = os.environ.copy()
            os.environ["FAKE_AGENT_EXIT"] = "7"

            try:
                with connect_db(home) as db:
                    result = run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="FIX",
                        role="coder",
                        profile=make_profile(script),
                        prompt="Fix the phase.",
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )
            finally:
                os.environ.clear()
                os.environ.update(old_environ)

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.exit_code, 7)
            self.assertEqual(result.error, "exit code 7")

    def test_agent_spawn_failure_marks_job_failed_and_unblocks_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            project, plan, phase = setup_state(home, repo)
            missing_profile = AgentProfile(
                name="missing",
                command=str(root / "does-not-exist"),
                prompt_args=[],
                write_flags=[],
                read_only_flags=[],
                output_capture="stdout",
            )
            script = root / "fake_agent.py"
            write_fake_agent(script)

            with connect_db(home) as db:
                failed = run_agent_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="IMPLEMENT",
                    role="coder",
                    profile=missing_profile,
                    prompt="Implement.",
                    repo_root=repo,
                    log_dir=home / "logs" / "phase-4",
                    timeout_seconds=5,
                )
                row = get_job(db, failed.job_id)
                second = run_agent_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="FIX",
                    role="coder",
                    profile=make_profile(script),
                    prompt="Try again.",
                    repo_root=repo,
                    log_dir=home / "logs" / "phase-4-second",
                    timeout_seconds=5,
                )

            self.assertEqual(failed.status, "FAILED")
            self.assertIsNone(failed.exit_code)
            self.assertIn("failed to start process", failed.error)
            self.assertEqual(row["status"], "FAILED")
            self.assertEqual(second.status, "SUCCEEDED")

    def test_agent_job_timeout_marks_failed_and_preserves_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            env = os.environ.copy()
            env["FAKE_AGENT_SLEEP"] = "5"

            with connect_db(home) as db:
                old_environ = os.environ.copy()
                os.environ.update(env)
                try:
                    result = run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="IMPLEMENT",
                        role="coder",
                        profile=make_profile(script),
                        prompt="Implement slowly.",
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=0.2,
                    )
                finally:
                    os.environ.clear()
                    os.environ.update(old_environ)

            self.assertEqual(result.status, "FAILED")
            self.assertIn("timeout after", result.error)
            self.assertTrue(result.log_path.exists())

    def test_timeout_escalates_to_sigkill_when_sigterm_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            old_environ = os.environ.copy()
            os.environ["FAKE_AGENT_SLEEP"] = "5"
            os.environ["FAKE_AGENT_IGNORE_TERM"] = "1"

            try:
                with connect_db(home) as db:
                    result = run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="IMPLEMENT",
                        role="coder",
                        profile=make_profile(script),
                        prompt="Ignore term.",
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=0.2,
                    )
            finally:
                os.environ.clear()
                os.environ.update(old_environ)

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.exit_code, -signal.SIGKILL)
            self.assertIn("SIGKILL", result.error)

    def test_reviewer_uses_readonly_flags_and_last_message_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            argv_path = root / "argv.json"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            old_environ = os.environ.copy()
            os.environ["FAKE_AGENT_ARGV"] = str(argv_path)

            try:
                with connect_db(home) as db:
                    result = run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="REVIEW",
                        role="reviewer",
                        profile=make_profile(script, output_capture="last-message-file"),
                        prompt="Review the phase.",
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )
            finally:
                os.environ.clear()
                os.environ.update(old_environ)

            argv = json.loads(argv_path.read_text(encoding="utf-8"))
            self.assertIn("Review the phase.", argv)
            self.assertIn("--read-only-flag", argv)
            self.assertNotIn("--write-flag", argv)
            self.assertIn("--output-last-message", argv)
            self.assertEqual(result.output_path.read_text(encoding="utf-8"), "last message\n")

    def test_refuses_to_start_when_project_has_running_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)

            with connect_db(home) as db:
                create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="REVIEW",
                    status="RUNNING",
                )
                with self.assertRaisesRegex(JobError, "already running"):
                    run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="IMPLEMENT",
                        role="coder",
                        profile=make_profile(script),
                        prompt="Implement.",
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )

    def test_checks_job_runs_in_order_and_stops_on_first_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            project, plan, phase = setup_state(home, repo)
            marker = repo / "should-not-exist"
            commands = [
                f"{shlex.quote(sys.executable)} -c \"print('first check')\"",
                f"{shlex.quote(sys.executable)} -c \"import sys; print('second check'); sys.exit(4)\"",
                f"{shlex.quote(sys.executable)} -c \"open({str(marker)!r}, 'w').write('ran')\"",
            ]

            with connect_db(home) as db:
                result = run_checks_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    commands=commands,
                    repo_root=repo,
                    log_dir=home / "logs" / "phase-4",
                    timeout_seconds=5,
                )
                row = get_job(db, result.job_id)

            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.exit_code, 4)
            self.assertIn("check failed", result.error)
            self.assertIn("first check", log_text)
            self.assertIn("second check", log_text)
            self.assertFalse(marker.exists())
            self.assertEqual(row["type"], "RUN_CHECKS")

    def test_checks_job_refuses_to_start_when_project_has_running_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            project, plan, phase = setup_state(home, repo)

            with connect_db(home) as db:
                create_job(
                    db,
                    project_id=project["id"],
                    plan_id=plan["id"],
                    phase_id=phase["id"],
                    job_type="IMPLEMENT",
                    status="RUNNING",
                )
                with self.assertRaisesRegex(JobError, "already running"):
                    run_checks_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        commands=[f"{shlex.quote(sys.executable)} -c \"print('nope')\""],
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )


if __name__ == "__main__":
    unittest.main()
