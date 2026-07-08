#!/usr/bin/env python3
"""Smoke-check agent-runner live log preview modes.

This script creates a temporary git repo and fake agent, runs the job engine,
and verifies that:

* previews default to a rolling one-line animation on a terminal and to readable
  newline-delimited lines when stderr is not a terminal;
* each command is printed to stderr while its output animates;
* rolling previews are fit to the terminal width so `\r\x1b[2K` clears the whole
  physical row (the regression that made previews look like "every line").

The width check runs the engine against a real pseudo-terminal with a narrow
winsize, which a StringIO capture cannot exercise (wrapping is a width effect).

Usage:

    python3 scripts/verify_live_logs.py            # run the automated checks
    python3 scripts/verify_live_logs.py demo       # watch the preview live
    python3 scripts/verify_live_logs.py demo lines  # force a specific mode

The `demo` mode streams a slow fake agent straight to your real terminal (no
capture, no override) so you can visually confirm the rolling line animates in
place. It honors whatever `AGENT_RUNNER_LIVE_LOGS` you export, or the optional
mode argument shown above.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import pty
import struct
import subprocess
import sys
import termios
import threading
import tty
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CHILD_LINE_COUNT = 20
# Every "[fake coding]: fake stdout from child NN/20" preview is ~43 columns, so
# this narrow width forces truncation and exercises the wrap-avoidance fix.
NARROW_COLS = 40

from agent_runner.config import AgentProfile  # noqa: E402
from agent_runner.jobs import run_agent_job  # noqa: E402
from agent_runner.storage import (  # noqa: E402
    connect_db,
    create_phase,
    create_plan,
    get_or_create_project,
)


class CaptureTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@contextlib.contextmanager
def patched_env(values: dict[str, str | None]):
    old = os.environ.copy()
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def git_init_with_commit(path: Path) -> None:
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


def write_fake_agent(path: Path) -> None:
    path.write_text(
        f"""
import sys

for index in range(1, {CHILD_LINE_COUNT} + 1):
    print(f"fake stdout from child {{index:02d}}/{CHILD_LINE_COUNT}")
    print(f"fake stderr from child {{index:02d}}/{CHILD_LINE_COUNT}", file=sys.stderr)
""".lstrip(),
        encoding="utf-8",
    )


def write_demo_agent(path: Path) -> None:
    """A slower agent that emits varied, wrap-tempting lines for live viewing."""
    path.write_text(
        """
import sys
import time

steps = [
    "reading agent_runner/jobs.py",
    "editing agent_runner/jobs.py: fit rolling preview to the terminal width",
    "editing agent_runner/jobs.py: print each command above its preview",
    "this line is intentionally very long so it would wrap without the fix "
    + "=" * 200 + " END",
    "editing tests/test_phase4_jobs.py: assert segments stay within the width",
    "running python3 -m pytest tests/test_phase4_jobs.py",
    "collected 30 items ................................",
    "another very long status line to prove truncation kicks in "
    + "#" * 200 + " END",
    "updating README.md",
    "updating docs/usage.md",
    "all checks passed",
]
for step in steps:
    print(step, flush=True)
    time.sleep(0.35)
