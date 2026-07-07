# Phase 3 Handoff: Show PR numbers on publish and merge

## Completed Work

Phase 3 is complete. The runner now extracts trailing `/pull/<number>` values from stored PR URLs and uses that number in operator-facing publish, merge, resume-merge, merge-required, and status output. Published and merged events also include the formatted PR reference.

The doc gate was handled by updating `docs/usage.md` to describe status PR formatting as `pr=#<number> (<url>)` when a stored PR URL yields a number.

## Decisions

- PR number extraction intentionally only recognizes a trailing `/pull/<number>` segment, with an optional trailing slash.
- URLs without a trailing PR number, empty strings, and `None` keep the old fallback behavior.
- The already-merged merge path prints the required concise message without the URL: `[agent-runner] phase PR #<num> already merged; skipping merge`.

## Files Changed

- `agent_runner/phase_loop.py`: added PR number extraction/formatting helpers and reused them in publish, merge, resume, and merge-required messages.
- `agent_runner/cli.py`: formats `status` publish state as `pr=#<num> (<url>)` when possible.
- `tests/test_phase_loop_pr.py`: covers PR number extraction and formatting fallbacks.
- `tests/test_phase7_close.py`: covers publish and merge stderr plus event messages in the phase close harness.
- `tests/test_phase2_storage.py`: covers numbered PR display in status output.
- `docs/usage.md`: documents the new numbered PR status display.
- `docs/plan-operator-ux.md`: marks Phase 3 complete with evidence.

## Checks Run

- `python3 -m compileall -q .`
- `python3 -m unittest discover -s tests -v`
- Review result: PASS with no blocking or non-blocking issues.

## Open Risks

- The full unittest run emitted existing `ResourceWarning` messages about unclosed SQLite connections in several tests, but all 114 tests passed.
- This closeout did not merge the phase PR or start Phase 4.

## Next-Phase Context

After the Phase 3 PR is merged and the runner starts a fresh phase branch, begin Phase 4 from `docs/plan-operator-ux.md`: opt-in one-shot fixer agent for blocked phases. Safe To Clear: Yes.
