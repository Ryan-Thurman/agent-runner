---
status: awaiting_human_verify
trigger: "[agent-runner] phase 5 review fix limit exhausted after 2 review FIX attempt(s); outstanding review blockers: reviewer requires the plan phase Status/Evidence write-back before CLOSE_PHASE."
severity: SEV2
created: 2026-08-02
updated: 2026-08-02
---

# Bug Investigation: reviewer requests runner-owned plan closeout

## Symptoms

- Expected: a phase whose code, checks, and review acceptance criteria pass proceeds from REVIEW to
  CLOSE_PHASE; only the closer writes `Status: COMPLETE` and `Evidence:`.
- Actual: the reviewer returned a `shouldFix` requiring Phase 5's pending status and absent
  evidence to be updated. FIX prompts forbid that edit, so two FIX attempts could not resolve the
  request and the runner blocked the phase.
- Errors / stack traces: `phase 5 review fix limit exhausted after 2 review FIX attempt(s)`.
- Reproduction (manual steps or automated command): run a review where the plan has sibling
  completed phases and the current phase remains `PENDING`; reviewer can demand closeout metadata
  before the runner starts CLOSE_PHASE.
- Reproduction status: confirmed-manual
- Reproduced by: operator log and phase-5 `review.json`
- First observed / since: 2026-08-02
- Affected area / users: all agent-runner projects using review agents; a spurious plan-metadata
  finding can exhaust the review-FIX budget and halt a valid phase.

## Current Focus

- Hypothesis under test: confirmed — `_review_prompt` omits the runner's plan-ownership rule,
  unlike IMPLEMENT and FIX, allowing a reviewer to treat closer-owned metadata as missing cleanup.
- Test / probe: add a review-protocol exclusion for plan status/evidence and handoff output, with a
  prompt-contract test that asserts the reviewer receives that rule.
- Expecting: the new contract prevents the reviewer from returning the unfulfillable finding while
  retaining review of the phase body, diff, and checks.
- next_action: operator runs `agent-runner run` from rct; Phase 5 should pass checks and receive a
  fresh REVIEW that does not request closer-owned plan metadata.

## Eliminated

| Hypothesis | Evidence it's wrong | When |
|---|---|---|
| Phase 5 must be manually marked COMPLETE before review | `_close_phase_prompt` explicitly assigns Status/Evidence write-back to CLOSE_PHASE; `_review_fix_prompt` explicitly forbids plan edits. | 2026-08-02 |

## Evidence

| When | Checked | Found | Implication |
|---|---|---|---|
| 2026-08-02 | Phase 5 `review.json` | The sole `shouldFix` requests Phase 5 status/evidence metadata. | The review finding is about lifecycle state, not phase work. |
| 2026-08-02 | Phase 5 `fix-prompt.md` | It says “Do not edit PLAN-agent-runner-2026-07-24.md”; only the runner owns status/evidence. | The requested FIX is impossible by design. |
| 2026-08-02 | `phase_loop.py` | `_close_phase_prompt` assigns the metadata write-back to the closer. | Reviewer must not gate pre-close progress on that metadata. |
| 2026-08-02 | `_review_prompt` and `tests/test_review_contract.py` | Review protocol has no equivalent ownership exclusion. | The omission is at the review-contract boundary. |
| 2026-08-02 | agent-runner worktree | Existing edits normalize missing PASS findings for a separate Phase 12 incident. | Preserve and exclude those edits from this repair. |
| 2026-08-02 | focused prompt-contract tests | `tests.test_review_contract` and `tests.test_plan_ownership` pass (12 tests). | The new rule is present and existing ownership behavior remains intact. |
| 2026-08-02 | full runner suite | `python3 -m unittest discover -s tests -q` exits zero. | The repair is compatible with the runner suite. |
| 2026-08-02 | installed CLI import | `agent_runner.__file__` resolves to `/Users/mac/workspaces/agent-runner/agent_runner/__init__.py`. | The `agent-runner` command will use this local repair without reinstalling. |
| 2026-08-02 | `agent-runner unblock --phase 5 --to CHECKING` in rct | Phase 5 moved from BLOCKED (blockedFrom=REVIEWING) to CHECKING. | The next `agent-runner run` starts a fresh check/review path rather than replaying the exhausted FIX budget. |

## Reasoning checkpoint

- hypothesis: The review prompt fails to state that plan status/evidence are closer-owned and out
  of review scope, causing the reviewer to issue an unfulfillable should-fix finding.
- confirming_evidence: The Phase 5 review requested exactly that edit; the FIX prompt forbids it;
  the closer prompt requires it later; `_review_prompt` currently contains no ownership exclusion.
- falsification_test: If `_review_prompt` already included an explicit exclusion, or if CLOSE_PHASE
  were not the owner of the metadata, this hypothesis would be false. Source inspection shows the
  opposite in both cases.
- fix_rationale: Tell the reviewer to evaluate the registered phase body and PR only, and to never
  request plan status/evidence or handoff updates before CLOSE_PHASE. This removes the cause before
  review findings reach the fixed-budget loop.
- blind_spots: Agents can still ignore prompts; the regression proves the contract is supplied but
  cannot guarantee a model never violates it. A later host-side finding filter would be broader and
  is not needed for the confirmed prompt omission.

## Resolution

- root_cause: `_review_prompt` omitted the closer-only plan lifecycle rule, while the matching FIX
  prompt forbade the reviewer-requested edit.
- fix: reviewers are explicitly told not to request `Status:`/`Evidence:` or handoff changes;
  CLOSE_PHASE remains the sole owner of those writes.
- verification: focused contract/ownership tests and the full runner suite pass; Phase 5 is reset
  to CHECKING for a fresh operator-started review.
- files_changed: `agent_runner/phase_loop.py`, `tests/test_review_contract.py`, and this record;
  the pre-existing Phase 12 changes remain separate.
- prevention / follow-ups: prompt-contract regression test; operator re-run is the final runtime
  confirmation.
