class AgentRunnerError(Exception):
    """Base class for user-facing CLI errors."""


class ConfigError(AgentRunnerError):
    """Raised when .agent-runner.json is missing or invalid."""


class GitRepoError(AgentRunnerError):
    """Raised when a command requires a git repository."""


class LockError(AgentRunnerError):
    """Raised when the per-project runner lock cannot be acquired."""


class PlanError(AgentRunnerError):
    """Raised when a project plan cannot be parsed or safely registered."""
