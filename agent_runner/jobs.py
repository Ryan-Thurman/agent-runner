import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import unicodedata
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
_LIVE_LOGS_LINE_VALUE = "lines"
_LIVE_LOGS_ROLLING_VALUE = "rolling"

# Half of the 1 MiB macOS ARG_MAX, which covers argv *and* environ. The rest is
# headroom for the flags, the environment, and the kernel's pointer table.
MAX_PROMPT_BYTES = 512 * 1024


@dataclass(frozen=True)
class LivePreviewContext:
    subject: str
    verb: str
    max_chars: int = _LIVE_PREVIEW_MAX_CHARS


class _LivePreviewRenderer:
    def __init__(
        self,
        context: LivePreviewContext,
        *,
        color_enabled: bool,
        line_mode: bool,
        stream,
    ) -> None:
        self._context = context
        self._color_enabled = color_enabled
        self._line_mode = line_mode
        self._stream = stream
        self._active = False

    def write(self, text: str) -> None:
        max_chars = self._effective_max_chars()
        for line in _preview_lines(text):
            preview = _format_live_preview_line(
                self._context,
                line,
                color_enabled=self._color_enabled,
                max_chars=max_chars,
            )
            if self._line_mode:
                print(preview, file=self._stream, flush=True)
            else:
                self._write_rolling(preview)

    def _effective_max_chars(self) -> int:
        # Line mode goes to logs/CI, so a generous fixed cap is fine. Rolling
        # mode overwrites one physical row with \r\x1b[2K, so the preview must
        # fit the terminal width; anything wider wraps and defeats the animation.
        if self._line_mode:
            return self._context.max_chars
        width = _terminal_width(self._stream)
        return max(1, min(self._context.max_chars, width - 1))

    def finish(self, lock: Optional[threading.Lock] = None) -> None:
        if lock is None:
            self._finish_unlocked()
            return
        with lock:
            self._finish_unlocked()

    def _finish_unlocked(self) -> None:
        if self._line_mode or not self._active:
            return
        try:
            self._stream.write("\r\x1b[2K")
            self._stream.flush()
        except OSError:
            pass
        finally:
            self._active = False

    def _write_rolling(self, preview: str) -> None:
        self._stream.write(f"\r\x1b[2K{preview}")
        self._stream.flush()
        self._active = True


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

    argv = _agent_argv(
        profile, role, _bounded_prompt(effective_prompt, prompt_path), output_path
    )
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
            command_display=shlex.join(argv[:-1]),
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
    if status == "FAILED":
        _print_job_failure(
            job_id=job["id"],
            job_type=job_type,
            error=error,
            log_path=log_path,
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
    return _run_shell_commands_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase_id,
        job_type="RUN_CHECKS",
        commands=commands,
        repo_root=repo_root,
        log_dir=log_dir,
        log_filename="checks.log",
        timeout_seconds=timeout_seconds,
        role="checks",
        profile_name="shell",
        failure_error_prefix="check failed",
        failure_event_type="checks.failed",
        failure_event_message_prefix="check failed",
    )


def run_plan_verify_job(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    commands: list[str],
    repo_root: Path,
    log_dir: Path,
    timeout_seconds: float,
    plan_path: str,
    phase_count: int,
    plan_hash: str,
) -> JobResult:
    plan_abs_path = (repo_root / plan_path).resolve()
    extra_env = {
        "AGENT_RUNNER_REPO_ROOT": str(repo_root.resolve()),
        "AGENT_RUNNER_PLAN_PATH": plan_path,
        "AGENT_RUNNER_PLAN_ABS_PATH": str(plan_abs_path),
        "AGENT_RUNNER_PLAN_PHASE_COUNT": str(phase_count),
        "AGENT_RUNNER_PLAN_HASH": plan_hash,
    }
    return _run_shell_commands_job(
        connection,
        project_id=project_id,
        plan_id=None,
        phase_id=None,
        job_type="PLAN_VERIFY",
        commands=commands,
        repo_root=repo_root,
        log_dir=log_dir,
        log_filename="plan-verify.log",
        timeout_seconds=timeout_seconds,
        role="plan-verify",
        profile_name="shell",
        failure_error_prefix="plan verify failed",
        failure_event_type="plan_verify.failed",
        failure_event_message_prefix="plan verify failed",
        extra_env=extra_env,
    )


