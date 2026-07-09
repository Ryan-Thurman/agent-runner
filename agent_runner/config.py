import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError


CONFIG_FILENAME = ".agent-runner.json"
DEFAULT_CHECKS = [
    "python3 -m compileall -q .",
    "python3 -m unittest discover -s tests",
]
NODE_CHECKS = ["npm test"]
PLACEHOLDER_CHECKS = [
    "sh -c 'echo \"agent-runner: replace the placeholder checks entry in "
    ".agent-runner.json with your project'\\''s real check command\" >&2; exit 1'"
]

REQUIRED_AGENT_FIELDS = {
    "command": str,
    "promptArgs": list,
    "writeFlags": list,
    "readOnlyFlags": list,
    "outputCapture": str,
}

OUTPUT_CAPTURE_MODES = {"stdout", "last-message-file", "structured-stdout"}

MERGE_STRATEGIES = {"merge", "squash", "rebase"}


@dataclass(frozen=True)
class AgentProfile:
    name: str
    command: str
    prompt_args: list[str]
    write_flags: list[str]
    read_only_flags: list[str]
    output_capture: str
    prompt_prefix: str = ""


@dataclass(frozen=True)
class ReviewTriageConfig:
    simple: str
    complex: str


@dataclass(frozen=True)
class RunnerConfig:
    path: Path
    data: dict[str, Any]
    agents: dict[str, AgentProfile]
    roles: dict[str, str]
    role_fallbacks: dict[str, list[str]]
    review_triage: ReviewTriageConfig | None
    plan_path: str
    plan_verify: list[str]
    checks: list[str]
    max_retries_per_phase: int
    auto_fix_attempts: int
    timeout_minutes: int
    auto_commit: bool
    allow_dirty: bool
    base_branch: str
    merge_on_close: bool
    merge_strategy: str
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
    plan_verify = _optional_string_list(data, "planVerify", default=[])
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

    role_fallbacks = _validate_role_fallbacks(
        data, agents=agents, roles=normalized_roles, warnings=warnings
    )
    review_triage = _validate_review_triage(data, agents=agents)

    max_retries = _required_int(data, "maxRetriesPerPhase", minimum=0)
    auto_fix_attempts = _optional_int(data, "autoFixAttempts", default=0, minimum=0)
    if auto_fix_attempts > 0 and "fixer" not in normalized_roles:
        raise ConfigError(
            "invalid config: autoFixAttempts > 0 requires roles.fixer"
        )
    timeout_minutes = _required_int(data, "timeoutMinutes", minimum=1)
    auto_commit = _required_bool(data, "autoCommit")
    allow_dirty = _required_bool(data, "allowDirty")
    base_branch = _optional_string(data, "baseBranch", default="main")
    merge_on_close = _optional_bool(data, "mergeOnClose", default=False)
    merge_strategy = _optional_string(data, "mergeStrategy", default="squash")
    if merge_strategy not in MERGE_STRATEGIES:
        allowed = ", ".join(sorted(MERGE_STRATEGIES))
        raise ConfigError(f"invalid mergeStrategy: expected one of {allowed}")
    if merge_on_close and not auto_commit:
        raise ConfigError(
            "invalid config: mergeOnClose requires autoCommit=true (the runner "
            "merges the reviewed phase PR, which only exists in the PR flow)"
        )

    return RunnerConfig(
        path=path,
        data=data,
        agents=agents,
        roles=normalized_roles,
        role_fallbacks=role_fallbacks,
        review_triage=review_triage,
        plan_path=plan_path,
        plan_verify=plan_verify,
        checks=checks,
        max_retries_per_phase=max_retries,
        auto_fix_attempts=auto_fix_attempts,
        timeout_minutes=timeout_minutes,
        auto_commit=auto_commit,
        allow_dirty=allow_dirty,
        base_branch=base_branch,
        merge_on_close=merge_on_close,
        merge_strategy=merge_strategy,
        warnings=warnings,
    )


def project_slug(repo_root: Path) -> str:
    name_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo_root.name.strip()).strip("-._")
    path_hash = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[
        :12
    ]
    return f"{name_slug or 'project'}-{path_hash}"


def detect_default_checks(repo_root: Path) -> list[str]:
    if (repo_root / "pyproject.toml").exists() or (repo_root / "setup.py").exists():
        return list(DEFAULT_CHECKS)
    if (repo_root / "tests").is_dir() and _repo_has_python_files(repo_root):
        return list(DEFAULT_CHECKS)
    if (repo_root / "package.json").exists():
        return list(NODE_CHECKS)
    return list(PLACEHOLDER_CHECKS)


