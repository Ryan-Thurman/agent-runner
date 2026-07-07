# Phase 5 Handoff: Review triage - route simple reviews to Sonnet, behavioral to Opus

## Completed Work

Phase 5 is complete. The runner now supports optional `reviewTriage` config, runs a read-only `TRIAGE` job before `REVIEW` when configured, routes simple reviews to the configured simple profile and behavioral reviews to the configured complex profile, and fails safe to the complex profile when triage fails or returns invalid JSON. The prior review blocker was fixed by using published PR file metadata as the stat fallback when `gh pr diff --stat` is unavailable.

The doc gate was handled by updating `docs/usage.md` and `README.md` for `reviewTriage`, including the generated default config example.

## Decisions

- `reviewTriage.simple` and `reviewTriage.complex` must both name configured profiles; unknown names are config errors.
- Triage is a one-shot read-only job using the simple profile and a stat summary, not the full patch.
- Any triage problem records the reason and routes to the complex profile without failing the phase.
- Existing reviewer fallbacks still run after the chosen primary review profile.

## Files Changed

- `agent_runner/config.py`: added `ReviewTriageConfig`, validation, and default config generation with pinned Sonnet/Opus routing.
- `agent_runner/jobs.py`: added `triage` to read-only roles.
- `agent_runner/storage.py`: added `TRIAGE` job type and orphan reset behavior.
- `agent_runner/phase_loop.py`: added review triage prompt, stat summary lookup/fallback, tier parsing, event/stderr output, and review profile selection.
- `README.md` and `docs/usage.md`: documented `reviewTriage` config and routing behavior.
- `tests/test_phase1_cli.py`, `tests/test_phase3_plan.py`, `tests/test_phase5_loop.py`, `tests/test_phase6_loop.py`, `tests/test_phase7_close.py`, `tests/test_phase8_operator.py`, `tests/test_phase9_autofix.py`: covered config validation/defaults and loop behavior with and without triage.
- `docs/plan-operator-ux.md`: marked Phase 5 complete with evidence.

## Checks Run

- `python3 -m compileall -q .`
- `python3 -m unittest discover -s tests -v`
- Review result: PASS with no blocking or non-blocking issues.

## Open Risks

- The full unittest run emitted existing `ResourceWarning` messages about unclosed SQLite connections, but all 127 tests passed.
- This closeout did not merge the phase PR, force-push, delete branches, or start future phase work.

## Next-Phase Context

After Phase 5 is merged, the five-phase `docs/plan-operator-ux.md` plan has no defined next phase. Safe To Clear: Yes.
