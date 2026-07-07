# Agent Runner Operator UX Plan

> Five-phase plan, executed by agent-runner itself. Phase 1 extends role
> fallbacks to the coder so the default config can use them. Phase 2 adds the
> short `autorun` entry point usable from any repo plus a ready-to-use default
> config for `init`. Phase 3 surfaces PR numbers on publish and merge in
> runner output. Phase 4 adds an opt-in one-shot fixer agent that repairs
> blocked phases and lets the run continue. Phase 5 adds review triage that
> routes simple reviews to Sonnet and behavior-change reviews to Opus.

## Context for the implementing agent

`agent-runner` is a local Python 3 CLI, stdlib only, in `agent_runner/`. Read
`docs/design.md` and `docs/usage.md` before starting, and keep changes minimal
and in the style of the existing code. The build plan history lives in
`docs/plan.md`; do not touch it.

## Running this plan (operator bootstrap)

This repo has no `.agent-runner.json` yet. To execute this plan with the
runner itself: run `python3 -m agent_runner init` (or `./agent-runner init`)
in this repo, then edit the generated config to set `"planPath":
"docs/plan-operator-ux.md"` and `"mergeOnClose": true` before `run`. The
existing sample config's checks already match this repo's real checks. With
`mergeOnClose` true the runner merges each phase PR and auto-advances; a PR
merged out-of-band is detected and treated as success (the phase completes
and the loop continues).

The self-hosted restart feature is already implemented (see "Self-hosted
restart after merges" in `docs/usage.md`): because this repo is the runner's
own checkout, each merged phase makes the runner re-exec itself, so every
subsequent phase in this plan runs on the code merged by the previous
phases.

## Standing rules (every phase)

- One phase per session/PR. Do not start future phases or refactor unrelated
  code.
- Ship tests with the change (stdlib `unittest`, matching the existing suite)
  and keep the full suite green.
- Update the docs the change touches (`README.md`, `docs/usage.md`) in the same
  phase.

## Phase 1: Extend role fallbacks to the coder role
Status: PENDING

Today only the reviewer retries with `roleFallbacks` profiles on quota/rate
limit failures; the fallback loop is open-coded in `_run_review` and
`validate_config` warns when any other role configures fallbacks. Phase 2's
default config needs a coder fallback, so generalize the mechanism first.

- Extract the retry-on-quota-failure loop from `_run_review` in
  `agent_runner/phase_loop.py` into a shared helper that takes the role name,
  the ordered profile list, and the `run_agent_job` arguments, and returns the
  final `JobResult` plus the profile that produced it. Keep the existing
  behavior: only advance to the next profile when `is_quota_failure` is true
  and a next profile exists; print the fallback notice to stderr and record a
  `<jobtype>.fallback` event (the existing `review.fallback` event type must
  not change).
- Use the helper for the coder's IMPLEMENT and FIX jobs (all FIX call sites,
  including resume paths) with the profile order: the `coder` role profile
  followed by `roleFallbacks.coder` entries.
- Generalize `_profiles_for_role` (currently reviewer-only around
  `phase_loop.py:1161`) rather than duplicating it.
- In `agent_runner/config.py`, remove the "only the reviewer role falls back"
  warning for the coder role; keep warning for any role that is neither
  `coder` nor `reviewer`.
- Update the `roleFallbacks` comment in `SAMPLE_CONFIG` and the fallback
  section of `docs/usage.md` to say both roles fall back.

Acceptance Criteria:
- A test drives an IMPLEMENT job whose primary coder profile fails with a
  quota-style message and asserts the fallback profile runs, the phase
  proceeds, and an `implement.fallback` event is recorded; an equivalent test
  covers FIX.
- Existing reviewer fallback tests still pass unchanged.
- A config with `roleFallbacks.coder` set produces no warning; a fallback for
  an unrelated role still warns.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.

## Phase 2: autorun shim usable from any repo, with a ready default init config
Status: PENDING

Add a short `autorun` entry point that works from any repository and make
`init` write a config that runs with minimal editing.

- Add an executable `autorun` shim at the repo root. It must resolve its own
  real path (`os.path.realpath(__file__)`, following symlinks), insert that
  checkout directory at the front of `sys.path`, and then call
  `agent_runner.cli.main`. This way `ln -s <checkout>/autorun ~/bin/autorun`
  (or `/usr/local/bin/autorun`) gives a global `autorun` command that works
  from inside any git worktree. Update the existing `agent-runner` shim the
  same way so a symlinked long form also works.
