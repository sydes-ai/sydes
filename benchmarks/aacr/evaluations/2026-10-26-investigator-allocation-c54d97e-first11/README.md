# Investigator lookup-allocation experiment: c54d97e, first-11, 2026-10-26

Design: [`../investigator-lookup-allocation-design.md`](../investigator-lookup-allocation-design.md). It was pre-registered and was not changed after the runs.
Product code: `c54d97e` on `feat/investigator-prototype`. Model `openai:gpt-5.6-sol`, judge `gpt-5.2`.
Cases: the frozen first-11 set ([`cases.json`](cases.json)).

**Decision: NO-GO.**
- C1 does not beat the control by more than the A1–A2 noise.
- C2 does, but the rule requires both C runs.
- Lookup allocation is not shown to be the bottleneck.

## Arms

All arms investigate the **identical hypotheses**. A1 ran the full pipeline and wrote `*.snapshot.json` (packet + Finder hypotheses). The other three replay that snapshot without the Finder.

| Arm | Mode | Allocation | Directory |
|---|---|---|---|
| A1 | full run + snapshot | sequential (4 per hypothesis, Finder order) | `arms/A1/` |
| A2 | replay | sequential | `arms/A2/` |
| C1 | replay | two_round (2 each, then leftover to UNKNOWN up to 6) | `arms/C1/` |
| C2 | replay | two_round | `arms/C2/` |

The budget is 12 lookups per PR and at most 5 hypotheses in every arm. Prompts, Finder, Verifier, tools and cases were not changed.

**Validity.** 10 of 11 cases are valid in every arm. `facebook__react@b045f18` (react-32499) is INVALID: the Finder timed out with a provider error in A1, so the replays inherit it. This is its third INVALID run in a row.

**Caveat.** A1 is a full run and A2–C2 are replays. The replays re-index the CBM graph in `fast` mode, so the A1–A2 noise includes that environmental difference as well as LLM variance.

## Primary metric: decided fraction (C+R)/H on the same 33 hypotheses

| Arm | H | CONFIRMED | REFUTED | UNKNOWN | Decided fraction | Zero-lookup hypotheses |
|---|---|---|---|---|---|---|
| A1 | 33 | 6 | 0 | 27 | 0.182 | 7 |
| A2 | 33 | 7 | 0 | 26 | 0.212 | 7 |
| C1 | 33 | 7 | 0 | 26 | 0.212 | **0** |
| C2 | 33 | 10 | 0 | 23 | 0.303 | **0** |

- Mean A = 0.197. Noise |A1−A2| = 0.030.
- **C1 − mean A = +0.015**, which is not beyond the noise.
- **C2 − mean A = +0.106**, which is beyond the noise.
- The zero-lookup sanity check passes: 0 in both C runs.

Decided hypotheses per arm (status, lookups):

| Hypothesis | A1 | A2 | C1 | C2 |
|---|---|---|---|---|
| browser-use 0b21e50 H1 | U4 | C4 | C2 | C2 |
| browser-use 0b21e50 H3 | U4 | C4 | U4 | C2 |
| browser-use 72f1585 H1 | C4 | C4 | C2 | C2 |
| dbeaver 8224826 H1 | U4 | U4 | U6 | **C6** (round 2) |
| dbeaver ed40132 H1 | U4 | U4 | U6 | **C2** (round 1) |
| react be58f26 H1 | C4 | U4 | C5 (round 2) | U5 |
| gofr b9cdcbb H1 | C4 | C4 | C2 | C2 |
| gemini 0fa1bd7 H1 | C4 | C4 | C4 (round 2) | C2 |
| gemini 0fa1bd7 H2 | U4 | U4 | U2 | **C4** (round 2) |
| gemini 0fa1bd7 H4 | C0 | C0 | C2 | C2 |
| gemini 0fa1bd7 H5 | C0 | C0 | C2 | C2 |

What the table shows:
- **No REFUTED in any arm.** Every gain is a new CONFIRMED, so the INCONCLUSIVE branch ("mostly new REFUTEDs or invalid findings") does not apply.
- **Verdicts flip between arms on the same hypothesis, often with the same lookup count.**
  - react H1: C, U, C, U.
  - browser-use H3: U4 in A1, C4 in A2.
  - dbeaver ed40132 H1: confirmed at round 1 in C2 with 2 lookups, but UNKNOWN in the other three arms with 4–6.
  - Verifier and Investigator variance therefore move the metric as much as the allocation change does.
- **Only three hypotheses were decided in C and in neither A arm,** all of them in C2: dbeaver 8224826 H1, dbeaver ed40132 H1 and gemini H2. Two of the three came from round 2.

## Secondary metrics

**Starved targets.** These are taken from A1's own trace: 27 hypotheses with 0 lookups (7) or UNKNOWN at the cap of 4 (20). The table counts how many are decided in each arm.

| A1 | A2 | C1 | C2 |
|---|---|---|---|
| 2 | 4 | 3 | 7 |

The 2 in A1 are gemini H4 and H5, which were CONFIRMED from the packet alone with 0 lookups. C1 decides one target that A did not decide in A1 (browser-use 0b21e50 H1), and A2 also decided it. C2 decides 3 that no A arm decided.

**Validated useful** (AACR matches + VALIDATED_DEFECT; see [`adjudication.md`](adjudication.md)):

| Arm | Validated useful | INVALID_FINDING |
|---|---|---|
| A1 | 6 | 0 |
| A2 | 7 | 0 |
| C1 | 7 | 0 |
| C2 | 9 | 0 |

The secondary conditions hold: validated useful does not fall and INVALID does not rise.

