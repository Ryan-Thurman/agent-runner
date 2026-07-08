# Agent Runner Roadmap

Last updated: 2026-07-08

This document summarizes the tracked plan files, what has landed, and the
current follow-up posture after dogfooding `agent-runner` on its own repo.

## Current State

- `docs/archive/plan.md`: archived core runner build plan. The local SQLite
  state still has stale history for the old Phase 8 dogfood run, so current
  status should prefer tracked plan files and newer completed plans over that
  archived row.
- `docs/archive/plan-smoke.md`: archived smoke plan. This covered the CLI
  `--version` smoke change.
- `docs/archive/plan-operator-ux.md`: archived operator UX plan. This added
  coder fallbacks, the `autorun` shim and default config, PR number display,
  opt-in `AUTOFIX`, and review triage.
- `docs/plan-live-logs.md`: complete. This covered live bounded job previews,
  color controls, docs, and dogfood notes.
- `docs/plan-roadmap.md`: complete. This executed the roadmap update from
  manual-merge reconciliation through roadmap-to-plan generation.
- No tracked implementation plan is currently pending. Use
  `agent-runner plan-roadmap` when new unfinished roadmap items need to become
  an executable phase plan.

## Completed Capability Areas

- Plan parsing, per-phase hashing, status markers, and plan registration.
- SQLite-backed projects, plans, phases, jobs, events, locks, and orphan reap.
- Agent job execution with prompt/log capture, timeouts, role-specific flags,
  and quota fallback for coder and reviewer roles.
- IMPLEMENT, RUN_CHECKS, REVIEW, FIX, CLOSE_PHASE, merge-on-close, and
  self-hosted restart after merge.
- Pause/resume, logs tailing, status JSON, unlock/reset-lock operator commands.
- `autoCommit` PR flow with publish metadata, PR number display, merge
  preflights, and stale-head protection.
- Optional one-shot `AUTOFIX` for resumable blocked phases.
- Optional review triage routing simple reviews to Sonnet and behavioral
  reviews to Opus.
- Manual-merge reconciliation for phase PRs.
- Bucketed review findings where any requested update drives
  `CHANGES_REQUESTED`, with legacy `blockingIssues` and `nonBlockingIssues`
  normalization.
- Runner-owned GitHub review or comment posting from extracted `review.json`.
- Bounded plan preamble context in IMPLEMENT, REVIEW, FIX, and CLOSE_PHASE
  prompts.
- Live bounded stderr previews for agent and check jobs, with complete logs
  preserved on disk.
- Python 3.14 SQLite connection cleanup for ResourceWarning-free tests.
- Current design and usage docs aligned with implemented behavior.
- `plan-roadmap`, which asks a configured planner/coder agent to translate
  unfinished roadmap items into an executable phase plan without starting
  implementation.

## Current Follow-Ups

- The archived `docs/archive/plan.md` SQLite row can still appear as stale
  local history in `agent-runner status`. Treat the tracked archived plan and
  completed newer plans as authoritative.
- Close-time `Evidence:` and doc-gate requirements are partly mechanical and
  partly prompt-enforced. That is the main known policy area to revisit if the
  runner needs stricter release governance.
- When new roadmap work is identified, add it here first, then run
  `python3 -m agent_runner plan-roadmap` to generate or update an executable
  plan before implementation.
