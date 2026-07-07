# Using agent-runner

This guide describes the runner as it exists after Phase 6. It is ready for
dogfooding the implementation, check, review, and retry-limited fix loop.
CLOSE_PHASE, pause/resume, and rich log tailing are still future phases.

## Current Loop

`agent-runner run` currently handles these phase statuses:

- `PENDING` or `IMPLEMENTING`: run the configured coder profile with an
  IMPLEMENT prompt for the next phase.
- Successful IMPLEMENT: stage implementation changes and run configured checks.
- Passing checks with `autoCommit=true`: verify the coder committed, pushed,
  and opened a PR for the current branch, record the PR metadata, then run the
  configured reviewer profile against the published PR diff plus phase content
  and check output.
- Passing checks with `autoCommit=false`: run the configured reviewer profile
  with phase content, `git diff --staged`, and check output.
- Failing checks: run a `FIX` job with `trigger="checks"` if retries remain,
  then rerun checks.
- `CHECKING`: resume by running checks without running IMPLEMENT again.
- `REVIEWING`: run review. `PASS` moves to `CLOSING`; `CHANGES_REQUESTED`
  runs a `FIX` job with `trigger="review"` if retries remain; `BLOCKED` stops.
- `FIXING`: resume the last available fix prompt, then rerun checks.
- `CLOSING`: stop with a message that later phases own the next step.
- `BLOCKED`: exit non-zero.

The automated loop is now:

```text
IMPLEMENT -> RUN_CHECKS -> REVIEW -> FIX/RUN_CHECKS/REVIEW -> CLOSE_PHASE
```

Only `CLOSE_PHASE` and later operational commands remain manual.

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
      "promptPrefix": "",
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
    "coder": "codex",
    "reviewer": "claude"
  },
  "maxRetriesPerPhase": 3,
  "timeoutMinutes": 45,
  "autoCommit": true,
  "allowDirty": false
}
```

Current notes:

- `roles.coder` is used for IMPLEMENT and FIX. With `autoCommit=true`, the
  coder/fixer prompt requires committing, pushing, and creating or updating a
  PR before the job exits. `roles.reviewer` is used for read-only review.
- `promptPrefix` is optional. When set, the runner prepends it to every prompt
  sent to that agent profile.
- `checks` run as shell commands from the repo root, in order. The first failure
  stops the check job.
- `timeoutMinutes` applies per agent/check process.
- `autoCommit=true` requires the GitHub CLI (`gh`) on `PATH`; after checks pass,
  the runner verifies `gh pr view` for the current branch before opening the
  reviewer.
- `allowDirty=false` is the safest dogfood setting. A dirty worktree blocks
  before an IMPLEMENT job starts.
- `allowDirty=true` warns and continues; after IMPLEMENT, the runner stages only
  paths that were not already dirty before the job.
- Review output must be strict JSON with `status`, `summary`, `blockingIssues`,
  `nonBlockingIssues`, and `recommendedFixPrompt`. Invalid review JSON blocks
  the phase and leaves the raw output in `review.log`.

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
- `REVIEWING`: ready to run the reviewer, or retry review after checks pass.
- `FIXING`: ready to resume a fix prompt after interruption.
- `CLOSING`: review passed; manual close-phase work is needed until Phase 7 lands.
- `BLOCKED`: implementation failed or the phase needs human intervention.
- `COMPLETE`: done; the loop skips it.

## Running a Phase

Before running:

```sh
git status --short
```

If `allowDirty=false`, this must be clean. Then run:

```sh
python3 -m agent_runner run
```

Expected Phase 6 success flow:

```text
[agent-runner] acquired lock for <project-slug>
[agent-runner] registered/resumed plan docs/plan.md with N phase(s)
[agent-runner] phase <n> review passed; moved to CLOSING
```

With `autoCommit=true`, after that:

```sh
python3 -m agent_runner status
```

The phase status line includes `branch_name`, `pr_url`, and `published_sha`.
The reviewer approved the published PR diff. Phase 7 will automate the
close-phase doc/plan/handoff step; until then, inspect and close manually.

With `autoCommit=false`, the reviewer still works from `git diff --staged`.

If checks fail, inspect:

```sh
python3 -m agent_runner status
python3 -m agent_runner logs
```

Then open the phase log directory under `~/.agent-runner/logs/.../phase-N/`.
`checks.log`, `fix.log`, `review.log`, and `review.json` show the convergence
history and outstanding blockers.

## Manual Handoff Until Phase 7-8

When a phase reaches `CLOSING`, the runner has stopped at the current automation
boundary. For dogfooding now:

1. Inspect the phase PR, `python3 -m agent_runner status`, and the phase logs.
2. Run any extra checks you need.
3. Update the plan status manually, usually from `CLOSING` to `COMPLETE`.
4. Set the next phase to `PENDING`.
5. Run `python3 -m agent_runner run` again.

When a phase reaches `BLOCKED`, use `python3 -m agent_runner status` and the
latest events to see why. IMPLEMENT failures are recorded as events and in the
job log.

## Logs and State

`status` prints human-readable status to stderr and JSON to stdout:

```sh
python3 -m agent_runner status
```

When a job is active, `status` shows the running job id, type, phase, start
time, and log path. `run` also prints job start lines as soon as it launches a
job, including the role/profile, log path, and child PID.

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
worktree. With `autoCommit=true`, the coder/fixer must leave committed and
pushed work on a PR before review starts. With `autoCommit=false`, after a
successful IMPLEMENT the runner stages the implementation so new files appear
in `git diff --staged`.

Closer jobs are not automated yet, so the final plan/handoff close remains
manual after a review pass moves the phase to `CLOSING`.
