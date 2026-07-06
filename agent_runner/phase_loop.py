import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import RunnerConfig
from .errors import JobError
from .jobs import JobResult, run_agent_job, run_checks_job
from .plan import ParsedPhase, ParsedPlan
from .storage import get_phase, record_event, update_phase_status


REVIEW_RESOLVED_INSTRUCTION = (
    "Verify these blocking issues are resolved; only new Blocking findings may block."
)


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
    if status == "CLOSING":
        return PhaseLoopResult(
            f"phase {phase_number} is {status}; later phases handle the next step"
        )
    if status == "FIXING":
        return _resume_fix(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            parsed_phase=parsed_phase,
            config=config,
            repo_root=repo_root,
        )
    if status == "REVIEWING":
        return _run_review(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            parsed_phase=parsed_phase,
            config=config,
            repo_root=repo_root,
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
        return _run_review(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=get_phase(connection, phase["id"]),
            parsed_phase=parsed_phase,
            config=config,
            repo_root=repo_root,
        )

    return _run_fix(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase=phase,
        parsed_phase=parsed_phase,
        config=config,
        repo_root=repo_root,
        trigger="checks",
        prompt=_checks_fix_prompt(parsed_phase, result),
        blocker_summary=_checks_blocker_summary(result),
        source_job=result,
    )


def _run_review(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    parsed_phase: ParsedPhase,
    config: RunnerConfig,
    repo_root: Path,
) -> PhaseLoopResult:
    profile = _profile_for_role(config, "reviewer")
    log_dir = Path(phase["log_dir"])
    result = run_agent_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_type="REVIEW",
        role="reviewer",
        profile=profile,
        prompt=_review_prompt(repo_root, parsed_phase, log_dir),
        repo_root=repo_root,
        log_dir=log_dir,
        timeout_seconds=config.timeout_minutes * 60,
    )
    if result.status != "SUCCEEDED":
        _block_phase_after_job(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job=result,
            message=f"REVIEW failed for phase {phase['phase_number']}: {result.error}",
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED after REVIEW failure",
            blocked=True,
        )

    try:
        review = _extract_review_json(result, log_dir)
    except JobError as exc:
        update_phase_status(connection, phase["id"], "BLOCKED")
        record_event(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job_id=result.job_id,
            event_type="phase.blocked",
            message=f"REVIEW produced invalid JSON for phase {phase['phase_number']}",
            data={"error": str(exc)},
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED after unparseable REVIEW JSON",
            blocked=True,
        )

    status = review["status"]
    blocking_issues = review["blockingIssues"]
    if status == "BLOCKED":
        update_phase_status(connection, phase["id"], "BLOCKED")
        record_event(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job_id=result.job_id,
            event_type="phase.blocked",
            message=f"review blocked phase {phase['phase_number']}",
            data={"summary": review["summary"], "blockingIssues": blocking_issues},
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED by reviewer: {review['summary']}",
            blocked=True,
        )

    if blocking_issues:
        return _run_fix(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            parsed_phase=parsed_phase,
            config=config,
            repo_root=repo_root,
            trigger="review",
            prompt=_review_fix_prompt(parsed_phase, blocking_issues),
            blocker_summary=_review_blocker_summary(blocking_issues),
            source_job=result,
        )

    update_phase_status(connection, phase["id"], "CLOSING")
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=result.job_id,
        event_type="phase.closing",
        message=f"review passed for phase {phase['phase_number']}; moved to CLOSING",
        data={
            "summary": review["summary"],
            "nonBlockingIssues": review["nonBlockingIssues"],
        },
    )
    return PhaseLoopResult(
        f"phase {phase['phase_number']} review passed; moved to CLOSING"
    )


def _run_fix(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    parsed_phase: ParsedPhase,
    config: RunnerConfig,
    repo_root: Path,
    trigger: str,
    prompt: str,
    blocker_summary: str,
    source_job: JobResult,
) -> PhaseLoopResult:
    if phase["retry_count"] >= config.max_retries_per_phase:
        return _block_retries_exhausted(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase=phase,
            trigger=trigger,
            blocker_summary=blocker_summary,
            source_job=source_job,
        )

    phase = update_phase_status(connection, phase["id"], "FIXING", increment_retry=True)
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=source_job.job_id,
        event_type="phase.fixing",
        message=f"{trigger} requested FIX for phase {phase['phase_number']}",
        data={"trigger": trigger, "retryCount": phase["retry_count"]},
    )

    profile = _profile_for_role(config, "coder")
    fix_result = run_agent_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_type="FIX",
        role="coder",
        profile=profile,
        prompt=prompt,
        repo_root=repo_root,
        log_dir=Path(phase["log_dir"]),
        timeout_seconds=config.timeout_minutes * 60,
        trigger=trigger,
    )
    if fix_result.status != "SUCCEEDED":
        _block_phase_after_job(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job=fix_result,
            message=f"FIX failed for phase {phase['phase_number']}: {fix_result.error}",
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED after FIX failure",
            blocked=True,
        )

    _git_add_all(repo_root)
    update_phase_status(connection, phase["id"], "CHECKING")
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=fix_result.job_id,
        event_type="phase.checking",
        message=f"FIX succeeded for phase {phase['phase_number']}; rerunning checks",
        data={"trigger": trigger},
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


