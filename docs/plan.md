# Agent Runner Build Plan

> **WiP.** Build plan for the `agent-runner` CLI described in `docs/design.md`.
> This plan uses the same `## Phase <n>: <title>` + `Acceptance Criteria:` format the
> runner itself parses, so from Phase 3 onward the runner can be dogfooded against the
> remainder of this plan.

## Context for the implementing agent

You are building a minimal local CLI, `agent-runner`, in **Python 3 (stdlib only:
`sqlite3`, `subprocess`, `hashlib`, `argparse`, `json`, `pathlib`)**. No third-party
dependencies, no build step. Target: macOS. Read `docs/design.md` before any phase — it
holds the schema, the agent-profile config shape, the prompt requirements, and the
design corrections. Where this plan and the design doc conflict, the design doc wins.

What the runner does: from inside a project worktree, `agent-runner run` reads a
markdown plan, tracks state in a global SQLite DB, and drives a per-phase loop —
launch a coder agent (e.g. Claude Code) to implement, run checks, launch a reviewer
agent (e.g. Codex) read-only to review, loop coder fixes until the review passes or
retries run out, then a closure job updates docs + plan + handoff and commits.

## Standing rules (every phase)

- One phase per session/PR. Do not start future phases or refactor unrelated code.
- Every phase ships with tests (stdlib `unittest` or `pytest` if already present) and a
  manual verification note in the phase handoff.
- The runner must never: auto-merge, force-push, delete branches/worktrees, delete files
  outside the repo, modify global git config, or run without its project lock.
- Never interrupt a running agent process; pause/stop takes effect at job boundaries.
- Prompts are written per **role** (coder/reviewer/closer), never per vendor. Reviewer
  jobs always get the profile's `readOnlyFlags`; coder/closer jobs get `writeFlags`.
- All timestamps ISO-8601 UTC. All CLI subcommands print human-readable lines to stderr
  and (where useful) JSON to stdout so scripts can parse them.

## Phase 1: CLI scaffold, config, project detection, locking
Status: COMPLETE

Build the skeleton everything else hangs on: an `agent-runner` entrypoint with
subcommand routing (`init`, `run`, `status`, `pause`, `resume`, `logs`, `reset-lock` —
stubs are fine for the ones whose phase hasn't landed), repo detection (cwd → git repo
root → `.agent-runner.json`), config load + validation against the agent-profile shape
in the design doc (`agents` map with `command`/`promptArgs`/`writeFlags`/
`readOnlyFlags`/`outputCapture`, `roles` map, `planPath`, `checks`, `maxRetriesPerPhase`,
`autoCommit`, `allowDirty`, per-job `timeoutMinutes`), the `~/.agent-runner/` directory
layout, and the project lock file (`locks/<project-slug>.lock` storing pid/repoPath/
startedAt).

Acceptance Criteria:
- `agent-runner init` creates `~/.agent-runner/` and writes a commented sample
  `.agent-runner.json` (with `claude` coder / `codex` reviewer profiles) into the repo;
  refuses to overwrite an existing config.
- `agent-runner run` outside a git repo, or without `.agent-runner.json`, exits non-zero
  with a clear message naming what is missing.
- Config validation rejects: unknown role reference, missing agent profile fields,
  empty `checks` accepted but warned.
- Second concurrent `run` for the same project refuses to start while the first holds
  the lock; a lock whose PID is dead is reaped automatically; `reset-lock` clears one
  explicitly; the lock is removed on clean exit and on SIGINT.

## Phase 2: SQLite state layer and event log
Status: COMPLETE

Implement the storage module: global DB at `~/.agent-runner/runner.sqlite`, WAL +
`busy_timeout=10000`, schema per the design doc **including its deltas** — `projects`,
`plans` (UNIQUE(project_id, path)), `phases` (per-phase `content_hash`,
UNIQUE(plan_id, phase_number)), `jobs` (types IMPLEMENT / RUN_CHECKS / REVIEW / FIX /
CLOSE_PHASE, `trigger` column, `started_sha`/`finished_sha`, indexes on `(phase_id)` and
`(project_id, status)`), `events`. Add the startup **orphan reap**: any job left RUNNING
by a dead runner is marked FAILED (`error="orphaned"`) and its phase reset so the job
re-enqueues. Wire `agent-runner status` to read real state (project, plan, phase table
with status/retries, last events).

