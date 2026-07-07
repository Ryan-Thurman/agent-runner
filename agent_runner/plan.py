import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import PlanError
from .lock import utc_now_iso
from .storage import PHASE_STATUSES, phase_log_dir


PHASE_HEADING_RE = re.compile(r"^## Phase\s+(\d+):\s*(.+?)\s*$")
STATUS_RE = re.compile(r"^Status:\s*([A-Z_]+)\s*$")
EVIDENCE_RE = re.compile(r"^Evidence:\s*(.+?)\s*$")
CHECKS_RE = re.compile(r"^Checks:\s*(.+?)\s*$")
PROTECTED_CHANGE_STATUSES = {
    "IMPLEMENTING",
    "CHECKING",
    "REVIEWING",
    "FIXING",
    "CLOSING",
    "MERGING",
    "COMPLETE",
    "BLOCKED",
}
PLAN_CONTEXT_CHAR_LIMIT = 4000
PLAN_CONTEXT_TRUNCATION_MARKER = "\n\n[plan context truncated]"


@dataclass(frozen=True)
class ParsedPhase:
    phase_number: int
    title: str
    status: str
    content: str
    content_hash: str
    plan_context: str = ""


@dataclass(frozen=True)
class ParsedPlan:
    path: str
    phases: list[ParsedPhase]
    content_hash: str
    plan_context: str = ""


@dataclass(frozen=True)
class PlanRegistrationResult:
    plan_id: int
    created: bool
    changed_phase_numbers: list[int]
    accepted_phase_numbers: list[int]
    phase_count: int


def parse_plan_markdown(text: str, *, path: str) -> ParsedPlan:
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    seen_phase_numbers: set[int] = set()
    for index, line in enumerate(lines):
        match = PHASE_HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            phase_number = int(match.group(1))
            if phase_number in seen_phase_numbers:
                raise PlanError(f"duplicate phase number in plan: {phase_number}")
            seen_phase_numbers.add(phase_number)
            headings.append((index, phase_number, match.group(2).strip()))

    first_heading_index = headings[0][0] if headings else len(lines)
    plan_context = _bounded_plan_context("".join(lines[:first_heading_index]))
    phases: list[ParsedPhase] = []
    for heading_index, phase_number, title in headings:
        next_heading_index = _next_heading_index(headings, heading_index, len(lines))
        content_lines = lines[heading_index + 1 : next_heading_index]
        status, hash_lines = _extract_status_and_hash_lines(content_lines)
        content = "".join(content_lines)
        content_hash = _hash_text("".join(hash_lines))
        phases.append(
            ParsedPhase(
                phase_number=phase_number,
                title=title,
                status=status,
                content=content,
                content_hash=content_hash,
                plan_context=plan_context,
            )
        )

    content_hash = _hash_text(
        "\n".join(
            f"{phase.phase_number}:{phase.title}:{phase.content_hash}"
            for phase in phases
        )
    )
    return ParsedPlan(
        path=path,
        phases=phases,
        content_hash=content_hash,
        plan_context=plan_context,
    )


def parse_plan_file(repo_root: Path, plan_path: str) -> ParsedPlan:
    repo_root = repo_root.resolve()
    path = (repo_root / plan_path).resolve()
    try:
        path.relative_to(repo_root)
    except ValueError as exc:
        raise PlanError(f"plan path escapes repository: {plan_path}") from exc
    if not path.exists():
        raise PlanError(f"missing plan file {plan_path}")
    if not path.is_file():
        raise PlanError(f"plan path is not a file: {plan_path}")
    return parse_plan_markdown(path.read_text(encoding="utf-8"), path=plan_path)


