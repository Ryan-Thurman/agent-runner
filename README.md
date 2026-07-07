# agent-runner

`agent-runner` is a local Python 3 CLI, stdlib only, that drives a markdown
implementation plan through disposable coding and review agents.

```text
IMPLEMENT -> RUN_CHECKS -> REVIEW -> FIX/RUN_CHECKS/REVIEW -> CLOSE_PHASE
```

The runner stores durable state in SQLite, writes prompts and logs under
`~/.agent-runner/logs/`, and re-derives the next job from phase status after a
restart. Agent profiles are vendor-neutral: config maps `roles.coder` and
`roles.reviewer` to profiles such as Codex or Claude, and the runner applies
write flags to coder/closer jobs and read-only flags to reviewer jobs.

## Install

Run from this checkout:

```sh
python3 -m agent_runner --version
python3 -m agent_runner status
python3 -m agent_runner run
```

The executable shim works too:

```sh
./agent-runner status
./agent-runner run
```

For a global command that works from any target git worktree, symlink the
short shim onto your `PATH`:

```sh
ln -s /path/to/agent-runner/autorun ~/bin/autorun
autorun --version
```

The long-form shim can be installed the same way if you prefer the old command:

```sh
ln -s /path/to/agent-runner/agent-runner ~/bin/agent-runner
```

Both shims resolve their real checkout path before importing the package, so a
symlinked `autorun` or `agent-runner` works from inside any repo.

## Configure

Initialize a target repo:

```sh
autorun init
```

From the runner checkout, `python3 -m agent_runner init` is equivalent.

Minimum `.agent-runner.json` shape:

```json
{
  "planPath": "docs/plan.md",
  "checks": ["python3 -m unittest discover -s tests"],
  "agents": {
    "codex": {
      "command": "codex",
      "promptArgs": ["exec"],
      "writeFlags": ["--sandbox", "workspace-write"],
      "readOnlyFlags": ["--sandbox", "read-only"],
      "outputCapture": "last-message-file"
    },
    "antigravity": {
      "command": "agy",
      "promptArgs": ["-p", "--print-timeout", "40m"],
      "writeFlags": ["--dangerously-skip-permissions"],
      "readOnlyFlags": ["--sandbox"],
      "outputCapture": "stdout"
    },
    "claude-opus": {
      "command": "claude",
      "promptArgs": ["--model", "claude-opus-4-8", "-p"],
      "writeFlags": ["--permission-mode", "acceptEdits"],
      "readOnlyFlags": ["--disallowedTools", "Edit,Write,NotebookEdit"],
      "promptPrefix": "",
      "outputCapture": "stdout"
    },
    "claude-sonnet": {
      "command": "claude",
      "promptArgs": ["--model", "claude-sonnet-5", "-p"],
      "writeFlags": ["--permission-mode", "acceptEdits"],
      "readOnlyFlags": ["--disallowedTools", "Edit,Write,NotebookEdit"],
      "promptPrefix": "",
      "outputCapture": "stdout"
    }
  },
  "roles": {
    "coder": "codex",
    "reviewer": "claude-opus",
    "fixer": "claude-opus"
  },
  "roleFallbacks": { "reviewer": ["antigravity"], "coder": ["claude-sonnet"] },
  "reviewTriage": { "simple": "claude-sonnet", "complex": "claude-opus" },
  "maxRetriesPerPhase": 3,
  "autoFixAttempts": 2,
  "timeoutMinutes": 45,
  "autoCommit": true,
  "allowDirty": false,
  "baseBranch": "main",
  "mergeOnClose": true,
  "mergeStrategy": "squash"
}
```

Plans use `## Phase <number>: <title>` headings and a runner-owned
`Status: <STATE>` line. The status and adjacent `Evidence:` line are excluded
from phase body hashes so closeout can update the plan without superseding it.
Markdown before the first phase heading is shared plan-level context: the runner
includes a deterministic 4000-character bounded copy in IMPLEMENT, REVIEW, FIX,
and CLOSE_PHASE prompts as data that cannot override runner safety or scope
rules.

