# Using agent-runner

This guide describes the current runner. It is ready for dogfooding the
implementation, check, review, retry-limited fix, close-phase, pause/resume,
crash recovery, log-tailing, and roadmap-to-plan loops.

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
- `REVIEWING`: run review. `PASS` runs `CLOSE_PHASE`; `CHANGES_REQUESTED`
  runs a `FIX` job with `trigger="review"` if retries remain; `BLOCKED` stops.
- `FIXING`: resume the last available fix prompt, then rerun checks.
- `CLOSING`: run the closer profile with write flags, validate the plan
  write-back and handoff, optionally commit, and mark the phase `COMPLETE`.
- `MERGING` (only with `mergeOnClose=true`): the close commit landed; push and
  merge the phase PR without re-running the closer. A phase PR that was already
  merged out-of-band (e.g. by an operator) counts as success.
- `BLOCKED`: exit non-zero. `agent-runner unblock` restores the status the
  phase was in when it blocked so `run` can retry it. If `autoFixAttempts` is
  greater than zero and `roles.fixer` is configured, `run` can first launch a
  one-shot `AUTOFIX` job to repair the blocker, unblock the phase, and continue
  in the same invocation.
- A `PAUSED` project does not start another job. Run `agent-runner resume`,
  then `agent-runner run`, to continue from the current phase status.

The automated loop is now:

```text
IMPLEMENT -> RUN_CHECKS -> REVIEW -> FIX/RUN_CHECKS/REVIEW -> CLOSE_PHASE
```

`CLOSE_PHASE` is automated. Operator pause and resume are project-level state
flips and never interrupt a running agent process.

## Installation

From this repository, run the CLI directly:

```sh
python3 -m agent_runner --version
python3 -m agent_runner status
python3 -m agent_runner run
```

The executable shim also works from this checkout:

```sh
./agent-runner --version
./agent-runner status
./agent-runner run
```

For a global command that works from any target git worktree, symlink the short
shim onto your `PATH`:

```sh
ln -s /path/to/agent-runner/autorun ~/bin/autorun
autorun --version
```

The long-form shim can be installed the same way:

```sh
ln -s /path/to/agent-runner/agent-runner ~/bin/agent-runner
```

