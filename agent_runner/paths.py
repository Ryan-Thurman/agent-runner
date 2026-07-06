import os
from pathlib import Path
from typing import Optional


def runner_home() -> Path:
    return Path(os.environ.get("AGENT_RUNNER_HOME", "~/.agent-runner")).expanduser()


def ensure_runner_layout(home: Optional[Path] = None) -> Path:
    root = home or runner_home()
    for child in ("locks", "logs"):
        (root / child).mkdir(parents=True, exist_ok=True)
    return root
