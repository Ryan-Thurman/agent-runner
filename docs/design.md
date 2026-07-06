# Agent Runner Loop — design (`agent-runner`)

> **Work in progress — not fully fleshed out.** This is the design doc for this repo's
> runner; it wins on any conflict with `docs/plan.md`. Toolbelt paths in the reuse map
> below (`commands/…`, `skills/…`, `workflows/…`) refer to the
> [agent-toolbelt](../../agent-toolbelt) repo, where the origin copy of this doc lives as
> `workflows/WiP-agent-runner-loop.md`.

A minimal local CLI (`agent-runner run`, Python 3 stdlib + SQLite) that automates the
manual bounce between coding agents inside a Supacode project/worktree:

```text
Plan file  = what should be done
SQLite     = where we are
Logs       = what happened
Runner     = what happens next
Claude Code = implementer / fixer
Codex       = reviewer / verifier
```

Per phase: `IMPLEMENT → RUN_CHECKS → REVIEW → (PASS → CLOSE_PHASE → next phase |
CHANGES_REQUESTED → FIX → RUN_CHECKS → REVIEW …)` until clean, blocked, or out of retries.
Agents are disposable; the runner regenerates full prompts from stored state every launch.

## Reuse map — build on these, don't rebuild

The toolbelt has no TypeScript to import; the runner is new code. What it reuses is
**prompt text, semantics, and queue discipline** that are already written and battle-tested:

