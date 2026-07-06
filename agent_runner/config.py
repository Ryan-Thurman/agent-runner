import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError


CONFIG_FILENAME = ".agent-runner.json"

REQUIRED_AGENT_FIELDS = {
    "command": str,
    "promptArgs": list,
    "writeFlags": list,
    "readOnlyFlags": list,
    "outputCapture": str,
}

OUTPUT_CAPTURE_MODES = {"stdout", "last-message-file", "structured-stdout"}


@dataclass(frozen=True)
class AgentProfile:
    name: str
    command: str
    prompt_args: list[str]
    write_flags: list[str]
    read_only_flags: list[str]
    output_capture: str


@dataclass(frozen=True)
class RunnerConfig:
    path: Path
    data: dict[str, Any]
    agents: dict[str, AgentProfile]
    roles: dict[str, str]
    plan_path: str
    checks: list[str]
    max_retries_per_phase: int
    timeout_minutes: int
    auto_commit: bool
    allow_dirty: bool
    warnings: list[str]


def strip_json_comments(text: str) -> str:
    result: list[str] = []
    i = 0
    in_string = False
    escaped = False
    length = len(text)
    while i < length:
        char = text[i]
        next_char = text[i + 1] if i + 1 < length else ""

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            i += 1
            continue

        if char == "/" and next_char == "/":
            i += 2
            while i < length and text[i] not in "\r\n":
                i += 1
            continue

        if char == "/" and next_char == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, length)
            continue

        result.append(char)
        i += 1

    return "".join(result)


def load_config(repo_root: Path) -> RunnerConfig:
    path = repo_root / CONFIG_FILENAME
    if not path.exists():
        raise ConfigError(f"missing {CONFIG_FILENAME} in {repo_root}")

    try:
        data = json.loads(strip_json_comments(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid {CONFIG_FILENAME}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"invalid {CONFIG_FILENAME}: top-level value must be an object")

    return validate_config(data, path)


def validate_config(data: dict[str, Any], path: Path) -> RunnerConfig:
    warnings: list[str] = []
    agents_data = _required_dict(data, "agents")
    roles = _required_dict(data, "roles")
    plan_path = _required_string(data, "planPath")
    checks = _required_string_list(data, "checks")

    if not checks:
        warnings.append("config checks is empty; no project checks will run")

    agents: dict[str, AgentProfile] = {}
    for name, profile in agents_data.items():
        if not isinstance(name, str) or not name:
            raise ConfigError("invalid agents: profile names must be non-empty strings")
        if not isinstance(profile, dict):
            raise ConfigError(f"invalid agents.{name}: profile must be an object")
        agents[name] = _validate_agent_profile(name, profile)

    if not agents:
        raise ConfigError("invalid agents: at least one agent profile is required")

    normalized_roles: dict[str, str] = {}
    for role, profile_name in roles.items():
        if not isinstance(role, str) or not role:
            raise ConfigError("invalid roles: role names must be non-empty strings")
        if not isinstance(profile_name, str) or not profile_name:
            raise ConfigError(f"invalid roles.{role}: must reference an agent profile name")
        if profile_name not in agents:
            raise ConfigError(
                f"invalid roles.{role}: unknown agent profile {profile_name!r}"
            )
        normalized_roles[role] = profile_name

    for required_role in ("coder", "reviewer"):
        if required_role not in normalized_roles:
            raise ConfigError(f"invalid roles: missing required role {required_role!r}")

    max_retries = _required_int(data, "maxRetriesPerPhase", minimum=0)
    timeout_minutes = _required_int(data, "timeoutMinutes", minimum=1)
    auto_commit = _required_bool(data, "autoCommit")
    allow_dirty = _required_bool(data, "allowDirty")

    return RunnerConfig(
        path=path,
        data=data,
        agents=agents,
        roles=normalized_roles,
        plan_path=plan_path,
        checks=checks,
        max_retries_per_phase=max_retries,
        timeout_minutes=timeout_minutes,
        auto_commit=auto_commit,
        allow_dirty=allow_dirty,
        warnings=warnings,
    )


def project_slug(repo_root: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo_root.name.strip()).strip("-._")
    return slug or "project"


def _validate_agent_profile(name: str, profile: dict[str, Any]) -> AgentProfile:
    for field, expected_type in REQUIRED_AGENT_FIELDS.items():
        if field not in profile:
            raise ConfigError(f"invalid agents.{name}: missing required field {field!r}")
        if not isinstance(profile[field], expected_type):
            raise ConfigError(
                f"invalid agents.{name}.{field}: expected {expected_type.__name__}"
            )

    command = profile["command"]
    if not command:
        raise ConfigError(f"invalid agents.{name}.command: must not be empty")

    prompt_args = _string_list(profile["promptArgs"], f"agents.{name}.promptArgs")
    write_flags = _string_list(profile["writeFlags"], f"agents.{name}.writeFlags")
    read_only_flags = _string_list(
        profile["readOnlyFlags"], f"agents.{name}.readOnlyFlags"
    )
    output_capture = profile["outputCapture"]
    if output_capture not in OUTPUT_CAPTURE_MODES:
        allowed = ", ".join(sorted(OUTPUT_CAPTURE_MODES))
        raise ConfigError(
            f"invalid agents.{name}.outputCapture: expected one of {allowed}"
        )

    return AgentProfile(
        name=name,
        command=command,
        prompt_args=prompt_args,
        write_flags=write_flags,
        read_only_flags=read_only_flags,
        output_capture=output_capture,
    )


def _required_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"invalid config: missing or invalid object field {key!r}")
    return value


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"invalid config: missing or invalid string field {key!r}")
    return value


def _required_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ConfigError(f"invalid config: missing or invalid list field {key!r}")
    return _string_list(value, key)


def _string_list(value: list[Any], field_name: str) -> list[str]:
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"invalid {field_name}: expected a list of strings")
    return list(value)


def _required_int(data: dict[str, Any], key: str, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(
            f"invalid config: field {key!r} must be an integer >= {minimum}"
        )
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"invalid config: field {key!r} must be a boolean")
    return value


SAMPLE_CONFIG = """{
  // Path to the markdown plan parsed by later phases.
  "planPath": "docs/plan.md",

  // Project checks run after implementation/fix jobs. Empty is allowed but warned.
  "checks": [
    "python3 -m compileall -q .",
    "python3 -m unittest discover -s tests -v"
  ],

  // Agent profiles are vendor-specific; roles below are vendor-swappable.
  "agents": {
    "claude": {
      "command": "claude",
      "promptArgs": ["-p"],
      "writeFlags": ["--permission-mode", "acceptEdits"],
      "readOnlyFlags": ["--disallowedTools", "Edit,Write,NotebookEdit"],
      "outputCapture": "stdout"
    },
    "codex": {
      "command": "codex",
      "promptArgs": ["exec"],
      "writeFlags": ["--sandbox", "workspace-write"],
      "readOnlyFlags": ["--sandbox", "read-only"],
      "outputCapture": "last-message-file"
    }
  },

  "roles": {
    "coder": "claude",
    "reviewer": "codex"
  },

  "maxRetriesPerPhase": 3,
  "timeoutMinutes": 45,
  "autoCommit": true,
  "allowDirty": false
}
"""
