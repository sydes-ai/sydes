# Behavioral evidence from DiffGenome (`--behavioral-map diffgenome`)

Experimental, off by default. Sydes' structural analysis says what *may* be connected.
[DiffGenome](../../../diffgenome) runs the target's own isolated unit tests under a sandbox,
records what actually executed, reconstructs behavior across mock/fake seams with a graded
join lattice, and emits a bounded change-centred artifact, `diffgenome-change/1`. This
integration consumes that artifact and merges it with the structural view **without
flattening provenance**. It never changes the verdict.

## Evidence model

```
CBM / STATIC            what may be connected            (Sydes, as before)
DiffGenome OBSERVED     what actually executed            in some isolated test
DiffGenome COMPOSED     what can be reconstructed         across a stand-in seam, graded
                                                          SYMBOL < ARG_SHAPE < VALUE < STATE
GAPS / BOUNDARIES       where neither gives enough        gap · unresolved · external
```

Every merged edge (`BehavioralEdge.evidence_class`) is exactly one of:

| class | meaning |
|---|---|
| `OBSERVED_RUNTIME` | a real caller called a real callee in one execution; `static_corroborated` when the structural trace has the same hop, `runtime_only` when it does not |
| `COMPOSED_STATE` / `COMPOSED_VALUE` / `COMPOSED_ARG_SHAPE` / `COMPOSED_SYMBOL` | reconstructed across a seam, at that grade; `state` and `exit` carry the STATE and exit verdicts |
| `STATIC_ONLY` | a structural hop no execution or reconstruction supports: *possible*, kept, never promoted |
| `GAP` | the real target was never executed by any test (or is a declaration with no body) |
| `UNRESOLVED` | a stand-in that could not be attributed to an in-repo symbol |
| `EXTERNAL` | a true external boundary, kept substituted |

There is no single confidence number. The classes are the labels the report shows.

## Matching static and runtime worlds

A structural step (file + symbol) matches a DiffGenome node when the files agree and the
step's symbol equals the node's qualified name or its trailing component
(`Server.createTransfer` ↔ `go:api.Server.createTransfer`). Nothing language- or
framework-specific: DiffGenome's adapters own that. Changed symbols in the diff that the
behavioral run did not see are listed as `changed_symbols_unmatched`, never dropped.

## Where it runs

`sydes verify-change … --behavioral-map diffgenome --behavioral-args '<runtime config>'`
runs after the structural pipeline and before AI recovery
(`sydes.behavioral.attach.attach_behavioral_evidence`). It invokes `diffgenome change` as a
subprocess (`SYDES_DIFFGENOME_COMMAND` or `diffgenome` on PATH), passes the change as
`merge_base..head`, forwards `--behavioral-args` opaquely, and reads back
`diffgenome-change.json`. `--behavioral-artifact <path>` consumes a pre-built artifact
instead (CI artifacts, deterministic replays). `--behavioral-probes N` is the probe budget:
0 by default, existing tests only, no LLM call from DiffGenome.

The result carries `behavioral: BehavioralEvidence` (`sydes.behavioral.models`), rendered as
"Behavioral effect (executed evidence)" in the terminal report after "System impact", and as a
`### Behavioral effect` Markdown section (`python -m sydes.behavioral.render
sydes-result.json`) that a PR-comment renderer appends.

## Fallback

Every failure is explicit and isolated:

| situation | result |
|---|---|
| `--behavioral-map off` (default) | no field, no section |
| DiffGenome not installed, no runtime config, unsupported runtime, no tests, collector or test-command failure, timeout, malformed artifact | `behavioral.status = "unavailable"` with the reason; one note; the report prints *"Behavioral execution evidence unavailable: … nothing here is evidence of no impact"* |
| merge bug | caught, reported as unavailable; structural result untouched |

Missing behavioral evidence is never rendered as "no impact", and the verdict, risk and
structural sections are byte-identical with or without it.

## What executed evidence may change (and what it may not)

