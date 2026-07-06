# agent-runner

> **Work in progress** — nothing runs yet; the design and build plan are settled, the
> code is being built phase by phase.

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
  `## Phase <n>:` format so the runner can dogfood its own remaining plan once
  Phase 3 lands.
- `.agent-runner.json` — the dogfood config for this repo (also serves as the
  reference config shape).

## Working agreement

One phase per branch/session. Read `docs/design.md` before starting a phase; follow the
standing rules in `docs/plan.md`. Prompt text and review semantics are borrowed from the
[agent-toolbelt](../agent-toolbelt) packs referenced in the design doc's reuse map.
