import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .config import (
    CONFIG_FILENAME,
    PLACEHOLDER_CHECKS,
    detect_default_checks,
    load_config,
    project_slug,
    sample_config_for_checks,
)
from .errors import AgentRunnerError, ConfigError, GitRepoError, LockError
from .git import find_git_root
from .lock import ProjectLock, SignalLockRelease, pid_is_alive, reset_project_lock
from .paths import ensure_runner_layout
from .phase_loop import (
    PhaseLoopResult,
    RESTART_COUNT_ENV,
    extract_pr_number,
    _git_add_all,
    _publish_instructions,
    _record_phase_published,
    _verify_published_phase,
    restart_count,
    run_phase_loop,
)
from .jobs import run_agent_job
from .plan import parse_plan_file, register_or_resume_plan
from .storage import (
    PHASE_STATUSES,
    connect_db,
    get_project,
    get_or_create_project,
    list_phases_for_plan,
    list_plans_for_project,
    list_recent_events,
    list_running_jobs_for_project,
    record_event,
    reap_orphaned_jobs,
    rows_to_dicts,
    update_phase_publish_metadata,
    update_phase_status,
    update_project_status,
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
    parser.add_argument(
        "--version",
        action="version",
        version=f"agent-runner {__version__}",
    )
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

    unblock_parser = subcommands.add_parser(
        "unblock", help="reset a BLOCKED phase so run can retry it"
    )
    unblock_parser.add_argument(
        "--phase",
        type=int,
        default=None,
        help="phase number to unblock (default: the first BLOCKED phase)",
    )
    unblock_parser.add_argument(
        "--to",
        default=None,
        metavar="STATUS",
        help=(
            "status to restore (default: the status recorded when the phase "
            "blocked)"
        ),
    )
    unblock_parser.set_defaults(func=cmd_unblock)

    logs_parser = subcommands.add_parser("logs", help="show latest phase logs")
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=40,
        help="number of lines to tail from the newest log file",
    )
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
    checks = detect_default_checks(repo_root)
    config_path.write_text(sample_config_for_checks(checks), encoding="utf-8")
    print(f"[agent-runner] initialized runner home: {home}", file=sys.stderr)
    print(f"[agent-runner] wrote sample config: {config_path}", file=sys.stderr)
    if checks == PLACEHOLDER_CHECKS:
        print(
            "[agent-runner] checks must be replaced before the first run; "
            "the generated entry is a failing placeholder",
            file=sys.stderr,
        )
    print(
        "[agent-runner] next: review planPath/checks in .agent-runner.json",
        file=sys.stderr,
    )
    print("[agent-runner] next: write docs/plan.md", file=sys.stderr)
    print("[agent-runner] next: run `autorun run`", file=sys.stderr)
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
            with connect_db(home) as db:
                project = get_or_create_project(db, slug=slug, repo_path=repo_root)
                if project["status"] == "PAUSED":
                    print(
                        "[agent-runner] project is PAUSED; run "
                        "`agent-runner resume` then `agent-runner run` to continue",
                        file=sys.stderr,
                    )
                    return 0
            hold_seconds = float(os.environ.get("AGENT_RUNNER_HOLD_SECONDS", "0"))
            if hold_seconds > 0:
                time.sleep(hold_seconds)
            with connect_db(home) as db:
                loop_result = run_phase_loop(
                    db,
                    project_id=project["id"],
                    plan_id=plan_result.plan_id,
                    parsed_plan=parsed_plan,
                    config=config,
                    repo_root=repo_root,
                )
                loop_result = _run_autofix_loop(
                    db,
                    project_id=project["id"],
                    plan_id=plan_result.plan_id,
                    parsed_plan=parsed_plan,
                    config=config,
                    repo_root=repo_root,
                    initial_result=loop_result,
                )
            print(f"[agent-runner] {loop_result.message}", file=sys.stderr)
            if loop_result.restart:
                _exec_self_restart(lock, repo_root)
            return 1 if loop_result.blocked else 0
    except KeyboardInterrupt:
        print("[agent-runner] interrupted; lock released", file=sys.stderr)
        return 130
    return 0


def _exec_self_restart(lock: ProjectLock, repo_root: Path) -> None:
    """Replace this process with a fresh `run` so just-merged code loads.

    exec preserves the PID and terminal, but context managers never unwind
    across it, so the project lock must be released by hand first. Exec'ing
    the repo shim by absolute path makes the fresh import resolve to this
    checkout regardless of how the original invocation found the package.
    The one-shot --accept-plan-change flag is deliberately not carried over.
    """
    os.environ[RESTART_COUNT_ENV] = str(restart_count() + 1)
    lock.release()
    shim = repo_root / "agent-runner"
    os.execv(sys.executable, [sys.executable, str(shim), "run"])


