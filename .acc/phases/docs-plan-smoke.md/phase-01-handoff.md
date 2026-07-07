## Completed Work
- Added the top-level `--version` argparse action in `agent_runner/cli.py`, printing `agent-runner 0.1.0` from `agent_runner.__version__`.
- Added `tests/test_phase1_cli.py::Phase1CliTests.test_version_flag_prints_package_version` to cover stdout, stderr, and exit code.
- Updated CLI usage docs in `README.md` and `docs/usage.md` for the new flag.
- Closed `docs/plan-smoke.md` phase 1 with `Status: COMPLETE` and an evidence line.

## Decisions
- Used argparse's built-in `action="version"` at the top-level parser so `--version` exits before any subcommand is required.
- Treated this as doc-impacting because it adds a public CLI flag.
- No future phase work was started.

## Files Changed
- `agent_runner/cli.py`
- `tests/test_phase1_cli.py`
- `README.md`
- `docs/usage.md`
- `docs/plan-smoke.md`
- `.acc/phases/docs-plan-smoke.md/phase-01-handoff.md`

## Checks Run
- `python3 -m agent_runner --version` prints `agent-runner 0.1.0` and exits 0.
- `python3 -m compileall -q .`
- `python3 -m unittest discover -s tests -v`
- Review result: PASS with no blocking or non-blocking issues.

## Open Risks
- The unittest run emitted pre-existing `ResourceWarning: unclosed database` messages from `agent_runner/storage.py:643`, but the suite completed successfully.
- No open functional risks for phase 1 are known.

## Next-Phase Context
- Next concrete action: none for this smoke plan; phase 1 is complete and the plan has no later phases.
- Safe To Clear: Yes; the plan status, evidence, docs decision, checks, review result, and file list are recorded here.
