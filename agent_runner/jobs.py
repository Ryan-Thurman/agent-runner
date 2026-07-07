import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import AgentProfile
from .errors import JobError
from .lock import utc_now_iso
from .storage import create_job, get_job, update_job_pid


WRITE_ROLES = {"coder", "closer", "fixer", "planner"}
READ_ONLY_ROLES = {"reviewer", "triage"}

# Vendor CLIs word these differently (codex, claude, gemini/antigravity), so
# match the common quota/rate-limit phrasings rather than any one CLI's text.
QUOTA_ERROR_PATTERN = re.compile(
    r"quota|rate.?limit|usage.?limit|too many requests|resource.?exhausted"
    r"|insufficient_quota|out of credits|credit balance|\b429\b",
    re.IGNORECASE,
)

# How much of the log tail to scan for quota signatures. Retried jobs append
# to the same log, so a bounded tail keeps the newest attempt's output in view.
_QUOTA_SCAN_TAIL_CHARS = 8000
_LIVE_PREVIEW_MAX_CHARS = 240
_TRUNCATION_MARKER = " ... [truncated]"
_COLOR_RESET = "\033[0m"
_COLOR_PREFIX = "\033[36m"
_LIVE_LOGS_DISABLE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class LivePreviewContext:
    subject: str
    verb: str
    max_chars: int = _LIVE_PREVIEW_MAX_CHARS


@dataclass(frozen=True)
class JobResult:
    job_id: int
    status: str
    exit_code: Optional[int]
    log_path: Path
    prompt_path: Optional[Path]
    output_path: Optional[Path]
    error: Optional[str]


def run_agent_job(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: Optional[int],
    phase_id: Optional[int],
    job_type: str,
    role: str,
    profile: AgentProfile,
    prompt: str,
    repo_root: Path,
    log_dir: Path,
    timeout_seconds: float,
    trigger: Optional[str] = None,
) -> JobResult:
    _ensure_no_running_job(connection, project_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    job_name = job_type.lower()
    prompt_path = log_dir / f"{job_name}-prompt.md"
    log_path = log_dir / f"{job_name}.log"
    output_path = log_dir / _output_filename(job_type, profile.output_capture)
    effective_prompt = _effective_prompt(profile, prompt)
    prompt_path.write_text(effective_prompt, encoding="utf-8")

    started_sha = _git_sha(repo_root)
    job = create_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase_id,
        job_type=job_type,
        status="RUNNING",
        trigger=trigger,
        prompt_path=prompt_path,
        log_path=log_path,
        output_path=output_path,
        started_sha=started_sha,
        started_at=utc_now_iso(),
    )

    argv = _agent_argv(profile, role, effective_prompt, output_path)
    exit_code: Optional[int]
    error: Optional[str]
    _print_job_start(
        job_id=job["id"],
        job_type=job_type,
        role=role,
        profile_name=profile.name,
        log_path=log_path,
    )
    try:
        exit_code, stdout, stderr, error = _run_process(
            argv,
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            shell=False,
            log_path=log_path,
            log_header="$ " + " ".join(argv) + "\n",
            live_preview_context=_live_preview_context(job_type, role, profile.name),
            on_spawn=lambda pid: _record_job_spawn(
                connection, job["id"], job_type, pid
            ),
        )
        if profile.output_capture in {"stdout", "structured-stdout"}:
            output_path.write_text(stdout, encoding="utf-8")
    except KeyboardInterrupt:
        _mark_job_interrupted(connection, job["id"], repo_root)
        raise
    except Exception as exc:
        exit_code = None
        error = _exception_message(exc)
        _append_error(log_path, error)

    if error is None and exit_code != 0:
        error = f"exit code {exit_code}"
    status = "SUCCEEDED" if exit_code == 0 and error is None else "FAILED"
    _finish_job(
        connection,
        job["id"],
        status=status,
        exit_code=exit_code,
        error=error,
        finished_sha=_git_sha(repo_root),
    )
    row = get_job(connection, job["id"])
    return JobResult(
        job_id=row["id"],
        status=row["status"],
        exit_code=row["exit_code"],
        log_path=log_path,
        prompt_path=prompt_path,
        output_path=output_path,
        error=row["error"],
    )


