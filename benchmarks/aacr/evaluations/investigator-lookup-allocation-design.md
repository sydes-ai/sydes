# Experiment design: Investigator lookup-budget allocation (not implemented)

Status: design only, 2026-10-26. Branch `feat/investigator-prototype` (product code at `bdb08a3`).

## Question

If the same 12 lookups per PR are allocated differently, are more hypotheses decided (CONFIRMED or REFUTED)?

## Evidence that motivates it

From `2026-10-26-investigator-b9391e5-first11-r1/` and
`2026-10-26-investigator-bdb08a3-first11-search-text-fix-r1/`:

- **Total lookups barely moved:** 108 → 112. PRs that used the whole budget: 8/10 → 9/10.
- **Hypotheses with 0 lookups:** 6 → 8. This includes a validated defect: browser-use-1482's test mocks `requests` while the code uses `httpx`. It was ranked H4 in the second run.
- **Several hypotheses ran out one read short:** they stopped at the 4-lookup cap with the deciding repo-local fact unread.
  - gemini H2: the array-typed setting.
  - gemini H3: `saveSettings`.
  - react-33059 H1: the Jest invocation, at `jest-cli.js:387`; the read stopped at line 240.
  - dbeaver-38915 H2/H3: `DBUtils.findObject` and `initDefaultsFrom`.
- **No remaining miss is explained by retrieval failure.** search_text false negatives are 0 after `bdb08a3`.

## How allocation works today (`src/sydes/verify/investigation.py`)

- Hypotheses run strictly in Finder order. Each gets `min(MAX_LOOKUPS_PER_HYPOTHESIS=4, lookups left)`.
- The Investigator almost never stops early: 27 of 29 hypotheses that got any lookups used all 4.
- So H1–H3 use up all 12 lookups and H4/H5 get none.
- The Verifier runs once per hypothesis, after that hypothesis's lookups.

## The single change: two-round, verifier-gated allocation

| | Control A (current) | Treatment C |
|---|---|---|
| Round 1 | Finder order, up to 4 each until 12 are used | Every hypothesis gets up to **2** lookups, then the Verifier |
| Round 2 | none | Leftover lookups go to hypotheses still UNKNOWN, in Finder order. Each resumes its own transcript, up to **6 lookups in total**, then the Verifier again. |
| Budget per PR | 12 | 12 |
| Hypothesis cap | 5 | 5 |

- **Why 2 in round 1:** with 5 hypotheses, 2 × 5 = 10, so no hypothesis gets zero lookups. With 3–4 hypotheses, 4–6 lookups are left for round 2, which covers the "one read short" cases.
- **Prompts are unchanged.** Round 2 resumes the existing Investigator prompt: hypothesis + lookup transcript + "lookups left".
- **The Verifier stays independent.** The Investigator never sees the round-1 Verifier status or its `missing` list. Feeding that back would change prompt content, so it would be a separate experiment.
- **Also unchanged:** evidence packet, causal attribution, CONFIRMED rules.
- **Round-1 decisions are final.** A hypothesis CONFIRMED or REFUTED in round 1 gets no round 2.
- **The parameters are fixed before any run:** `ROUND1_LOOKUPS = 2` and a total cap of 6 per hypothesis. They are not tuned afterwards.

### Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Even split of 12 across hypotheses (3/3/2/2/2) | Removes zero-lookup hypotheses but cuts the cap below 4, so the "one read short" misses get worse. gemini had 5 hypotheses. |
| Investigate the most severe hypotheses first | The starved validated defect was P3, so it would drop further. P1 hypotheses are not systematically later in Finder order. |
| A larger total budget | Out of scope: total size isn't what's binding; how lookups are allocated is. |

## Controlling variance

Two single runs differed more by noise than by the change: the Finder's hypothesis set differed in every PR. So the arms must investigate **identical hypotheses**.

