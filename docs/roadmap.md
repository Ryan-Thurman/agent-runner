# Agent Runner Roadmap

Last updated: 2026-07-07

This document summarizes the tracked plan files, what has landed, and the
remaining work observed while dogfooding `agent-runner` on its own repo.

## Current State

- `docs/archive/plan.md`: archived core runner build plan. The local SQLite
  state still has stale history for the old Phase 8 dogfood run, so future work
  should prefer the tracked plan files and add state reconciliation rather than
  relying on that stale row.
- `docs/archive/plan-smoke.md`: archived smoke plan. This covered the CLI
  `--version` smoke change.
- `docs/archive/plan-operator-ux.md`: archived operator UX plan. This added
  coder fallbacks, the `autorun` shim and default config, PR number display,
  opt-in `AUTOFIX`, and review triage.
- `docs/plan-live-logs.md`: not started. All three phases are still `PENDING`.
- `docs/plan-roadmap.md`: executable plan derived from this roadmap. Use it
  when you want `agent-runner` to implement the recommended roadmap items.

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

## Recommended Next Roadmap

### 1. State Reconciliation for Manual PR Merges

Problem: when an operator manually merges a phase PR, SQLite can still say the
phase is `BLOCKED` or otherwise in progress even though the tracked plan and
GitHub PR show completion.

Plan:

- On `run`, after plan registration and orphan reap, inspect non-complete
  phases with stored PR metadata.
- If `gh pr view <url>` reports the PR as merged, verify the local base branch
  contains the merge commit and the plan file says `Status: COMPLETE` for that
  phase.
- If the protected phase body hash matches, mark the SQLite phase `COMPLETE`,
  refresh PR metadata, clear `blocked_from`, and record a `phase.reconciled`
  event.
- If the PR is merged but close metadata is missing or hashes do not match,
  block with a clear operator message rather than guessing.

### 2. Review Contract and Fix-All-Findings Flow

Problem: the current review schema distinguishes `blockingIssues` from
`nonBlockingIssues`, and the fixer only receives blockers. In an autonomous
runner loop, review findings are not advisory.

Plan:

- Update the review prompt to require all requested updates before approval.
- Normalize review output into finding buckets such as `blocking`, `shouldFix`,
  and `nitpick`.
- `PASS` means all finding buckets are empty.
- Any non-empty finding bucket means `CHANGES_REQUESTED`.
- Feed all requested updates to the FIX prompt, grouped by bucket, while
  preserving the one-fix plus one-re-review churn limit.
- Keep backward compatibility for old `blockingIssues` and `nonBlockingIssues`
  payloads during migration.

### 3. Runner-Owned GitHub Review Posting

Problem: some reviewer agents return useful findings but do not reliably post a
GitHub review.

Plan:

- After `review.json` is extracted, mechanically render it into a whole-PR
  Markdown review body.
- For `PASS`, run `gh pr review <url> --approve --body-file <file>`.
- For requested changes, run
  `gh pr review <url> --request-changes --body-file <file>`.
- For `BLOCKED`, post a PR comment instead of an approval/request-changes
  review.
- Include an idempotency marker with plan path, phase number, review job id,
  and reviewed SHA.
- Treat GitHub posting failures as non-fatal by default: record
  `review.github_post_failed`, keep the runner's review result authoritative,
  and consider a future strict config flag only if needed.

### 4. Prompt Context Hygiene

Problem: top-level plan guidance does not automatically reach phase prompts;
only the active phase body is used. This means standing rules and review
contracts can be missed unless duplicated inside a phase.

Plan:

- Add a parsed plan preamble or standing-context block to prompts for
  IMPLEMENT, REVIEW, FIX, and CLOSE_PHASE.
- Keep the included context bounded and explicit so prompt size stays
  predictable.
- Treat plan content as data, not instructions that override runner rules.
- Add tests proving top-level plan context appears in relevant prompts.

### 5. Live Logs Plan

Problem: operators still need to open log files to see agent/check progress in
real time.

Plan:

- Execute `docs/plan-live-logs.md` Phase 1: formatter, labels, truncation, and
  color mode.
- Execute Phase 2: stream bounded previews to stderr during agent and check
  jobs while preserving complete log files and output capture semantics.
- Execute Phase 3: docs, output polish, and dogfood notes.

### 6. Hardening and Documentation Cleanup

Known cleanup items:

- Fix existing Python 3.14 `ResourceWarning` noise from unclosed SQLite
  connections in tests.
- Update `docs/design.md`, which still contains early "still to build" language
  now that the runner exists.
- Review `docs/usage.md` for leftover plan-like snippets and stale status
  markers.
- Consider enforcing close-time `Evidence:` and doc-gate requirements more
  strictly, or documenting why they remain prompt-enforced.

### 7. Roadmap-to-Plan Generation

Problem: the roadmap is useful for humans, but `agent-runner` executes phase
plans. Today an operator has to manually translate unfinished roadmap items into
an executable plan.

Plan:

- Add a runner workflow or command that asks a configured agent to read
  `docs/roadmap.md`, identify unfinished roadmap items, and generate or update
  an executable plan such as `docs/plan-roadmap.md`.
- The generated plan should use normal `## Phase N` and `Status: PENDING`
  markers so `agent-runner` can execute it.
- The workflow should stop after creating or updating the plan. It should not
  start implementation until a later `run`.
- Include acceptance criteria in generated phases so review has a concrete
  contract.

## Suggested Execution Order

1. State reconciliation for manual PR merges.
2. Review contract and fix-all-findings flow.
3. Runner-owned GitHub review posting.
4. Prompt context hygiene.
5. Live logs plan.
6. Hardening and documentation cleanup.
7. Roadmap-to-plan generation.
