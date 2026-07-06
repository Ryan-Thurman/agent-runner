import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import RunnerConfig
from .errors import JobError
from .jobs import JobResult, run_agent_job, run_checks_job
from .plan import ParsedPhase, ParsedPlan
from .storage import create_job, get_phase, record_event, update_phase_status


@dataclass(frozen=True)
class PhaseLoopResult:
    message: str
    blocked: bool = False


def run_phase_loop(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    parsed_plan: ParsedPlan,
    config: RunnerConfig,
    repo_root: Path,
) -> PhaseLoopResult:
    phase = _next_action_phase(connection, plan_id)
    if phase is None:
        return PhaseLoopResult("no phase is ready for Phase 5 work")

    phase_number = phase["phase_number"]
    parsed_phase = _parsed_phase(parsed_plan, phase_number)
    status = phase["status"]

    if status == "BLOCKED":
        return PhaseLoopResult(
            f"phase {phase_number} is BLOCKED; inspect status before rerunning",
            blocked=True,
        )
    if status in {"REVIEWING", "CLOSING"}:
        return PhaseLoopResult(
            f"phase {phase_number} is {status}; later phases handle the next step"
        )
    if status == "FIXING":
        return PhaseLoopResult(
            f"phase {phase_number} is FIXING; Phase 6 handles fix execution"
        )
    if status in {"PENDING", "IMPLEMENTING"}:
        preexisting_dirty_paths = None
        if status == "PENDING":
            preexisting_dirty_paths = _ensure_clean_or_allowed(config, repo_root)
        return _run_implement(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            parsed_phase=parsed_phase,
            config=config,
            repo_root=repo_root,
            preexisting_dirty_paths=preexisting_dirty_paths,
        )
    if status == "CHECKING":
        return _run_checks(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            parsed_phase=parsed_phase,
            config=config,
            repo_root=repo_root,
        )

    raise JobError(f"unsupported phase status: {status}")


def _run_implement(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    parsed_phase: ParsedPhase,
    config: RunnerConfig,
    repo_root: Path,
    preexisting_dirty_paths: Optional[set[str]],
) -> PhaseLoopResult:
    phase = update_phase_status(connection, phase["id"], "IMPLEMENTING")
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        event_type="phase.implementing",
        message=f"started IMPLEMENT for phase {phase['phase_number']}",
    )
    profile = _profile_for_role(config, "coder")
    result = run_agent_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_type="IMPLEMENT",
        role="coder",
        profile=profile,
        prompt=_implement_prompt(repo_root, parsed_phase),
        repo_root=repo_root,
        log_dir=Path(phase["log_dir"]),
        timeout_seconds=config.timeout_minutes * 60,
    )
    if result.status != "SUCCEEDED":
        _block_phase_after_job(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job=result,
            message=f"IMPLEMENT failed for phase {phase['phase_number']}: {result.error}",
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED after IMPLEMENT failure",
            blocked=True,
        )

    _stage_implementation_changes(repo_root, preexisting_dirty_paths)
    update_phase_status(connection, phase["id"], "CHECKING")
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=result.job_id,
        event_type="phase.checking",
        message=f"IMPLEMENT succeeded; staged changes for phase {phase['phase_number']}",
    )
    return _run_checks(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase=get_phase(connection, phase["id"]),
        parsed_phase=parsed_phase,
        config=config,
        repo_root=repo_root,
    )


def _run_checks(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    parsed_phase: ParsedPhase,
    config: RunnerConfig,
    repo_root: Path,
) -> PhaseLoopResult:
    result = run_checks_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        commands=config.checks,
        repo_root=repo_root,
        log_dir=Path(phase["log_dir"]),
        timeout_seconds=config.timeout_minutes * 60,
    )
    if result.status == "SUCCEEDED":
        update_phase_status(connection, phase["id"], "REVIEWING")
        record_event(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job_id=result.job_id,
            event_type="phase.reviewing",
            message=f"checks passed for phase {phase['phase_number']}",
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} checks passed; moved to REVIEWING"
        )

    phase = update_phase_status(connection, phase["id"], "FIXING", increment_retry=True)
    fix_prompt_path = Path(phase["log_dir"]) / "fix-checks-prompt.md"
    fix_prompt_path.write_text(
        _checks_fix_prompt(parsed_phase, result),
        encoding="utf-8",
    )
    fix_job = create_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_type="FIX",
        status="PENDING",
        trigger="checks",
        prompt_path=fix_prompt_path,
        log_path=Path(phase["log_dir"]) / "fix.log",
        error=result.error,
    )
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=fix_job["id"],
        event_type="phase.fixing",
        message=f"checks failed for phase {phase['phase_number']}; enqueued FIX",
        data={"checkJobId": result.job_id, "trigger": "checks"},
    )
    return PhaseLoopResult(
        f"phase {phase['phase_number']} checks failed; enqueued checks-triggered FIX"
    )