1. **Snapshot run.** Run the normal pipeline once with an opt-in flag that writes `*.snapshot.json`: the full packet text plus the Finder's hypotheses. The current artifact stores only packet ids and `chars`, so past runs cannot be replayed. This run is control A1.
2. **Replay.** A bench-only `replay` command loads the snapshot and runs only Investigator → Verifier under arm A or C. It writes result and investigation JSON in the normal layout, so `eval`/`summarize` run unchanged.
3. **Runs on the frozen first-11 set.** C1 and C2 measure treatment variance.

   | Run | Mode | Purpose |
   |---|---|---|
   | A1 | Full snapshot run | Control |
   | A2 | Replay, arm A | Control-vs-control noise |
   | C1 | Replay, arm C | Treatment |
   | C2 | Replay, arm C | Treatment |

   A replay is about 15–20 LLM calls per PR, roughly a third of a full run.
4. **Validity.** Provider-error cases are INVALID under the existing rule. A case is compared only if it is valid in every arm.
5. **Leakage.** AACR references are kept out. The snapshot holds only Sydes's own packet and hypotheses, and the leakage tests are extended to snapshot and replay.

## Metrics (pre-registered)

**Primary**, measured on the identical hypothesis set:
- Decided fraction, (C+R)/H, for C vs A. The treatment must beat A by more than the A1–A2 difference, in both C runs.
- Zero-lookup hypotheses: 0 in C by construction. This is a sanity check, not the result.

**Secondary:**
- Resolution of the starved hypotheses. These are the zero-lookup or cap-hit-UNKNOWN cases identified from Sydes traces, not from AACR.
- Validated useful = AACR matches + VALIDATED_DEFECT. Only unmatched CONFIRMED findings are adjudicated, read-only.
- INVALID_FINDING count must not increase.
- AACR file-level and legacy metrics are reported but not decisive: there are only 3–5 matches, so noise dominates.

**Cost:**
- Lookups ≤ 12 per PR.
- Investigator + Verifier calls ≤ about +25%. Round 2 adds at most one Verifier call per UNKNOWN hypothesis.
- Seconds and tokens per PR.

**Diagnostics:**
- Lookups per hypothesis in each round.
- Whether round-2 lookups read the fact the round-1 Verifier listed as missing. This is post-hoc only, never fed back.
- How empty `tests_for`/`runtime_for`/`callers` results split between rounds.

### Decision rule

| Verdict | Condition | Consequence |
|---|---|---|
| **GO** | Decided fraction improves beyond noise in both C runs, validated useful does not fall, and cost is within bounds | Two-round allocation becomes the proposed Investigator default. It is still reviewed, not merged automatically. |
| **INCONCLUSIVE** | The gain is mostly new REFUTEDs or findings adjudicated as invalid | — |
| **NO-GO** | No gain beyond noise | Allocation is not the bottleneck. Next candidates: Verifier strictness or Finder coverage. |

## Implementation outline (for later)

- **`investigation.run`:** add `allocation="sequential" | "two_round"`, defaulting to `sequential`.
  - Production defaults and the default artifact stay unchanged.
  - New constants: `ROUND1_LOOKUPS = 2` and `MAX_LOOKUPS_PER_HYPOTHESIS_TWO_ROUND = 6`.
- **`investigate_one`:** accepts a prior `gathered` list and a quota, so round 2 resumes the same transcript.
- **Artifact:** `budget.allocation`, the lookup allotment per hypothesis, and `rounds` (round-1 and final outcomes).
- **Flags:** `--investigator-allocation` (hidden) and `--investigator-snapshot-out`.
- **Harness:** a `replay` subcommand.
- **Tests,** with the existing `Scripted` client:
  - 5 hypotheses each get ≥ 2 lookups;
  - total ≤ 12;
  - a hypothesis decided in round 1 gets no round 2;
  - the Verifier still never sees the Investigator's reasons;
  - the default-path artifact is unchanged;
  - no AACR text appears in the snapshot or replay.
- **Constraints:** everything stays on `feat/investigator-prototype`. No merge, no release.