Acceptance Criteria:
- Fresh DB is created lazily with the full schema; re-running is idempotent.
- Unit tests cover: unique constraints firing, orphan reap flipping RUNNING→FAILED and
  resetting the phase status, events written with project/plan/phase/job linkage.
- `status` on a project with no plan yet says so instead of stack-tracing.
- Log/prompt/output files live on disk under `logs/<project>/<plan>/phase-N/`; only
  their paths are stored in SQLite.

## Phase 3: Plan parsing, registration, and change detection
Status: COMPLETE

Parse the plan markdown: phases are `## Phase <number>: <title>` blocks, each phase's
content runs to the next phase heading. A runner-owned `Status: <STATE>` marker line
directly under the heading is **excluded from the per-phase content hash** (this is what
lets the closure job write status back without superseding the plan). On `run`: register
or resume the plan — same per-phase hashes resume; a changed PENDING phase is updated in
place; a changed phase that is in progress or COMPLETE warns and blocks unless
`--accept-plan-change` is passed (which re-hashes and continues).

Acceptance Criteria:
- Parser unit tests: numbered phases with gaps, missing `Status:` line (treated as
  PENDING), content between preamble and first phase ignored, trailing phase captured.
- Editing only a `Status:` line changes no phase hash.
- Editing a PENDING phase's body updates that phase row and hash without touching others.
- Editing an IMPLEMENTING/COMPLETE phase's body blocks with a named phase and hint to use
  `--accept-plan-change`; the flag unblocks and records an event.

## Phase 4: Job execution engine
Status: COMPLETE

The generic "run an agent or a command as a job" layer: create the job row, write the
prompt to `phase-N/<type>-prompt.md`, launch the child process from the repo root with
the role-appropriate flags from the agent profile, stream stdout/stderr to the job's log
file, enforce the configured timeout (kill the process group, mark FAILED), record
`started_sha`/`finished_sha` (`git rev-parse HEAD`), exit code, and timestamps. Support
the three output-capture modes from the design doc: `stdout`, `last-message-file`
(e.g. `codex exec --output-last-message <path>`), and structured stdout (e.g.
`claude -p --output-format json`). Also implement the plain-command variant used by
RUN_CHECKS (no prompt, sequential commands, combined `checks.log`).

Acceptance Criteria:
- Unit tests with a fake agent script: success, non-zero exit, timeout kill (job FAILED,
  runner alive), log file contains both streams, SHAs recorded.
- Reviewer-role launches provably include `readOnlyFlags` and never `writeFlags`
  (asserted in tests via the fake agent echoing its argv).
- A second job cannot start while one is RUNNING for the project (one-active-job rule).
- Checks variant runs commands in order, stops at first failure, records which failed.

## Phase 5: IMPLEMENT and RUN_CHECKS loop
Status: COMPLETE

Wire the first half of the phase loop. Dirty-repo gate first (`git status --porcelain`;
block unless `allowDirty`, then warn). IMPLEMENT: generate the coder prompt — thin mode
when the toolbelt is installed in the target repo (invoke the installed
`/dev-implement-task`-style command), embedded fallback template otherwise; both carry
the scope rules (this phase only, no future work, no unrelated refactors, tests with
behavior changes) and the phase body. On success: `git add -A` (so untracked files are
visible to review in local mode), phase → CHECKING, enqueue RUN_CHECKS. On checks pass,
`autoCommit=true` requires a committed, pushed PR before REVIEWING; `autoCommit=false`
keeps the staged local review path. On checks fail → FIXING with the failing output
captured as the fix context. Console output in the `[agent-runner]` style from the
design doc.

Acceptance Criteria:
- End-to-end test with a fake coder that creates a new untracked file: after IMPLEMENT
  the file is staged and appears in `git diff --staged`.
- Coder non-zero exit → phase BLOCKED, run stops, `status` explains why.
- Checks fail → phase FIXING and a FIX job is enqueued with `trigger="checks"` and the
  check output stored as its context; checks pass → phase REVIEWING.
- Dirty repo without `allowDirty` blocks before any job starts.

## Phase 6: REVIEW and FIX convergence loop
Status: COMPLETE