def is_quota_failure(result: JobResult) -> bool:
    if result.status == "SUCCEEDED":
        return False
    if result.error and QUOTA_ERROR_PATTERN.search(result.error):
        return True
    try:
        log_text = result.log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return QUOTA_ERROR_PATTERN.search(log_text[-_QUOTA_SCAN_TAIL_CHARS:]) is not None


def run_checks_job(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: Optional[int],
    phase_id: Optional[int],
    commands: list[str],
    repo_root: Path,
    log_dir: Path,
    timeout_seconds: float,
) -> JobResult:
    _ensure_no_running_job(connection, project_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "checks.log"
    started_sha = _git_sha(repo_root)
    job = create_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase_id,
        job_type="RUN_CHECKS",
        status="RUNNING",
        log_path=log_path,
        started_sha=started_sha,
        started_at=utc_now_iso(),
    )

    failed_command: Optional[str] = None
    final_exit_code = 0
    error: Optional[str] = None
    try:
        log_path.write_text("", encoding="utf-8")
        _print_job_start(
            job_id=job["id"],
            job_type="RUN_CHECKS",
            role="checks",
            profile_name="shell",
            log_path=log_path,
        )
        for command in commands:
            exit_code, stdout, stderr, process_error = _run_process(
                command,
                repo_root=repo_root,
                timeout_seconds=timeout_seconds,
                shell=True,
                log_path=log_path,
                log_header=f"$ {command}\n",
                live_preview_context=_live_preview_context(
                    "RUN_CHECKS", "checks", "checks"
                ),
                on_spawn=lambda pid: _record_job_spawn(
                    connection, job["id"], "RUN_CHECKS", pid
                ),
            )
            if process_error is not None or exit_code != 0:
                failed_command = command
                final_exit_code = exit_code
                error = process_error or f"check failed: {command}"
                break
    except KeyboardInterrupt:
        _mark_job_interrupted(connection, job["id"], repo_root)
        raise
    except Exception as exc:
        final_exit_code = None
        error = _exception_message(exc)
        _append_error(log_path, error)

    status = "SUCCEEDED" if error is None else "FAILED"
    _finish_job(
        connection,
        job["id"],
        status=status,
        exit_code=final_exit_code,
        error=error,
        finished_sha=_git_sha(repo_root),
    )
    if failed_command is not None:
        _record_event(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase_id,
            job_id=job["id"],
            event_type="checks.failed",
            message=f"check failed: {failed_command}",
        )
    row = get_job(connection, job["id"])
    return JobResult(
        job_id=row["id"],
        status=row["status"],
        exit_code=row["exit_code"],
        log_path=log_path,
        prompt_path=None,
        output_path=None,
        error=row["error"],
    )


def _agent_argv(
    profile: AgentProfile, role: str, prompt: str, output_path: Path
) -> list[str]:
    if role in READ_ONLY_ROLES:
        role_flags = profile.read_only_flags
    elif role in WRITE_ROLES:
        role_flags = profile.write_flags
    else:
        raise JobError(f"unknown job role: {role}")

    argv = [profile.command, *profile.prompt_args, *role_flags]
    if profile.output_capture == "last-message-file":
        argv.extend(["--output-last-message", str(output_path)])
    argv.append(prompt)
    return argv


def _effective_prompt(profile: AgentProfile, prompt: str) -> str:
    if not profile.prompt_prefix:
        return prompt
    return f"{profile.prompt_prefix.rstrip()}\n\n{prompt}"


