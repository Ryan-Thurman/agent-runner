# The review contract (`agent-runner`)

Reference design for what the runner sends a reviewer, what a reviewer must send back,
and which findings send a phase back around the loop. Companion to `docs/design.md`;
the executable plan that implements it is `docs/plan-review-contract.md`.

## Why this exists

Three problems, one root cause: the runner was doing work on the agents' behalf that the
agents are better placed to do themselves.

**REVIEW died with `[Errno 7] Argument list too long: 'codex'`.** `_review_prompt`
(`agent_runner/phase_loop.py`) shelled out to `gh pr diff <url> --patch` and pasted the
entire patch into the prompt string, and `jobs._agent_argv` appends that string as the
final argv entry. On one phase PR the patch was 1.26 MB of rebuilt minified bundles, the
prompt reached 1,325,823 bytes, and macOS `execve` caps argv plus environ at 1 MiB. The
reviewer never started; the job failed three times.

Commit `5be094d` treated the symptom by shrinking the payload — `diffs.elide_diff` dropped
oversized per-file hunks, `jobs._bounded_prompt` truncated the remainder. Both are
workarounds for putting the diff in the prompt at all. The reviewer has `gh` and a
checkout. It can fetch its own diff, at whatever granularity it decides it needs, and never
touch `execve`'s limit.

**The reviews were verbose.** `_render_github_review_body` emitted `# Phase 4 Review:
CHANGES_REQUESTED`, `## Summary`, `## Reviewed SHA`, `## Findings` with a `### blocking` /
`### shouldFix` / `### nitpick` heading each (printing `- None` under the empty ones), and
finally `## Recommended Fix Prompt`. The reviewer's JSON contract mandated
`recommendedFixPrompt` plus legacy `blockingIssues` / `nonBlockingIssues` mirrors of
`findings`. `recommendedFixPrompt` is a reviewer authoring a prompt for the fixer — work the
runner is better positioned to do itself, from the findings it already has.

**The review loop churned.** `_review_requested_updates` flattened *all three* finding
buckets, and `_validate_review_payload` coerced `PASS` → `CHANGES_REQUESTED` whenever that
list was non-empty. So one `nitpick` — "consider renaming this variable" — spawned a `FIX`
job, a full re-run of the check suite, and a fresh `REVIEW`. The re-review was a new model
call against a changed tree, and it found new nitpicks, because a model asked "any polish?"
essentially always answers yes. Round two exhausted `REVIEW_FIX_ATTEMPT_LIMIT = 2` and the
phase landed `BLOCKED` over a variable name. Two prompt lines fed it: "list every requested
update you can identify instead of saving issues for later rounds", and
`REVIEW_RESOLVED_INSTRUCTION`'s invitation to report "remaining **or new**" updates across
all buckets, every round.

## The contract

A reviewer prints this and nothing else — no prose outside the JSON, no other keys:

```json
{
  "status": "PASS | CHANGES_REQUESTED | BLOCKED",
  "summary": "one or two sentences",
  "findings": {
    "blocking":  ["path:line — problem; required change"],
    "shouldFix": [],
    "nitpick":   []
  }
}
```

Findings are free-form strings, one finding per string, one line each.

**Bucket semantics.** `blocking` and `shouldFix` gate: either one sends the phase to `FIX`.
`nitpick` is advisory — it is posted to the PR for a human and never spawns a job. The
reviewer is told this, so it stops filing cleanup under `shouldFix` to be safe.

`PASS` is valid only when `blocking` and `shouldFix` are both empty. A `PASS` carrying gating
findings is coerced to `CHANGES_REQUESTED`; a `CHANGES_REQUESTED` carrying none is an error.

**Round 2 and later.** The first review lists every requested update it can identify. A
re-review confirms each prior finding is resolved and raises a new finding *only if it is
`blocking`*. Without this bound, each round is an independent full review, so the finding set
grows faster than a two-attempt budget can drain it.

## No diff in any prompt

Prompts carry the **PR URL**. Every agent — coder, reviewer, fixer — uses `gh` to fetch
whatever it needs. The reviewer prompt states the PR number and URL, the branch, the reviewed
SHA, and the base branch, and says `gh` is authenticated and the network is available.
Checks output and the prior `review.json` are passed as **paths**, not inlined; the agent can
read them.

When `autoCommit=false` there is no PR, and the reviewer is told to run `git diff --staged`
itself.