## Commands

- `--version`: print the installed `agent-runner` package version and exit.
- `run`: register or resume the plan, reap orphaned `RUNNING` jobs, and run the
  next job derived from SQLite phase status.
- `status`: print human status to stderr and JSON state to stdout.
- `pause`: mark the project `PAUSED`; active jobs finish, then the loop stops at
  the next job boundary.
- `resume`: mark the project `ACTIVE` so the next `run` continues.
- `unblock [--phase N] [--to STATUS]`: reset a `BLOCKED` phase to the status it
  had when it blocked so `run` can retry it.
- `logs [-n N]`: print the latest phase log directory and tail the newest log.
- `reset-lock`: clear a stale project lock when no runner is active.

## Safety Rules

The runner does not auto-merge, force-push, delete branches, delete files
outside the repo, modify global git config, or interrupt a running agent for
pause. With `allowDirty=false`, a dirty worktree blocks before a new IMPLEMENT
job. With `autoCommit=true`, review requires a clean pushed PR for the current
branch; with `autoCommit=false`, the runner stages local changes so new files
are visible in `git diff --staged`. With `autoFixAttempts>0`, blocked phases can
launch the configured one-shot `fixer` profile to repair the blocker and resume
the same `run`; fixer prompts forbid nested `autorun`/`agent-runner`
invocations and require publishing fixes when `autoCommit=true`.

## Dogfood Transcript

Phase 8 was dogfooded against a temporary two-phase toy repo using the real
configured `codex` profile for coder, reviewer, and closer. The toy plan asked
for `alpha.txt` in phase 1 and `beta.txt` in phase 2, with
`python3 checks/check_artifacts.py` as the stub check.

```text
$ AGENT_RUNNER_HOME=<tmp>/home PYTHONPATH=/Users/mac/workspaces/agent-runner \
  python3 -m agent_runner run
[agent-runner] acquired lock for toy-repo-3831be32867c
[agent-runner] registered plan docs/plan.md with 2 phase(s)
[agent-runner] starting IMPLEMENT job 1 (role=coder, profile=codex)
[agent-runner] starting RUN_CHECKS job 2 (role=checks, profile=shell)
[agent-runner] starting REVIEW job 3 (role=reviewer, profile=codex)
[agent-runner] starting CLOSE_PHASE job 4 (role=closer, profile=codex)
[agent-runner] phase 1 complete; next phase 2 is PENDING

$ git commit -m 'Complete toy phase 1'
[main a15d5d9] Complete toy phase 1

$ AGENT_RUNNER_HOME=<tmp>/home PYTHONPATH=/Users/mac/workspaces/agent-runner \
  python3 -m agent_runner run
[agent-runner] acquired lock for toy-repo-3831be32867c
[agent-runner] resumed plan docs/plan.md with 2 phase(s)
[agent-runner] starting IMPLEMENT job 5 (role=coder, profile=codex)
[agent-runner] starting RUN_CHECKS job 6 (role=checks, profile=shell)
[agent-runner] starting REVIEW job 7 (role=reviewer, profile=codex)
[agent-runner] starting CLOSE_PHASE job 8 (role=closer, profile=codex)
[agent-runner] phase 2 complete; plan complete

$ python3 -m agent_runner status
[agent-runner] plan: docs/plan.md (COMPLETE)
[agent-runner]   phase 1: COMPLETE retries=0
[agent-runner]   phase 2: COMPLETE retries=0
```

`logs -n 20` printed the latest phase directory and tailed
`phase-2/close_phase.log`, including the closer’s summary that `docs/plan.md`
was set to `Status: COMPLETE`, the phase handoff was written, and
`python3 checks/check_artifacts.py` passed.

## Start Here

- `docs/design.md` is the design source of truth.
- `docs/plan-roadmap.md` is the current executable plan for upcoming work.
- `docs/roadmap.md` summarizes completed work and planned follow-ups.
- `docs/archive/` contains completed historical plans.
- `docs/usage.md` has the detailed command and recovery guide.
- `.agent-runner.json` is this repo's dogfood config.
