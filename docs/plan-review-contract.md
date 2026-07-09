# Review Contract Execution Plan

> Executable plan derived from `docs/review-contract.md`. Read that document first;
> it explains why each change exists. Point `.agent-runner.json`'s `planPath` here to
> run it.

## Context for the implementing agent

`agent-runner` is a local Python 3 CLI, stdlib only, in `agent_runner/`. Read
`docs/review-contract.md`, `docs/design.md`, and `docs/usage.md` before starting.
Keep each phase tightly scoped, update public docs for behavior changes, and add
focused `unittest` coverage. Do not start future phases.

This plan rewrites the runner's own review loop. Two consequences for anyone
working it:

The runner reloads its own code only after a phase PR merges, so each phase is
reviewed by the code that existed before it. Phase 2 lands the new review
contract and is itself reviewed under the old one; Phase 3 is the first phase
reviewed under the new one. This is intended. Do not try to make a phase take
effect within its own review.

Because the contract is mid-migration, **the REVIEW prompt the runner sends you
is authoritative** for the JSON shape you must return — not this preamble, and
not `docs/review-contract.md`, which describes the end state. Return exactly the
keys that prompt asks for.

Reviews in this workflow are not advisory. Report every update you want before
approval. On this plan, confine findings to the two gating buckets: `blocking`
for correctness, safety, data-loss, or contract breakage, and `shouldFix` for
expected phase cleanup. **Leave `nitpick` empty.** Cosmetic notes cost a full
FIX, RUN_CHECKS, and REVIEW round under the code this plan is replacing, and a
phase can burn both its fix attempts on a variable name. If you would file a
nitpick, put it in `summary` as prose instead.

Re-reviews verify that the prior round's findings are resolved. Raise a new
finding on a re-review only if it is `blocking`.

## Phase 1: Give read-only agents `gh` and network access
Status: COMPLETE
Evidence: commit pending; `python3 -m compileall -q .` passed;
`python3 -m unittest discover -s tests -v` passed (216 tests);
`codex exec --sandbox read-only -c sandbox_read_only.network_access=true "run: gh pr view 36 --json number"`
returned `{"number":36}`; `agy --print-timeout 40m --model "Gemini 3.1 Pro (High)"
--sandbox -p "run: gh pr view 36 -R Ryan-Thurman/agent-runner --json number"`
returned `{"number": 36}`. Files changed: `.agent-runner.json`,
`agent_runner/config.py`, `tests/test_phase1_cli.py`, `docs/plan-review-contract.md`.
Note: the first Antigravity probe using the existing config order showed `-p`
consumed the next flag as the prompt, so the generated Antigravity sample and
this repo config now put `-p` at the end of role flags.

Headless `claude -p` denies unmatched permission prompts rather than asking, and
the reviewer's `readOnlyFlags` are only `--disallowedTools=Edit,Write,NotebookEdit`.
Bash is never pre-allowed, so a reviewer told to run `gh pr diff` is denied before
it starts. Grant read-only shell access so later phases can remove the diff from
the prompt. This phase is purely additive: prompts still inline the diff, so
nothing breaks if a sandbox probe comes back negative.

- Add `claude_read_only_allowed_tools()` to `agent_runner/config.py`, beside the
  existing `claude_write_allowed_tools()`. Return read-only verbs only:
  `Bash(gh pr diff:*)`, `Bash(gh pr view:*)`, `Bash(gh pr checks:*)`,
  `Bash(gh api:*)`, `Bash(git diff:*)`, `Bash(git log:*)`, `Bash(git show:*)`.
- Do **not** use `Bash(gh:*)`. That would let a reviewer comment on or merge its
  own PR. The runner owns every PR write.
- Use the `=`-joined flag form. `--allowedTools` and `--disallowedTools` are
  variadic, and the space-separated form swallows the positional prompt the
  runner appends last.
- Wire it into the `claude-opus` and `claude-sonnet` `readOnlyFlags` in
  `SAMPLE_CONFIG_TEMPLATE`, alongside the existing `--disallowedTools`.
- Probe whether `codex --sandbox read-only` can reach the network with
  `-c sandbox_read_only.network_access=true`. The documented key,
  `sandbox_workspace_write.network_access`, applies to write mode only, so this
  may not be recognized. If it works, add it to codex's `readOnlyFlags`. If it
  does not, leave codex alone, move `roles.reviewer` in this repo's
  `.agent-runner.json` to a `claude` profile, and record in `docs/usage.md` that
  codex is not a viable networked reviewer.
- Probe `antigravity`'s `--sandbox` the same way; it is the configured reviewer
  fallback.
- Record both probe results in the phase handoff, whichever way they land.

Acceptance Criteria:
- A test asserts `claude_read_only_allowed_tools()` contains no bare `Bash(gh:*)`
  and no write verb (`gh pr merge`, `gh pr comment`, `git push`).
- A test asserts reviewer `readOnlyFlags` in the sample config carry both an
  `--allowedTools=` and a `--disallowedTools=` entry, each in `=`-joined form.
