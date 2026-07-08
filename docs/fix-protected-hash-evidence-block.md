# Fix: wrapped `Evidence:` notes no longer trip the protected-body check

## Symptom

A `CLOSE_PHASE` job did all its real work — reran checks (ruff + pytest green),
wrote the phase handoff, and rewrote the phase's `Evidence:` line — but the
runner rejected the output during post-close validation and left the worktree
dirty:

```
closer changed the protected phase body; only status/evidence
metadata write-back is allowed
```

The stored phase content hash had changed (e.g. `0eee1703…` → `76860709…`) even
though only the runner-owned evidence note was touched.

## Root cause

The runner protects the phase body with a content hash. `Status:` and the
runner-owned `Evidence:` block that follows it are excluded from that hash, so
close-phase write-back does not count as a plan body change.

The closer prompt requires evidence on a single line
(`agent_runner/phase_loop.py`: "Keep Evidence on one line"). But when a phase's
pre-existing evidence was **wrapped across two lines**:

```
Evidence: closeout docs/memory/roadmap sync; `ruff check src scripts` green;
`HOME=... python3 -m pytest -q` green.
```

`_extract_status_and_hash_lines()` / `_skip_runner_metadata()` in
`agent_runner/plan.py` only skipped the block after `Evidence:` when that block
contained a `Checks:` line. With no `Checks:` line, the wrapped continuation
line was hashed as **protected phase body**.

So when the closer collapsed the two-line evidence down to one line — exactly as
instructed — it removed a line the validator counted as body. The hash changed,
and validation blocked before commit. This was a mismatch between the closer
instruction and the parser, not a source/test failure.

## Fix

`agent_runner/plan.py` — `_skip_runner_metadata()` now treats the **entire**
contiguous non-blank block after `Evidence:` (up to the next blank line) as
runner-owned metadata, regardless of whether a `Checks:` line is present. A
wrapped, multi-line evidence note and an optional `Checks:` line are all
excluded from the protected hash.

Behavior, by evidence shape:

```
# Excluded from the hash (runner-owned metadata), body starts after the blank line:
Status: COMPLETE
Evidence: one-line note
                              <- blank line
Phase body...

Status: COMPLETE
Evidence: wrapped note that continues
onto a second line
                              <- blank line
Phase body...
```

The phase body must be separated from the evidence block by a blank line — this
was already the canonical format the closer produces.

Result: rewriting evidence (collapsing a wrapped note to one line, rephrasing,
etc.) never trips the protected-body check.

## Regression test

`tests/test_phase3_plan.py::test_wrapped_evidence_without_checks_does_not_change_phase_hash`
asserts a one-line evidence note and its wrapped two-line equivalent produce the
same phase content hash. The existing `Checks:`-block and single-line-evidence
cases still pass unchanged.

## Unblocking an already-blocked close

The parser fix prevents recurrence but does not retroactively change a hash that
was already stored under the old rule. To clear a phase blocked by this bug:

- **Fastest:** restore the evidence to its exact prior wrapped form (matching the
  stored hash) and rerun. Or
- **Durable (with this fix deployed):** re-register the plan so the phase hash is
  recomputed under the new rule (`--accept-plan-change` for that phase), then the
  collapsed one-line evidence validates.

## Related

- `docs/usage.md` — plan format contract (the `Evidence:` block definition).
- `docs/design.md` — `CLOSE_PHASE` write-back and the per-phase content hash.