def _run_autofix_loop(
    db,
    *,
    project_id: int,
    plan_id: int,
    parsed_plan,
    config,
    repo_root: Path,
    initial_result: PhaseLoopResult,
) -> PhaseLoopResult:
    if config.auto_fix_attempts <= 0 or "fixer" not in config.roles:
        return initial_result

    result = initial_result
    attempts_by_phase: dict[int, int] = {}
    while result.blocked:
        phase = _blocked_phase_for_plan(db, plan_id)
        if phase is None or not phase["blocked_from"]:
            return result

        blocking_message = _latest_blocking_message(db, phase["id"], result.message)
        if _requires_human_intent(blocking_message):
            return result

        used_attempts = attempts_by_phase.get(phase["id"], 0)
        if used_attempts >= config.auto_fix_attempts:
            return result

        paused = _autofix_paused_result_if_needed(db, project_id)
        if paused is not None:
            return paused

        attempt = used_attempts + 1
        attempts_by_phase[phase["id"]] = attempt
        profile = config.agents[config.roles["fixer"]]
        print(
            "[agent-runner] "
            f"phase {phase['phase_number']} blocked; auto-fix attempt "
            f"{attempt}/{config.auto_fix_attempts} with profile {profile.name}",
            file=sys.stderr,
            flush=True,
        )

        parsed_phase = _parsed_phase_for_number(parsed_plan, phase["phase_number"])
        fix_result = run_agent_job(
            db,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job_type="AUTOFIX",
            role="fixer",
            profile=profile,
            prompt=_autofix_prompt(
                phase=phase,
                parsed_phase=parsed_phase,
                blocking_message=blocking_message,
                require_publish=config.auto_commit,
            ),
            repo_root=repo_root,
            log_dir=Path(phase["log_dir"]),
            timeout_seconds=config.timeout_minutes * 60,
        )
        if fix_result.status != "SUCCEEDED":
            return result

        phase = _prepare_successful_autofix_resume(
            db,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            job_id=fix_result.job_id,
            config=config,
            repo_root=repo_root,
        )
        target = _unblock_phase(
            db,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            to_status=phase["blocked_from"],
        )
        record_event(
            db,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job_id=fix_result.job_id,
            event_type="phase.autofix",
            message=(
                f"auto-fix attempt {attempt}/{config.auto_fix_attempts} "
                f"succeeded for phase {phase['phase_number']}; unblocked to {target}"
            ),
            data={
                "attempt": attempt,
                "maxAttempts": config.auto_fix_attempts,
                "profile": profile.name,
                "to": target,
            },
        )
        result = run_phase_loop(
            db,
            project_id=project_id,
            plan_id=plan_id,
            parsed_plan=parsed_plan,
            config=config,
            repo_root=repo_root,
        )
    return result


def _prepare_successful_autofix_resume(
    db,
    *,
    project_id: int,
    plan_id: int,
    phase,
    job_id: int,
    config,
    repo_root: Path,
):
    if not config.auto_commit:
        _git_add_all(repo_root)
        return phase

    metadata = _verify_published_phase(repo_root)
    phase = update_phase_publish_metadata(
        db,
        phase["id"],
        publish_mode="pr",
        branch_name=metadata.branch_name,
        pr_url=metadata.pr_url,
        published_sha=metadata.published_sha,
    )
    _record_phase_published(
        db,
        project_id=project_id,
        plan_id=plan_id,
        job_id=job_id,
        phase=phase,
        metadata=metadata,
    )
    return phase


def _blocked_phase_for_plan(db, plan_id: int):
    return db.execute(
        """
        SELECT * FROM phases
        WHERE plan_id = ? AND status = 'BLOCKED'
        ORDER BY phase_number
        LIMIT 1
        """,
        (plan_id,),
    ).fetchone()


def _latest_blocking_message(db, phase_id: int, fallback: str) -> str:
    row = db.execute(
        """
        SELECT message FROM events
        WHERE phase_id = ? AND event_type = 'phase.blocked'
        ORDER BY id DESC
        LIMIT 1
        """,
        (phase_id,),
    ).fetchone()
    if row is None or not row["message"]:
        return fallback
    return row["message"]


def _requires_human_intent(blocking_message: str) -> bool:
    lower = blocking_message.lower()
    return any(
        marker in lower
        for marker in (
            "plan changed for phase",
            "protected phase body",
            "registered phase",
            "body on origin/",
        )
    )