def _run_shell_commands_job(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: Optional[int],
    phase_id: Optional[int],
    job_type: str,
    commands: list[str],
    repo_root: Path,
    log_dir: Path,
    log_filename: str,
    timeout_seconds: float,
    role: str,
    profile_name: str,
    failure_error_prefix: str,
    failure_event_type: Optional[str] = None,
    failure_event_message_prefix: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> JobResult:
    _ensure_no_running_job(connection, project_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_filename
    started_sha = _git_sha(repo_root)
    job = create_job(
        connection,
        project_id=project_id,
        plan_id=plan_id,
        phase_id=phase_id,
        job_type=job_type,
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
            job_type=job_type,
            role=role,
            profile_name=profile_name,
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
                command_display=command,
                live_preview_context=_live_preview_context(
                    job_type, role, profile_name
                ),
                extra_env=extra_env,
                on_spawn=lambda pid: _record_job_spawn(
                    connection, job["id"], job_type, pid
                ),
            )
            if process_error is not None or exit_code != 0:
                failed_command = command
                final_exit_code = exit_code
                error = process_error or f"{failure_error_prefix}: {command}"
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
    if status == "FAILED":
        _print_job_failure(
            job_id=job["id"],
            job_type=job_type,
            error=error,
            log_path=log_path,
        )
    if failed_command is not None and failure_event_type is not None:
        message_prefix = failure_event_message_prefix or failure_error_prefix
        _record_event(
            connection,
            project_id=project_id,
            plan_id=plan_id,
            phase_id=phase_id,
            job_id=job["id"],
            event_type=failure_event_type,
            message=f"{message_prefix}: {failed_command}",
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


def _bounded_prompt(prompt: str, prompt_path: Path) -> str:
    """Keep the prompt argv under ARG_MAX.

    `execve` caps argv plus environ at 1 MiB on macOS, and the prompt is one
    argv entry, so an oversized prompt fails the job with "Argument list too
    long" before the agent starts. Callers should avoid embedding unbounded
    content; this is the last resort that keeps a job runnable when one of them
    grows unexpectedly. The untruncated prompt is on disk already, so point the
    agent at it.
    """
    encoded = prompt.encode("utf-8")
    if len(encoded) <= MAX_PROMPT_BYTES:
        return prompt
    # Cut on a byte budget, then drop any partial character at the seam.
    kept = encoded[:MAX_PROMPT_BYTES].decode("utf-8", errors="ignore")
    return (
        f"{kept}\n\n[agent-runner] prompt truncated at {MAX_PROMPT_BYTES} bytes "
        f"to stay under ARG_MAX. Full prompt: {prompt_path}\n"
    )


def _run_process(
    command: list[str] | str,
    *,
    repo_root: Path,
    timeout_seconds: float,
    shell: bool,
    log_path: Path,
    log_header: str,
    command_display: Optional[str] = None,
    live_preview_context: Optional[LivePreviewContext] = None,
    extra_env: Optional[dict[str, str]] = None,
    on_spawn: Optional[Callable[[int], None]] = None,
) -> tuple[Optional[int], str, str, Optional[str]]:
    live_preview = _live_preview_writer(live_preview_context)
    if live_preview is not None and command_display:
        # Print the command on its own line so it persists while the rolling
        # preview animates (and is later cleared) on the line below it.
        print(f"[agent-runner] $ {command_display}", file=sys.stderr, flush=True)
    lock: Optional[threading.Lock] = None
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(log_header)
            log_file.flush()
            stdout_thread: Optional[threading.Thread] = None
            stderr_thread: Optional[threading.Thread] = None
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            lock = threading.Lock()
            env = None
            if extra_env is not None:
                env = os.environ.copy()
                env.update(extra_env)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=repo_root,
                    shell=shell,
                    env=env,
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
    finally:
        if live_preview is not None:
            live_preview.finish(lock)


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


def _print_job_failure(
    *,
    job_id: int,
    job_type: str,
    error: Optional[str],
    log_path: Path,
) -> None:
    detail = error or "job failed"
    print(
        f"[agent-runner] {job_type} job {job_id} failed: {detail}",
        file=sys.stderr,
        flush=True,
    )
    print(f"[agent-runner]   log: {log_path}", file=sys.stderr, flush=True)


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
    if job_type == "PLAN_VERIFY":
        return LivePreviewContext(subject="plan", verb="verifying")

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
) -> Optional[_LivePreviewRenderer]:
    if context is None:
        return None
    stream = sys.stderr
    mode = _live_logs_mode()
    if mode is None:
        return None
    return _LivePreviewRenderer(
        context,
        color_enabled=_resolve_color_enabled(stream=stream),
        line_mode=mode == _LIVE_LOGS_LINE_VALUE,
        stream=stream,
    )


def _preview_lines(text: str) -> list[str]:
    lines = text.splitlines()
    return lines or [""]


def _format_live_preview_line(
    context: LivePreviewContext,
    text: str,
    *,
    color_enabled: bool,
    max_chars: int,
) -> str:
    prefix = f"[{context.subject} {context.verb}]:"
    body = text.rstrip("\r\n")
    plain = prefix if not body.strip() else f"{prefix} {body}"
    truncated = _truncate_visible(plain, max_chars)
    if not color_enabled:
        return truncated
    if truncated == prefix:
        return f"{_COLOR_PREFIX}{truncated}{_COLOR_RESET}"
    colored_prefix = f"{_COLOR_PREFIX}{prefix}{_COLOR_RESET}"
    return truncated.replace(prefix, colored_prefix, 1)


def _char_width(char: str) -> int:
    # Combining marks and zero-width formatting/control glyphs advance the cursor
    # by nothing; East-Asian wide/fullwidth glyphs and most emoji take two cells.
    if unicodedata.combining(char) or unicodedata.category(char) in {
        "Mn",
        "Me",
        "Cf",
        "Cc",
    }:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _cut_to_width(text: str, max_cols: int) -> str:
    used = 0
    for index, char in enumerate(text):
        width = _char_width(char)
        if used + width > max_cols:
            return text[:index]
        used += width
    return text


def _truncate_visible(text: str, max_cols: int) -> str:
    # max_cols is a terminal-column budget, so measure by rendered display width
    # (wide/zero-width glyphs) rather than code-point count; otherwise a preview
    # can still overflow one physical row and reintroduce the wrapping bug.
    if _display_width(text) <= max_cols:
        return text
    marker_cols = _display_width(_TRUNCATION_MARKER)
    if max_cols <= marker_cols:
        return _cut_to_width(_TRUNCATION_MARKER, max_cols)
    return _cut_to_width(text, max_cols - marker_cols).rstrip() + _TRUNCATION_MARKER


def _terminal_width(stream) -> int:
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, AttributeError):
        # StringIO/pipes have no real fileno; fall back to COLUMNS or 80.
        return shutil.get_terminal_size((80, 24)).columns


def _live_logs_mode() -> Optional[str]:
    # Rolling (the width-fit one-line animation) is the default everywhere.
    # Opt into `lines` for readable captured/CI stderr, or 0/false/no/off to
    # silence previews entirely.
    value = os.environ.get("AGENT_RUNNER_LIVE_LOGS")
    normalized = value.strip().lower() if value is not None else ""
    if normalized in _LIVE_LOGS_DISABLE_VALUES:
        return None
    if normalized == _LIVE_LOGS_LINE_VALUE:
        return _LIVE_LOGS_LINE_VALUE
    return _LIVE_LOGS_ROLLING_VALUE


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
    live_preview: Optional[_LivePreviewRenderer] = None,
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
                        live_preview.write(text)
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