def _resume_fix(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    parsed_phase: ParsedPhase,
    config: RunnerConfig,
    repo_root: Path,
) -> PhaseLoopResult:
    prior_fix = connection.execute(
        """
        SELECT *
        FROM jobs
        WHERE phase_id = ? AND type = 'FIX' AND prompt_path IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (phase["id"],),
    ).fetchone()
    if prior_fix is None:
        update_phase_status(connection, phase["id"], "BLOCKED")
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED: no FIX prompt is available",
            blocked=True,
        )
    prompt = Path(prior_fix["prompt_path"]).read_text(encoding="utf-8")
    trigger = prior_fix["trigger"] or "review"
    source_job = JobResult(
        job_id=prior_fix["id"],
        status=prior_fix["status"],
        exit_code=prior_fix["exit_code"],
        log_path=Path(prior_fix["log_path"]),
        prompt_path=Path(prior_fix["prompt_path"]),
        output_path=Path(prior_fix["output_path"]) if prior_fix["output_path"] else None,
        error=prior_fix["error"],
    )
    return _run_fix_without_increment(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase=phase,
        parsed_phase=parsed_phase,
        config=config,
        repo_root=repo_root,
        trigger=trigger,
        prompt=prompt,
        source_job=source_job,
    )


def _run_fix_without_increment(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    parsed_phase: ParsedPhase,
    config: RunnerConfig,
    repo_root: Path,
    trigger: str,
    prompt: str,
    source_job: JobResult,
) -> PhaseLoopResult:
    profile = _profile_for_role(config, "coder")
    fix_result = run_agent_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_type="FIX",
        role="coder",
        profile=profile,
        prompt=prompt,
        repo_root=repo_root,
        log_dir=Path(phase["log_dir"]),
        timeout_seconds=config.timeout_minutes * 60,
        trigger=trigger,
    )
    if fix_result.status != "SUCCEEDED":
        _block_phase_after_job(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase["id"],
            job=fix_result,
            message=f"FIX failed for phase {phase['phase_number']}: {fix_result.error}",
        )
        return PhaseLoopResult(
            f"phase {phase['phase_number']} BLOCKED after FIX failure",
            blocked=True,
        )
    _git_add_all(repo_root)
    update_phase_status(connection, phase["id"], "CHECKING")
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=fix_result.job_id,
        event_type="phase.checking",
        message=f"FIX resumed for phase {phase['phase_number']}; rerunning checks",
        data={"trigger": trigger, "sourceJobId": source_job.job_id},
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


def _git_diff_staged(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--staged"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise JobError(result.stderr.strip() or "git diff --staged failed")
    return result.stdout


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


def _block_retries_exhausted(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase: sqlite3.Row,
    trigger: str,
    blocker_summary: str,
    source_job: JobResult,
) -> PhaseLoopResult:
    update_phase_status(connection, phase["id"], "BLOCKED")
    message = (
        f"phase {phase['phase_number']} retries exhausted after "
        f"{phase['retry_count']} FIX attempt(s); outstanding {trigger} blockers: "
        f"{blocker_summary}"
    )
    record_event(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase["id"],
        job_id=source_job.job_id,
        event_type="phase.blocked",
        message=message,
        data={
            "trigger": trigger,
            "retryCount": phase["retry_count"],
            "outstandingBlockers": blocker_summary,
        },
    )
    return PhaseLoopResult(message, blocked=True)


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


def _review_prompt(repo_root: Path, phase: ParsedPhase, log_dir: Path) -> str:
    previous_review = ""
    review_json_path = log_dir / "review.json"
    if review_json_path.exists():
        previous_review = (
            "Previous review.json:\n"
            "```json\n"
            f"{review_json_path.read_text(encoding='utf-8')}\n"
            "```\n\n"
            f"{REVIEW_RESOLVED_INSTRUCTION}\n\n"
        )

    return (
        "Review the staged phase work independently. Do not edit files.\n\n"
        f"Phase {phase.phase_number}: {phase.title}\n\n"
        "Return strict JSON only with this shape:\n"
        "{\n"
        '  "status": "PASS | CHANGES_REQUESTED | BLOCKED",\n'
        '  "summary": "string",\n'
        '  "blockingIssues": [],\n'
        '  "nonBlockingIssues": [],\n'
        '  "recommendedFixPrompt": "string"\n'
        "}\n\n"
        "Only Blocking findings belong in blockingIssues. Should Fix and Nice to "
        "Have findings belong in nonBlockingIssues and are not gating.\n\n"
        f"{previous_review}"
        "Phase body:\n"
        f"{phase.content}\n\n"
        "git diff --staged:\n"
        "```diff\n"
        f"{_git_diff_staged(repo_root)}\n"
        "```\n\n"
        "Check output:\n"
        "```text\n"
        f"{_checks_log_text(log_dir)}\n"
        "```"
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


def _review_fix_prompt(phase: ParsedPhase, blocking_issues: list[Any]) -> str:
    return (
        "Fix only the listed review blocking issues for this phase.\n\n"
        f"Phase {phase.phase_number}: {phase.title}\n\n"
        "Rules:\n"
        "- Fix only the listed blocking issues below.\n"
        "- Do not fix non-blocking review notes unless required by a listed blocker.\n"
        "- Do not start future phases.\n"
        "- Avoid unrelated refactors.\n"
        "- Add or update tests when behavior changes.\n\n"
        "Phase body:\n"
        f"{phase.content}\n\n"
        "Blocking issues:\n"
        "```json\n"
        f"{json.dumps(blocking_issues, indent=2, sort_keys=True)}\n"
        "```"
    )


def _checks_log_text(log_dir: Path) -> str:
    checks_log = log_dir / "checks.log"
    if not checks_log.exists():
        return ""
    return checks_log.read_text(encoding="utf-8")


def _checks_blocker_summary(checks: JobResult) -> str:
    if checks.error:
        return checks.error
    if checks.log_path.exists():
        return _single_line(checks.log_path.read_text(encoding="utf-8"), limit=240)
    return "checks failed"


def _review_blocker_summary(blocking_issues: list[Any]) -> str:
    return _single_line(json.dumps(blocking_issues, sort_keys=True), limit=240)


def _single_line(text: str, *, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _extract_review_json(result: JobResult, log_dir: Path) -> dict[str, Any]:
    raw_output = _review_capture_output(result)
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        _append_review_capture_to_log(result, raw_output)
        raise JobError(f"invalid review JSON: {exc}") from exc

    review = _validate_review_payload(payload)
    (log_dir / "review.json").write_text(
        json.dumps(review, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return review


def _review_capture_output(result: JobResult) -> str:
    if result.output_path is not None and result.output_path.exists():
        return result.output_path.read_text(encoding="utf-8")
    if result.log_path.exists():
        return result.log_path.read_text(encoding="utf-8")
    return ""


def _append_review_capture_to_log(result: JobResult, raw_output: str) -> None:
    if not raw_output:
        return
    with result.log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("\n[captured review output]\n")
        log_file.write(raw_output)
        if not raw_output.endswith("\n"):
            log_file.write("\n")


def _validate_review_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise JobError("review JSON must be an object")

    status = payload.get("status")
    if status not in {"PASS", "CHANGES_REQUESTED", "BLOCKED"}:
        raise JobError("review JSON status must be PASS, CHANGES_REQUESTED, or BLOCKED")

    summary = payload.get("summary")
    if not isinstance(summary, str):
        raise JobError("review JSON summary must be a string")

    blocking_issues = payload.get("blockingIssues")
    if not isinstance(blocking_issues, list):
        raise JobError("review JSON blockingIssues must be a list")

    non_blocking_issues = payload.get("nonBlockingIssues")
    if not isinstance(non_blocking_issues, list):
        raise JobError("review JSON nonBlockingIssues must be a list")

    recommended_fix_prompt = payload.get("recommendedFixPrompt")
    if not isinstance(recommended_fix_prompt, str):
        raise JobError("review JSON recommendedFixPrompt must be a string")

    return {
        "status": status,
        "summary": summary,
        "blockingIssues": blocking_issues,
        "nonBlockingIssues": non_blocking_issues,
        "recommendedFixPrompt": recommended_fix_prompt,
    }


def _toolbelt_installed(repo_root: Path) -> bool:
    return any(
        path.exists()
        for path in (
            repo_root / ".atb" / "skills" / "dev-lite-workflow",
            repo_root / ".agents" / "skills" / "dev-lite-workflow",
            repo_root / "AGENTS.md",
        )
    )