def register_or_resume_plan(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    project_slug: str,
    logs_dir: Path,
    parsed_plan: ParsedPlan,
    accept_plan_change: bool = False,
) -> PlanRegistrationResult:
    changed_phase_numbers: list[int] = []
    accepted_phase_numbers: list[int] = []
    log_dirs: list[Path] = []

    try:
        connection.execute("BEGIN")
        plan = _get_plan_by_path(connection, project_id, parsed_plan.path)
        created = plan is None
        now = utc_now_iso()

        if plan is None:
            cursor = connection.execute(
                """
                INSERT INTO plans (
                    project_id, path, content_hash, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, parsed_plan.path, parsed_plan.content_hash, "PENDING", now, now),
            )
            plan_id = cursor.lastrowid
            _insert_event(
                connection,
                project_id=project_id,
                plan_id=plan_id,
                event_type="plan.registered",
                message=f"registered plan {parsed_plan.path}",
                data={"phaseCount": len(parsed_plan.phases)},
            )
        else:
            plan_id = plan["id"]
            connection.execute(
                """
                UPDATE plans
                SET content_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (parsed_plan.content_hash, now, plan_id),
            )

        for phase in parsed_plan.phases:
            existing_phase = _get_phase_by_number(connection, plan_id, phase.phase_number)
            log_dir = phase_log_dir(
                logs_dir,
                project_slug=project_slug,
                plan_path=parsed_plan.path,
                phase_number=phase.phase_number,
            )
            log_dirs.append(log_dir)

            if existing_phase is None:
                cursor = connection.execute(
                    """
                    INSERT INTO phases (
                        project_id, plan_id, phase_number, title, status,
                        content_hash, log_dir, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        plan_id,
                        phase.phase_number,
                        phase.title,
                        phase.status,
                        phase.content_hash,
                        str(log_dir),
                        now,
                        now,
                    ),
                )
                _insert_event(
                    connection,
                    project_id=project_id,
                    plan_id=plan_id,
                    phase_id=cursor.lastrowid,
                    event_type="phase.registered",
                    message=f"registered phase {phase.phase_number}: {phase.title}",
                    data={"contentHash": phase.content_hash},
                )
                continue

            if existing_phase["content_hash"] == phase.content_hash:
                _sync_phase_metadata(connection, existing_phase["id"], phase, log_dir, now)
                continue

            if existing_phase["status"] == "PENDING" or accept_plan_change:
                _sync_phase_metadata(
                    connection,
                    existing_phase["id"],
                    phase,
                    log_dir,
                    now,
                    content_hash=phase.content_hash,
                )
                event_type = "phase.plan_change_accepted"
                if existing_phase["status"] == "PENDING":
                    event_type = "phase.plan_change_updated"
                    changed_phase_numbers.append(phase.phase_number)
                else:
                    accepted_phase_numbers.append(phase.phase_number)
                _insert_event(
                    connection,
                    project_id=project_id,
                    plan_id=plan_id,
                    phase_id=existing_phase["id"],
                    event_type=event_type,
                    message=f"updated phase {phase.phase_number} from plan change",
                    data={
                        "oldContentHash": existing_phase["content_hash"],
                        "newContentHash": phase.content_hash,
                        "phaseStatus": existing_phase["status"],
                    },
                )
                continue

            if _should_defer_manual_merge_reconciliation(existing_phase, phase):
                _sync_phase_metadata(
                    connection, existing_phase["id"], phase, log_dir, now
                )
                continue

            if existing_phase["status"] in PROTECTED_CHANGE_STATUSES:
                raise PlanError(
                    "plan changed for phase "
                    f"{phase.phase_number} ({phase.title}) while status is "
                    f"{existing_phase['status']}; rerun with --accept-plan-change to accept"
                )

            raise PlanError(
                "plan changed for phase "
                f"{phase.phase_number} ({phase.title}) while status is "
                f"{existing_phase['status']}"
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    for log_dir in log_dirs:
        log_dir.mkdir(parents=True, exist_ok=True)

    return PlanRegistrationResult(
        plan_id=plan_id,
        created=created,
        changed_phase_numbers=changed_phase_numbers,
        accepted_phase_numbers=accepted_phase_numbers,
        phase_count=len(parsed_plan.phases),
    )


def _extract_status_and_hash_lines(lines: list[str]) -> tuple[str, list[str]]:
    if not lines:
        return "PENDING", []
    status_index = 0
    while status_index < len(lines) and not lines[status_index].strip():
        status_index += 1
    if status_index == len(lines):
        return "PENDING", list(lines)

    match = STATUS_RE.match(lines[status_index].rstrip("\r\n"))
    if not match:
        return "PENDING", list(lines)
    status = match.group(1)
    if status not in PHASE_STATUSES:
        raise PlanError(f"invalid phase status marker: {status}")
    hash_start_index = status_index + 1
    if hash_start_index < len(lines):
        evidence_match = EVIDENCE_RE.match(lines[hash_start_index].rstrip("\r\n"))
        if evidence_match:
            hash_start_index = _skip_runner_metadata(lines, hash_start_index)
    return status, list(lines[hash_start_index:])


def _bounded_plan_context(text: str) -> str:
    context = text.strip()
    if len(context) <= PLAN_CONTEXT_CHAR_LIMIT:
        return context
    content_limit = PLAN_CONTEXT_CHAR_LIMIT - len(PLAN_CONTEXT_TRUNCATION_MARKER)
    return context[:content_limit].rstrip() + PLAN_CONTEXT_TRUNCATION_MARKER


def _skip_runner_metadata(lines: list[str], evidence_index: int) -> int:
    hash_start_index = evidence_index + 1
    if hash_start_index >= len(lines) or not lines[hash_start_index].strip():
        return hash_start_index

    blank_index = hash_start_index
    while blank_index < len(lines) and lines[blank_index].strip():
        blank_index += 1

    metadata_lines = lines[hash_start_index:blank_index]
    if any(CHECKS_RE.match(line.rstrip("\r\n")) for line in metadata_lines):
        return blank_index
    return hash_start_index


def _next_heading_index(
    headings: list[tuple[int, int, str]], current_heading_index: int, line_count: int
) -> int:
    for heading_index, _, _ in headings:
        if heading_index > current_heading_index:
            return heading_index
    return line_count


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get_plan_by_path(
    connection: sqlite3.Connection, project_id: int, path: str
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM plans
        WHERE project_id = ? AND path = ?
        """,
        (project_id, path),
    ).fetchone()