def _run_process(
    command: list[str] | str,
    *,
    repo_root: Path,
    timeout_seconds: float,
    shell: bool,
    log_path: Path,
    log_header: str,
    live_preview_context: Optional[LivePreviewContext] = None,
    on_spawn: Optional[Callable[[int], None]] = None,
) -> tuple[Optional[int], str, str, Optional[str]]:
    live_preview = _live_preview_writer(live_preview_context)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(log_header)
        log_file.flush()
        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        lock = threading.Lock()
        try:
            process = subprocess.Popen(
                command,
                cwd=repo_root,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            error = f"failed to start process: {exc}"
            log_file.write(f"\n[error]\n{error}\n")
            return None, "", "", error
        if on_spawn is not None:
            try:
                on_spawn(process.pid)
            except Exception as exc:
                with lock:
                    log_file.write(
                        f"\n[warning]\nfailed to report spawned process: {exc}\n"
                    )
                    log_file.flush()
        error = None
        try:
            stdout_thread = threading.Thread(
                target=_pump_stream,
                args=(process.stdout, stdout_chunks, log_file, lock, live_preview),
            )
            stderr_thread = threading.Thread(
                target=_pump_stream,
                args=(process.stderr, stderr_chunks, log_file, lock, live_preview),
            )
            stdout_thread.start()
            stderr_thread.start()
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            exit_code, signal_name = _kill_process_group(process)
            error = f"timeout after {timeout_seconds:g}s; killed with {signal_name}"
        except KeyboardInterrupt:
            if process.poll() is None:
                _kill_process_group(process)
            error = "interrupted"
            with lock:
                log_file.write(f"\n[error]\n{error}\n")
                log_file.flush()
            raise
        finally:
            _close_stream_without_thread(process.stdout, stdout_thread)
            _close_stream_without_thread(process.stderr, stderr_thread)
            _join_thread(stdout_thread, timeout=2)
            _join_thread(stderr_thread, timeout=2)
        if error is not None:
            with lock:
                log_file.write(f"\n[error]\n{error}\n")
                log_file.flush()
        return exit_code, "".join(stdout_chunks), "".join(stderr_chunks), error


def _print_job_start(
    *,
    job_id: int,
    job_type: str,
    role: str,
    profile_name: str,
    log_path: Path,
) -> None:
    print(
        "[agent-runner] "
        f"starting {job_type} job {job_id} "
        f"(role={role}, profile={profile_name})",
        file=sys.stderr,
        flush=True,
    )
    print(f"[agent-runner]   log: {log_path}", file=sys.stderr, flush=True)


def _print_job_spawned(job_id: int, job_type: str, pid: int) -> None:
    print(
        f"[agent-runner] spawned {job_type} job {job_id} pid={pid}",
        file=sys.stderr,
        flush=True,
    )


def _record_job_spawn(
    connection: sqlite3.Connection, job_id: int, job_type: str, pid: int
) -> None:
    update_job_pid(connection, job_id, pid)
    _print_job_spawned(job_id, job_type, pid)


def _live_preview_context(
    job_type: str, role: str, profile_name: str
) -> LivePreviewContext:
    if job_type == "RUN_CHECKS":
        return LivePreviewContext(subject="checks", verb="checking")

    verb_by_job_type = {
        "ROADMAP_PLAN": "planning",
        "IMPLEMENT": "coding",
        "REVIEW": "reviewing",
        "TRIAGE": "reviewing",
        "FIX": "fixing",
        "AUTOFIX": "fixing",
        "CLOSE_PHASE": "closing",
    }
    verb = verb_by_job_type.get(job_type, role.lower())
    return LivePreviewContext(subject=profile_name, verb=verb)


def _live_preview_writer(
    context: Optional[LivePreviewContext],
) -> Optional[Callable[[str], None]]:
    if context is None or not _live_logs_enabled():
        return None
    color_enabled = _resolve_color_enabled()

    def write(text: str) -> None:
        for line in _preview_lines(text):
            print(
                _format_live_preview_line(
                    context, line, color_enabled=color_enabled
                ),
                file=sys.stderr,
                flush=True,
            )

    return write


def _preview_lines(text: str) -> list[str]:
    lines = text.splitlines()
    return lines or [""]


def _format_live_preview_line(
    context: LivePreviewContext,
    text: str,
    *,
    color_enabled: bool,
) -> str:
    prefix = f"{context.subject} {context.verb}:"
    body = text.rstrip("\r\n")
    plain = prefix if not body.strip() else f"{prefix} {body}"
    truncated = _truncate_visible(plain, context.max_chars)
    if not color_enabled:
        return truncated
    if truncated == prefix:
        return f"{_COLOR_PREFIX}{truncated}{_COLOR_RESET}"
    colored_prefix = f"{_COLOR_PREFIX}{prefix}{_COLOR_RESET}"
    return truncated.replace(prefix, colored_prefix, 1)


def _truncate_visible(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:max_chars]
    keep = max_chars - len(_TRUNCATION_MARKER)
    return text[:keep].rstrip() + _TRUNCATION_MARKER


def _live_logs_enabled() -> bool:
    value = os.environ.get("AGENT_RUNNER_LIVE_LOGS")
    if value is None:
        return True
    return value.strip().lower() not in _LIVE_LOGS_DISABLE_VALUES


def _resolve_color_enabled(
    *,
    mode: Optional[str] = None,
    stream=None,
    env: Optional[dict[str, str]] = None,
) -> bool:
    env = os.environ if env is None else env
    mode = env.get("AGENT_RUNNER_COLOR", "auto") if mode is None else mode
    normalized = mode.strip().lower()
    if normalized == "always":
        return True
    if normalized == "never":
        return False
    if normalized != "auto":
        normalized = "auto"
    if "NO_COLOR" in env:
        return False
    stream = sys.stderr if stream is None else stream
    return bool(getattr(stream, "isatty", lambda: False)())


def _pump_stream(
    stream,
    chunks: list[str],
    log_file,
    lock: threading.Lock,
    live_preview: Optional[Callable[[str], None]] = None,
) -> None:
    if stream is None:
        return
    with stream:
        for text in stream:
            chunks.append(text)
            with lock:
                log_file.write(text)
                log_file.flush()
                if live_preview is not None:
                    try:
                        live_preview(text)
                    except OSError:
                        pass


def _join_thread(thread: Optional[threading.Thread], *, timeout: float) -> None:
    if thread is not None:
        thread.join(timeout=timeout)


def _close_stream_without_thread(stream, thread: Optional[threading.Thread]) -> None:
    if thread is None and stream is not None:
        stream.close()


def _kill_process_group(process: subprocess.Popen) -> tuple[int, str]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return process.returncode or -signal.SIGTERM, "SIGTERM"
    try:
        return process.wait(timeout=2), "SIGTERM"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return process.returncode or -signal.SIGKILL, "SIGKILL"
        return process.wait(), "SIGKILL"


def _ensure_no_running_job(connection: sqlite3.Connection, project_id: int) -> None:
    running = connection.execute(
        """
        SELECT * FROM jobs
        WHERE project_id = ? AND status = 'RUNNING'
        ORDER BY id
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if running is not None:
        raise JobError(
            "another job is already running "
            f"(job {running['id']}, type {running['type']})"
        )


def _finish_job(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    exit_code: Optional[int],
    error: Optional[str],
    finished_sha: Optional[str],
) -> None:
    now = utc_now_iso()
    connection.execute(
        """
        UPDATE jobs
        SET status = ?,
            exit_code = ?,
            error = ?,
            finished_sha = ?,
            finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (status, exit_code, error, finished_sha, now, now, job_id),
    )
    connection.commit()


def _mark_job_interrupted(
    connection: sqlite3.Connection, job_id: int, repo_root: Path
) -> None:
    _finish_job(
        connection,
        job_id,
        status="FAILED",
        exit_code=None,
        error="interrupted",
        finished_sha=_git_sha(repo_root),
    )


def _append_error(log_path: Path, error: str) -> None:
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n[error]\n{error}\n")


def _exception_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _record_event(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    event_type: str,
    message: str,
    plan_id: Optional[int] = None,
    phase_id: Optional[int] = None,
    job_id: Optional[int] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            project_id, plan_id, phase_id, job_id, event_type, message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, plan_id, phase_id, job_id, event_type, message, utc_now_iso()),
    )
    connection.commit()


def _git_sha(repo_root: Path) -> Optional[str]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _output_filename(job_type: str, output_capture: str) -> str:
    if output_capture == "structured-stdout":
        return f"{job_type.lower()}-output.json"
    if output_capture == "last-message-file":
        return f"{job_type.lower()}-last-message.txt"
    return f"{job_type.lower()}-output.txt"