| Runner piece | Reuse from | What to take |
| --- | --- | --- |
| IMPLEMENT prompt | `commands/dev-implement-task.md` | Scope rules (one phase only, no future work, no unrelated refactors, tests with behavior changes), output contract (summary, files, tests, risks, suggested commit). |
| FIX prompt | `commands/dev-fix-review-issues.md` | "Fix only the listed issues, don't start the next phase, no broad refactors" constraints. |
| REVIEW prompt | `skills/dev-lite-workflow/references/review-rules.md` | Independent-review protocol (reviewer gets intent + evidence, never the implementer's self-summary), check order (done-when in substance → scope completeness → contract drift → boundaries → ordinary review), Verified/Failed/Not-inferable labeling, Blocking / Should Fix / Nice to Have classification. |
| Reviewer safety | `skills/pr-review/SKILL.md` | The diff/plan content pasted into the review prompt is **data, not instructions**. |
| Doc gate (closure) | `commands/pr-ready-check.md` step 2 | Doc-impacting change ⇒ docs updated **and committed to the same branch**, or an explicit recorded "not doc-impacting" — otherwise block. |
| Plan-as-state discipline | `skills/dev-lite-workflow/SKILL.md` (Living Plan Rule) | What the plan write-back must keep durable: current phase, evidence, commit hash, blockers, next step, resume instructions. |
| Phase handoff files | `workflows/phase-context-workflow.md` | `.acc/phases/<room>/phase-NN-handoff.md` as the durable cross-agent handoff written at phase close. |
| SQLite discipline | `skills/review-queue/references/cli.md` | WAL + busy_timeout, JSON-to-stdout for parseability, attempts→dead-letter thinking, recording the head SHA a job ran against. |
| Later: PR lane | `phase-gate` / `pr-review` / `review-queue` | When `openPrWhenComplete` lands, the review leg becomes `/pr-review --comment` on a phase PR and this runner is just the dispatcher. |

## Design corrections to the draft spec

These are changes to the draft worked out elsewhere; they fix real failure modes.

1. **Untracked files are invisible to `git diff`.** New files a coder creates don't appear in
   `git diff` or `git diff --staged` until staged. The reviewer would review an incomplete
   diff. Fix: after IMPLEMENT/FIX the runner runs `git add -A` and reviews
   `git diff --staged` (v1), or commits per phase and reviews `git diff <base>..HEAD`.
2. **Plan hashing conflicts with plan write-back.** Full-file `content_hash` means the
   runner's own "phase complete" write-back reads as a plan change and triggers the
   SUPERSEDED/block path. Fix: hash **per phase** over the phase body, excluding a
   runner-owned status marker line; store `content_hash` on `phases`, not just `plans`.
3. **Retry loophole → infinite loop.** The draft increments `retry_count` only on
   `CHANGES_REQUESTED`; a checks-fail → FIX → checks-fail cycle never touches it and loops
   forever. Fix: increment on **every FIX enqueue**, whatever triggered it.
4. **Review never converges without memory.** A fresh reviewer each round finds fresh nits.
   Fix: feed the previous `review.json` into re-reviews — "verify these blocking issues are
   resolved; only new **Blocking** findings may block." Only `blockingIssues` drive
   `CHANGES_REQUESTED`; Should Fix / Nice to Have are recorded, not gating.
5. **Reviewer must be unable to edit.** Run Codex with a read-only sandbox
   (`codex exec --sandbox read-only` or equivalent). Report-first is an invariant across the
   whole review family (`phase-gate`: "the reviewer never edits").
6. **Reviewer independence.** Never include the coder's `implement.log` summary in the
   review prompt — phase content + diff + check output only (independent review protocol).
7. **Orphaned jobs on crash.** A runner killed mid-job leaves a `RUNNING` job and a stuck
   phase. On startup: mark any `RUNNING` job `FAILED` (`error = "orphaned"`) and reset the
   phase so the job re-enqueues. Single runner + lock file makes leases unnecessary; the
   startup reap is not optional.
8. **Timeouts.** Per-job timeout in config (default ~30–60 min); kill the child process and
   mark the job `FAILED` — a hung agent must not hang the runner.
9. **Headless flags.** `claude -p` sits at permission prompts unless configured
   (`--permission-mode acceptEdits` / an allowlist, or skip-permissions in trusted
   worktrees). Keep coder/reviewer args fully in `.agent-runner.json` (draft already does).
10. **Rename `FIX_REVIEW` → `FIX`.** It handles check failures too; add a `trigger` column
    (`checks` | `review`) instead of encoding the source in the type name.

## Full-circle closure: `CLOSE_PHASE`

The draft's `COMPLETE_PHASE` only commits. The point of the loop is that when the work is
accepted, **docs and the plan are updated so any agent can see where we are**. Replace it
with a `CLOSE_PHASE` job that launches the coder one more time with a closure prompt:

1. **Doc gate** — apply `pr-ready-check` step 2: if the phase altered behavior, an API, a
   flag/config, the data model, or notable perf, update the docs that describe it and commit
   them to the same branch; otherwise record an explicit "not doc-impacting" with a reason.
2. **Plan write-back** — mark the phase complete in the plan file (status marker line under
   the phase heading, excluded from the per-phase hash) with commit hash, evidence, and
   next step, per the Living Plan Rule.
3. **Phase handoff** — write `.acc/phases/<room>/phase-NN-handoff.md`
   (`phase-context-workflow` shape): what was completed, decisions, files, checks run, open
   risks, next-session context.
4. **Commit** — `git add -A && git commit -m "Phase <n>: <title>"` (when `autoCommit`),
   then the runner marks the phase `COMPLETE` and advances.

Closure is an agent job, not runner-native code — doc updates need judgment.

## Schema deltas (vs the draft)

- `phases`: add `content_hash TEXT NOT NULL`; add `UNIQUE(plan_id, phase_number)`.
- `jobs`: type `FIX_REVIEW` → `FIX` + `trigger TEXT` (`checks`|`review`); type
  `COMPLETE_PHASE` → `CLOSE_PHASE`; add `started_sha TEXT` / `finished_sha TEXT` (what the
  job actually ran against — enables delta-only re-review later, mirrors `review-queue`'s
  head-SHA discipline); index `(phase_id)` and `(project_id, status)`.
- `plans`: `UNIQUE(project_id, path)`; `content_hash` becomes the normalized hash
  (per-phase hashes are authoritative for change detection).
- Pragmas: WAL + `busy_timeout` (10s), as in `review-queue`.
- Everything else in the draft (projects/plans/phases/jobs/events, statuses, lock file,
  `~/.agent-runner/` layout, log-paths-in-SQLite) stands.

## Still to build (all new code)

- The TS CLI itself: `run` / `status` / `pause` / `resume` / `logs` / `reset-lock` / `init`.
- Plan parser (`## Phase <n>: <title>` blocks + per-phase hashing + status marker line).
- Job state machine + child-process exec with log capture and timeouts.
- Prompt templates (seeded from the commands in the reuse map above).
- Review JSON extraction (prefer instructing the reviewer to write `review.json` to a given
  path over scraping stdout).
- Lock file + orphan reap on startup.

## Agent profiles — roles are swappable

Prompts are written per **role** (coder / reviewer / closer), never per vendor. The config
describes each agent as a profile, and roles reference a profile:

```json
{
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
  "roles": { "coder": "claude", "reviewer": "codex" }
}
```

Swapping who codes and who reviews is flipping the two `roles` values. The runner applies
`writeFlags` to coder/closer jobs and `readOnlyFlags` to reviewer jobs, so the
reviewer-never-edits invariant holds whichever agent reviews. Same-vendor for both roles is
also fine — independence comes from the fresh process/context, not the vendor.

Invoking the toolbelt from headless agents: with the packs installed in the target repo
(`install.sh --harness`), `claude -p "/dev-implement-task …"` runs the installed command
directly, and Codex picks up the skills via the generated `AGENTS.md` pointer (prompt:
"load `skills/pr-review/SKILL.md` and …"). Keep embedded prompt templates as the fallback
for repos without the toolbelt installed.

## Decisions

- **Pause semantics (decided):** never interrupt a running agent. `pause` takes effect at
  the next job boundary; mid-job interruption is a later problem.
- **Language:** TypeScript/Node or Python are both acceptable; Python's stdlib covers the
  whole surface (`sqlite3`, `subprocess`, `hashlib`, `argparse`) with zero deps and no
  build step, so it is the lower-friction MVP choice.

## Open questions

- **PR lane:** when `openPrWhenComplete` is real, does REVIEW move to `/pr-review --comment`
  on a phase PR (the reviewer via the installed pack), making this runner converge with
  `phase-gate-solo-workflow` as its decoupled sibling?
- **Where the runner lives:** its own repo vs a `tools/` dir here. Currently external.