- Make `init` write a `checks` list detected from the target repo instead of a
  guess. In `cmd_init` (or a small helper in `agent_runner/config.py`),
  inspect the repo root, first match wins:
  - `pyproject.toml`, `setup.py`, or a `tests/` directory alongside Python
    files → `["python3 -m compileall -q .", "python3 -m unittest discover -s
    tests"]`.
  - `package.json` → `["npm test"]`.
  - Otherwise a placeholder that fails loudly so an unedited config can never
    auto-merge unverified work: `["sh -c 'echo \"agent-runner: replace the
    placeholder checks entry in .agent-runner.json with your project'\''s real
    check command\" >&2; exit 1'"]`.
- Update `SAMPLE_CONFIG` in `agent_runner/config.py` so `init` writes a ready
  default:
  - Roles: `codex` as coder, `claude-opus` as reviewer.
  - Profiles: `codex`, `antigravity`, and two claude profiles derived from the
    current `claude` sample profile: `claude-opus` (`"promptArgs": ["--model",
    "claude-opus-4-8", "-p"]`) and `claude-sonnet` (`"promptArgs": ["--model",
    "claude-sonnet-5", "-p"]`). Every claude profile in the generated config
    MUST pin `--model` explicitly — reviews must never run on the claude CLI's
    default model (Fable), so no unpinned `claude` profile may remain in the
    sample. Add a `//` comment on the reviewer role saying reviews are pinned
    to Opus/Sonnet deliberately.
  - Active `roleFallbacks`: `{"reviewer": ["antigravity"], "coder":
    ["claude-sonnet"]}` (supported after Phase 1).
  - `planPath` `docs/plan.md`, `baseBranch` `main`, `autoCommit` true,
    `mergeOnClose` true, `mergeStrategy` `squash`, `maxRetriesPerPhase` 3,
    `timeoutMinutes` 45, `allowDirty` false.
  - Keep the explanatory `//` comments (the loader already strips them) and
    update the `mergeOnClose` comment, which currently describes `false` as
    the shipped value. Because the `checks` value is now computed per repo,
    restructure `SAMPLE_CONFIG` as a template (e.g. a format slot for the
    checks array) and keep a module-level default so existing imports and
    tests still have a parseable sample.
- After writing the config, `init` must print the follow-up steps to stderr:
  review `planPath`/`checks` in `.agent-runner.json`, write the plan file,
  then run `autorun run`. When the placeholder check was written, say
  explicitly that `checks` must be replaced before the first run.
- Document the `autorun` shim and the symlink install in `README.md` (Install
  section) and `docs/usage.md` (Installation section).

Acceptance Criteria:
- `./autorun --version`, `./autorun status`, and a symlinked `autorun` invoked
  from a different git repository all work; a test covers running the shim
  through a symlink from another directory using `subprocess`.
- In a fresh temporary git repo, `autorun init` writes `.agent-runner.json`
  and `load_config` parses it without errors or warnings; tests cover the
  Python-project, `package.json`, and placeholder detection branches, and
  assert the placeholder check command exits non-zero.