def _get_phase_by_number(
    connection: sqlite3.Connection, plan_id: int, phase_number: int
) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT * FROM phases
        WHERE plan_id = ? AND phase_number = ?
        """,
        (plan_id, phase_number),
    ).fetchone()


def _should_defer_manual_merge_reconciliation(
    existing_phase: sqlite3.Row, phase: ParsedPhase
) -> bool:
    return (
        existing_phase["status"] == "BLOCKED"
        and phase.status == "COMPLETE"
        and existing_phase["branch_name"]
        and existing_phase["pr_url"]
        and existing_phase["published_sha"]
    )


def _sync_phase_metadata(
    connection: sqlite3.Connection,
    phase_id: int,
    phase: ParsedPhase,
    log_dir: Path,
    updated_at: str,
    *,
    content_hash: Optional[str] = None,
) -> None:
    if content_hash is None:
        connection.execute(
            """
            UPDATE phases
            SET title = ?, log_dir = ?, updated_at = ?
            WHERE id = ?
            """,
            (phase.title, str(log_dir), updated_at, phase_id),
        )
        return
    connection.execute(
        """
        UPDATE phases
        SET title = ?, content_hash = ?, log_dir = ?, updated_at = ?
        WHERE id = ?
        """,
        (phase.title, content_hash, str(log_dir), updated_at, phase_id),
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    event_type: str,
    message: str,
    plan_id: Optional[int] = None,
    phase_id: Optional[int] = None,
    data: Optional[dict] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            project_id, plan_id, phase_id, event_type, message,
            data_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            plan_id,
            phase_id,
            event_type,
            message,
            None if data is None else json.dumps(data, sort_keys=True),
            utc_now_iso(),
        ),
    )