""".lstrip(),
        encoding="utf-8",
    )


def make_profile(script: Path) -> AgentProfile:
    return AgentProfile(
        name="fake",
        command=sys.executable,
        prompt_args=[str(script)],
        write_flags=["--write-flag"],
        read_only_flags=["--read-only-flag"],
        output_capture="stdout",
    )


@contextlib.contextmanager
def job_environment(agent_writer=write_fake_agent):
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"
        home = root / "home"
        script = root / "fake_agent.py"
        repo.mkdir()
        git_init_with_commit(repo)
        agent_writer(script)

        with connect_db(home) as db:
            project = get_or_create_project(db, slug="repo", repo_path=repo)
            plan = create_plan(db, project_id=project["id"], path="docs/plan.md")
            phase = create_phase(
                db,
                project_id=project["id"],
                plan_id=plan["id"],
                phase_number=1,
                title="Live logs smoke",
                content_hash="hash",
            )
        yield home, repo, script, project, plan, phase


def _run_job(home, repo, script, project, plan, phase):
    with connect_db(home) as db:
        result = run_agent_job(
            db,
            project_id=project["id"],
            plan_id=plan["id"],
            phase_id=phase["id"],
            job_type="IMPLEMENT",
            role="coder",
            profile=make_profile(script),
            prompt="Smoke-check live logs.",
            repo_root=repo,
            log_dir=home / "logs" / "phase-1",
            timeout_seconds=10,
        )
    if result.status != "SUCCEEDED":
        raise AssertionError(f"job failed unexpectedly: {result.error}")
    return result


def run_fake_job(*, live_logs: str | None, tty_stream: bool) -> tuple[str, str, str]:
    with job_environment() as env:
        stderr = CaptureTTY() if tty_stream else io.StringIO()
        with patched_env(
            {
                "AGENT_RUNNER_LIVE_LOGS": live_logs,
                "AGENT_RUNNER_COLOR": "never",
                # Keep StringIO smoke checks free of width truncation; the real
                # width behavior is covered by the pty check below.
                "COLUMNS": "200",
            }
        ), contextlib.redirect_stderr(stderr):
            result = _run_job(*env)
        return (
            stderr.getvalue(),
            result.log_path.read_text(encoding="utf-8"),
            result.output_path.read_text(encoding="utf-8"),
        )


def run_fake_job_on_pty(*, cols: int) -> str:
    """Run the engine with stderr attached to a real pty of the given width."""
    with job_environment() as env:
        master_fd, slave_fd = pty.openpty()
        # Raw mode avoids \n -> \r\n translation so we measure exact segments.
        tty.setraw(slave_fd)
        fcntl.ioctl(
            slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0)
        )
        chunks: list[bytes] = []

        def reader() -> None:
            while True:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)

        thread = threading.Thread(target=reader)
        thread.start()
        slave = os.fdopen(slave_fd, "w", buffering=1, encoding="utf-8", newline="")
        try:
            with patched_env(
                {
                    "AGENT_RUNNER_LIVE_LOGS": "rolling",
                    "AGENT_RUNNER_COLOR": "never",
                }
            ), contextlib.redirect_stderr(slave):
                _run_job(*env)
        finally:
            slave.flush()
            slave.close()
            thread.join(timeout=2)
            with contextlib.suppress(OSError):
                os.close(master_fd)
        return b"".join(chunks).decode("utf-8", "replace")


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label}: expected to find {needle!r}")


def assert_not_contains(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label}: did not expect to find {needle!r}")


def assert_preview_count(text: str, prefix: str, expected: int, label: str) -> None:
    actual = text.count(prefix)
    if actual != expected:
        raise AssertionError(
            f"{label}: expected {expected} preview lines with {prefix!r}, got {actual}"
        )


def run_demo(mode: str | None) -> int:
    """Stream a slow fake agent to the real terminal so the preview is visible."""
    if mode:
        os.environ["AGENT_RUNNER_LIVE_LOGS"] = mode
    active = os.environ.get("AGENT_RUNNER_LIVE_LOGS") or "auto (rolling on a TTY)"
    is_tty = sys.stderr.isatty()
    print(f"[demo] live-log mode: {active}", file=sys.stderr)
    print(f"[demo] stderr is a terminal: {is_tty}", file=sys.stderr)
    print(
        "[demo] watch the '[fake coding]:' line below: in rolling mode it should\n"
        "[demo] update in place on ONE line (never scrolling, never wrapping).\n",
        file=sys.stderr,
    )
    with job_environment(agent_writer=write_demo_agent) as env:
        _run_job(*env)
    print(
        "\n[demo] done. One steady line that changed in place = the fix works.",
        file=sys.stderr,
    )
    return 0


def run_checks() -> int:
    default_stderr, default_log, default_output = run_fake_job(
        live_logs=None,
        tty_stream=True,
    )
    assert_contains(default_stderr, "[agent-runner] starting IMPLEMENT", "default")
    # The command is printed, but the prompt text is not.
    assert_contains(default_stderr, "[agent-runner] $ ", "default")
    assert_not_contains(default_stderr, "Smoke-check live logs.", "default cmd")
    # Env-unset on a terminal now animates a rolling line by default.
    assert_contains(default_stderr, "\r\x1b[2K[fake coding]:", "default")
    assert_contains(default_log, "fake stdout from child 20/20", "default log")
    assert_contains(default_output, "fake stdout from child 20/20", "default output")
    print("PASS default: rolling animation + printed command on a terminal")

    nontty_stderr, _, _ = run_fake_job(live_logs=None, tty_stream=False)
    assert_contains(nontty_stderr, "\r\x1b[2K[fake coding]:", "non-tty default")
    print("PASS non-tty default: rolling is the default even without a TTY")

    lines_stderr, lines_log, _ = run_fake_job(live_logs="lines", tty_stream=False)
    assert_preview_count(
        lines_stderr, "[fake coding]:", CHILD_LINE_COUNT * 2, "lines"
    )
    assert_not_contains(lines_stderr, "\r\x1b[2K", "lines")
    assert_contains(lines_log, "fake stderr from child 20/20", "lines log")
    print("PASS lines: explicit readable previews emitted")

    disabled_stderr, _, _ = run_fake_job(live_logs="0", tty_stream=True)
    assert_not_contains(disabled_stderr, "[fake coding]:", "disabled")
    assert_not_contains(disabled_stderr, "[agent-runner] $ ", "disabled")
    print("PASS disabled: AGENT_RUNNER_LIVE_LOGS=0 stays quiet")

    pty_output = run_fake_job_on_pty(cols=NARROW_COLS)
    assert_contains(pty_output, "[agent-runner] $ ", "pty")
    # Segment 0 holds lifecycle/command text before the first rolling write.
    rolling_segments = [
        seg for seg in pty_output.split("\r\x1b[2K")[1:] if seg
    ]
    if not rolling_segments:
        raise AssertionError("pty: expected rolling preview segments")
    for segment in rolling_segments:
        if len(segment) > NARROW_COLS - 1:
            raise AssertionError(
                "pty: rolling segment "
                f"{segment!r} exceeds terminal width {NARROW_COLS}; it would wrap"
            )
    print(f"PASS pty: rolling previews fit within {NARROW_COLS} columns (no wrap)")

    print("All live-log smoke checks passed.")
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] in {"demo", "--demo"}:
        mode = args[1] if len(args) > 1 else None
        return run_demo(mode)
    return run_checks()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