- A test asserts that every profile in the generated config whose command is
  `claude` includes `--model` in its `promptArgs` (guarding against reviews
  ever running on the CLI's default model).
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.

## Phase 3: Show PR numbers on publish and merge
Status: PENDING

Surface the phase PR number in runner output so an operator can follow the
loop from the terminal.

- Add a small helper in `agent_runner/phase_loop.py` that extracts the PR
  number from a stored PR URL (the trailing `/pull/<number>` segment; return
  `None` if it does not match), plus a formatting helper that renders a URL as
  `PR #<num> (<url>)` when a number is found and the bare URL otherwise. Reuse
  these everywhere below instead of open-coding the regex.
- When the runner records the published PR for a phase, print
  `[agent-runner] phase <N> PR #<num> opened: <url>` to stderr and include
  `#<num>` in the `phase.published` event message. Note there are two
  `phase.published` record sites in `phase_loop.py` (the normal post-checks
  publish and the resume-after-publish-verification path); cover both.
- When the merge succeeds, print
  `[agent-runner] phase <N> PR #<num> merged (<strategy>)` to stderr and
  include `#<num>` in the `phase.merged` event message. The already-merged
  path must print `[agent-runner] phase PR #<num> already merged; skipping
  merge`.
- Use the formatting helper in the other operator-facing PR messages so the
  number shows consistently: the `resuming merge for phase <N>` stderr line
  and the `phase <N> complete; merge PR ... before starting phase <M>` loop
  result used when `mergeOnClose` is false.
- In `status` output, the per-phase publish state must show the PR as
  `pr=#<num> (<url>)` when the URL yields a number, falling back to the bare
  URL otherwise.
- Keep behavior unchanged when a phase has no PR URL or the URL has no
  trailing number.

Acceptance Criteria:
- Runner stderr shows `PR #<num> opened` after publish and `PR #<num> merged`
  after merge in the existing phase-7 test harness; tests assert both lines
  and the updated event messages.
- Unit tests cover the number-extraction helper: a normal
  `https://github.com/<owner>/<repo>/pull/12` URL, a URL with no trailing
  number, and an empty/`None` URL.
- `status` shows `pr=#<num>` for a published phase; a test covers it.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.

## Phase 4: Opt-in one-shot fixer agent for blocked phases
Status: PENDING

When a phase blocks, let a short-lived agent repair the problem and let the
same `autorun run` invocation continue, instead of stopping for a human. No
agent process may outlive its single job: the fixer is a one-shot subprocess
via the existing `run_agent_job` machinery, exactly like IMPLEMENT/REVIEW
jobs.

- Config: add an optional `fixer` role (any configured agent profile) and an
  optional integer `autoFixAttempts` (default 0 = disabled). Config
  validation: `autoFixAttempts > 0` requires the `fixer` role. Update the
  Phase 2 generated default config (`SAMPLE_CONFIG`) in this phase to add
  `"fixer": "claude-opus"` to `roles` and `"autoFixAttempts": 2`, with a `//`
  comment explaining the behavior.
- Job plumbing for the new job type, all in this phase:
  - Add `AUTOFIX` to `JOB_TYPES` in `agent_runner/storage.py` and to the
    `CHECK(type IN (...))` constraint in the `jobs` table schema.
  - Existing databases keep the old CHECK (tables are `CREATE TABLE IF NOT
    EXISTS`; SQLite cannot alter a CHECK). Add a small helper in
    `storage.py`, called from schema setup, that reads the `jobs` table's SQL
    from `sqlite_master` and, when its type list does not cover `JOB_TYPES`,
    rebuilds the table in a transaction (create new table with the current
    schema, copy rows, drop old, rename). Drive the helper from `JOB_TYPES`
    so later phases adding job types get the migration for free.
  - Add `"AUTOFIX": "BLOCKED"` to `ORPHAN_PHASE_STATUS` (the phase is BLOCKED
    while the fixer runs, so an orphaned fixer leaves it BLOCKED).
  - Add `"fixer"` to `WRITE_ROLES` in `agent_runner/jobs.py` so
    `run_agent_job` accepts the role and applies the profile's `writeFlags`.
- Driver loop: implement in `cmd_run` (or a small helper it calls), around
  `run_phase_loop`, not inside it. When the loop returns a blocked result and
  auto-fix is enabled:
  1. Load the blocked phase and its `blocked_from` status. If there is no
     `blocked_from` (not resumable), stop and report as today.
  2. Skip auto-fix for blocks that need human intent — plan-content change
     protection — and stop as today.
  3. Run one AUTOFIX job with the fixer profile in write mode via
     `run_agent_job`. The prompt must include: the phase heading and content,
     the blocking event message, and the tail of the newest phase log file.
     The prompt must instruct the agent to fix the underlying problem only —
     commit nothing, and never invoke `autorun`/`agent-runner` itself (the
     running process holds the project lock; a nested `run` would deadlock).
  4. On fixer success, unblock the phase to `blocked_from` (reuse the
     `cmd_unblock` logic; factor it into a shared function rather than
     duplicating it), record a `phase.autofix` event, and re-enter
     `run_phase_loop`.
  5. On fixer failure, or once a phase has used `autoFixAttempts` attempts in
     this invocation, leave the phase BLOCKED and return the blocked result as
     today. Track attempts per phase in memory for the current invocation
     only.
- Print one stderr line per attempt:
  `[agent-runner] phase <N> blocked; auto-fix attempt <i>/<max> with profile
  <name>` and, after the rerun,
  the normal loop output. Honor pause: check the project PAUSED state before
  each auto-fix attempt, as the loop does at job boundaries.
- Document the `fixer` role, `autoFixAttempts`, the one-shot/no-daemon
  behavior, and the no-nested-run rule in `docs/usage.md`; mention the knob in
  `README.md`'s config example.

Acceptance Criteria:
- With a stub fixer profile, a test drives a phase that blocks on a failing
  check, asserts the AUTOFIX job runs, the phase is unblocked to its
  `blocked_from` status, the loop continues in the same invocation, and a
  `phase.autofix` event is recorded.
- A test asserts the attempt cap: after `autoFixAttempts` failed fixes the
  phase stays BLOCKED and `run` exits non-zero as today.
- A test asserts `autoFixAttempts` 0 (or an absent `fixer` role) leaves
  today's blocking behavior untouched, and that `autoFixAttempts > 0` without
  a `fixer` role is a config error.
- A migration test creates a database with the pre-existing `jobs` CHECK
  constraint (without `AUTOFIX`), reopens it through `connect_db`, and
  asserts an `AUTOFIX` job row can be inserted and prior rows survive.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.

## Phase 5: Review triage — route simple reviews to Sonnet, behavioral to Opus
Status: PENDING

Before each REVIEW job, ask a cheap model to classify the phase's diff and
pick the reviewer profile accordingly. A reviewer agent cannot switch its own
model mid-run (the model is fixed when the CLI process starts), so this is a
separate, tiny, read-only triage job. Like every other job, it is a one-shot
subprocess.

- Config: add an optional `reviewTriage` object:
  `{"simple": "<profile>", "complex": "<profile>"}`. Both values must name
  configured agent profiles; validation rejects unknown names. When absent,
  review behavior is unchanged (the `reviewer` role profile runs every
  review).
- Job plumbing: add `TRIAGE` to `JOB_TYPES` in `agent_runner/storage.py` and
  to the `jobs` CHECK constraint (the Phase 4 rebuild helper migrates
  existing databases automatically once `JOB_TYPES` grows); add `"TRIAGE":
  "REVIEWING"` to `ORPHAN_PHASE_STATUS`; add `"triage"` to `READ_ONLY_ROLES`
  in `agent_runner/jobs.py`.
- When `reviewTriage` is configured, before the REVIEW job run one TRIAGE job
  via `run_agent_job` using the `simple` profile in read-only mode. The prompt
  contains the phase heading/content and the published diff's stat summary
  (`gh pr diff --stat` output or equivalent, not the full patch), and asks for
  strict JSON only: `{"tier": "simple"}` or `{"tier": "complex"}`, with one
  sentence of guidance: docs, comments, renames, config text, or small
  mechanical changes with no behavior change are `simple`; anything that
  changes runtime behavior, logic, error handling, concurrency, security, or
  data handling is `complex`.
- Map the tier to the reviewer profile for this REVIEW job. On any triage
  problem — job failure, timeout, unparseable or unexpected JSON — fail safe
  to the `complex` profile and record why in the event. Never fail the phase
  because triage failed.
- `roleFallbacks.reviewer` still applies after the chosen primary profile,
  exactly as today.
- Print `[agent-runner] review triage: phase <N> tier=<tier>; reviewing with
  profile <name>` to stderr and record a `review.triage` event with the tier
  and chosen profile.
- Update the Phase 2 generated default config to include
  `"reviewTriage": {"simple": "claude-sonnet", "complex": "claude-opus"}`
  with a `//` comment explaining the routing, keeping the never-unpinned-model
  rule.
- Document `reviewTriage` in `docs/usage.md` and mention it in `README.md`'s
  config example.

Acceptance Criteria:
- With a stub triage agent returning `{"tier": "simple"}`, a test asserts the
  REVIEW job runs with the `simple` profile; returning `{"tier": "complex"}`
  routes to the `complex` profile; garbage output routes to `complex` and the
  phase still completes.
- A test asserts a config without `reviewTriage` runs reviews exactly as
  before (no TRIAGE job spawned), and that a `reviewTriage` naming an unknown
  profile is a config error.
- A test asserts the `review.triage` event and stderr line are emitted with
  the chosen tier and profile.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.