def _autofix_paused_result_if_needed(db, project_id: int) -> Optional[PhaseLoopResult]:
    project = get_project(db, project_id)
    if project["status"] != "PAUSED":
        return None
    return PhaseLoopResult(
        "project is PAUSED; run `agent-runner resume` then "
        "`agent-runner run` to continue"
    )


def _parsed_phase_for_number(parsed_plan, phase_number: int):
    for phase in parsed_plan.phases:
        if phase.phase_number == phase_number:
            return phase
    raise AgentRunnerError(f"registered phase {phase_number} is missing from parsed plan")


def _autofix_prompt(
    *, phase, parsed_phase, blocking_message: str, require_publish: bool
) -> str:
    log_tail = _newest_phase_log_tail(Path(phase["log_dir"]))
    publish = _publish_instructions(require_publish, update_existing=True)
    commit_rule = (
        "- Publish the fixer changes before exiting, following the publish "
        "requirements below.\n"
        if require_publish
        else "- Do not commit anything.\n"
    )
    return (
        "Fix the underlying problem that blocked this phase. This is a one-shot "
        "write-capable fixer job.\n\n"
        "Rules:\n"
        "- Fix only the blocker described below.\n"
        "- Do not start future phases or unrelated refactors.\n"
        f"{commit_rule}"
        "- Never invoke `autorun`, `agent-runner`, or any nested runner command; "
        "the current process holds the project lock and a nested run would deadlock.\n"
        "- Return a concise summary of the files changed and checks, if any, you ran.\n\n"
        f"{publish}"
        f"Phase {parsed_phase.phase_number}: {parsed_phase.title}\n\n"
        "Phase body:\n"
        f"{parsed_phase.content}\n\n"
        "Blocking event message:\n"
        f"{blocking_message}\n\n"
        "Newest phase log tail:\n"
        f"{log_tail}"
    )


def _newest_phase_log_tail(log_dir: Path) -> str:
    newest_log = _newest_log_file(log_dir)
    if newest_log is None:
        return "(no phase log file found)\n"
    return (
        f"{newest_log}:\n"
        "```text\n"
        f"{''.join(_tail_lines(newest_log, 80))}"
        "\n```"
    )