The reviewer leg and the retry loop, with the four convergence rules from the design
doc: (1) the review prompt contains phase content + the published PR diff when
`autoCommit=true` or `git diff --staged` when `autoCommit=false` + check output —
**never the coder's summary/log** (independent review); (2) re-reviews receive the
previous `review.json` with the instruction "verify these blocking issues are resolved;
only new Blocking findings may block"; (3) only `blockingIssues` drive
CHANGES_REQUESTED — Should Fix / Nice to Have are recorded, not gating; (4)
`retry_count` increments on **every** FIX enqueue, whether triggered by checks or
review. Review output is strict JSON (`status` PASS | CHANGES_REQUESTED | BLOCKED,
`summary`, `blockingIssues`, `nonBlockingIssues`, `recommendedFixPrompt`) extracted via
the profile's capture mode into `review.json`; unparseable JSON → phase BLOCKED. FIX
prompts carry only the listed issues plus the original phase body, with the
fix-only-what's-listed constraints. Retries exhausted → phase BLOCKED with a clear
console summary of outstanding blockers.

Acceptance Criteria:
- Test with a scripted reviewer: PASS advances to CLOSE_PHASE; CHANGES_REQUESTED
  enqueues FIX (`trigger="review"`), increments retry, and after FIX the loop re-runs
  checks then review; BLOCKED stops immediately.
- A checks-fail → FIX → checks-fail cycle exhausts `maxRetriesPerPhase` and blocks
  (no infinite loop).
- The re-review prompt provably contains the prior `review.json` and the resolved-issues
  instruction; the first review prompt provably lacks any coder output.
- Reviewer emitting non-JSON garbage → phase BLOCKED, raw output preserved in
  `review.log`.

## Phase 7: CLOSE_PHASE — the full circle
Status: PENDING

On PASS, launch the **closer** (coder profile, write flags) with the closure prompt:
(1) doc gate — if the phase changed behavior, an API, a flag/config, the data model, or
notable perf, update the docs that describe it in the same tree, or record an explicit
"not doc-impacting: <reason>"; (2) plan write-back — set the phase's `Status: COMPLETE`
marker line and append a one-line evidence note (commit hash, checks) under the phase
heading; (3) write the phase handoff to `.acc/phases/<plan-slug>/phase-NN-handoff.md`
(completed work, decisions, files changed, checks run, open risks, next-phase context).
Then the runner commits (`git commit -m "Phase <n>: <title>"` when `autoCommit`; "nothing
to commit" is logged, not fatal), marks the phase COMPLETE, and advances to the next
PENDING phase. When no phases remain, the plan and project go COMPLETE with a summary.

Acceptance Criteria:
- After a passing phase: plan file shows `Status: COMPLETE` for that phase, per-phase
  hash comparison still reports "unchanged" (marker exclusion works end to end), and the
  handoff file exists with the required sections.
- The closure commit contains the code, the doc updates, the plan write-back, and the
  handoff together on the current branch.
- Closer non-zero exit or timeout → phase BLOCKED (work is not silently marked complete).
- Multi-phase plan: completing phase 1 automatically starts phase 2's IMPLEMENT.

## Phase 8: Resume, pause, logs, and end-to-end dogfood
Status: PENDING

Finish the operator surface. `pause`/`resume` flip project status; a paused project
stops at the next job boundary and `run` on it explains how to resume. `run` on an
interrupted project resumes from SQLite: orphan reap, then re-derive the next action
from phase status (IMPLEMENTING → re-enqueue IMPLEMENT, CHECKING → RUN_CHECKS, etc.).
`logs` prints the latest phase's log directory and tails the newest log. Then dogfood:
run the full loop against a toy repo (fixture with a 2-phase plan and stub check
scripts) using the real configured agents at least once, and capture the transcript in
the repo's README along with install/usage docs.

Acceptance Criteria:
- `kill -9` the runner mid-IMPLEMENT; restarting `run` reaps the orphan, re-runs the
  phase from IMPLEMENT, and completes normally.
- `pause` during a running job lets the job finish, then stops before the next job;
  `resume` + `run` continues from the correct state.
- Swapping `roles` in config (coder↔reviewer vendors) passes the Phase 6 test suite
  unchanged.
- The MVP acceptance list from the design doc passes end to end on the toy repo, and the
  README documents setup, config, commands, and the safety rules.
