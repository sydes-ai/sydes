# DiffGenome runtime evidence in Sydes

Sydes reads one thing from DiffGenome: the `runtime` section (`diffgenome-runtime/1`) of
`diffgenome-change.json`. It never reads traces, mechanics, or genome output to make
decisions. The code lives in `sydes/behavioral/runtime.py` (`RuntimeEvidence`).

## What the contract feeds

| Sydes stage | runtime input | effect |
|---|---|---|
| reachability | observed application→application call edges, resolved to index symbols (file, name, line) | added to `structural.call_edges` as `diffgenome:observed-runtime`, which gives the `runtime_observed_reachability` strategy; static duplicates are skipped and test frames are never callers |
| test mapping | exact tests and subtests per changed function | supporting evidence; `missing_test_mapping` counts as answered when tests were observed |
| verification gaps | `function_not_executed`, `branch_outcome_not_observed`, `branch_not_evaluated`, `stand_in_reached` | `VerificationGap(source="runtime")`, counted in the summary; **the verdict does not change** |
| AI recovery | executed functions with application entry roots | an unresolved changed symbol is answered only when it matches exactly one executed function that has an entry root; recovery is still asked about the rest, and the recovery reason names them |
| report | per-function ✓/✗, tests, "entered via", exits, gaps, `not_reported` | the "Runtime evidence" section in the terminal and behavioral reports |
| prompts (opt-in) | compact runtime text | only with `--behavioral-context on` |

Entry roots are the topmost non-test application frames, plus intermediate frames that tests
enter directly. "Not executed" always means not executed within `universe.test_scope`.

## Install

Sydes depends on `diffgenome>=0.1.7` from PyPI (the minimum supported version: 0.1.4 skipped
pytest configs in subdirectories and 0.1.5 broke `filterwarnings = error` projects; neither can
be resolved) (Python 3.12+; on 3.11 the behavioral map reports
unavailable). The `diffgenome` command is found on PATH, else next to Sydes' interpreter;
`SYDES_DIFFGENOME_COMMAND` overrides both.

## CLI

- `--behavioral-map diffgenome --runtime-evidence auto` (default `auto`): uses the contract when present.
  In live mode, DiffGenome runs between `merge-base` and `HEAD` before analysis.
- `--runtime-evidence off`: off.
- `--behavioral-context on`: also adds the runtime text to the model prompts.

The genome is not needed: an artifact without a `genome` section works fully.

## Validation (deterministic, `--llm-policy never`, `auto` vs `off`)

| PR | unresolved changed symbols (off → auto) | runtime gaps | still sent to recovery |
|---|---|---|---|
| Baserow #5507 | 8 → 3 | 5 | RestoreViewRowOperationType, `__init__`, `ready` |
| Baserow #5972 | 10 → 9 | 9 | 9 symbols (listed in the recovery reason) |
| Baserow #5666 | 2 → 1 | 11 | `handle` |
| simplebank #103 | 30 → 30 | 9 | runtime answers 5 of the recovery questions |

The verdict is unchanged in all four. Entry roots match the code: #5666 enters via
`Command.handle`, and #5507 via `ActionHandler.undo`/`redo`. Unit tests are in
`tests/test_behavioral_context.py`.
