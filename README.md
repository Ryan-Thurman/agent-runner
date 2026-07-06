# agent-runner

> **Work in progress** — Phase 5 is merged, so the runner can register plans, run
> IMPLEMENT jobs, stage changes, and run checks. Review, fix execution, close-phase,
> pause/resume, and full log tailing are still being built.

A minimal local CLI (Python 3, stdlib only) that automates the handoff loop between
coding agents inside a project worktree:

```text
Plan file   = what should be done
SQLite      = where we are
Logs        = what happened
Runner      = what happens next
Coder role  = implements / fixes   (e.g. Claude Code)
Reviewer    = reviews, read-only   (e.g. Codex)
```

Per phase: `IMPLEMENT → RUN_CHECKS → REVIEW → (PASS → CLOSE_PHASE → next phase |
CHANGES_REQUESTED → FIX → RUN_CHECKS → REVIEW …)` until clean, blocked, or out of
retries. Agents are disposable; the runner regenerates full prompts from stored state.
Roles are vendor-swappable via agent profiles in `.agent-runner.json`.

## Start here

- `docs/design.md` — the design: reuse map, corrections, schema, agent profiles,
  CLOSE_PHASE full-circle closure. **Wins on any conflict with the plan.**
- `docs/plan.md` — the 8-phase build plan, written in the runner's own
  `## Phase <n>:` format so the runner can dogfood its own remaining plan.
- `docs/usage.md` — how to configure and run the current Phase 5 runner while
  dogfooding.
- `.agent-runner.json` — the dogfood config for this repo (also serves as the
  reference config shape).

## Working agreement

One phase per branch/session. Read `docs/design.md` before starting a phase; follow the
standing rules in `docs/plan.md`. Prompt text and review semantics are borrowed from the
[agent-toolbelt](../agent-toolbelt) packs referenced in the design doc's reuse map.