def _unblock_phase(
    db,
    *,
    project_id: int,
    plan_id: int,
    phase,
    to_status: Optional[str],
) -> str:
    if phase["status"] != "BLOCKED":
        raise AgentRunnerError(
            f"phase {phase['phase_number']} is {phase['status']}, not BLOCKED"
        )

    target = to_status or phase["blocked_from"]
    if not target:
        raise AgentRunnerError(
            f"phase {phase['phase_number']} has no recorded pre-block status; "
            "rerun with --to STATUS (e.g. --to MERGING)"
        )
    target = target.upper()
    resumable = sorted(PHASE_STATUSES - {"BLOCKED", "COMPLETE"})
    if target not in resumable:
        raise AgentRunnerError(
            f"cannot unblock to {target}; choose one of {', '.join(resumable)}"
        )

    update_phase_status(db, phase["id"], target)
    record_event(
        db,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        event_type="phase.unblocked",
        message=f"phase {phase['phase_number']} unblocked to {target}",
        data={"to": target, "blockedFrom": phase["blocked_from"]},
    )
    return target


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    with connect_db(home) as db:
        project = get_or_create_project(db, slug=slug, repo_path=repo_root)
        if _project_lock_is_live(home / "locks", slug, repo_root):
            reaped_jobs = []
        else:
            reaped_jobs = reap_orphaned_jobs(db, project["id"])
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
        running_jobs = list_running_jobs_for_project(db, project["id"])

    print(f"[agent-runner] project: {repo_root}", file=sys.stderr)
    if reaped_jobs:
        print(
            f"[agent-runner] reaped {len(reaped_jobs)} orphaned job(s)",
            file=sys.stderr,
        )
    if running_jobs:
        print("[agent-runner] running jobs:", file=sys.stderr)
        for job in running_jobs:
            print(f"[agent-runner]   {_format_running_job(job)}", file=sys.stderr)
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
        if events:
            print("[agent-runner] recent events:", file=sys.stderr)
            for event in events:
                print(
                    "[agent-runner]   "
                    f"{event['event_type']}: {event['message']}",
                    file=sys.stderr,
                )

    payload = {
        "project": dict(project),
        "plans": plan_payloads,
        "runningJobs": rows_to_dicts(running_jobs),
        "recentEvents": rows_to_dicts(events),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _project_lock_is_live(locks_dir: Path, slug: str, repo_root: Path) -> bool:
    path = locks_dir / f"{slug}.lock"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(payload, dict):
        return False
    pid = payload.get("pid")
    repo_path = payload.get("repoPath")
    if not isinstance(pid, int) or not isinstance(repo_path, str):
        return False
    try:
        matches_repo = Path(repo_path).resolve() == repo_root.resolve()
    except OSError:
        return False
    return matches_repo and pid_is_alive(pid)


def cmd_pause(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    with connect_db(home) as db:
        project = get_or_create_project(db, slug=slug, repo_path=repo_root)
        update_project_status(db, project["id"], "PAUSED")
        record_event(
            db,
            project_id=project["id"],
            event_type="project.paused",
            message="pause requested; runner will stop at the next job boundary",
        )
    print("[agent-runner] pause requested; active jobs will finish", file=sys.stderr)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    with connect_db(home) as db:
        project = get_or_create_project(db, slug=slug, repo_path=repo_root)
        update_project_status(db, project["id"], "ACTIVE")
        record_event(
            db,
            project_id=project["id"],
            event_type="project.resumed",
            message="resume requested",
        )
    print("[agent-runner] project resumed", file=sys.stderr)
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    config = load_config(repo_root)
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    with connect_db(home) as db:
        project = get_or_create_project(db, slug=slug, repo_path=repo_root)
        plans = list_plans_for_project(db, project["id"])
        plan = next((p for p in plans if p["path"] == config.plan_path), None)
        if plan is None:
            raise AgentRunnerError(
                f"no registered plan at {config.plan_path}; run `agent-runner run` "
                "to register it first"
            )
        phases = list_phases_for_plan(db, plan["id"])
        if args.phase is not None:
            phase = next(
                (p for p in phases if p["phase_number"] == args.phase), None
            )
            if phase is None:
                raise AgentRunnerError(
                    f"plan {config.plan_path} has no phase {args.phase}"
                )
        else:
            phase = next((p for p in phases if p["status"] == "BLOCKED"), None)
            if phase is None:
                print(
                    f"[agent-runner] no BLOCKED phase in {config.plan_path}",
                    file=sys.stderr,
                )
                return 0
        target = _unblock_phase(
            db,
            project_id=project["id"],
            plan_id=plan["id"],
            phase=phase,
            to_status=args.to,
        )
    print(
        f"[agent-runner] phase {phase['phase_number']} unblocked to {target}; "
        "run `agent-runner run` to continue",
        file=sys.stderr,
    )
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    repo_root = find_git_root()
    home = ensure_runner_layout()
    slug = project_slug(repo_root)
    with connect_db(home) as db:
        project = get_or_create_project(db, slug=slug, repo_path=repo_root)
        phase = db.execute(
            """
            SELECT phases.*
            FROM phases
            JOIN plans ON plans.id = phases.plan_id
            WHERE phases.project_id = ?
            ORDER BY phases.updated_at DESC, phases.id DESC
            LIMIT 1
            """,
            (project["id"],),
        ).fetchone()
    if phase is None:
        log_root = home / "logs" / slug
        print(str(log_root))
        print("[agent-runner] no phase logs registered yet", file=sys.stderr)
        return 0

    log_dir = Path(phase["log_dir"])
    print(str(log_dir))
    print(f"[agent-runner] latest phase log dir: {log_dir}", file=sys.stderr)
    newest_log = _newest_log_file(log_dir)
    if newest_log is None:
        print("[agent-runner] no log files found in latest phase", file=sys.stderr)
        return 0
    print(f"[agent-runner] tailing: {newest_log}", file=sys.stderr)
    for line in _tail_lines(newest_log, max(args.lines, 0)):
        print(line, end="" if line.endswith("\n") else "\n")
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


def _newest_log_file(log_dir: Path) -> Optional[Path]:
    if not log_dir.exists():
        return None
    candidates = [path for path in log_dir.glob("*.log") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _tail_lines(path: Path, line_count: int) -> list[str]:
    if line_count <= 0:
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(
        keepends=True
    )
    return lines[-line_count:]


def _format_publish_state(phase: dict) -> str:
    details = []
    for key in ("publish_mode", "branch_name", "published_sha"):
        if phase.get(key):
            details.append(f"{key}={phase[key]}")
    if phase.get("pr_url"):
        pr_number = extract_pr_number(phase["pr_url"])
        if pr_number is None:
            details.append(f"pr={phase['pr_url']}")
        else:
            details.append(f"pr=#{pr_number} ({phase['pr_url']})")
    return f" ({', '.join(details)})" if details else ""


def _format_running_job(job: dict) -> str:
    job = dict(job)
    phase = ""
    if job.get("phase_number") is not None:
        phase = f" phase={job['phase_number']}"
    log = f" log={job['log_path']}" if job.get("log_path") else ""
    started = f" started={job['started_at']}" if job.get("started_at") else ""
    return f"job {job['id']}: {job['type']}{phase}{started}{log}"
