import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .config import CONFIG_FILENAME, SAMPLE_CONFIG, load_config, project_slug
from .errors import AgentRunnerError, ConfigError, GitRepoError, LockError
from .git import find_git_root
from .lock import ProjectLock, SignalLockRelease, reset_project_lock
from .paths import ensure_runner_layout


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AgentRunnerError as exc:
        print(f"[agent-runner] error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-runner")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="create runner dirs and config")
    init_parser.set_defaults(func=cmd_init)

    run_parser = subcommands.add_parser("run", help="run the next project job")
    run_parser.set_defaults(func=cmd_run)

    status_parser = subcommands.add_parser("status", help="show project status")
    status_parser.set_defaults(func=cmd_status)

    pause_parser = subcommands.add_parser("pause", help="pause at the next job boundary")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subcommands.add_parser("resume", help="resume a paused project")
    resume_parser.set_defaults(func=cmd_resume)

    logs_parser = subcommands.add_parser("logs", help="show log location")
    logs_parser.set_defaults(func=cmd_logs)

    reset_parser = subcommands.add_parser("reset-lock", help="clear this project lock")
    reset_parser.set_defaults(func=cmd_reset_lock)

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    config_path = repo_root / CONFIG_FILENAME
    if config_path.exists():
        raise ConfigError(f"{CONFIG_FILENAME} already exists at {config_path}")
    config_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
    print(f"[agent-runner] initialized runner home: {home}", file=sys.stderr)
    print(f"[agent-runner] wrote sample config: {config_path}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    config = load_config(repo_root)
    emit_config_warnings(config.warnings)
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    lock = ProjectLock(home / "locks", slug, repo_root)

    try:
        with lock, SignalLockRelease(lock):
            print(f"[agent-runner] acquired lock for {slug}", file=sys.stderr)
            hold_seconds = float(os.environ.get("AGENT_RUNNER_HOLD_SECONDS", "0"))
            if hold_seconds > 0:
                time.sleep(hold_seconds)
            print(
                "[agent-runner] run loop is not implemented until Phase 2+; "
                "config and lock checks passed",
                file=sys.stderr,
            )
    except KeyboardInterrupt:
        print("[agent-runner] interrupted; lock released", file=sys.stderr)
        return 130
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    print(
        f"[agent-runner] status storage is not implemented yet for {repo_root}",
        file=sys.stderr,
    )
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    find_git_root()
    print("[agent-runner] pause is not implemented until Phase 8", file=sys.stderr)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    find_git_root()
    print("[agent-runner] resume is not implemented until Phase 8", file=sys.stderr)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    print(str(home / "logs" / project_slug(repo_root)))
    print("[agent-runner] detailed logs are not implemented yet", file=sys.stderr)
    return 0


def cmd_reset_lock(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    path = reset_project_lock(home / "locks", project_slug(repo_root))
    print(f"[agent-runner] cleared lock: {path}", file=sys.stderr)
    return 0


def emit_config_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"[agent-runner] warning: {warning}", file=sys.stderr)
