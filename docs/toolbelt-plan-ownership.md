# dev-lite-workflow assumes it owns the plan document

**For:** agent-toolbelt maintainers
**From:** agent-runner (headless orchestrator that shells into agent CLIs)
**Status:** agent-runner has a local workaround; the underlying conflict is upstream.
Partially addressed 2026-07-09: the new `auto-agent-contract` pack documents plan
ownership for *plan authors* (`references/plan-format.md`: `Status:`/`Evidence:`
are runner-owned, the phase body is protected). The `dev-lite-workflow` commands
are unchanged, so the acceptance criteria below are still unmet and the local
override stays.

## Summary

The `dev-lite-workflow` commands instruct the agent to record progress in the
Implementation Plan document. That is correct for an interactive session, where
the plan is the only durable state. It is wrong for a headless orchestrator,
where the plan is immutable input and progress lives in the runner's database.

When agent-runner invokes `/dev-implement-task`, the coder follows the command
file, edits the plan, and the phase can never close.

## What happened

agent-runner parses a plan into `## Phase N: <title>` sections and hashes each
phase body. The `Status:` and `Evidence:` lines directly under the heading are
runner-owned metadata excluded from that hash; a dedicated closer job writes
them once the phase is reviewed and accepted. Everything else in the body is
protected: if its hash changes mid-phase, the spec that CHECK, REVIEW, and FIX
are working from is no longer the spec that IMPLEMENT was given, so the runner
blocks the phase.

In a real run (`racc`, phase 2), the coder invoked via `/dev-implement-task`
committed this alongside its implementation:

```diff
 ## Phase 2: Dashboard Setup-Command Confirmation
+Status: COMPLETE 2026-07-09
+Evidence: desktop setup confirmation tests, `pnpm --dir apps/desktop test`, ...

 Add the dashboard path for setup-command trust. The daemon and CLI already
 enforce `trusted_setup_hash` through `run.start.confirmed_setup_command`; the
-desktop currently surfaces `setup_confirmation_required` as guidance but cannot
-confirm and retry itself.
+desktop now handles `setup_confirmation_required` by confirming and retrying the
+exact daemon-provided setup command.
```

The `Status:`/`Evidence:` insertion was harmless — that region is runner-owned
metadata and is excluded from the hash. The damage is the third hunk: having
been told to keep the plan current, the agent also reflowed the phase body into
past tense to reflect that the work was now done. That is a protected-body
change. The phase failed close-phase validation, blocked, and needed manual
repair. Four jobs (CHECK, REVIEW, FIX, CLOSE_PHASE) ran against a phase that
could not close.

This is not an agent misbehaving. It did exactly what the command file said.

## Where the instructions are

`commands/dev-implement-task.md` — the one agent-runner actually invokes:

- Rules: *"Update the Implementation Plan document before and after the task so
  the current task, status, evidence, checks, next step, and resume
  instructions are durable."*
- Required Steps 7: *"Update the plan document to mark the task `In Progress`."*
- Required Steps 11: *"Update the plan document with task status, evidence,
  checks, next step, and resume instructions."*
- Output section: `## Plan Document Updates`

The same assumption runs through the rest of the pack:

| Command | Instruction |
| --- | --- |
| `dev-fix-review-issues.md` | *"Update the Implementation Plan with fix status, evidence, checks, remaining issues, next step, and resume instructions."* |
| `dev-start-phase.md` | *"Update the Implementation Plan current state with the selected phase…"* |
| `dev-pr-review.md` | *"Update the Implementation Plan with PR readiness result, branch/PR notes…"* |
| `dev-phase-review.md` | Review checklist asks: *"Was the Implementation Plan updated with completed task status, evidence, review findings, next step, and resume instructions?"* |

`dev-phase-review.md` is the worst of these for a headless caller. A reviewer
that asks whether the plan was updated will raise a blocking finding when the
coder correctly left it alone, and the resulting FIX job will then edit the
plan — turning a clean run into a blocked one via the review loop.

## What we want

A way for a caller to declare that the plan document is read-only, without
forking the pack. Three options, roughly in order of preference:

**1. A plan-ownership contract the commands honor.** Have each command check for
a declared owner of the plan document and skip its plan-write steps when the
caller owns it — e.g. a `planDocument: { owner: "caller" | "agent" }` key in the
pack's config, or an `ATB_PLAN_DOCUMENT_OWNER=caller` environment variable. The
plan-write steps become conditional rather than unconditional. This keeps one
set of commands and lets the interactive and headless loops share them.

**2. A `headless-agent-contract` pack.** Ship a sibling pack whose commands are
the same minus the plan-document steps, minus the interactive prompts, and with
an explicit "the orchestrator owns progress state" preamble. Headless callers
opt into that pack. More duplication, but no conditional logic in the command
files, and it leaves room for headless-only rules (bounded output, no
interactive confirmation, deterministic final message).

**3. At minimum, a documented guarantee about *where* the agent may write.**
If the plan-write steps stay unconditional, scope them: say that an agent may
only add or rewrite a `Status:` / `Evidence:` block directly under a phase
heading, and must never edit phase prose — including rewording it to past tense
once the work is done. That single sentence would have prevented this failure,
because the metadata write was already tolerated. This is the cheapest fix and
is worth doing regardless of whether option 1 or 2 lands.

Whatever the mechanism, `dev-phase-review.md`'s "was the plan updated?" check
needs to be conditional on the same signal. A reviewer enforcing a rule the
coder was told to skip is worse than either behavior alone.

## What agent-runner does today

As of 2026-07-09 we also patched the local install: the plan-write steps and
`Plan Document Updates` output sections are removed from
`.claude/commands/dev-*.md`, `.atb/skills/dev-lite-workflow/`, and
`.atb/templates/dev-*.md`, and the runner-owned rule is stated once in
`CLAUDE.md` and `AGENTS.md` (below the toolbelt marker block), covering both
the Claude Code and Codex writer surfaces. All of these locations are
untracked, so this is a local patch, not a fork — and a toolbelt reinstall or
update restores the upstream skill text, which is why the layers below stay.

We inject an explicit override into the IMPLEMENT and FIX prompts, after the
`/dev-implement-task` invocation:

> Do not edit `<plan path>`. The runner owns that file and tracks phase status
> itself. Never mark a phase complete, add status or evidence notes, or reword
> the phase body to past tense once the work is done — a phase whose body no
> longer matches the registered text fails its protected-body check and blocks
> the run. If a project command tells you to update the plan document, skip
> that step.

This works, but it means every headless caller has to know the pack's internals
well enough to contradict them, and it relies on the model resolving a direct
conflict between the command file and the prompt in the prompt's favor. A
first-class opt-out would be more robust than a louder instruction.

We also now re-hash the phase body after every writer job, so drift is caught at
the job that caused it rather than four jobs later. That is our bug to fix and
we have fixed it; it does not depend on anything upstream.

## Implementation note

`dev-lite-workflow` is triplicated in agent-toolbelt and the copies are
byte-identical-checked by `scripts/check-skill-sync.sh`. Any edit to the command
files has to land in all three copies or CI fails.

## Acceptance criteria

For agent-runner to drop its local override:

- A documented, machine-readable way to declare the plan document read-only.
- With it set, `/dev-implement-task` and `/dev-fix-review-issues` make no edits
  to the plan document, and their `## Plan Document Updates` output section is
  omitted or explicitly reports "skipped: caller owns the plan document".
- With it set, `/dev-phase-review` does not raise a finding about the plan
  document not being updated.
- Unset, current interactive behavior is unchanged.
