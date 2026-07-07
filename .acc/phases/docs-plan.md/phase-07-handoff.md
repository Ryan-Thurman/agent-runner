# Phase 7 Handoff: CLOSE_PHASE - the full circle

## Completed Work
Implemented and accepted the `CLOSE_PHASE` path for the phase runner. The runner now launches the closer after a PASS review, validates close write-back before marking completion, supports auto-commit close work, completes the plan/project when no phases remain, and auto-starts the next pending phase when configured.

Added close preflight guards so auto-commit close only proceeds when stored PR review metadata still matches the current branch and local `HEAD`, preventing stale or wrong-branch close work from being marked complete.

Doc gate: doc-impacting close behavior and safety checks are documented in `docs/usage.md`.

## Decisions
The closer uses the coder profile with write-capable close flags instead of adding a separate closer role. Close validation keeps marker-only plan edits out of phase hash comparisons so `Status:` and adjacent `Evidence:` updates do not invalidate the accepted phase body.

The accepted non-blocking review notes are carried forward rather than fixed in this close: evidence-line validation is recommended but not blocking, the doc-gate fallback is not enforced when no docs changed, and Python 3.14 emitted sqlite3 `ResourceWarning` messages during the full test run.

## Files Changed
- `.acc/phases/agent-runner-build-plan/phase-07-handoff.md`
- `.acc/phases/docs-plan.md/phase-07-handoff.md`
- `.agent-runner.json`
- `README.md`
- `agent_runner/phase_loop.py`
- `agent_runner/plan.py`
- `agent_runner/storage.py`
- `docs/plan.md`
- `docs/usage.md`
- `tests/test_phase1_cli.py`
- `tests/test_phase3_plan.py`
- `tests/test_phase5_loop.py`
- `tests/test_phase6_loop.py`
- `tests/test_phase7_close.py`

## Checks Run
- `python3 -m compileall -q .`
- `python3 -m unittest discover -s tests -v` - 76 tests passed.

## Open Risks
Close validation still does not enforce that the closer wrote an `Evidence:` line, although the prompt and docs require it.

The doc-gate fallback text is still not enforced when no docs changed. This phase did update `docs/usage.md`, so the accepted phase is covered.

The full test output included Python 3.14 sqlite3 `ResourceWarning` messages from unclosed database connections even though tests passed.

## Next-Phase Context
Next concrete action: begin Phase 8 from `docs/plan.md` after this closure commit, focusing on resume, pause, logs, and end-to-end dogfood. Do not revisit the accepted Phase 7 close logic unless one of the recorded non-blocking review risks is explicitly pulled into Phase 8 scope.