def sample_config_for_checks(checks: list[str]) -> str:
    return SAMPLE_CONFIG_TEMPLATE.format(
        checks=_format_checks(checks),
        claude_allowed_tools=claude_write_allowed_tools(checks),
        claude_read_only_allowed_tools=claude_read_only_allowed_tools(),
    )


def claude_write_allowed_tools(checks: list[str]) -> str:
    """Bash allowlist for headless claude write roles: git/gh plus the leading
    command of each configured check, as =-joined --allowedTools rules."""
    commands = ["git", "gh"]
    for check in checks:
        tokens = check.split()
        if tokens and tokens[0] not in commands:
            commands.append(tokens[0])
    return ",".join(f"Bash({command}:*)" for command in commands)


def claude_read_only_allowed_tools() -> str:
    """Bash allowlist for headless claude read-only review roles."""
    commands = [
        "gh pr diff",
        "gh pr view",
        "gh pr checks",
        "gh api",
        "git diff",
        "git log",
        "git show",
    ]
    return ",".join(f"Bash({command}:*)" for command in commands)


def _repo_has_python_files(repo_root: Path) -> bool:
    for path in repo_root.rglob("*.py"):
        if ".git" not in path.relative_to(repo_root).parts:
            return True
    return False


def _format_checks(checks: list[str]) -> str:
    encoded = json.dumps(checks, indent=4)
    return "\n".join(f"  {line}" for line in encoded.splitlines())


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
    prompt_prefix = profile.get("promptPrefix", "")
    if not isinstance(prompt_prefix, str):
        raise ConfigError(f"invalid agents.{name}.promptPrefix: expected string")

    return AgentProfile(
        name=name,
        command=command,
        prompt_args=prompt_args,
        write_flags=write_flags,
        read_only_flags=read_only_flags,
        output_capture=output_capture,
        prompt_prefix=prompt_prefix,
    )


def _validate_role_fallbacks(
    data: dict[str, Any],
    *,
    agents: dict[str, AgentProfile],
    roles: dict[str, str],
    warnings: list[str],
) -> dict[str, list[str]]:
    value = data.get("roleFallbacks")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("invalid roleFallbacks: expected an object")

    role_fallbacks: dict[str, list[str]] = {}
    for role, profile_names in value.items():
        if not isinstance(role, str) or role not in roles:
            raise ConfigError(
                f"invalid roleFallbacks.{role}: must reference a configured role"
            )
        if not isinstance(profile_names, list):
            raise ConfigError(f"invalid roleFallbacks.{role}: expected a list")
        names = _string_list(profile_names, f"roleFallbacks.{role}")
        for name in names:
            if name not in agents:
                raise ConfigError(
                    f"invalid roleFallbacks.{role}: unknown agent profile {name!r}"
                )
        if role not in {"coder", "reviewer", "planner"} and names:
            warnings.append(
                f"roleFallbacks.{role} is configured but only the coder, planner, "
                "and reviewer roles fall back on quota failures today"
            )
        role_fallbacks[role] = names
    return role_fallbacks


def _validate_review_triage(
    data: dict[str, Any], *, agents: dict[str, AgentProfile]
) -> ReviewTriageConfig | None:
    value = data.get("reviewTriage")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("invalid reviewTriage: expected an object")

    simple = value.get("simple")
    complex_profile = value.get("complex")
    if not isinstance(simple, str) or not simple:
        raise ConfigError("invalid reviewTriage.simple: expected an agent profile name")
    if not isinstance(complex_profile, str) or not complex_profile:
        raise ConfigError("invalid reviewTriage.complex: expected an agent profile name")
    for tier, name in (("simple", simple), ("complex", complex_profile)):
        if name not in agents:
            raise ConfigError(
                f"invalid reviewTriage.{tier}: unknown agent profile {name!r}"
            )
    return ReviewTriageConfig(simple=simple, complex=complex_profile)


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


def _optional_string_list(
    data: dict[str, Any], key: str, *, default: list[str]
) -> list[str]:
    value = data.get(key, default)
    if not isinstance(value, list):
        raise ConfigError(f"invalid config: field {key!r} must be a list")
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


def _optional_int(
    data: dict[str, Any], key: str, *, default: int, minimum: int
) -> int:
    value = data.get(key, default)
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


def _optional_string(data: dict[str, Any], key: str, *, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"invalid config: field {key!r} must be a non-empty string")
    return value


