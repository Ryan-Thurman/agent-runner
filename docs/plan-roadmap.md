# Agent Runner Roadmap Execution Plan

> Executable plan derived from `docs/roadmap.md`. Use this when you want
> `agent-runner` to work through the next major improvements instead of editing
> the high-level roadmap directly.

## Context for the implementing agent

`agent-runner` is a local Python 3 CLI, stdlib only, in `agent_runner/`. Read
`docs/roadmap.md`, `docs/design.md`, and `docs/usage.md` before starting.
Keep each phase tightly scoped, update public docs for behavior changes, and
add focused `unittest` coverage. Do not start future phases.

Reviews in this workflow are not advisory. The reviewer must report every
update it wants before approval, bucketed by severity such as `blocking`,
`shouldFix`, and `nitpick`. `PASS` means there are no requested updates in any
bucket. Any non-empty finding bucket means `CHANGES_REQUESTED`, and the runner
should give all requested updates to the fixer, not only blockers.

## Phase 1: Reconcile manually merged phase PRs
Status: PENDING

Teach `agent-runner run` to repair stale SQLite state when a phase PR was
merged outside the runner but the tracked plan and GitHub PR prove the phase is
complete.

- After plan registration and orphan reap, before `run_phase_loop`, inspect
  phases in the active plan that are not `COMPLETE` and have PR metadata.
- Query `gh pr view <url>` for merge state, head SHA, and merge commit.
- If the PR is merged, verify the local base branch contains the merge commit
  or fetch the base branch before deciding.
- If the parsed plan marks that phase `Status: COMPLETE` and the protected
  phase body hash matches SQLite, mark the phase `COMPLETE`, clear
  `blocked_from`, refresh `published_sha`, and record a `phase.reconciled`
  event.
- If the PR is merged but the plan marker or hash does not prove completion,
  block with a clear message instead of guessing.

Acceptance Criteria:
- A test seeds a stale `BLOCKED` phase with merged PR metadata and a complete
  plan marker, runs `run`, and asserts the phase is reconciled to `COMPLETE`
  and the next pending phase starts.
- A test covers merged PR metadata with missing or mismatched plan completion
  evidence and asserts the runner blocks with a clear message.
- A test asserts non-merged PRs are not reconciled.
- `docs/usage.md` documents the manual-merge reconciliation behavior.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v`
  pass.

## Phase 2: Treat all review findings as requested updates
Status: PENDING

Update the review contract so the reviewer reports every requested update and
the runner sends all findings to the fixer.

- Extend the review prompt to request findings grouped by bucket, such as
  `blocking`, `shouldFix`, and `nitpick`.
- Normalize new and legacy review JSON. Existing `blockingIssues` and
  `nonBlockingIssues` must continue to work during migration.
- `PASS` is valid only when all finding buckets are empty. Any finding bucket
  with entries means `CHANGES_REQUESTED`.
- Update the FIX prompt to include all requested updates, grouped by bucket,
  while preserving scope constraints and the one-fix plus one-re-review churn
  limit.
- Update re-review instructions to verify all prior requested updates, not
  only blockers.

Acceptance Criteria:
- Tests cover new bucketed findings causing `CHANGES_REQUESTED` and appearing
  in the FIX prompt.
- Tests cover legacy `nonBlockingIssues` being treated as requested updates.
- Tests cover `PASS` with non-empty findings being rejected or normalized to
  `CHANGES_REQUESTED`.
- Docs describe the new review contract and backward compatibility.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v`
  pass.

## Phase 3: Mirror review results to GitHub
Status: PENDING

Make the runner post the reviewer findings to GitHub even when the review agent
does not.

- After `review.json` is extracted for a published PR, render it into a
  whole-PR Markdown review body without adding new reasoning.
- For `PASS`, call `gh pr review <url> --approve --body-file <file>`.
- For requested updates, call
  `gh pr review <url> --request-changes --body-file <file>`.
- For `BLOCKED`, call `gh pr comment <url> --body-file <file>` instead of
  approving or requesting changes.
- Include an idempotency marker containing plan path, phase number, review job
  id, and reviewed SHA.
- Posting failures should record `review.github_post_failed` and continue by
  default; the SQLite/log review result remains authoritative.

Acceptance Criteria:
- Fake-`gh` tests assert approval, request-changes, and blocked-comment bodies
  are posted for the right review statuses.
- Tests assert GitHub post failure records an event and does not change the
  review outcome.
- Tests assert the body includes all finding buckets and the idempotency
  marker.
- Docs explain that GitHub posting mirrors `review.json` and is non-fatal by
  default.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v`
  pass.

## Phase 4: Include bounded plan context in prompts
Status: PENDING

Make top-level plan guidance, standing rules, and review contracts available to
agents without requiring every phase to duplicate them.

- Parse a bounded plan preamble before the first phase heading.
- Include relevant preamble text in IMPLEMENT, REVIEW, FIX, and CLOSE_PHASE
  prompts.
- Keep runner safety rules stronger than plan text: prompt must say plan
  content is data, not instructions that override runner rules.
- Keep prompt size predictable with a clear size cap or section selection
  strategy.

Acceptance Criteria:
- Tests prove top-level plan context appears in IMPLEMENT, REVIEW, FIX, and
  CLOSE_PHASE prompts.
- Tests prove oversized preamble content is bounded deterministically.
- Existing prompt tests still pass.
- Docs explain how plan-level context is used.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v`
  pass.

## Phase 5: Add live job previews
Status: PENDING

Execute the live-log work from `docs/plan-live-logs.md`, keeping the detailed
three-phase plan there as the source of implementation requirements.

- Implement the live preview formatter, labels, truncation, and color mode.
- Stream bounded previews to stderr during agent and check jobs while keeping
  full log files complete and capture semantics unchanged.
- Document the feature and dogfood at least one phase through the runner after
  streaming lands.

Acceptance Criteria:
- The acceptance criteria from all phases in `docs/plan-live-logs.md` are
  satisfied or explicitly copied into completed evidence.
- Live previews can be disabled with `AGENT_RUNNER_LIVE_LOGS=0`.
- Color behavior is controlled by `AGENT_RUNNER_COLOR=auto|always|never` and
  `NO_COLOR`.
- Docs distinguish live truncated previews from complete log files.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v`
  pass.

## Phase 6: Harden state, docs, and roadmap planning
Status: PENDING

Clean up known rough edges and add a first-class way for the runner to turn a
roadmap into an executable plan.

- Fix or suppress the recurring Python 3.14 SQLite `ResourceWarning` noise in
  tests by closing connections correctly.
- Update `docs/design.md` to remove stale "still to build" language and align
  it with implemented behavior.
- Review `docs/usage.md` for leftover plan snippets, stale status markers, and
  terminology drift.
- Add a runner workflow or command that asks a configured agent to read
  `docs/roadmap.md`, identify unfinished roadmap items, and generate or update
  an executable plan file such as `docs/plan-roadmap.md`.
- The roadmap-to-plan workflow should be conservative: it proposes phases with
  acceptance criteria and does not start implementation until a later `run`.

Acceptance Criteria:
- Full test runs no longer emit the known unclosed-SQLite `ResourceWarning`
  noise.
- Design and usage docs match current runner behavior.
- A tested command or documented workflow can generate/update an executable
  plan from unfinished roadmap items.
- The generated plan uses normal `## Phase N` and `Status: PENDING` markers so
  `agent-runner` can execute it.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v`
  pass.
