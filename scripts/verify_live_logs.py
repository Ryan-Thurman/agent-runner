#!/usr/bin/env python3
"""Interactive demo of agent-runner live log previews.

Run this in a real terminal to *watch* the rolling preview animate in place:

    python3 scripts/verify_live_logs.py          # honor $AGENT_RUNNER_LIVE_LOGS / default
    python3 scripts/verify_live_logs.py rolling   # force the rolling line
    python3 scripts/verify_live_logs.py lines      # force newline-delimited previews
    python3 scripts/verify_live_logs.py 0          # disable previews

It streams a slow fake agent straight to your terminal (no capture, no env
override) so you can confirm, by eye, that each command is printed and its output
animates on one line without wrapping.

The *automated* assertions for this behavior — including the real-pty
narrow-width regression guard that a captured StringIO cannot exercise — live in
`tests/test_phase4_jobs.py` and run under `python3 -m unittest discover -s tests`.
This script deliberately reuses that module's fixtures so the two never drift.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
# The shared fixtures live in the test module; import them instead of re-deriving.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import os  # noqa: E402

from agent_runner.jobs import run_agent_job  # noqa: E402
from agent_runner.storage import connect_db  # noqa: E402
from test_phase4_jobs import (  # noqa: E402
    git_init_with_commit,
    make_profile,
    setup_state,
)


def write_demo_agent(path: Path) -> None:
    """A slow agent that emits varied, wrap-tempting lines for live viewing."""
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
    "collected 33 items ................................",
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


def run_demo(mode: str | None) -> int:
    if mode:
        os.environ["AGENT_RUNNER_LIVE_LOGS"] = mode
    active = os.environ.get("AGENT_RUNNER_LIVE_LOGS") or "default (rolling)"
    print(f"[demo] live-log mode: {active}", file=sys.stderr)
    print(f"[demo] stderr is a terminal: {sys.stderr.isatty()}", file=sys.stderr)
    print(
        "[demo] watch the '[fake coding]:' line below: in rolling mode it should\n"
        "[demo] update in place on ONE line (never scrolling, never wrapping).\n",
        file=sys.stderr,
    )

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"
        home = root / "home"
        script = root / "demo_agent.py"
        repo.mkdir()
        git_init_with_commit(repo)
        write_demo_agent(script)
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
                prompt="Smoke-check live logs.",
                repo_root=repo,
                log_dir=home / "logs" / "phase-demo",
                timeout_seconds=30,
            )

    print(
        "\n[demo] done. One steady line that changed in place = the fix works.",
        file=sys.stderr,
    )
    print(f"[demo] full output was captured to {result.log_path}", file=sys.stderr)
    return 0 if result.status == "SUCCEEDED" else 1


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a not in {"demo", "--demo"}]
    mode = args[0] if args else None
    return run_demo(mode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
