# Agent Runner Smoke Plan

> Tiny single-phase plan used to smoke-test the runner end to end against its
> own repo. The original build plan lives in `docs/plan.md`.

## Standing rules (every phase)

- One phase per session/PR. Keep the change minimal; do not refactor unrelated
  code or start work beyond this plan.
- Ship tests with the change (stdlib `unittest`, matching the existing suite).

## Phase 1: Add a --version flag to the CLI
Status: PENDING

Expose the existing `agent_runner.__version__` through the command line. Add a
`--version` flag to the top-level argument parser in `agent_runner/cli.py`
using argparse's built-in `action="version"`, printing `agent-runner <version>`
(e.g. `agent-runner 0.1.0`).

Acceptance Criteria:
- `python3 -m agent_runner --version` prints `agent-runner 0.1.0` and exits 0.
- A unit test in `tests/` covers the flag's output and exit code.
- Existing checks pass: `python3 -m compileall -q .` and
  `python3 -m unittest discover -s tests`.
