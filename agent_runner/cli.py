import argparse
import json
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
from .plan import parse_plan_file, register_or_resume_plan
from .storage import (
    connect_db,
    get_or_create_project,
    list_phases_for_plan,
    list_plans_for_project,
    list_recent_events,
    reap_orphaned_jobs,
    rows_to_dicts,
)


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
    run_parser.add_argument(
        "--accept-plan-change",
        action="store_true",
        help="accept changed in-progress or complete phase content",
    )
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
            parsed_plan = parse_plan_file(repo_root, config.plan_path)
            with connect_db(home) as db:
                project = get_or_create_project(db, slug=slug, repo_path=repo_root)
                reaped_jobs = reap_orphaned_jobs(db, project["id"])
                plan_result = register_or_resume_plan(
                    db,
                    project_id=project["id"],
                    project_slug=slug,
                    logs_dir=home / "logs",
                    parsed_plan=parsed_plan,
                    accept_plan_change=args.accept_plan_change,
                )
            if reaped_jobs:
                print(
                    f"[agent-runner] reaped {len(reaped_jobs)} orphaned job(s)",
                    file=sys.stderr,
                )
            print(
                "[agent-runner] "
                f"{'registered' if plan_result.created else 'resumed'} plan "
                f"{config.plan_path} with {plan_result.phase_count} phase(s)",
                file=sys.stderr,
            )
            if plan_result.changed_phase_numbers:
                phase_list = ", ".join(
                    str(number) for number in plan_result.changed_phase_numbers
                )
                print(
                    f"[agent-runner] updated changed phase(s): {phase_list}",
                    file=sys.stderr,
                )
            if plan_result.accepted_phase_numbers:
                phase_list = ", ".join(
                    str(number) for number in plan_result.accepted_phase_numbers
                )
                print(
                    f"[agent-runner] accepted protected plan change(s): {phase_list}",
                    file=sys.stderr,
                )
            hold_seconds = float(os.environ.get("AGENT_RUNNER_HOLD_SECONDS", "0"))
            if hold_seconds > 0:
                time.sleep(hold_seconds)
            print(
                "[agent-runner] job loop is not implemented until Phase 4+; "
                "config, lock, storage, and plan checks passed",
                file=sys.stderr,
            )
    except KeyboardInterrupt:
        print("[agent-runner] interrupted; lock released", file=sys.stderr)
        return 130
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    with connect_db(home) as db:
        project = get_or_create_project(db, slug=slug, repo_path=repo_root)
        plans = list_plans_for_project(db, project["id"])
        plan_payloads = []
        for plan in plans:
            phases = list_phases_for_plan(db, plan["id"])
            plan_payloads.append(
                {
                    **dict(plan),
                    "phases": rows_to_dicts(phases),
                }
            )
        events = list_recent_events(db, project["id"])

    print(f"[agent-runner] project: {repo_root}", file=sys.stderr)
    if not plan_payloads:
        print("[agent-runner] no plan registered yet", file=sys.stderr)
    else:
        for plan in plan_payloads:
            print(
                f"[agent-runner] plan: {plan['path']} ({plan['status']})",
                file=sys.stderr,
            )
            if not plan["phases"]:
                print("[agent-runner]   no phases registered yet", file=sys.stderr)
            for phase in plan["phases"]:
                publish = _format_publish_state(phase)
                print(
                    "[agent-runner]   "
                    f"phase {phase['phase_number']}: {phase['status']} "
                    f"retries={phase['retry_count']}{publish}",
                    file=sys.stderr,
                )

    payload = {
        "project": dict(project),
        "plans": plan_payloads,
        "recentEvents": rows_to_dicts(events),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
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


def _format_publish_state(phase: dict) -> str:
    details = []
    for key in ("publish_mode", "branch_name", "pr_url", "published_sha"):
        if phase.get(key):
            details.append(f"{key}={phase[key]}")
    return f" ({', '.join(details)})" if details else ""