- `codex exec --sandbox read-only ... "run: gh pr view <n> --json number"` is run
  by hand against a real PR and the result is recorded in the handoff.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v` pass.

## Phase 2: Replace the diff with the PR URL and shrink the review contract
Status: COMPLETE
Evidence: commit pending; `python3 -m compileall -q .` passed;
`python3 -m unittest discover -s tests -v` passed (208 tests); `rg -n
"from agent_runner\.diffs|from \.diffs|import agent_runner\.diffs|elide_diff|_published_phase_diff\(|_git_diff_staged\("
agent_runner tests` returned no matches. Files changed: `agent_runner/phase_loop.py`,
`agent_runner/jobs.py`, deleted `agent_runner/diffs.py`, deleted
`tests/test_diff_elision.py`, added `tests/test_review_contract.py`, updated review
fixtures and expectations across `tests/test_phase1_cli.py`,
`tests/test_phase3_plan.py`, `tests/test_phase5_loop.py`, `tests/test_phase6_loop.py`,
`tests/test_phase7_close.py`, `tests/test_phase8_operator.py`,
`tests/test_phase9_autofix.py`, `tests/test_review_json_extraction.py`, and updated
`docs/plan-review-contract.md`.

The REVIEW job died three times with `[Errno 7] Argument list too long: 'codex'`
because `_review_prompt` pastes the whole `gh pr diff` patch into a prompt that
becomes one argv entry. Stop passing the diff. Give agents the PR URL and let them
fetch what they need.

This phase must land atomically: `_render_github_review_body` and
`_review_fix_prompt` both index `review["recommendedFixPrompt"]`, so the moment
`_validate_review_payload` stops emitting that key they raise `KeyError`. Prompt,
validator, renderer, and fixer prompt change together or the runner breaks.

- `_review_prompt` (`agent_runner/phase_loop.py`): remove the diff. State the PR
  number and URL (reuse `format_pr_url`), branch, reviewed SHA, and base branch;
  say `gh` is authenticated and the network is available; tell the reviewer to
  fetch its own diff and that it must not post comments, push, or merge.
- Pass the checks log and the prior `review.json` as **paths**, not inlined text.
- When `autoCommit=false` there is no PR: tell the reviewer to run
  `git diff --staged` itself.
- If the reviewer cannot run shell commands at all, it must return `BLOCKED` with
  the reason in `summary`. An un-migrated config must fail loudly, not silently
  pass a PR it never read.
- Narrow the reviewer's output contract to `status`, `summary`, and `findings`
  only. State that findings are free-form strings, one per string, one line each,
  and that no other keys and no prose outside the JSON are allowed.
- `_validate_review_payload`: drop the `recommendedFixPrompt` requirement and the
  key from the returned dict. Drop `blockingIssues` and `nonBlockingIssues` from
  the returned dict and from `_normalize_review_findings`'s ingestion. Reject
  unknown top-level keys. Coerce a non-string finding item to `json.dumps(item)`.
- Keep the `PASS`-with-findings coercion and the `CHANGES_REQUESTED`-with-no-findings
  error exactly as they are; Phase 3 changes which findings they read.
- `_render_github_review_body`: collapse to a bolded verdict line, the summary, and
  the non-empty buckets. Omit empty buckets rather than printing `- None`. Drop the
  `## Reviewed SHA` and `## Recommended Fix Prompt` sections. Preserve the
  `<!-- agent-runner-review ... -->` marker verbatim; manual-merge reconciliation
  reads it.
- `_review_fix_prompt`: take a new `pr_url` argument and lead with "These are the
  findings from the code review of PR #N (<url>). Fix all of them." Delete the
  `recommendedFixPrompt` block. Add: the diff is not included, use
  `gh pr diff <url>` for context. Render findings as a grouped markdown checklist
  rather than a `json.dumps` blob.
- Delete `agent_runner/diffs.py`, `tests/test_diff_elision.py`, and the
  `elide_diff` import. Delete `_published_phase_diff` and `_git_diff_staged` if
  nothing else calls them. **Keep `jobs._bounded_prompt`** — the phase body and
  plan context still ride in argv.
- Update the `recommendedFixPrompt` fixtures across the existing test suite
  (`tests/test_phase1_cli.py`, `test_phase3_plan.py`, `test_phase5_loop.py`,
  `test_phase6_loop.py`, `test_phase7_close.py`, `test_phase8_operator.py`,
  `test_phase9_autofix.py`, `test_review_json_extraction.py` — roughly 13 sites).

Acceptance Criteria:
- A test asserts the generated review prompt contains the PR URL and does not
  contain `diff --git`.
- A test asserts `_validate_review_payload` accepts `{status, summary, findings}`
  and rejects a payload carrying `recommendedFixPrompt` or `blockingIssues`.
- A test asserts the rendered GitHub body contains no `Recommended Fix Prompt`,
  no `- None`, and still contains the `agent-runner-review` marker.
