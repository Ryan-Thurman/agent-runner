import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Callable, Optional, Union

from .errors import LockError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProjectLock:
    def __init__(self, locks_dir: Path, project_slug: str, repo_root: Path):
        self.locks_dir = locks_dir
        self.project_slug = project_slug
        self.repo_root = repo_root
        self.path = locks_dir / f"{project_slug}.lock"
        self.acquired = False

    def acquire(self) -> None:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        existing = self._read_existing()
        if existing:
            pid = existing.get("pid")
            repo_path = existing.get("repoPath", "unknown")
            started_at = existing.get("startedAt", "unknown")
            if isinstance(pid, int) and pid_is_alive(pid):
                if not self._matches_repo_path(repo_path):
                    raise LockError(
                        "project lock collision "
                        f"(lock {self.path}, repo {repo_path}, pid {pid})"
                    )
                raise LockError(
                    "project is already locked "
                    f"(pid {pid}, repo {repo_path}, started {started_at})"
                )
            self.path.unlink(missing_ok=True)

        payload = {
            "pid": os.getpid(),
            "repoPath": str(self.repo_root),
            "startedAt": utc_now_iso(),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self.path, flags, 0o644)
        except FileExistsError as exc:
            raise LockError(f"project lock already exists: {self.path}") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            json.dump(payload, lock_file, indent=2)
            lock_file.write("\n")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        existing = self._read_existing()
        if existing and existing.get("pid") == os.getpid():
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> "ProjectLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def _read_existing(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            with self.path.open(encoding="utf-8") as lock_file:
                payload = json.load(lock_file)
        except (json.JSONDecodeError, OSError):
            return {"pid": None, "repoPath": "unknown", "startedAt": "unknown"}
        if not isinstance(payload, dict):
            return {"pid": None, "repoPath": "unknown", "startedAt": "unknown"}
        return payload

    def _matches_repo_path(self, repo_path: object) -> bool:
        if not isinstance(repo_path, str) or not repo_path:
            return False
        try:
            return Path(repo_path).resolve() == self.repo_root.resolve()
        except OSError:
            return False


def reset_project_lock(locks_dir: Path, project_slug: str) -> Path:
    path = locks_dir / f"{project_slug}.lock"
    path.unlink(missing_ok=True)
    return path


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SignalLockRelease:
    def __init__(self, lock: ProjectLock):
        self.lock = lock
        self.previous_int: Optional[
            Union[Callable[[int, Optional[FrameType]], None], int]
        ] = None

    def __enter__(self) -> "SignalLockRelease":
        self.previous_int = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_sigint)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.previous_int is not None:
            signal.signal(signal.SIGINT, self.previous_int)

    def _handle_sigint(self, signum: int, frame: Optional[FrameType]) -> None:
        self.lock.release()
        raise KeyboardInterrupt
