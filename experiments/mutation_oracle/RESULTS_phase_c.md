# Phase C — mutation testing as a reachability oracle

Experiment only. `main` untouched. No file under `src/sydes/` modified.
Branch: `experiment/mutation-oracle` (off `experiment/claim-verifier`).

## Design decision, adopted mid-plan

The original plan treated a mutation "flip" as a GATE — required before a
Likely→Established promotion. An external review recommended reframing it
as `behavioral_coverage` EVIDENCE reported alongside Phase B's structural
verification, never a blocking requirement. Adopted: my own original design
already carried the honest caveat ("no flip ≠ unreachable") but still
treated it as a hard requirement, which risks blocking a legitimate,
structurally-verified promotion purely because test coverage happens to be
thin — a common, not rare, situation. Evidence-only avoids that failure
mode without losing any of the value.

## Method

For each case: run the mapped tests UNMUTATED first (baseline — a flip is
only meaningful if the test passed beforehand), apply one real mutmut-style
mutation to the actual file on the real `py-real-01-max-output-duration`
branch, rerun the same tests, then unconditionally restore the file via
`git checkout --` (try/finally, same git-safety discipline as every prior
round: status guard before switching state, restore after).

## Results

**Positive case** — the real dependency Phase B already verified
structurally end-to-end (`_within_duration_ceiling` behind `Duration`,
a field on `OpenAISpeechRequest`, which `create_speech` accepts):

- Mutation: `if value > settings.max_output_duration_s:` →
  `if value >= settings.max_output_duration_s:`
  (`api/src/structures/schemas.py:73`, a relational-operator mutation —
  the standard mutmut boundary-shift operator, not invented for this case)
- Mapped tests: `api/tests/test_request_bounds.py` (real, pre-existing
  tests, not written for this experiment)
- Baseline: 23/23 passed. Mutated: **`test_max_duration_seconds_accepts_at_server_ceiling` failed** (the exact test whose name states the boundary this mutation shifts).
- **`behavioral_coverage: CONFIRMED`**

**Negative case** — a known-FALSE candidate from the original 8-case
fixture (`model_status`, PR6), as a specificity control:

- Mutation: `if not settings.allow_dev_unload:` → `if settings.allow_dev_unload:`
  (`api/src/routers/development.py:536` — `model_status`'s own, unrelated
  kill-switch guard)
- Same mapped tests (`test_request_bounds.py`)
- Baseline: 23/23 passed. Mutated: **23/23 still passed — no flip.**
- Confirms the oracle does not produce spurious signal for genuinely
  unrelated code; a flip here would have meant the mapped-test set was too
  broad/leaky, not that the dependency was real.

## Test suite

5 unit tests (`test_mutation_oracle.py`) against synthetic, disposable git
repos (real `git init`/`commit`, since restoration relies on `git checkout
--`, not just a bare tmp_path): exact-line mutation + indent preservation,
drift protection (refuses to mutate when the cited line no longer matches
— the same "citation must hold up" discipline as Phase B, applied to
mutation targeting instead of claim verification), a real flip, a real
non-flip (specificity), and refusal on an already-dirty target file.

## What this validates

- The oracle correctly distinguishes a real dependency (flip) from an
  unrelated one (no flip) on real, pre-existing tests it did not need to
  know about in advance.
- Drift protection means a stale mutation spec fails loudly rather than
  silently mutating the wrong line — the same failure mode Phase B's
  citation checker exists to catch, applied here to the mutation target
  itself.
- Consistent with the adopted design decision: this round reports
  `behavioral_coverage` as informational evidence. No promotion logic
  exists yet to gate on it (none was built), and none should be, per the
  recommendation adopted at the top of this file.

## Recommendation

Feed into Phase D as an evidence field, not a gate: any promoted claim
should carry `behavioral_coverage: CONFIRMED | NOT_FLIPPED | UNCHECKED`
alongside Phase B's `structural_status`, with `NOT_FLIPPED` routed to
Sydes' existing "add a test covering this" recommendation rather than any
downgrade of the structural verification.

**merge experiment? NO.** Branch `experiment/mutation-oracle` pushed to
`sydes-ai/sydes`, no PR opened, `main` untouched.
