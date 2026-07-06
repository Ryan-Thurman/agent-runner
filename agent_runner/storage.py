import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .lock import utc_now_iso


DB_FILENAME = "runner.sqlite"

JOB_TYPES = {"IMPLEMENT", "RUN_CHECKS", "REVIEW", "FIX", "CLOSE_PHASE"}
JOB_STATUSES = {"PENDING", "RUNNING", "SUCCEEDED", "FAILED"}
PHASE_STATUSES = {
    "PENDING",
    "IMPLEMENTING",
    "CHECKING",
    "REVIEWING",
    "FIXING",
    "CLOSING",
    "COMPLETE",
    "BLOCKED",
}

ORPHAN_PHASE_STATUS = {
    "IMPLEMENT": "IMPLEMENTING",
    "RUN_CHECKS": "CHECKING",
    "REVIEW": "REVIEWING",
    "FIX": "FIXING",
    "CLOSE_PHASE": "CLOSING",
}


@dataclass(frozen=True)
class StoragePaths:
    db_path: Path
    logs_dir: Path


def storage_paths(home: Path) -> StoragePaths:
    return StoragePaths(db_path=home / DB_FILENAME, logs_dir=home / "logs")


def connect_db(home: Path) -> sqlite3.Connection:
    paths = storage_paths(home)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(paths.db_path)
    connection.row_factory = sqlite3.Row
    configure_connection(connection)
    ensure_schema(connection)
    return connection


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            repo_path TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, path)
        );

        CREATE TABLE IF NOT EXISTS phases (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
            phase_number INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            retry_count INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT NOT NULL,
            publish_mode TEXT,
            branch_name TEXT,
            pr_url TEXT,
            published_sha TEXT,
            log_dir TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(plan_id, phase_number)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
            phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            trigger TEXT,
            prompt_path TEXT,
            log_path TEXT,
            output_path TEXT,
            error TEXT,
            started_sha TEXT,
            finished_sha TEXT,
            exit_code INTEGER,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(type IN ('IMPLEMENT', 'RUN_CHECKS', 'REVIEW', 'FIX', 'CLOSE_PHASE')),
            CHECK(status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
            CHECK(trigger IS NULL OR trigger IN ('checks', 'review'))
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_phase_id ON jobs(phase_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_project_status ON jobs(project_id, status);

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            plan_id INTEGER REFERENCES plans(id) ON DELETE SET NULL,
            phase_id INTEGER REFERENCES phases(id) ON DELETE SET NULL,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            data_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    connection.commit()


def get_or_create_project(
    connection: sqlite3.Connection, *, slug: str, repo_path: Path
) -> sqlite3.Row:
    resolved_path = str(repo_path.resolve())
    now = utc_now_iso()
    connection.execute(
        """
        INSERT INTO projects (slug, repo_path, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(repo_path) DO UPDATE SET
            slug = excluded.slug,
            updated_at = excluded.updated_at
        """,
        (slug, resolved_path, now, now),
    )
    connection.commit()
    return get_project_by_repo_path(connection, resolved_path)


def get_project_by_repo_path(
    connection: sqlite3.Connection, repo_path: Path | str
) -> sqlite3.Row:
    resolved_path = str(Path(repo_path).resolve())
    row = connection.execute(
        "SELECT * FROM projects WHERE repo_path = ?", (resolved_path,)
    ).fetchone()
    if row is None:
        raise LookupError(f"project is not registered: {resolved_path}")
    return row


def find_project_by_repo_path(
    connection: sqlite3.Connection, repo_path: Path | str
) -> Optional[sqlite3.Row]:
    resolved_path = str(Path(repo_path).resolve())
    return connection.execute(
        "SELECT * FROM projects WHERE repo_path = ?", (resolved_path,)
    ).fetchone()


def create_plan(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    path: str,
    content_hash: Optional[str] = None,
    status: str = "PENDING",
) -> sqlite3.Row:
    now = utc_now_iso()
    cursor = connection.execute(
        """
        INSERT INTO plans (project_id, path, content_hash, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, path, content_hash, status, now, now),
    )
    connection.commit()
    return get_plan(connection, cursor.lastrowid)


def get_plan(connection: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise LookupError(f"plan is not registered: {plan_id}")
    return row


def list_plans_for_project(
    connection: sqlite3.Connection, project_id: int
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM plans WHERE project_id = ? ORDER BY id", (project_id,)
        )
    )


def create_phase(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: int,
    phase_number: int,
    title: str,
    content_hash: str,
    status: str = "PENDING",
    retry_count: int = 0,
    publish_mode: Optional[str] = None,
    branch_name: Optional[str] = None,
    pr_url: Optional[str] = None,
    published_sha: Optional[str] = None,
    log_dir: Optional[Path] = None,
) -> sqlite3.Row:
    if status not in PHASE_STATUSES:
        raise ValueError(f"invalid phase status: {status}")
    now = utc_now_iso()
    cursor = connection.execute(
        """
        INSERT INTO phases (
            project_id, plan_id, phase_number, title, status, retry_count,
            content_hash, publish_mode, branch_name, pr_url, published_sha,
            log_dir, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            plan_id,
            phase_number,
            title,
            status,
            retry_count,
            content_hash,
            publish_mode,
            branch_name,
            pr_url,
            published_sha,
            str(log_dir) if log_dir is not None else None,
            now,
            now,
        ),
    )
    connection.commit()
    return get_phase(connection, cursor.lastrowid)


def get_phase(connection: sqlite3.Connection, phase_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM phases WHERE id = ?", (phase_id,)).fetchone()
    if row is None:
        raise LookupError(f"phase is not registered: {phase_id}")
    return row


def list_phases_for_plan(
    connection: sqlite3.Connection, plan_id: int
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM phases
            WHERE plan_id = ?
            ORDER BY phase_number
            """,
            (plan_id,),
        )
    )


def update_phase_status(
    connection: sqlite3.Connection,
    phase_id: int,
    status: str,
    *,
    increment_retry: bool = False,
) -> sqlite3.Row:
    if status not in PHASE_STATUSES:
        raise ValueError(f"invalid phase status: {status}")
    now = utc_now_iso()
    if increment_retry:
        connection.execute(
            """
            UPDATE phases
            SET status = ?,
                updated_at = ?,
                retry_count = retry_count + 1
            WHERE id = ?
            """,
            (status, now, phase_id),
        )
    else:
        connection.execute(
            """
            UPDATE phases
            SET status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, now, phase_id),
        )
    connection.commit()
    return get_phase(connection, phase_id)


def create_job(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    plan_id: Optional[int],
    phase_id: Optional[int],
    job_type: str,
    status: str = "PENDING",
    trigger: Optional[str] = None,
    prompt_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    error: Optional[str] = None,
    started_sha: Optional[str] = None,
    finished_sha: Optional[str] = None,
    exit_code: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> sqlite3.Row:
    if job_type not in JOB_TYPES:
        raise ValueError(f"invalid job type: {job_type}")
    if status not in JOB_STATUSES:
        raise ValueError(f"invalid job status: {status}")
    now = utc_now_iso()
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            project_id, plan_id, phase_id, type, status, trigger, prompt_path,
            log_path, output_path, error, started_sha, finished_sha, exit_code,
            started_at, finished_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            plan_id,
            phase_id,
            job_type,
            status,
            trigger,
            str(prompt_path) if prompt_path is not None else None,
            str(log_path) if log_path is not None else None,
            str(output_path) if output_path is not None else None,
            error,
            started_sha,
            finished_sha,
            exit_code,
            started_at,
            finished_at,
            now,
            now,
        ),
    )
    connection.commit()
    return get_job(connection, cursor.lastrowid)


def get_job(connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise LookupError(f"job is not registered: {job_id}")
    return row


def record_event(
    connection: sqlite3.Connection,
    *,
    project_id: int,
    event_type: str,
    message: str,
    plan_id: Optional[int] = None,
    phase_id: Optional[int] = None,
    job_id: Optional[int] = None,
    data: Optional[dict[str, Any]] = None,
) -> sqlite3.Row:
    cursor = connection.execute(
        """
        INSERT INTO events (
            project_id, plan_id, phase_id, job_id, event_type, message,
            data_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            plan_id,
            phase_id,
            job_id,
            event_type,
            message,
            json.dumps(data, sort_keys=True) if data is not None else None,
            utc_now_iso(),
        ),
    )
    connection.commit()
    return get_event(connection, cursor.lastrowid)


def get_event(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise LookupError(f"event is not registered: {event_id}")
    return row


def list_recent_events(
    connection: sqlite3.Connection, project_id: int, *, limit: int = 5
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT * FROM events
            WHERE project_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (project_id, limit),
        )
    )


def reap_orphaned_jobs(connection: sqlite3.Connection, project_id: int) -> list[int]:
    running_jobs = list(
        connection.execute(
            """
            SELECT * FROM jobs
            WHERE project_id = ? AND status = 'RUNNING'
            ORDER BY id
            """,
            (project_id,),
        )
    )
    if not running_jobs:
        return []

    now = utc_now_iso()
    reaped_ids: list[int] = []
    try:
        connection.execute("BEGIN")
        for job in running_jobs:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'FAILED',
                    error = 'orphaned',
                    finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, job["id"]),
            )
            if job["phase_id"] is not None:
                phase_status = ORPHAN_PHASE_STATUS[job["type"]]
                connection.execute(
                    """
                    UPDATE phases
                    SET status = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (phase_status, now, job["phase_id"]),
                )
            connection.execute(
                """
                INSERT INTO events (
                    project_id, plan_id, phase_id, job_id, event_type,
                    message, data_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    job["plan_id"],
                    job["phase_id"],
                    job["id"],
                    "job.orphaned",
                    "marked orphaned running job as failed",
                    json.dumps({"jobType": job["type"]}, sort_keys=True),
                    now,
                ),
            )
            reaped_ids.append(job["id"])
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    return reaped_ids


def phase_log_dir(
    logs_dir: Path, *, project_slug: str, plan_path: str, phase_number: int
) -> Path:
    return logs_dir / project_slug / slug_for_path(plan_path) / f"phase-{phase_number}"


def slug_for_path(path: str) -> str:
    slug = "-".join(Path(path).parts)
    slug = "".join(char if char.isalnum() or char in "._-" else "-" for char in slug)
    return slug.strip("-._") or "plan"


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