Both shims resolve their real checkout path before importing the package, so a
symlinked `autorun` or `agent-runner` works from inside any repo. The CLI must
be run from inside the git worktree it should operate on.

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
autorun init
```

From the runner checkout, `python3 -m agent_runner init` is equivalent.

That creates `.agent-runner.json` if it does not already exist. It detects a
default `checks` list for Python or npm projects; otherwise it writes a failing
placeholder check that must be replaced before the first run.

Minimum config shape:

```json
{
  "planPath": "docs/plan.md",
  "checks": [
    "python3 -m compileall -q .",
    "python3 -m unittest discover -s tests"
  ],
  "agents": {
    "codex": {
      "command": "codex",
      "promptArgs": ["exec"],
      "writeFlags": ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"],
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
      "writeFlags": ["--permission-mode=acceptEdits", "--allowedTools=Bash(git:*),Bash(gh:*),Bash(python3:*)"],
      "readOnlyFlags": ["--disallowedTools=Edit,Write,NotebookEdit"],
      "promptPrefix": "",
      "outputCapture": "stdout"
    },
    "claude-sonnet": {
      "command": "claude",
      "promptArgs": ["--model", "claude-sonnet-5", "-p"],
      "writeFlags": ["--permission-mode=acceptEdits", "--allowedTools=Bash(git:*),Bash(gh:*),Bash(python3:*)"],
      "readOnlyFlags": ["--disallowedTools=Edit,Write,NotebookEdit"],
      "promptPrefix": "",
      "outputCapture": "stdout"
    }
  },
  "roles": {
    "coder": "codex",
    "reviewer": "claude-opus",
    "fixer": "claude-opus"
  },
  "roleFallbacks": {
    "reviewer": ["antigravity"],
    "coder": ["claude-sonnet"]
  },
  "reviewTriage": {
    "simple": "claude-sonnet",
    "complex": "claude-opus"
  },
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

Current notes:

- `roles.coder` is used for IMPLEMENT and normal FIX jobs. With
  `autoCommit=true`, those prompts require committing, pushing, and creating or
  updating a PR before the job exits. `roles.reviewer` is used for read-only
  review. `roles.fixer` is optional and used only for one-shot `AUTOFIX` jobs
  when `autoFixAttempts` is greater than zero.
- `autoFixAttempts` defaults to `0`, which disables auto-fix. Values above zero
  require `roles.fixer`. Each phase can consume up to that many auto-fix
  attempts total; the count is derived from the `AUTOFIX` jobs recorded for the
  phase, so restarting `run` (including the automatic post-merge restart) does
  not reset the budget.
- When the fixer gives up — the budget is exhausted or an `AUTOFIX` job fails —
  the runner escalates by filing a GitHub issue on the repo (`gh issue create`)
  containing the phase, the blocking message, the give-up reason, and the
  newest phase log tail (the fixer's diagnosis), so a human can review and fix
  it. A successful post is recorded as a `phase.autofix_escalated` event, which
  also prevents duplicate issues for the same blocking message across restarts.
  A failed post (no `gh`, no remote, not authenticated) only prints a warning
  and is retried on the next run. The issue body is also written to
  `autofix-escalation.md` in the phase log directory.
- Agent CLI flag pitfalls the runner cannot detect for you: the runner appends
  the prompt as the final positional argument, and the `claude` CLI's
  `--allowedTools`/`--disallowedTools` options are variadic — written as
  separate arguments (`"--disallowedTools", "Edit,Write"`) they swallow the
  prompt and the job dies with "Input must be provided". Always use the
  `=`-joined form (`"--disallowedTools=Edit,Write"`). In headless `-p` mode
  there is no one to answer permission prompts, so a write-role `claude` under
  bare `--permission-mode acceptEdits` aborts on the first Bash command outside
  its allowlist. Write roles should therefore pre-allow the commands they are
  expected to run — `init` generates
  `--allowedTools=Bash(git:*),Bash(gh:*),...` covering git, gh, and the leading
  command of each configured check. If a fixer job still dies on a denied
  command, add that command to the allowlist; `--dangerously-skip-permissions`
  also works but removes all permission gating from an autonomous write agent,
  so treat it as a last resort. Similarly, codex's `workspace-write`
  sandbox disables network by default, which breaks dependency fetches and
  pushes; the `-c sandbox_workspace_write.network_access=true` override keeps
  the filesystem sandbox while restoring network.
- An `AUTOFIX` job is a short-lived subprocess launched through the same
  `run_agent_job` machinery as IMPLEMENT and REVIEW jobs. It is not a daemon and
  no fixer process is kept alive after its single job. The prompt includes the
  phase content, the blocking event message, and the newest phase log tail.
  With `autoCommit=true`, fixer prompts require committing, pushing, and
  updating the existing PR before the job exits; with `autoCommit=false`, they
  explicitly forbid committing. All fixer prompts forbid invoking `autorun`,
  `agent-runner`, or nested runner commands because the current `run` process
  holds the project lock.
- `promptPrefix` is optional. When set, the runner prepends it to every prompt
  sent to that agent profile.
- `roleFallbacks` is optional and maps a role to an ordered list of agent
  profiles. When a coder IMPLEMENT/FIX job, planner ROADMAP_PLAN job, or
  reviewer REVIEW job fails with a quota/rate-limit error (429, "usage limit",
  "quota exceeded", and similar), the runner reruns the job with the next
  profile and records a `<jobtype>.fallback` event such as
  `implement.fallback`, `roadmap_plan.fallback`, `fix.fallback`, or
  `review.fallback`. Any other failure blocks the job without falling back.
  Other roles are accepted but warned about. The sample config includes an
  `antigravity` profile (the `agy` CLI) suitable as a fallback on a separate
  quota pool.
- `reviewTriage` is optional. When configured, the runner launches one
  read-only `TRIAGE` job before each `REVIEW` using the `simple` profile. The
  triage prompt includes the phase body and a stat-only diff summary, then asks
  for `{"tier": "simple"}` or `{"tier": "complex"}`. The selected tier chooses
  the primary reviewer profile for that review; `roleFallbacks.reviewer` still
  applies after it. If triage fails, times out, or returns invalid JSON, the
  runner records the reason in a `review.triage` event and reviews with the
  `complex` profile without blocking the phase.
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
- `mergeOnClose=true` (requires `autoCommit`) makes the loop fully autonomous:
  after the reviewer passes the PR and CLOSE_PHASE lands the doc/plan write-back,
  the runner pushes the close commit and merges the phase PR with
  `mergeStrategy` (`squash` by default). Before merging, it re-verifies the PR
  against GitHub: still open, not a draft, on the stored phase branch, at the
  pushed close commit, and without reported merge conflicts — a stale or
  drifted PR blocks the phase instead of merging. Because GitHub's API can
  briefly report the pre-push head right after a push, a PR-head mismatch is
  retried up to 5 times, 30 seconds apart, before blocking. Before the next phase's
  IMPLEMENT, the
  runner verifies the previous phase's PR is MERGED, fetches
  `origin/<baseBranch>`, and starts the phase on a fresh
  `dev/phase-NN-<title>` branch cut from it — the coder never starts on a
  stale base or a reused branch. A pre-existing phase branch with commits not
  on the base blocks the phase instead of being clobbered.
- `mergeOnClose=false` keeps a human in the loop: after CLOSE_PHASE the runner
  stops and asks you to merge the phase PR before it will start the next phase.
- Review output must be strict JSON with `status`, `summary`, `findings`, and
  `recommendedFixPrompt`. `findings` is grouped by bucket, currently
  `blocking`, `shouldFix`, and `nitpick`. `PASS` is valid only when every
  findings bucket is empty; any non-empty bucket is treated as
  `CHANGES_REQUESTED` and sent to the review-triggered FIX prompt. During the
  migration, legacy `blockingIssues` and `nonBlockingIssues` payloads are still
  accepted: `blockingIssues` maps to `findings.blocking`, while
  `nonBlockingIssues` maps to `findings.shouldFix`. The runner still writes
  those legacy fields into normalized `review.json` for compatibility. The
  extractor tolerates common agent framing: prose before or after the JSON, a
  ```json code fence anywhere in the output (the last parseable block wins),
  and the `claude -p --output-format json` envelope (the document is read from
  its `result` field). Output with no parseable JSON document blocks the phase
  and leaves the raw output in `review.log`.
- With `autoCommit=true`, the runner mirrors normalized `review.json` back to
  the published PR after extraction. `PASS` posts a whole-PR approval review,
  `CHANGES_REQUESTED` posts a whole-PR request-changes review, and `BLOCKED`
  posts a PR comment instead of a review decision. The body is mechanical: it
  includes the review status, summary, all finding buckets, the recommended fix
  prompt, and an idempotency marker with the plan path, phase number, review job
  id, and reviewed SHA. GitHub posting is a workflow gate for published PRs:
  failures record a `review.github_post_failed` event and block the phase before
  the runner starts any review-triggered fix or close job. The normalized
  `review.json` remains available in the phase log directory for retry or
  operator recovery.
- The closer uses the configured coder profile with write flags. It must update
  docs or record a `not doc-impacting` reason, set the phase plan marker to
  `Status: COMPLETE`, add an `Evidence:` line, and write the phase handoff.

## Plan Format

The plan file is markdown. Phases are discovered from headings like:

```md
## Phase 7: CLOSE_PHASE - the full circle
Status: PENDING

Close the accepted phase...

Acceptance Criteria:
- A passing phase writes docs, plan evidence, and a handoff.
```

Rules:

- Use `## Phase <number>: <title>` headings.
- Text before the first phase heading is treated as plan-level context. Use it
  for standing guidance, shared acceptance notes, or review contracts that apply
  across phases.
- IMPLEMENT, REVIEW, FIX, and CLOSE_PHASE prompts include this plan-level
  context, bounded deterministically to 4000 characters. Oversized preambles are
  truncated at that cap and marked as truncated.
- Plan-level context is prompt data only. It can guide agents, but it does not
  override runner safety rules, phase scope rules, or explicit job
  requirements.
- Add `Status: <STATE>` directly under the phase heading.
- If the status line is missing, the runner treats the phase as `PENDING`.
- The status line and the runner-owned `Evidence:` line immediately after it are
  excluded from the phase content hash, so close-phase write-back does not count
  as a plan body change. For compatibility with earlier write-backs, a wrapped
  evidence block that contains a `Checks:` line is also treated as runner-owned
  metadata.
- Duplicate phase numbers and invalid status values are rejected.

Useful statuses while dogfooding:

- `PENDING`: ready for the runner to start.
- `REVIEWING`: ready to run the reviewer, or retry review after checks pass.
- `FIXING`: ready to resume a fix prompt after interruption.
- `CLOSING`: ready to run or resume `CLOSE_PHASE`.
- `MERGING`: close commit landed; ready to retry the phase PR merge.
- `BLOCKED`: implementation failed or the phase needs human intervention.
- `COMPLETE`: done; the loop skips it.

## Roadmap Planning

`agent-runner` can ask a configured agent to translate unfinished roadmap items
into an executable plan without starting implementation:

```sh
python3 -m agent_runner plan-roadmap
```

By default, this reads `docs/roadmap.md` and writes the configured `planPath`
from `.agent-runner.json`, which is commonly `docs/plan-roadmap.md`. Override
those paths when needed:

```sh
python3 -m agent_runner plan-roadmap \
  --roadmap docs/roadmap.md \
  --output docs/plan-roadmap.md
```

The command uses `roles.planner` when configured, otherwise `roles.coder`, with
write flags because the agent edits the output plan file. It records a
`ROADMAP_PLAN` job and a `roadmap.plan_generated` event, validates that the
result is parseable markdown with `## Phase N: Title` headings, explicit
`Status:` markers, and at least one `Status: PENDING` phase, then stops. It
does not register the generated plan, run phases, commit changes, push, open a
PR, or merge anything. Run `agent-runner run` later to execute the plan.

## Running a Phase

Before running:

```sh
git status --short
```

If `allowDirty=false`, this must be clean. Then run:

```sh
python3 -m agent_runner run
```

Expected success flow:

```text
[agent-runner] acquired lock for <project-slug>
[agent-runner] registered/resumed plan docs/plan.md with N phase(s)
[agent-runner] starting IMPLEMENT job 1 (role=coder, profile=codex)
[codex coding]: ...
[agent-runner] starting RUN_CHECKS job 2 (role=checks, profile=shell)
[checks checking]: ...
[agent-runner] starting REVIEW job 3 (role=reviewer, profile=claude-opus)
[claude-opus reviewing]: ...
[agent-runner] phase <n> complete; plan complete
```

The `[codex coding]:`, `[checks checking]:`, and similar previews show the
latest child-process output on one rolling line by default. The rolling preview
uses carriage-return/clear-line control sequences, including when stderr is
redirected. Set `AGENT_RUNNER_LIVE_LOGS=lines` for readable newline-delimited
previews in captured stderr or CI logs. Long lines end with `... [truncated]`
in the preview only. The complete stdout/stderr remains in the phase `.log`
files under `~/.agent-runner/logs/`, and agent output capture files keep their
exact configured contents.

With `autoCommit=true`, close-phase changes are committed with:

```text
Phase <n>: <title>
```

If another phase is still `PENDING`, the runner starts its IMPLEMENT job after
the closure commit. With `autoCommit=false`, the runner marks the phase complete
but stops before starting the next pending phase so local staged work can be
handled deliberately.

### Self-hosted restart after merges

When the runner operates on its own checkout (the `agent_runner` package lives
inside the target repo, as when agent-runner develops itself), a merged phase
brings new runner code into the working tree, but the running process still
has the old modules in memory. In that case, with `mergeOnClose=true`, the
runner does not auto-advance in-process after a merge: it records a
`runner.restart` event, prints `restarting to load updated runner code`,
releases the project lock, and replaces itself (`exec`, same PID and terminal)
with a fresh `run` via the repo's `agent-runner` shim. The new process
re-registers the plan and continues with the next phase on the just-merged
code. The one-shot `--accept-plan-change` flag is not carried across a
restart.

For any other repo, behavior is unchanged. Set
`AGENT_RUNNER_NO_SELF_RESTART=1` to disable the restart and keep in-process
auto-advance; a restart counter (`AGENT_RUNNER_RESTART_COUNT`) caps runaway
restarts at 32 per chain, falling back to in-process advance past the cap.
POSIX only.

### Manual merge reconciliation

If a phase PR is merged outside the runner while SQLite still says the phase is
`BLOCKED`, the next `run` attempts a conservative repair before starting more
work. For blocked registered phases with PR metadata, the runner checks
`gh pr view <url>` for the PR state, head SHA, and merge commit. When the PR is
`MERGED`, it verifies that the configured `baseBranch` contains the merge
commit, fetching `origin/<baseBranch>` once if needed.

The phase is marked `COMPLETE` only when the plan also marks that phase
`Status: COMPLETE` and the protected phase body hash still matches the
registered SQLite hash. On success, `blocked_from` is cleared, `published_sha`
is refreshed from the PR head SHA, and a `phase.reconciled` event is recorded.
If the merged PR lacks matching plan evidence, the runner leaves the phase
blocked with a message explaining which proof is missing instead of guessing.
Open or otherwise unmerged PRs are not reconciled.

To pause at the next job boundary while a run is active:

```sh
python3 -m agent_runner pause
```

The active agent or check job is allowed to finish. The runner then exits before
starting the next job and prints the resume command. Continue with:

```sh
python3 -m agent_runner resume
python3 -m agent_runner run
```

Running while paused is non-destructive; it explains that the project is paused
and exits without launching a job.

After a published review:

```sh
python3 -m agent_runner status
```

The phase status line includes `branch_name`, the PR URL as `pr=#<number>
(<url>)` when the URL ends in `/pull/<number>`, and `published_sha`.
The reviewer approved that published PR diff. Before `CLOSE_PHASE` runs with
`autoCommit=true`, the runner rechecks that stored PR metadata and requires a
clean worktree on the stored `branch_name` with local `HEAD` still matching
`published_sha`. Only the later closure commit may move local `HEAD` beyond the
stored reviewed SHA.

With `autoCommit=false`, the reviewer still works from `git diff --staged`.

If checks fail, inspect:

```sh
python3 -m agent_runner status
python3 -m agent_runner logs
```

Then open the phase log directory under `~/.agent-runner/logs/.../phase-N/`.
`checks.log`, `fix.log`, `review.log`, and `review.json` show the convergence
history and outstanding blockers.

## Close Handoff

When `CLOSE_PHASE` succeeds, the runner validates that the closer wrote:

1. `Status: COMPLETE` plus an `Evidence:` line in the plan.
2. A handoff at `.acc/phases/<plan-slug>/phase-NN-handoff.md`.
3. The handoff sections `Completed Work`, `Decisions`, `Files Changed`,
   `Checks Run`, `Open Risks`, and `Next-Phase Context`.

Closer failure, timeout, missing handoff, missing completion marker, or protected
phase-body hash drift marks the phase `BLOCKED` instead of silently completing it.

When a phase reaches `BLOCKED`, use `python3 -m agent_runner status` and the
latest events to see why. IMPLEMENT failures are recorded as events and in the
job log. Once the cause is addressed, unblock the phase and rerun:

```sh
python3 -m agent_runner unblock
python3 -m agent_runner run
```

`unblock` restores the status the phase had when it blocked (recorded as
`blocked_from`) and takes `--phase N` to pick a phase and `--to STATUS` to
override the restored status — useful for phases blocked before `blocked_from`
existed. A phase blocked because retries ran out will block again on resume
unless you fix the underlying findings or raise `maxRetriesPerPhase`.
Review-driven fixes are stricter than check-driven fixes: the runner allows one
review-triggered FIX, then one re-review. If that re-review still reports
blocking issues, the phase blocks instead of starting another PR review cycle.

When auto-fix is enabled, `run` tries the configured `fixer` before returning
the blocked result, but only for resumable blocks with `blocked_from` recorded.
It skips blockers that need human intent, such as protected plan-content
changes. If the fixer process fails or the phase has consumed its
`autoFixAttempts` budget (counted from the phase's recorded `AUTOFIX` jobs, so
it survives runner restarts), the phase remains `BLOCKED` and the command
exits non-zero as usual.

## Logs and State

`status` prints human-readable status to stderr and JSON to stdout:

```sh
python3 -m agent_runner status
```

When a job is active, `status` shows the running job id, type, phase, start
time, and log path. `run` also prints job start lines as soon as it launches a
job, including the role/profile, log path, and child PID.

During `run`, agent and check jobs also stream compact live previews to stderr
with labels derived from job metadata:

- `IMPLEMENT` with the coder profile prints `[<profile> coding]: ...`.
- `REVIEW` with the reviewer profile prints `[<profile> reviewing]: ...`.
- `FIX` with the coder profile prints `[<profile> fixing]: ...`.
- `CLOSE_PHASE` with the closer profile prints `[<profile> closing]: ...`.
- `RUN_CHECKS` prints `[checks checking]: ...`.

Previews are not a substitute for logs. By default, the preview uses one
rolling line and may include carriage-return/clear-line control sequences in
redirected stderr. Set `AGENT_RUNNER_LIVE_LOGS=lines` for readable
newline-delimited previews in captured stderr or CI logs. Previews may be
truncated and colored, while the log files are complete and uncolored child
output. Set
`AGENT_RUNNER_LIVE_LOGS=0` to disable previews:

```sh
AGENT_RUNNER_LIVE_LOGS=0 python3 -m agent_runner run
```

Set `AGENT_RUNNER_LIVE_LOGS=lines` to force newline-delimited preview mode.

Color is controlled by `AGENT_RUNNER_COLOR=auto|always|never`. The default
`auto` emits ANSI color only when stderr is a TTY and `NO_COLOR` is not set.
Use `always` to force color and `never` to suppress raw ANSI escapes even in an
interactive terminal.

`logs` prints the latest registered phase log directory and tails the newest
`.log` file:

```sh
python3 -m agent_runner logs
python3 -m agent_runner logs -n 80
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
- `close_phase-prompt.md`: prompt sent to the closer.
- `close_phase.log`: streamed stdout/stderr from close-phase work.

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

Job rows store the spawned child PID. When startup recovery reaps an orphaned
`RUNNING` job, it also attempts to terminate that job's process group before
re-enqueueing the phase from SQLite state.

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

The runner does not force-push, delete branches, delete files outside the repo,
modify global git config, or interrupt running agent processes for pause/resume.
It merges only the reviewed phase PR, only when `mergeOnClose=true`, and only
after CLOSE_PHASE validation passes; with `mergeOnClose=false` it never merges.

Before a normal IMPLEMENT job starts, the default dirty gate requires a clean
worktree. With `autoCommit=true`, the coder/fixer must leave committed and
pushed work on a PR before review starts. With `autoCommit=false`, after a
successful IMPLEMENT the runner stages the implementation so new files appear
in `git diff --staged`.

Closer jobs run with write flags, but the runner still validates their output
before marking a phase complete.