**AACR file-level metrics** are reported but not decisive. Each arm has 56 refs across 10 valid cases.

| Arm | Findings | Matched | P | R | Balanced |
|---|---|---|---|---|---|
| A1 | 6 | 4 | 0.667 | 0.071 | 0.129 |
| A2 | 7 | 5 | 0.714 | 0.089 | 0.159 |
| C1 | 7 | 5 | 0.714 | 0.089 | 0.159 |
| C2 | 10 | 7 | 0.700 | 0.125 | 0.212 |

## Cost

Cost is measured as Investigator + Verifier only. A1's Finder usage, taken from its snapshot, is subtracted. Means are per valid PR.

| Arm | Lookups (total / max per PR) | Inv+Ver calls | Input tokens | Output tokens | Wall seconds* |
|---|---|---|---|---|---|
| A1 | 100 / 12 | 13.4 | 33,146 | 6,696 | 431.8 (full run) |
| A2 | 100 / 12 | 13.4 | 32,541 | 6,171 | 115.7 |
| C1 | 108 / 12 | 15.8 | 43,526 | 9,190 | 146.7 |
| C2 | 102 / 12 | 15.0 | 39,393 | 7,951 | 134.2 |

\*Compare wall seconds replay to replay only (C vs A2).

Against A, C costs:
- **+15% calls**, which is within the pre-registered bound of about +25%;
- about +27% input tokens;
- about +39% output tokens;
- about +21% wall time.

The budget of 12 lookups per PR held in every arm.

## Diagnostics

**Lookups per hypothesis per round (C).**
- Round 1 used 66 lookups in each C run. Every hypothesis got 2, except gofr, whose single hypothesis got 2 and stopped there.
- Round 2 used 42 lookups in C1 (14 hypotheses) and 36 in C2 (12 hypotheses).
- Per-case allotments are in `analysis.json` → `arms.*.per_case`.

**New evidence between verdicts.**

| | C1 | C2 |
|---|---|---|
| Round-2 hypotheses | 14 | 12 |
| Re-verified (all had new lookups) | 14 | 12 |
| Re-verified with only empty new lookups | 0 | 0 |
| Status changed | 2 | 2 |
| …of which with new non-empty evidence | 2 | 2 |
| Changes | UNKNOWN→CONFIRMED ×2 | UNKNOWN→CONFIRMED ×2 |

- Round 2 converts about 1 in 6 of the hypotheses it revisits.
- In C1, both conversions (react H1, gemini H1) had already been decided by A at 4 lookups. Round 2 only won back what the 2-lookup round-1 cut had lost.
- In C2, both conversions (dbeaver 8224826 H1, gemini H2) are new.

**Did round 2 read what the round-1 Verifier said was missing?** This was checked post-hoc, by matching identifiers from the round-1 `missing` list against round-2 lookup args and results. At least one missing term was read in 8 of 11 round-2 hypotheses in C1 and 7 of 9 in C2, so the Investigator mostly aimed at the right fact even without seeing the Verifier's output. Even so, reading the named fact rarely changed the verdict.

**Empty `tests_for` / `runtime_for` / `callers` results.**

| Arm | Round 1 empty / total | Round 2 empty / total |
|---|---|---|
| A1 | 11 / 17 | — |
| A2 | 9 / 13 | — |
| C1 | 4 / 7 | 7 / 7 |
| C2 | 6 / 8 | 7 / 7 |

Every round-2 structural lookup came back empty. The useful round-2 evidence came from `read`/`search_text`.

## Decision

| Condition | Result |
|---|---|
| Decided fraction beyond noise in **both** C runs | **No.** C1 +0.015 vs noise 0.030; C2 +0.106. |
| Validated useful does not fall | Yes: 6/7 → 7/9 |
| Cost within bounds | Yes: +15% calls, ≤ 12 lookups |
| Gain mostly new REFUTED or invalid | No: no REFUTED, 0 invalid |

**NO-GO** under the pre-registered rule.

Interpretation, not part of the rule:
- The treatment is cost-bounded and does no harm. It removes zero-lookup hypotheses, and in C2 it decided 3 hypotheses no control decided.
- However, the variance between the two C runs (0.212 vs 0.303, i.e. 3 hypotheses) is three times the A1–A2 noise.
- Verdicts flip on the same hypothesis at the same lookup count.
- With 33 hypotheses, most of what separates the arms is Verifier and Investigator variance, not allocation.
- The design names the next candidates: **Verifier strictness** (23–27 of 33 hypotheses stay UNKNOWN in every arm, even when round 2 read the missing fact) and Finder coverage.
- Two-round allocation is not proposed as the default.

## Files

| File | Contents |
|---|---|
| `arms/{A1,A2,C1,C2}/` | Full run directories: Sydes results and `*.investigation.json`, A1's `*.snapshot.json`, telemetry, AACR eval-file_level/legacy, summary, logs, invalid |
| `analysis.json` | Output of `analyze.py`: per arm, per case, targets, rounds, unmatched findings, decision inputs |
| `analyze.py` | The analysis script, as run (its paths point at `benchmarks/aacr/results/`) |
| `run-experiment.sh` | The driver that ran A1 → A2 → C1 → C2. It reads the API key from a local file and contains no secret. |
| `adjudication.md` | Unmatched CONFIRMED findings, every arm |
| `cases.json` | The frozen first-11 case list |

AACR references entered only the evaluator and the post-hoc adjudication. The snapshot and the replays contain only Sydes's own packet and hypotheses. `test_no_leakage.py` (including the replay test) passes.