If an agent cannot run shell commands at all — an old config without a reviewer shell
allowlist, see below — it returns `BLOCKED` with the reason in `summary`. An un-migrated
config must fail loudly, not silently pass a PR it never looked at.

Consequence: `diffs.elide_diff` has no callers and is deleted. `jobs._bounded_prompt` stays —
the phase body and plan context still ride in argv, so an ARG_MAX backstop is still earned,
but the largest prompt the runner can emit is now bounded by the plan, not by PR size.

## Agent permissions

Headless `claude -p` **denies** unmatched permission prompts rather than asking, and the
reviewer's `readOnlyFlags` were only `--disallowedTools=Edit,Write,NotebookEdit`. Bash was
never pre-allowed, so a reviewer told to run `gh pr diff` is denied before it starts.

`claude_read_only_allowed_tools()` (beside the existing `claude_write_allowed_tools()` in
`config.py`) returns read-only verbs only:

```
Bash(gh pr diff:*),Bash(gh pr view:*),Bash(gh pr checks:*),Bash(gh api:*),
Bash(git diff:*),Bash(git log:*),Bash(git show:*)
```

Deliberately **not** `Bash(gh:*)` — that would let a reviewer comment on or merge its own PR.
The runner owns every PR write. Use the `=`-joined flag form: `--allowedTools` and
`--disallowedTools` are variadic, and the space-separated form swallows the positional prompt
the runner appends last.

`codex --sandbox read-only -c sandbox_read_only.network_access=true` was probed against
PR #36 and successfully ran `gh pr view 36 --json number`. If a future Codex build rejects
that key, move the reviewer role to a network-capable `claude` profile. `antigravity`'s
`--sandbox` ("terminal restrictions enabled") was also probed successfully when `gh` was
given an explicit `-R owner/repo`; keep `-p` at the end of the role flags so it consumes the
runner prompt, not another flag.

## The runner posts the review, not the reviewer

A reviewer with `gh` could post its own comment. It should not:

- The runner must parse the JSON regardless — the verdict is the only input to the state
  machine (`PASS` → `CLOSING`, `CHANGES_REQUESTED` → `FIX`, `BLOCKED` → block).
- The runner posts **after** validation and only for non-`PASS`. If the reviewer posted first,
  an unparseable review would leave a comment that drove no transition: the PR says "changes
  requested" while the phase sits `BLOCKED` on invalid JSON. Today they cannot diverge.
- The comment carries `<!-- agent-runner-review plan=… phase=… review_job=… reviewed_sha=… -->`,
  and `review_job` is a database id the agent does not have.
- `_run_agent_job_with_fallbacks` re-runs REVIEW on a quota failure. Each attempt that got far
  enough to shell out would leave its own comment; the runner posts once, from the attempt that
  won.

Inline PR line comments (`POST /repos/{owner}/{repo}/pulls/{n}/comments`) are a real
improvement to human review UX and are **not** a churn fix: the fixer reads findings from
`review.json` via its prompt, never from the PR. Out of scope here, and they would want
`{file, line, body}` findings rather than free-form strings.

## The posted comment

```
<!-- agent-runner-review plan=… phase=4 review_job=12 reviewed_sha=abc123def456 -->
**Phase 4 review — CHANGES_REQUESTED** · reviewed `abc123def456`

<summary>

**Blocking**
- agent_runner/phase_loop.py:1102 — status is read before the lock is acquired; acquire it first.

**Should fix**
- …
```

Empty buckets are omitted rather than printed as `- None`. No `## Reviewed SHA` section, no
`## Recommended Fix Prompt`. A `BLOCKED` review with no findings prints `No findings.` The
marker comment is preserved verbatim — manual-merge reconciliation reads it.

## The fixer prompt

Leads with an explicit pre-prompt:

> These are the findings from the code review of PR #N (<url>). Fix all of them.

Then the existing rules — root cause not symptom, no regressions, run the full suite, no
future phases, the `REVIEW_FIX_ATTEMPT_LIMIT` warning, the `reviewed_sha` baseline rule — plus:
the diff is not included; use `gh pr diff <url>` or `git diff` for context.

Findings render as a grouped markdown checklist, not a `json.dumps` blob: the fixer is reading
a to-do list, not parsing one. `blocking` and `shouldFix` are the must-fix list the pre-prompt
refers to. `nitpick` items are listed separately as "optional, only if trivial", so a FIX round
already in flight can sweep them up without the pre-prompt promising work the runner never
gated on.
