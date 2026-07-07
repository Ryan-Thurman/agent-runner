# Phase 7 Handoff: CLOSE_PHASE - the full circle

## Completed Work
Implemented the `CLOSE_PHASE` leg in `agent_runner/phase_loop.py`. Review PASS and resumed `CLOSING` phases now launch a closer job using the coder profile with the `closer` write role, validate plan write-back and handoff output, optionally commit close changes, mark the phase `COMPLETE`, and mark the plan/project `COMPLETE` when no phases remain. With `autoCommit=true`, the runner starts the next pending phase after closure.

## Decisions
The closer uses the configured coder profile with write flags rather than requiring a separate `roles.closer` config entry. The runner validates closer output before marking complete: `Status: COMPLETE`, unchanged phase body hash, and required handoff sections. With `autoCommit=false`, completion stops before auto-starting the next phase so uncommitted local review work is not mixed into the next phase.

## Files Changed
- `agent_runner/phase_loop.py`
- `agent_runner/plan.py`
- `agent_runner/storage.py`
- `README.md`
- `docs/usage.md`
- `docs/plan.md`
- `tests/test_phase1_cli.py`
- `tests/test_phase3_plan.py`
- `tests/test_phase5_loop.py`
- `tests/test_phase6_loop.py`
- `tests/test_phase7_close.py`
- `.acc/phases/agent-runner-build-plan/phase-07-handoff.md`

## Checks Run
- `python3 -m py_compile agent_runner/phase_loop.py agent_runner/plan.py agent_runner/storage.py`
- `python3 -m unittest tests.test_phase7_close -v`
- `python3 -m unittest tests.test_phase3_plan tests.test_phase5_loop tests.test_phase6_loop -v`
- `python3 -m unittest discover -s tests -v`

## Open Risks
`Evidence:` lines are intentionally excluded from phase hashing only when placed directly after the `Status:` marker. Closure commits cannot embed their own final SHA in the same commit without changing that SHA; the final commit SHA should be taken from git/PR metadata.

## Next-Phase Context
Phase 8 should build pause/resume/logs behavior on top of the now complete phase state machine. Pay special attention to resume behavior for orphaned `CLOSE_PHASE` jobs and to log display for `close_phase.log`, `close_phase-prompt.md`, and handoff paths.
