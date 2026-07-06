# Using agent-runner

This guide describes the runner as it exists after Phase 5. It is ready for
dogfooding the start of the loop: plan registration, IMPLEMENT, staging,
RUN_CHECKS, and checks-triggered FIX queueing. REVIEW, FIX execution,
CLOSE_PHASE, pause/resume, and rich log tailing are still future phases.

## Current Loop

`agent-runner run` currently handles these phase statuses:

- `PENDING` or `IMPLEMENTING`: run the configured coder profile with an
  IMPLEMENT prompt for the next phase.
- Successful IMPLEMENT: stage implementation changes and run configured checks.
- Passing checks: mark the phase `REVIEWING` and stop. Manual review starts
  here until Phase 6 lands.
- Failing checks: mark the phase `FIXING`, create a pending `FIX` job with
  `trigger="checks"`, write the check output into the fix prompt, and stop.
- `CHECKING`: resume by running checks without running IMPLEMENT again.
- `FIXING`: stop with a message that Phase 6 owns fix execution.
- `REVIEWING` or `CLOSING`: stop with a message that later phases own the next
  step.
- `BLOCKED`: exit non-zero.

The full intended loop is still:

```text
IMPLEMENT -> RUN_CHECKS -> REVIEW -> FIX/RUN_CHECKS/REVIEW -> CLOSE_PHASE
```

Only the `IMPLEMENT -> RUN_CHECKS` leg is automated today.

## Installation

From this repository, run the CLI directly:

```sh
python3 -m agent_runner status
python3 -m agent_runner run
```

The executable shim also works from this checkout:

```sh
./agent-runner status
./agent-runner run
```

For another local repo, put this checkout on `PYTHONPATH`, or install/wrap the
`agent-runner` shim in your shell path. The CLI must be run from inside the git
worktree it should operate on.

Runner state lives outside the repo by default:

```text
~/.agent-runner/
  runner.sqlite
  locks/
  logs/
```

For isolated tests, set `AGENT_RUNNER_HOME`:

```sh
AGENT_RUNNER_HOME="$(mktemp -d)" python3 -m agent_runner status
```

## Project Setup

Run this from the target repo:

```sh
python3 -m agent_runner init
```

That creates `.agent-runner.json` if it does not already exist. Edit it before
running the loop.

Minimum config shape:

```json
{
  "planPath": "docs/plan.md",
  "checks": [
    "python3 -m compileall -q .",
    "python3 -m unittest discover -s tests -v"
  ],
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
```

Current notes:

- `roles.coder` is used now. `roles.reviewer` is validated now and used in
  Phase 6.
- `checks` run as shell commands from the repo root, in order. The first failure
  stops the check job.
- `timeoutMinutes` applies per agent/check process.
- `allowDirty=false` is the safest dogfood setting. A dirty worktree blocks
  before an IMPLEMENT job starts.
- `allowDirty=true` warns and continues; after IMPLEMENT, the runner stages only
  paths that were not already dirty before the job.

## Plan Format

The plan file is markdown. Phases are discovered from headings like:

```md
## Phase 6: REVIEW and FIX convergence loop
Status: PENDING

Build the reviewer leg...

Acceptance Criteria:
- Reviewer PASS advances to CLOSE_PHASE.
```

Rules:

- Use `## Phase <number>: <title>` headings.
- Add `Status: <STATE>` directly under the phase heading.
- If the status line is missing, the runner treats the phase as `PENDING`.
- The status line is excluded from the phase content hash, so status write-back
  will not count as a plan body change.
- Duplicate phase numbers and invalid status values are rejected.

Useful statuses while dogfooding:

- `PENDING`: ready for the runner to start.
- `REVIEWING`: implementation and checks passed; do manual review.
- `FIXING`: checks failed; manual fix is needed until Phase 6 lands.
- `BLOCKED`: implementation failed or the phase needs human intervention.
- `COMPLETE`: done; the Phase 5 loop skips it.

## Running a Phase

Before running:

```sh
git status --short
```

If `allowDirty=false`, this must be clean. Then run:

```sh
python3 -m agent_runner run
```

Expected Phase 5 success flow:

```text
[agent-runner] acquired lock for <project-slug>
[agent-runner] registered/resumed plan docs/plan.md with N phase(s)
[agent-runner] phase <n> checks passed; moved to REVIEWING
```

After that:

```sh
git diff --staged
python3 -m agent_runner status
```

The staged diff is the implementation output that should be manually reviewed
until Phase 6 is implemented.

If checks fail, inspect:

```sh
python3 -m agent_runner status
python3 -m agent_runner logs
```

Then open the phase log directory under `~/.agent-runner/logs/.../phase-N/`.
The generated `fix-checks-prompt.md` contains the check failure context for the
future FIX job.

## Manual Handoff Until Phase 6-8

When a phase reaches `REVIEWING`, the runner has stopped at the current
automation boundary. For dogfooding now:

1. Review `git diff --staged`.
2. Run any extra checks you need.
3. If changes are good, commit manually.
4. Update the plan status manually, usually from `REVIEWING` to `COMPLETE`.
5. Set the next phase to `PENDING`.
6. Run `python3 -m agent_runner run` again.

When a phase reaches `FIXING`, Phase 6 is not available yet. Use the generated
fix prompt and check log as context, fix manually, stage the fix, rerun checks,
and update the plan status when ready.

When a phase reaches `BLOCKED`, use `python3 -m agent_runner status` and the
latest events to see why. IMPLEMENT failures are recorded as events and in the
job log.

## Logs and State

`status` prints human-readable status to stderr and JSON to stdout:

```sh
python3 -m agent_runner status
```

`logs` currently prints the project log root:

```sh
python3 -m agent_runner logs
```

Phase logs are under:

```text
~/.agent-runner/logs/<project-slug>/<plan-slug>/phase-<number>/
```

Common files:

- `implement-prompt.md`: prompt sent to the coder.
- `implement.log`: streamed stdout/stderr from the coder process.
- `implement-output.txt` or similar: captured agent output, depending on the
  profile capture mode.
- `checks.log`: combined check command output.
- `fix-checks-prompt.md`: generated context when checks fail.

The SQLite DB is at `~/.agent-runner/runner.sqlite`.

## Locks and Recovery

Only one runner process can operate on a project at a time. The lock file stores
the runner PID, repo path, and start time. If a live lock is stuck and you know
no runner is active, clear it:

```sh
python3 -m agent_runner reset-lock
```

On startup, the runner reaps orphaned `RUNNING` jobs left by a killed process,
marks them failed, and resets the phase to the corresponding in-progress state.
For an interrupted IMPLEMENT, rerunning skips the initial dirty gate so leftover
agent changes do not block crash recovery.

## Plan Changes

On every `run`, the plan is parsed and compared with the registered phase
hashes.

- Editing a `PENDING` phase body updates that phase hash.
- Editing an in-progress, complete, or blocked phase body blocks the run.
- To accept a protected plan body change intentionally:

```sh
python3 -m agent_runner run --accept-plan-change
```

Use this sparingly while dogfooding; it changes what the runner considers the
canonical phase body.

## Safety Rules

The runner currently does not auto-merge, force-push, delete branches, delete
files outside the repo, modify global git config, or interrupt running agent
processes.

Before a normal IMPLEMENT job starts, the default dirty gate requires a clean
worktree. After a successful IMPLEMENT, the runner stages the implementation so
new files appear in `git diff --staged`.

Reviewer and closer jobs are not automated yet, so commits and PRs are still
manual after `REVIEWING`.
