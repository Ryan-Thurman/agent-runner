import subprocess
from pathlib import Path
from typing import Optional

from .errors import GitRepoError


def find_git_root(cwd: Optional[Path] = None) -> Path:
    start = cwd or Path.cwd()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise GitRepoError("not inside a git repository; run from a project worktree")
    return Path(result.stdout.strip()).resolve()
