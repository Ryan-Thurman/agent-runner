import contextlib
import io
import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_runner.config import AgentProfile
from agent_runner.errors import JobError
from agent_runner.jobs import (
    LivePreviewContext,
    _LivePreviewRenderer,
    _display_width,
    _format_live_preview_line,
    _live_preview_context,
    _live_preview_writer,
    _resolve_color_enabled,
    _run_process,
    _terminal_width,
    _truncate_visible,
    run_agent_job,
    run_checks_job,
)
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

long_stdout = os.environ.get("FAKE_AGENT_LONG_STDOUT")
if long_stdout:
    print(long_stdout)

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


def make_prefixed_profile(script: Path, prefix: str) -> AgentProfile:
    return AgentProfile(
        name="fake",
        command=sys.executable,
        prompt_args=[str(script)],
        write_flags=["--write-flag"],
        read_only_flags=["--read-only-flag"],
        output_capture="stdout",
        prompt_prefix=prefix,
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
    def test_live_preview_labels_for_job_types(self):
        cases = [
            ("IMPLEMENT", "coder", "codex", "codex", "coding"),
            ("REVIEW", "reviewer", "claude", "claude", "reviewing"),
            ("FIX", "coder", "codex", "codex", "fixing"),
            ("CLOSE_PHASE", "closer", "claude", "claude", "closing"),
            ("RUN_CHECKS", "checks", "shell", "checks", "checking"),
        ]

        for job_type, role, profile, subject, verb in cases:
            with self.subTest(job_type=job_type):
                context = _live_preview_context(job_type, role, profile)
                self.assertEqual(context.subject, subject)
                self.assertEqual(context.verb, verb)

    def test_live_preview_truncates_without_changing_original_text(self):
        original = "x" * 80
        context = LivePreviewContext(subject="codex", verb="coding", max_chars=40)

        preview = _format_live_preview_line(
            context, original, color_enabled=False, max_chars=40
        )

        self.assertLessEqual(len(preview), 40)
        self.assertIn("[truncated]", preview)
        self.assertEqual(original, "x" * 80)

    def test_live_preview_formats_blank_lines_as_single_prefix(self):
        context = LivePreviewContext(subject="checks", verb="checking")

        preview = _format_live_preview_line(
            context, "   \n", color_enabled=False, max_chars=240
        )

        self.assertEqual(preview, "[checks checking]:")

    def test_live_preview_color_modes(self):
        class FakeStream:
            def __init__(self, is_tty: bool):
                self._is_tty = is_tty

            def isatty(self):
                return self._is_tty

        self.assertTrue(
            _resolve_color_enabled(
                mode="auto", stream=FakeStream(True), env={}
            )
        )
        self.assertFalse(
            _resolve_color_enabled(
                mode="auto", stream=FakeStream(False), env={}
            )
        )
        self.assertTrue(
            _resolve_color_enabled(
                mode="always", stream=FakeStream(False), env={}
            )
        )
        self.assertFalse(
            _resolve_color_enabled(
                mode="never", stream=FakeStream(True), env={}
            )
        )
        self.assertFalse(
            _resolve_color_enabled(
                mode="auto", stream=FakeStream(True), env={"NO_COLOR": "1"}
            )
        )

    def test_live_preview_defaults_to_rolling_on_tty(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        stderr = FakeTTY()
        context = LivePreviewContext(subject="codex", verb="coding")

        with mock.patch("sys.stderr", stderr), mock.patch.dict(
            os.environ, {"AGENT_RUNNER_COLOR": "never"}, clear=False
        ):
            os.environ.pop("AGENT_RUNNER_LIVE_LOGS", None)
            preview = _live_preview_writer(context)
            self.assertIsNotNone(preview)
            preview.write("hello\n")
            preview.finish()

        output = stderr.getvalue()
        self.assertIn("\r\x1b[2K[codex coding]: hello", output)
        self.assertTrue(output.endswith("\r\x1b[2K"))

    def test_live_preview_rolling_fits_terminal_width(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        stderr = FakeTTY()
        context = LivePreviewContext(subject="codex", verb="coding")

        with mock.patch("sys.stderr", stderr), mock.patch.dict(
            os.environ,
            {
                "AGENT_RUNNER_COLOR": "never",
                "AGENT_RUNNER_LIVE_LOGS": "rolling",
                "COLUMNS": "40",
            },
            clear=False,
        ):
            preview = _live_preview_writer(context)
            preview.write("x" * 200 + "\n")
            preview.finish()

        # Each rolling segment must fit within COLUMNS-1 so \r\x1b[2K clears the
        # whole physical row instead of leaving wrapped remnants on screen.
        segments = [seg for seg in stderr.getvalue().split("\r\x1b[2K") if seg]
        self.assertTrue(segments)
        for segment in segments:
            self.assertLessEqual(len(segment), 39)

    @unittest.skipUnless(hasattr(os, "openpty"), "requires pty support")
    def test_terminal_width_reads_real_pty_winsize(self):
        # Exercise the primary os.get_terminal_size(fileno) branch (StringIO
        # fakes only hit the COLUMNS/shutil fallback), so a broken fd lookup is
        # caught by the suite rather than only by the standalone script.
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        slave = os.fdopen(slave_fd, "w", encoding="utf-8")
        try:
            fcntl.ioctl(
                slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 57, 0, 0)
            )
            self.assertEqual(_terminal_width(slave), 57)
        finally:
            slave.close()
            os.close(master_fd)

    @unittest.skipUnless(hasattr(os, "openpty"), "requires pty support")
    def test_rolling_preview_fits_real_pty_width(self):
        import fcntl
        import pty
        import struct
        import termios

        cols = 40
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))
        chunks: list[bytes] = []

        def reader():
            while True:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)

        thread = threading.Thread(target=reader)
        thread.start()
        slave = os.fdopen(slave_fd, "w", buffering=1, encoding="utf-8", newline="")
        try:
            renderer = _LivePreviewRenderer(
                LivePreviewContext(subject="codex", verb="coding"),
                color_enabled=False,
                line_mode=False,
                stream=slave,
            )
            renderer.write("x" * 200 + "\n")
            renderer.finish()
        finally:
            slave.flush()
            slave.close()
            thread.join(timeout=2)
            with contextlib.suppress(OSError):
                os.close(master_fd)

        output = b"".join(chunks).decode("utf-8", "replace")
        segments = [seg for seg in output.split("\r\x1b[2K")[1:] if seg]
        self.assertTrue(segments)
        for segment in segments:
            self.assertLessEqual(len(segment), cols - 1)

    def test_truncate_visible_counts_wide_and_zero_width_glyphs(self):
        # A wide glyph consumes two columns; combining/zero-width marks consume
        # none. Truncation must honor that so the result never overflows the row.
        wide = "\u6f22" * 30  # CJK ideograph (two columns each)
        self.assertEqual(_display_width("\u6f22" * 5), 10)
        self.assertLessEqual(_display_width(_truncate_visible(wide, 10)), 10)

        # base letter + combining acute accent renders as one column
        combining = "e\u0301" * 30
        self.assertEqual(_display_width(combining), 30)
        # zero-width space (category Cf) advances the cursor by nothing
        self.assertEqual(_display_width("\u200b" * 10), 0)

    def test_live_preview_expands_tabs_so_rolling_row_never_overflows(self):
        # Agents that echo source lines (codex) emit tabs. A tab advances the
        # cursor to the next tab stop, so an unexpanded preview wraps onto a
        # second row that \r\x1b[2K never clears, stranding it on screen.
        context = LivePreviewContext(subject="codex", verb="fixing")
        tabbed = "\t".join("word" for _ in range(20))

        preview = _format_live_preview_line(
            context, tabbed, color_enabled=False, max_chars=60
        )

        self.assertNotIn("\t", preview)
        self.assertLessEqual(_display_width(preview), 60)

    def test_live_preview_uses_rolling_line_when_enabled_and_clears_line(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        stderr = FakeTTY()
        context = LivePreviewContext(subject="codex", verb="coding")

        with mock.patch("sys.stderr", stderr), mock.patch.dict(
            os.environ,
            {
                "AGENT_RUNNER_COLOR": "never",
                "AGENT_RUNNER_LIVE_LOGS": "rolling",
            },
            clear=False,
        ):
            preview = _live_preview_writer(context)
            self.assertIsNotNone(preview)
            preview.write("first line\n")
            preview.write("second line\n")
            preview.finish()

        output = stderr.getvalue()
        self.assertIn("\r\x1b[2K[codex coding]: first line", output)
        self.assertIn("\r\x1b[2K[codex coding]: second line", output)
        self.assertNotIn("- [codex coding]", output)
        self.assertNotIn("\\ [codex coding]", output)
        self.assertNotIn("\n", output)
        self.assertTrue(output.endswith("\r\x1b[2K"))

    def test_live_preview_defaults_to_rolling_on_non_tty(self):
        stderr = io.StringIO()
        context = LivePreviewContext(subject="codex", verb="coding")

        with mock.patch("sys.stderr", stderr), mock.patch.dict(
            os.environ, {"AGENT_RUNNER_COLOR": "never", "COLUMNS": "200"}, clear=False
        ):
            os.environ.pop("AGENT_RUNNER_LIVE_LOGS", None)
            preview = _live_preview_writer(context)
            self.assertIsNotNone(preview)
            preview.write("first line\n")
            preview.write("second line\n")
            preview.finish()

        # Rolling is the default even when stderr is not a terminal.
        self.assertEqual(
            stderr.getvalue(),
            "\r\x1b[2K[codex coding]: first line"
            "\r\x1b[2K[codex coding]: second line"
            "\r\x1b[2K",
        )

    def test_live_preview_unrecognized_value_defaults_to_rolling(self):
        stderr = io.StringIO()
        context = LivePreviewContext(subject="codex", verb="coding")

        with mock.patch("sys.stderr", stderr), mock.patch.dict(
            os.environ,
            {
                "AGENT_RUNNER_COLOR": "never",
                "AGENT_RUNNER_LIVE_LOGS": "1",
                "COLUMNS": "200",
            },
            clear=False,
        ):
            preview = _live_preview_writer(context)
            self.assertIsNotNone(preview)
            preview.write("first line\n")
            preview.finish()

        self.assertEqual(
            stderr.getvalue(),
            "\r\x1b[2K[codex coding]: first line\r\x1b[2K",
        )

    def test_live_preview_lines_mode_uses_newline_delimited_previews(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True

        stderr = FakeTTY()
        context = LivePreviewContext(subject="codex", verb="coding")

        with mock.patch("sys.stderr", stderr), mock.patch.dict(
            os.environ,
            {"AGENT_RUNNER_COLOR": "never", "AGENT_RUNNER_LIVE_LOGS": "lines"},
            clear=False,
        ):
            preview = _live_preview_writer(context)
            self.assertIsNotNone(preview)
            preview.write("first line\n")
            preview.finish()

        self.assertEqual(stderr.getvalue(), "[codex coding]: first line\n")

    def test_live_preview_finish_ignores_closed_tty_cleanup(self):
        class ClosingTTY(io.StringIO):
            def isatty(self):
                return True

            def write(self, text):
                if text == "\r\x1b[2K":
                    raise OSError("closed")
                return super().write(text)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "ok.py"
            script.write_text("print('done')\n", encoding="utf-8")
            log_path = root / "preview.log"

            with mock.patch("sys.stderr", ClosingTTY()), mock.patch.dict(
                os.environ, {"AGENT_RUNNER_COLOR": "never"}, clear=False
            ):
                exit_code, stdout, stderr, error = _run_process(
                    [sys.executable, str(script)],
                    repo_root=repo,
                    timeout_seconds=5,
                    shell=False,
                    log_path=log_path,
                    log_header="$ ok\n",
                    live_preview_context=LivePreviewContext(
                        subject="codex", verb="coding"
                    ),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout, "done\n")
            self.assertEqual(stderr, "")
            self.assertIsNone(error)
            self.assertIn("done", log_path.read_text(encoding="utf-8"))

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

    def test_agent_job_live_preview_streams_to_stderr_and_preserves_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            stderr = io.StringIO()

            with mock.patch("sys.stderr", stderr), mock.patch.dict(
                os.environ,
                {
                    "AGENT_RUNNER_COLOR": "never",
                    "AGENT_RUNNER_LIVE_LOGS": "lines",
                },
                clear=False,
            ):
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

            stderr_text = stderr.getvalue()
            log_text = result.log_path.read_text(encoding="utf-8")
            self.assertIn("[fake coding]: fake stdout", stderr_text)
            self.assertIn("[fake coding]: fake stderr", stderr_text)
            # The command is printed to the terminal, but the prompt is not.
            self.assertIn("[agent-runner] $ ", stderr_text)
            self.assertNotIn("Implement the phase.", stderr_text)
            self.assertNotIn("\r\x1b[2K", stderr_text)
            self.assertNotIn("\033[36m", stderr_text)
            self.assertIn("fake stdout", log_text)
            self.assertIn("fake stderr", log_text)
            self.assertEqual(result.output_path.read_text(encoding="utf-8"), "fake stdout\n")

    def test_agent_job_live_preview_truncates_only_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            long_output = "long-" + ("x" * 320)
            stderr = io.StringIO()

            with mock.patch("sys.stderr", stderr), mock.patch.dict(
                os.environ,
                {
                    "AGENT_RUNNER_COLOR": "never",
                    "AGENT_RUNNER_LIVE_LOGS": "lines",
                    "FAKE_AGENT_LONG_STDOUT": long_output,
                },
                clear=False,
            ):
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

            stderr_text = stderr.getvalue()
            self.assertIn("[truncated]", stderr_text)
            self.assertNotIn(long_output, stderr_text)
            self.assertIn(long_output, result.log_path.read_text(encoding="utf-8"))
            self.assertIn(long_output, result.output_path.read_text(encoding="utf-8"))

    def test_agent_job_live_preview_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "fake_agent.py"
            write_fake_agent(script)
            project, plan, phase = setup_state(home, repo)
            stderr = io.StringIO()

            with mock.patch("sys.stderr", stderr), mock.patch.dict(
                os.environ, {"AGENT_RUNNER_LIVE_LOGS": "0"}, clear=False
            ):
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

            self.assertNotIn("[fake coding]:", stderr.getvalue())
            self.assertIn("fake stdout", result.log_path.read_text(encoding="utf-8"))

    def test_checks_job_live_preview_uses_checks_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            home = root / "home"
            repo.mkdir()
            git_init_with_commit(repo)
            project, plan, phase = setup_state(home, repo)
            stderr = io.StringIO()

            with mock.patch("sys.stderr", stderr), mock.patch.dict(
                os.environ,
                {
                    "AGENT_RUNNER_COLOR": "never",
                    "AGENT_RUNNER_LIVE_LOGS": "lines",
                },
                clear=False,
            ):
                with connect_db(home) as db:
                    result = run_checks_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        commands=[
                            f"{shlex.quote(sys.executable)} -c \"print('check output')\""
                        ],
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )

            self.assertEqual(result.status, "SUCCEEDED")
            self.assertIn("[checks checking]: check output", stderr.getvalue())
            # Each check command is printed to the terminal above its preview.
            self.assertIn("[agent-runner] $ ", stderr.getvalue())
            self.assertIn("print('check output')", stderr.getvalue())
            self.assertIn("check output", result.log_path.read_text(encoding="utf-8"))

    def test_live_preview_color_is_forced_when_requested(self):
        context = LivePreviewContext(subject="codex", verb="coding")

        preview = _format_live_preview_line(
            context, "colored", color_enabled=True, max_chars=240
        )

        self.assertIn("\033[36m", preview)
        self.assertIn("\033[0m", preview)

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

    def test_agent_prompt_includes_profile_prefix(self):
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
            prefix = "Use a specific review agent."
            prompt = "Review the phase."

            try:
                with connect_db(home) as db:
                    result = run_agent_job(
                        db,
                        project_id=project["id"],
                        plan_id=plan["id"],
                        phase_id=phase["id"],
                        job_type="REVIEW",
                        role="reviewer",
                        profile=make_prefixed_profile(script, prefix),
                        prompt=prompt,
                        repo_root=repo,
                        log_dir=home / "logs" / "phase-4",
                        timeout_seconds=5,
                    )
            finally:
                os.environ.clear()
                os.environ.update(old_environ)

            expected_prompt = f"{prefix}\n\n{prompt}"
            argv = json.loads(argv_path.read_text(encoding="utf-8"))
            self.assertIn(expected_prompt, argv)
            self.assertEqual(result.prompt_path.read_text(encoding="utf-8"), expected_prompt)

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
            stderr = io.StringIO()

            try:
                with mock.patch("sys.stderr", stderr), connect_db(home) as db:
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
            self.assertIn("[fake fixing]: fake stdout", stderr.getvalue())
            self.assertIn("fake stdout", result.log_path.read_text(encoding="utf-8"))
            self.assertIn(
                f"[agent-runner] FIX job {result.job_id} failed: exit code 7",
                stderr.getvalue(),
            )
            self.assertIn(f"[agent-runner]   log: {result.log_path}", stderr.getvalue())

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

    def test_run_process_interrupt_during_thread_setup_kills_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "sleep.py"
            script.write_text(
                "import time\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )

            with mock.patch(
                "agent_runner.jobs.threading.Thread", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    _run_process(
                        [sys.executable, str(script)],
                        repo_root=repo,
                        timeout_seconds=5,
                        shell=False,
                        log_path=root / "interrupt.log",
                        log_header="$ sleep\n",
                    )

            self.assertIn(
                "interrupted",
                (root / "interrupt.log").read_text(encoding="utf-8"),
            )

    def test_run_process_timeout_does_not_hang_on_open_grandchild_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "grandchild_pipe.py"
            script.write_text(
                """
import subprocess
import sys
import time

subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
time.sleep(30)
""".lstrip(),
                encoding="utf-8",
            )

            exit_code, stdout, stderr, error = _run_process(
                [sys.executable, str(script)],
                repo_root=repo,
                timeout_seconds=0.2,
                shell=False,
                log_path=root / "timeout.log",
                log_header="$ grandchild\n",
            )

            self.assertEqual(exit_code, -signal.SIGTERM)
            self.assertIn("timeout after", error)

    def test_spawn_notification_failure_does_not_leak_or_fail_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git_init_with_commit(repo)
            script = root / "ok.py"
            script.write_text("print('done')\n", encoding="utf-8")

            exit_code, stdout, stderr, error = _run_process(
                [sys.executable, str(script)],
                repo_root=repo,
                timeout_seconds=5,
                shell=False,
                log_path=root / "spawn.log",
                log_header="$ ok\n",
                on_spawn=lambda pid: (_ for _ in ()).throw(BrokenPipeError("closed")),
            )

            self.assertEqual(exit_code, 0)
            self.assertIsNone(error)
            self.assertEqual(stdout, "done\n")
            self.assertIn(
                "failed to report spawned process",
                (root / "spawn.log").read_text(encoding="utf-8"),
            )

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
