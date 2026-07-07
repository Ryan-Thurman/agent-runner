# Agent Runner Live Logs Plan

> Three-phase plan for adding interactive, tail-like job output to
> `agent-runner run`. This intentionally skips a Typer migration and keeps the
> CLI stdlib-only; the user-facing improvement is live visibility plus clear
> terminal coloring.

## Context for the implementing agent

`agent-runner` is a local Python 3 CLI in `agent_runner/`, currently stdlib-only
and based on `argparse`. Read `docs/design.md`, `docs/usage.md`, and the job
execution code in `agent_runner/jobs.py` before starting. The existing job
engine already writes complete stdout/stderr output to per-phase log files; this
plan adds a bounded live preview to stderr while preserving those log files as
the source of truth.

To dogfood this plan with the runner itself, set `.agent-runner.json` `planPath`
to `docs/plan-live-logs.md`. Keep one phase per runner session/PR.

## Standing rules (every phase)

- Keep the project stdlib-only. Do not add Typer, Rich, Click, colorama, or any
  other dependency in this plan.
- Preserve machine-readable behavior: stdout remains reserved for command
  payloads such as `status` JSON and `logs` output; live job display goes to
  stderr only.
- Full logs must remain complete and untruncated on disk. Truncation, coloring,
  and prefixes apply only to the live terminal preview.
- Use ANSI color only when appropriate for an interactive terminal, with a
  deterministic way for tests to force color on or off.
- Keep output readable when redirected, in CI, or under tests: no raw color
  escapes unless color is explicitly forced.
- Add focused `unittest` coverage and keep the existing suite green.

## Phase 1: Terminal live-log formatter
Status: PENDING

Add a small internal formatter for live job lines without wiring it into
subprocess execution yet.

- Create a minimal helper in `agent_runner/jobs.py` or a small adjacent module
  if that keeps the code clearer. It should format one process-output line into
  a stderr preview line with:
  - a stable prefix based on job context, for example `codex coding: ...`,
    `claude reviewing: ...`, `claude fixing: ...`, `checks checking: ...`;
  - bounded line length for the live preview, with a clear truncation marker;
  - color support for the prefix and/or job verb using raw ANSI escape codes.
- Add a color mode resolver, preferably `auto | always | never`, controlled by
  an environment variable such as `AGENT_RUNNER_COLOR`. Default `auto` should
  emit color only when `sys.stderr.isatty()` is true and `NO_COLOR` is not set.
- Keep labels derived from existing job metadata:
  - `IMPLEMENT` + coder -> `coding`
  - `REVIEW` + reviewer -> `reviewing`
  - `FIX` + coder -> `fixing`
  - `CLOSE_PHASE` + closer -> `closing`
  - `RUN_CHECKS` -> `checking`
- Keep vendor/profile names human-sized. Use `profile.name` for agent jobs and
  `checks` for shell checks; do not parse command paths unless needed.
- Treat empty or whitespace-only lines conservatively: either pass through a
  single blank preview or suppress repeated blank spam, but document the choice
  in the tests.

Acceptance Criteria:
- Unit tests cover label generation for IMPLEMENT, REVIEW, FIX, CLOSE_PHASE,
  and RUN_CHECKS.
- Unit tests cover truncation: live preview is shortened, while the original
  text remains available to the caller unchanged.
- Unit tests cover color mode behavior for `auto`, `always`, `never`, and
  `NO_COLOR`.
- Existing tests still pass with `python3 -m compileall -q .` and
  `python3 -m unittest discover -s tests`.

## Phase 2: Stream live previews during jobs
Status: PENDING

Wire the formatter into the existing job process pumps so users see output while
the subprocess is running.

- Extend `_run_process` so its stdout/stderr pump can optionally call a live
  preview callback after writing each chunk to the log file.
- For `run_agent_job`, build the live preview context from `job_type`, `role`,
  and `profile.name`, then pass it to `_run_process`.
- For `run_checks_job`, pass a checks-specific live preview context for each
  check command.
- Preserve existing capture semantics:
  - `stdout` output capture must still collect the exact child stdout.
  - `structured-stdout` must remain parseable and unaffected by preview output.
  - `last-message-file` must remain unchanged.
  - Both stdout and stderr must still be written completely to the `.log` file.
- Avoid interleaving unreadable output. Since stdout and stderr are pumped on
  separate threads, protect terminal writes with the same kind of lock used for
  log writes or another small shared lock.
- Add a disable switch such as `AGENT_RUNNER_LIVE_LOGS=0` for non-interactive
  runs. Default should be enabled so `agent-runner run` visibly streams work.

Acceptance Criteria:
- A fake-agent test proves live preview lines are emitted to stderr while the
  complete stdout/stderr still lands in the log file.
- A fake-agent test proves long output is truncated only in stderr preview, not
  in the log file or captured stdout.
- A checks-job test proves shell check output uses a `checks checking:` style
  prefix.
- Tests assert no ANSI escapes appear when color is disabled or stderr is not a
  TTY, and ANSI escapes appear when color is forced.
- Existing timeout, interrupt, spawn-failure, and output-capture tests still
  pass.

## Phase 3: Operator polish, docs, and dogfood
Status: PENDING

Polish the operator-facing behavior, document it, and run the feature through
the runner itself.

- Review the start/spawn messages in `agent_runner/jobs.py` so live previews
  read cleanly around existing `[agent-runner] starting ...` and `log: ...`
  lines. Keep those existing lifecycle messages, but avoid redundant noise.
- Update `docs/usage.md` and `README.md` with:
  - what live logs look like;
  - where full logs are still stored;
  - how to disable live previews with `AGENT_RUNNER_LIVE_LOGS=0`;
  - how to control color with `AGENT_RUNNER_COLOR=auto|always|never` and
    `NO_COLOR`.
- Dogfood this plan by running at least one phase through `agent-runner run`
  after Phase 2 lands, so the terminal transcript exercises real live previews
  for coding, checks, and reviewing where possible.
- Add or update a short note in the relevant phase evidence with checks run and
  any manual observations from the dogfood run.

Acceptance Criteria:
- Documentation clearly distinguishes live truncated previews from complete
  phase log files.
- A manual dogfood note records the command used, whether colors appeared as
  expected, and where the complete log file was written.
- The complete suite passes:
  `python3 -m compileall -q .` and
  `python3 -m unittest discover -s tests`.
- No Typer or other third-party dependency is added.