- A test asserts the fix prompt leads with the pre-prompt line and names the PR.
- `agent_runner/diffs.py` and `tests/test_diff_elision.py` no longer exist and
  nothing imports `elide_diff`.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v` pass.

## Phase 3: Stop nitpicks from churning the review loop
Status: COMPLETE
Evidence: commit pending; `python3 -m compileall -q .` passed;
`python3 -m unittest discover -s tests -v` passed (210 tests). Added focused
coverage in `tests/test_phase6_loop.py` for nitpick-only reviews passing without
a review-triggered FIX job and shouldFix-only reviews still spawning FIX. Updated
`agent_runner/phase_loop.py` prompt, re-review, gating, and review-fix checklist
behavior; updated `tests/test_review_contract.py` for the new bucket semantics.

`_review_requested_updates` flattens all three finding buckets, and
`_validate_review_payload` coerces `PASS` to `CHANGES_REQUESTED` whenever that list
is non-empty. One `nitpick` therefore spawns a FIX job, a full re-run of the check
suite, and a fresh REVIEW — which, being a new model call, finds new nitpicks. Round
two exhausts `REVIEW_FIX_ATTEMPT_LIMIT` and the phase blocks on a variable name.
This is the observed PR churn.

- Add `REVIEW_GATING_BUCKETS = ("blocking", "shouldFix")` beside the existing
  `REVIEW_FINDING_BUCKETS`.
- `_review_requested_updates` returns items from the gating buckets only. Its three
  callers — the `PASS` coercion, the `CHANGES_REQUESTED`-with-no-findings check, and
  the `if requested_updates:` branch that spawns `_run_fix` — then agree that a
  nitpick-only review is a `PASS`.
- `_review_finding_summary` and `_render_github_review_body` keep reporting **all**
  buckets. A nitpick still reaches a human on the PR comment; it just never spawns
  a job.
- Rewrite `REVIEW_RESOLVED_INSTRUCTION`: on a re-review, confirm each prior finding
  is resolved and raise a new finding only if it is `blocking`. The current text
  invites "remaining or new" updates across all buckets every round, so the finding
  set grows faster than a two-attempt budget can drain it.
- Keep the first-review instruction to list every requested update it can identify;
  that is right for gating findings and only harmful for nitpicks, which no longer
  gate.
- State the bucket semantics in the reviewer prompt — `blocking` and `shouldFix`
  send the phase back for a fix, `nitpick` is advisory and shown to a human — so
  reviewers stop filing cleanup under `shouldFix` to be safe.
- `_review_fix_prompt` lists gating findings as the must-fix checklist and nitpicks
  separately as "optional, only if trivial", so a FIX round already in flight can
  sweep them up without the pre-prompt promising work the runner never gated on.

Acceptance Criteria:
- A test asserts a review whose only findings are `nitpick` stays `PASS`, advances
  the phase to `CLOSING`, and creates no `FIX` job row for that phase.
- A test asserts a review with one `shouldFix` finding still spawns `FIX`.
- A test asserts nitpick findings still appear in the rendered GitHub comment.
- `tests/test_phase6_loop.py` already drives the multi-round review/fix/review path
  and is the right home for the first two.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v` pass.

## Phase 4: Sweep the remaining prompts and reconcile the docs
Status: PENDING

Finish the smaller prompts the earlier phases did not touch, and bring the design
and usage docs in line with the contract that now exists.

- `_review_triage_prompt`: a `--stat` is a file list, not a diff, and triage should
  stay one cheap call, so keep it inline rather than making the triage agent shell
  out. Bound `_published_phase_diff_stat` at roughly 200 lines with a
  `… +N more files` note so a very wide PR cannot reintroduce an oversized prompt.
- `_autofix_prompt` in `agent_runner/cli.py`: include `phase["pr_url"]` when the
  phase has one, and the `review.json` path when it exists, so a review-blocked
  phase reaches the fixer with the findings. Keep the blocking event message and
  the log tail.
- `docs/design.md`: rewrite correction #13 (diff elision) as "agents fetch their own
  diff via `gh`". Update correction #4 to drop the legacy-field normalization and to
  record the new bucket semantics.
- `docs/usage.md`: rewrite the review-contract paragraph — the new JSON shape, no
  `recommendedFixPrompt`, no legacy fields, which buckets gate a FIX round, the
  re-review rule, and the reviewer-profile migration note from Phase 1.
- Cross-link `docs/review-contract.md` from `docs/design.md`.

Acceptance Criteria:
- A test asserts a diff stat longer than the cap is truncated and carries the
  `+N more files` note.
- A test asserts the autofix prompt includes the PR URL when the phase has one and
  omits it cleanly when it does not.
- `docs/design.md` and `docs/usage.md` contain no reference to `recommendedFixPrompt`,
  `blockingIssues`, `nonBlockingIssues`, or `elide_diff`.
- `python3 -m compileall -q .` and `python3 -m unittest discover -s tests -v` pass.