| consumer | effect |
|---|---|
| verdict, risk, obligation status | none. Executing the changed code is not asserting the obligation's statement. |
| obligations on flows through the change | the tests observed executing it are attached as `supporting_tests`, tier `C_declared` ("exercises, no assertion established"), `match_rule = diffgenome:observed-execution`; a test the diff itself introduced or edited is flagged `changed_in_diff` |
| AI recovery trigger | a gap executed evidence already answers is not sent to the model (`recovery.trigger.answered_by_behavioral`): *missing test mapping* when tests were observed executing the change; *unresolved changed symbols* only when every changed symbol is connected, by observed or reconstructed calls, to a step of one of Sydes' own flows. Reaching some caller is not enough. |
| "What Sydes could not establish" | the unresolved-symbol bullet says executed evidence shows the entry path, when it does; a bullet states how many tests execute the change without being mapped as asserting it |
| PR comment (sydes-action renderer) | `### Behavioral effect` after "What it may affect"; the regression-test row reads "N test(s) execute the change, incl. M changed in this diff; none mapped as asserting it" instead of "Not found"; an "Executed in isolation (DiffGenome)" row; observed tests listed as run in isolation, never as "Checks the behavior: Yes"; the before-merging nudge asks to confirm an assertion rather than to add a test |

Tests that executed the changed code and **failed** in DiffGenome's run are kept apart
(`tests_failed_on_path`) and called out; they never count as support.

## Probe budget in integrated mode

`--behavioral-probes N` (default 0). DiffGenome's `change` command only spends probes on
gaps within one hop of a changed symbol (`--probe-max-distance 1`); farther gaps are listed
in the artifact's notes as excluded by the budget policy. Each probe is at most one model
call per attempt. On simplebank this keeps the `SQLStore.TransferTx` gap (distance 1) and
drops `Queries.GetAccount` (distance 2).

## CI

DiffGenome refuses to run target tests without an OS sandbox, and today it has one only on
macOS (Seatbelt). A Linux runner therefore produces no artifact, a
`diffgenome-status.json` with `status: refused` and the reason, and a Sydes result whose
behavioral section says exactly that. The recommended wiring is two jobs: DiffGenome where
the target's tests run under confinement, uploading `diffgenome-change.json` (or the status
file); Sydes consuming it with `--behavioral-artifact`. `ci-local-simulation.sh` reproduces
both the working hand-off and the Linux refusal locally; see `diffgenome-ci-local/`.

## Open policy decision: can executed evidence ever verify an obligation?

Not decided here; recorded for the maintainers. Options:

1. **Never** (implemented). Executed evidence is supporting only.
2. **Mapping from Sydes, execution from DiffGenome.** When Sydes itself mapped a test to an
   obligation at tier A or B (the "does it assert this" half) and DiffGenome observed that
   same test pass while executing the changed symbol (the "was it run on this code" half),
   resolve the obligation as `passed` even under `--no-run-tests`. This is Sydes' own rule
   (mapped + executed + passed) with the execution supplied by a confined run on the head
   revision. Risk: DiffGenome's run is of the head tree in a sandbox, not the repository's
   CI environment.
3. **A test the diff introduced, observed executing the changed symbol and passing**, counts
   for obligations the change introduced. Weaker: nothing establishes that it asserts the
   statement.

Recommendation: 2, behind its own flag, once a second maintainer agrees that a sandboxed
run of the head tree is acceptable execution evidence. 3 is not recommended.

## Matching static and runtime steps: assessment

File + trailing symbol name matched every static step that has an in-repo body on both
cases (simplebank: 4 of 6 steps executed, the other 2 genuinely never executed; Kokoro: both
route handlers). No false "possible only" was observed, so the matcher was left as is.

## Validation

- `docs/integration/diffgenome-simplebank/`: Go, the insufficient-balance change.
- `docs/integration/diffgenome-kokoro/`: Python, PY-H-01 (a change deep in text processing).
