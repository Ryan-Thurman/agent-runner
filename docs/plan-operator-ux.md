# Agent Runner Operator UX Plan

> Two-phase plan, executed by agent-runner itself. Phase 1 adds the short `AR`
> entry point usable from any repo plus a ready-to-use default config for
> `init`. Phase 2 surfaces PR numbers on publish and merge in runner output.

## Context for the implementing agent

`agent-runner` is a local Python 3 CLI, stdlib only, in `agent_runner/`. Read
`docs/design.md` and `docs/usage.md` before starting, and keep changes minimal
and in the style of the existing code. The build plan history lives in
`docs/plan.md`; do not touch it.

## Standing rules (every phase)

- One phase per session/PR. Do not start future phases or refactor unrelated
  code.
- Ship tests with the change (stdlib `unittest`, matching the existing suite)
  and keep the full suite green.
- Update the docs the change touches (`README.md`, `docs/usage.md`) in the same
  phase.

## Phase 1: AR shim usable from any repo, with a ready default init config
Status: PENDING

Add a short `AR` entry point that works from any repository and make
`agent-runner init` write a config that runs with minimal editing.

- Add an executable `AR` shim at the repo root. It must resolve its own real
  path (`os.path.realpath(__file__)`, following symlinks), insert that checkout
  directory at the front of `sys.path`, and then call `agent_runner.cli.main`.
  This way `ln -s <checkout>/AR ~/bin/AR` (or `/usr/local/bin/AR`) gives a
  global `AR` command that works from inside any git worktree. Update the
  existing `agent-runner` shim the same way so a symlinked long form also
  works.
- Update `SAMPLE_CONFIG` in `agent_runner/config.py` so `init` writes a
  ready default: `codex` as coder and reviewer, `claude` and `antigravity`
  profiles included, an active `roleFallbacks` of `{"reviewer":
  ["antigravity"]}`, `planPath` `docs/plan.md`, `baseBranch` `main`,
  `autoCommit` true, `mergeOnClose` true, `mergeStrategy` `squash`,
  `maxRetriesPerPhase` 3, `timeoutMinutes` 45, `allowDirty` false, and a
  single placeholder `checks` entry the operator must replace with the target
  repo's real check command. Keep the explanatory `//` comments; the loader
  already strips them.
- After writing the config, `init` must print the follow-up steps to stderr:
  edit `planPath`/`checks` in `.agent-runner.json`, write the plan file, then
  run `AR run`.
- Document the `AR` shim and the symlink install in `README.md` (Install
  section) and `docs/usage.md` (Installation section).

Acceptance Criteria:
- `./AR --version`, `./AR status`, and a symlinked `AR` invoked from a
  different git repository all work; a test covers running the shim through a
  symlink from another directory using `subprocess`.
- In a fresh temporary git repo, `AR init` writes `.agent-runner.json` and
  `load_config` parses it without errors; a test covers this.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.

## Phase 2: Show PR numbers on publish and merge
Status: PENDING

Surface the phase PR number in runner output so an operator can follow the
loop from the terminal.

- Add a small helper that extracts the PR number from a stored PR URL (the
  trailing `/pull/<number>` segment; return `None` if it does not match).
- When the runner records the published PR for a phase (the point where the
  `phase.published` event is written), print
  `[agent-runner] phase <N> PR #<num> opened: <url>` to stderr and include
  `#<num>` in the `phase.published` event message.
- When the merge succeeds, print
  `[agent-runner] phase <N> PR #<num> merged (<strategy>)` to stderr and
  include `#<num>` in the `phase.merged` event message. The already-merged
  path must print `[agent-runner] phase PR #<num> already merged; skipping
  merge`.
- In `status` output, the per-phase publish state must show the PR as
  `pr=#<num> (<url>)` when the URL yields a number, falling back to the bare
  URL otherwise.
- Keep behavior unchanged when a phase has no PR URL or the URL has no
  trailing number.

Acceptance Criteria:
- Runner stderr shows `PR #<num> opened` after publish and `PR #<num> merged`
  after merge in the existing phase-7 test harness; tests assert both lines
  and the updated event messages.
- `status` shows `pr=#<num>` for a published phase; a test covers it.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests`
  pass.