def _next_action_phase(
    connection: sqlite3.Connection, plan_id: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM phases
        WHERE plan_id = ?
          AND status IN (
              'PENDING', 'IMPLEMENTING', 'CHECKING', 'FIXING',
              'REVIEWING', 'CLOSING', 'BLOCKED'
          )
        ORDER BY phase_number
        LIMIT 1
        """,
        (plan_id,),
    ).fetchone()


def _parsed_phase(parsed_plan: ParsedPlan, phase_number: int) -> ParsedPhase:
    for phase in parsed_plan.phases:
        if phase.phase_number == phase_number:
            return phase
    raise JobError(f"registered phase {phase_number} is missing from parsed plan")


def _profile_for_role(config: RunnerConfig, role: str):
    try:
        return config.agents[config.roles[role]]
    except KeyError as exc:
        raise JobError(f"missing configured role: {role}") from exc


def _ensure_clean_or_allowed(config: RunnerConfig, repo_root: Path) -> Optional[set[str]]:
    dirty_paths = _git_dirty_paths(repo_root)
    if not dirty_paths:
        return None
    if config.allow_dirty:
        print(
            "[agent-runner] warning: worktree is dirty; continuing",
            file=sys.stderr,
            flush=True,
        )
        return dirty_paths
    raise JobError(
        "dirty worktree; commit, stash, or set allowDirty=true before starting a job"
    )


def _git_dirty_paths(repo_root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise JobError(result.stderr.strip() or "git status failed")
    return _parse_porcelain_paths(result.stdout)


def _parse_porcelain_paths(output: str) -> set[str]:
    paths: set[str] = set()
    for line in output.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            old_path, new_path = path.split(" -> ", 1)
            paths.add(old_path)
            paths.add(new_path)
        else:
            paths.add(path)
    return paths


def _stage_implementation_changes(
    repo_root: Path, preexisting_dirty_paths: Optional[set[str]]
) -> None:
    if preexisting_dirty_paths is None:
        _git_add_all(repo_root)
        return

    changed_paths = _git_dirty_paths(repo_root)
    paths_to_stage = sorted(changed_paths - preexisting_dirty_paths)
    if not paths_to_stage:
        return
    _git_add_paths(repo_root, paths_to_stage)


def _git_add_all(repo_root: Path) -> None:
    result = subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise JobError(result.stderr.strip() or "git add -A failed")


def _git_add_paths(repo_root: Path, paths: list[str]) -> None:
    result = subprocess.run(
        ["git", "add", "-A", "--", *paths],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise JobError(result.stderr.strip() or "git add -A failed")


def _block_phase_after_job(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase_id: int,
    job: JobResult,
    message: str,
) -> None:
    update_phase_status(connection, phase_id, "BLOCKED")
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase_id,
        job_id=job.job_id,
        event_type="phase.blocked",
        message=message,
        data={"error": job.error, "exitCode": job.exit_code},
    )


def _implement_prompt(repo_root: Path, phase: ParsedPhase) -> str:
    if _toolbelt_installed(repo_root):
        return (
            "/dev-implement-task\n\n"
            f"Phase {phase.phase_number}: {phase.title}\n\n"
            "Scope rules: implement only this phase; do not start future phases; "
            "avoid unrelated refactors; add or update tests with behavior changes.\n\n"
            f"{phase.content}"
        )
    return (
        "You are implementing one phase from this repository's plan.\n\n"
        f"Phase {phase.phase_number}: {phase.title}\n\n"
        "Rules:\n"
        "- Implement only this phase.\n"
        "- Do not start future phases.\n"
        "- Avoid unrelated refactors.\n"
        "- Add or update tests for behavior changes.\n"
        "- Return a brief summary, files changed, tests run, risks, and suggested "
        "commit message.\n\n"
        "Phase body:\n"
        f"{phase.content}"
    )


def _checks_fix_prompt(phase: ParsedPhase, checks: JobResult) -> str:
    output = ""
    if checks.log_path.exists():
        output = checks.log_path.read_text(encoding="utf-8")
    return (
        "Fix only the check failure for this phase.\n\n"
        f"Phase {phase.phase_number}: {phase.title}\n\n"
        "Rules:\n"
        "- Fix only issues demonstrated by the check output below.\n"
        "- Do not start future phases.\n"
        "- Avoid unrelated refactors.\n"
        "- Add or update tests when behavior changes.\n\n"
        "Phase body:\n"
        f"{phase.content}\n\n"
        "Check failure context:\n"
        f"{output}"
    )


def _toolbelt_installed(repo_root: Path) -> bool:
    return any(
        path.exists()
        for path in (
            repo_root / ".atb" / "skills" / "dev-lite-workflow",
            repo_root / ".agents" / "skills" / "dev-lite-workflow",
            repo_root / "AGENTS.md",
        )
    )