def _optional_bool(data: dict[str, Any], key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"invalid config: field {key!r} must be a boolean")
    return value


SAMPLE_CONFIG_TEMPLATE = """{{
  // Path to the markdown plan parsed by later phases.
  "planPath": "docs/plan.md",

  // Optional commands for `agent-runner plan-validate`; receive plan metadata
  // in AGENT_RUNNER_PLAN_* environment variables.
  "planVerify": [],

  // Project checks detected by init; review these before the first run.
  "checks": {checks},

  // Agent profiles are vendor-specific; roles below are vendor-swappable.
  "agents": {{
    // codex workspace-write disables network by default, which breaks
    // dependency fetches (cargo/pnpm) and pushes; the -c override re-enables
    // network while keeping the filesystem sandbox.
    "codex": {{
      "command": "codex",
      "promptArgs": ["exec"],
      "writeFlags": ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"],
      "readOnlyFlags": ["--sandbox", "read-only"],
      "outputCapture": "last-message-file"
    }},
    "antigravity": {{
      "command": "agy",
      "promptArgs": ["--print-timeout", "40m"],
      "writeFlags": ["--dangerously-skip-permissions", "-p"],
      "readOnlyFlags": ["--sandbox", "-p"],
      "outputCapture": "stdout"
    }},
    // claude flag rules: --allowedTools/--disallowedTools are VARIADIC — the
    // space-separated form ("--disallowedTools", "Edit,Write") swallows the
    // positional prompt the runner appends last. Always use the =-joined form.
    // Write roles run headless (-p): unmatched permission prompts are DENIED,
    // not asked, so writers pre-allow the Bash commands they need (git/gh and
    // the configured checks) alongside acceptEdits. Widen the allowlist if a
    // fixer job dies on a denied command; --dangerously-skip-permissions works
    // as a last resort but removes all gating on autonomous write jobs.
    // Reviewers stay read-only via disallowedTools and can run narrowly
    // allowlisted read-only gh/git commands to inspect PRs.
    "claude-opus": {{
      "command": "claude",
      "promptArgs": ["--model", "claude-opus-4-8", "-p"],
      "writeFlags": ["--permission-mode=acceptEdits", "--allowedTools={claude_allowed_tools}"],
      "readOnlyFlags": ["--allowedTools={claude_read_only_allowed_tools}", "--disallowedTools=Edit,Write,NotebookEdit"],
      "promptPrefix": "",
      "outputCapture": "stdout"
    }},
    "claude-sonnet": {{
      "command": "claude",
      "promptArgs": ["--model", "claude-sonnet-5", "-p"],
      "writeFlags": ["--permission-mode=acceptEdits", "--allowedTools={claude_allowed_tools}"],
      "readOnlyFlags": ["--allowedTools={claude_read_only_allowed_tools}", "--disallowedTools=Edit,Write,NotebookEdit"],
      "promptPrefix": "",
      "outputCapture": "stdout"
    }}
  }},

  "roles": {{
    "coder": "codex",
    // Reviews are pinned to Opus/Sonnet deliberately; do not use the claude CLI default.
    "reviewer": "claude-opus",
    "fixer": "claude-opus"
  }},

  // When a role's agent fails on a quota/rate limit, the runner retries coder
  // IMPLEMENT/FIX, planner ROADMAP_PLAN, and reviewer REVIEW jobs with these
  // profiles in order.
  "roleFallbacks": {{ "reviewer": ["antigravity"], "coder": ["claude-sonnet"] }},

  // Route simple reviews to Sonnet and behavioral reviews to Opus; both models
  // are explicitly pinned in the profiles above.
  "reviewTriage": {{ "simple": "claude-sonnet", "complex": "claude-opus" }},

  "maxRetriesPerPhase": 3,
  // If a phase blocks, run up to this many one-shot fixer jobs in the same run.
  "autoFixAttempts": 2,
  "timeoutMinutes": 45,
  "autoCommit": true,
  "allowDirty": false,

  // Branch the runner treats as the integration base for phase branches.
  "baseBranch": "main",

  // With mergeOnClose=true (requires autoCommit), the runner merges the
  // reviewed phase PR after CLOSE_PHASE, then starts the next phase on a new
  // branch cut from the latest origin/<baseBranch>. Set false to stop after
  // each phase and wait for a human to merge.
  "mergeOnClose": true,
  "mergeStrategy": "squash"
}}
"""


SAMPLE_CONFIG = sample_config_for_checks(DEFAULT_CHECKS)
