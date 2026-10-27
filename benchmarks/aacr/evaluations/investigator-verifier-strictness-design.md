# Experiment design: Investigator Verifier strictness (not implemented)

Status: design frozen 2026-10-27, before any experimental call. Treatment wording corrected once before implementation (see below). Branch `feat/investigator-prototype` (product code `c54d97e`).
Follows the NO-GO in [`2026-10-26-investigator-allocation-c54d97e-first11/`](2026-10-26-investigator-allocation-c54d97e-first11/README.md).

## Question

The Verifier sees exactly the same evidence in every arm. If its standard of proof changes from "a demonstrated failure" to "a mechanism the code establishes", are more hypotheses decided without more invalid findings?

## Evidence that motivates it

All counts below come from the allocation experiment's four arms, which investigated the same 33 hypotheses in 10 valid cases.

**Every UNKNOWN came from the LLM Verifier itself.**
- In all 102 UNKNOWN outcomes, `check_confirmation` downgraded nothing.
- The mechanical gates (a verified defect quote, a verified quote of the changed lines, a causal link, a `supported_claim`) never blocked a verdict.
- So strictness is a matter of the prompt and the model, not the code gates.

**Many UNKNOWNs already had everything a CONFIRMED needs.**
- **The mechanical checks would have passed:** in A1, 10 of 27 UNKNOWNs had a verified defect quote, a verified quote of the changed lines and a stated causal link.
- **The same held in the other arms:** A2 10/26, C1 11/26, C2 5/23.
- **Most UNKNOWNs had verified changed-line quotes:** 21–25 per arm.

**The `missing` lists mostly ask for things a repository cannot provide.** The lists were keyword-coded (approximate). A1 / A2 counts are hypotheses with at least one item in the category:

| Category | Example from the traces | A1 | A2 |
|---|---|---|---|
| Demonstration: runtime result, observed failure, executed test | react H1: "a runtime result showing which tests execute or are skipped" for an `it.only` the PR commits | 14 | 17 |
| Intent: contract, requirement, expected behavior | gemini H3: "evidence of the command's contract…" while also noting "the utility code establishes the conditional overwrite behavior" | 9 | 11 |
| External: SDK, browser, provider or library behavior | cline H1: what OpenAI 4.83.0 sends to an Azure endpoint | 8 | 8 |
| Reachability: callers, a concrete in-repo path | dbeaver 8224826 H4: the call sites of `initializeContextState` | 10 | 10 |

How each category relates to this experiment:
- **Demonstration** can never be met. The Investigator cannot run code, and runtime evidence exists only when Sydes already has it.
- **Intent** is met only when a comment or test happens to state it.
- **External** facts are correctly UNKNOWN. This change must not touch them.
- **Reachability** is legitimate and can be retrieved.

**Verifier outcomes for hand-validated defects are unstable on the same claim.**
- react H1 (`it.only` committed, VALIDATED_DEFECT) went C, U, C, U across the four arms.
- browser-use 0b21e50 H3 (the httpx/requests mock, VALIDATED_DEFECT) was U4 in A1 and C4 in A2.
- In those arms the hypothesis had the same number of lookups. The verdict sits on the line between "the mechanism is shown" and "the failure is demonstrated".

**REFUTED was never used:** 0 of 132 outcomes. The same demand for demonstration blocks refutation: "absence prevents refutation", as dbeaver bc3ac7b H2 put it.

## The single change: the Verifier's standard of proof

This is a **prompt-only** change to `VERIFIER_SYSTEM`. The following stay fixed:
- the input the Verifier sees;
- its output schema;
- `check_confirmation`;
- the Finder, the Investigator and the tools.

The treatment inserts the paragraph below after "Do not confirm on plausibility." It is frozen here and will not be edited after any run.

> Standard of proof. The evidence decides when the repository's own code establishes the
> mechanism: a concrete path through quoted code, reachable from code the change touches,
> that produces the behavior; and the supplied evidence establishes why that behavior is
> wrong (the code's stated contract in names, docstrings, comments or tests; language,
> test-runner or library semantics only where those semantics are present in the supplied
> evidence; or an inconsistency between two quoted pieces of the repository). When that
> holds, do not require a runtime demonstration, an executed test, an observed failure, or a
> separately documented requirement: lack of those alone is not a reason for UNKNOWN. Do not
> use unstated or remembered external framework, library, language-runtime, test-runner or
> service behavior to satisfy this standard; such semantics count only when they are present
> in the supplied evidence. The same standard decides REFUTED: if quoted code shows the path
> cannot be taken or does not produce the behavior, refute. Stay UNKNOWN when the deciding
> fact is outside the evidence: the behavior of an external system, library or service that
> the evidence does not show; a reachability step that no quoted code establishes; or intent
> that neither the code nor its tests state.

**Pre-implementation correction (2026-10-27, frozen before the first experimental call).** The first draft let
"the language's or test runner's own documented semantics" establish why the behavior is wrong. That would let the
Verifier use remembered external semantics as evidence. The frozen text now requires those semantics to be present in
the supplied evidence, and adds an explicit sentence forbidding unstated or remembered framework, library,
language-runtime, test-runner or service behavior. The resulting rule:
- repository code may establish the mechanism;
- supplied evidence must establish why the behavior is wrong;
- no runtime reproduction is needed when the mechanism and the violated standard are already established;
- no separately written requirement is needed when repository evidence establishes the standard;
- external behavior absent from the evidence, unestablished reachability and unstated intent stay UNKNOWN.

The control prompt is the current `VERIFIER_SYSTEM` at `c54d97e`, byte for byte. The treatment is that prompt with the
paragraph above inserted directly after "Do not confirm on plausibility." and nothing else changed.

**Why this wording:**
- **Demonstration and intent are named as insufficient reasons for UNKNOWN only when the code already shows the mechanism.** "Do not confirm on plausibility" stays.
- **External facts and unestablished reachability are named as valid reasons for UNKNOWN,** so the change cannot license guessing about SDKs or browsers.
- **The rule is symmetric for REFUTED,** so it isn't purely a loosening toward CONFIRMED.
- **It names no language, framework or bug pattern.** That respects the module's "no bug pattern … rule" constraint.

### Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Relax `check_confirmation` | It downgraded 0 outcomes. It isn't binding, and it is the safety net. |
| Majority vote over several Verifier samples | It addresses variance, not strictness, and costs ×N calls. It is a candidate follow-up whose need this experiment measures (see diagnostics). |
| Show the Verifier the Investigator's reasoning or summary | Breaks independence, which is a protocol invariant. |
| Let the Verifier request lookups | Changes the Investigator/tool loop. That would be a second variable. |
| A surfaced PLAUSIBLE status or warning tier | Breaks the claim discipline that anything not CONFIRMED is UNKNOWN, never a warning. It is a product change. |
| A different Verifier model | Would confound the prompt with the model. Possibly a separate experiment. |

## Controlling variance: freeze the Verifier's exact input

The allocation experiment showed that verdicts flip on the same hypothesis even with the same lookup count. This experiment therefore holds **everything** up to the Verifier fixed.

1. **Evidence freeze (E0).** Replay the existing A1 snapshot (`arms/A1/sydes/*.snapshot.json`, the same 33 hypotheses) once under `sequential` allocation, the production default. A new opt-in flag writes `*.verifier-input.json`: for each hypothesis, the exact `shown` object that `verify()` sends (hypothesis fields, diffs, packet evidence, and **full** lookup results). Today's `investigation.json` keeps only 600-character excerpts, which were truncated in 282 of 410 lookups. E0 itself is not an arm; it only fixes the input.
2. **Verifier-only replay.** A bench-only replay loads `*.verifier-input.json` and runs only `verify()` → `check_confirmation` → findings for each hypothesis, under prompt `control` or `strict`. It writes the result JSON and the investigation JSON in the normal layout, so `eval` and `summarize` run unchanged.
3. **Arms.** All arms read identical inputs. The only differences are the prompt and sampling noise.

   | Arm | Verifier prompt | Purpose |
   |---|---|---|
   | V-A1, V-A2, V-A3 | control (current `VERIFIER_SYSTEM`) | Pure Verifier noise: same input, same prompt |
   | V-T1, V-T2, V-T3 | strict standard of proof (above) | Treatment |

   Each arm makes 33 Verifier calls. Three runs per arm are affordable here, where full runs were not, and they give a noise range rather than a single difference.
4. **Validity.** Cases INVALID in E0 are excluded. A Verifier provider error makes the case INVALID in that arm, and a case counts only if it is valid in every arm. react-32499 is already excluded by the snapshot.
5. **Leakage.** The verifier-input file holds only Sydes's own packet, hypotheses and lookup results. The leakage tests are extended to the new file and to the Verifier replay.

## Metrics (pre-registered)

**Primary:**
- **Decided fraction (C+R)/H** on the same 33 inputs.
- **Pass condition:** the minimum of V-T1..3 must exceed the maximum of V-A1..3. Every treatment run must beat every control run.

**Precision guard.** Adjudication is decisive here, because the change can only make confirming easier.
- **Newly confirmed hypotheses:** CONFIRMED in at least 2 of 3 treatment runs and at most 1 of 3 control runs.
- Adjudicate every one, using the existing classes: VALIDATED_DEFECT / PLAUSIBLE_UNVERIFIED / INVALID_FINDING. An AACR match counts as validated.
- **New-precision** = (validated + matched) / newly confirmed.
- **INVALID_FINDING per run** (mean over the 3 runs): treatment ≤ control + 1.

**Secondary:**
- **Known-real recall.** Count how many of the hand-validated claims that were UNKNOWN in some allocation arm (react H1, browser-use 0b21e50 H3) are CONFIRMED in each run.
- **REFUTED count.** This must also be adjudicated. A REFUTED that hides a VALIDATED_DEFECT is an error, counted like an INVALID_FINDING.
- **Validated useful per run** = AACR matches + VALIDATED_DEFECT. Report the mean and range.
- AACR file-level and legacy P, R and balanced are reported but not decisive.

**Cost:**
- The prompt adds a fixed number of tokens per call (about 220 more input tokens).
- Report Verifier output tokens and seconds per hypothesis.
- No extra calls by construction.

**Diagnostics:**
- **Verdict stability.** Per hypothesis, the share of the 3 runs that agree with the majority, for control vs treatment. If treatment is not more stable, a majority vote stays a separate candidate.
- **The `missing` categories** (same keyword coding as above) for UNKNOWNs in control vs treatment. The External and Reachability counts should barely move. If External UNKNOWNs turn into CONFIRMED, that is a red flag and goes to adjudication first.
- **Transitions** per hypothesis (control majority → treatment majority): U→C, U→R, C→U, C→R.
- **Downgrades by `check_confirmation`.** These are expected to rise if the model claims CONFIRMED with quotes it can't back. They are reported per category.

### Decision rule

| Verdict | Condition | Consequence |
|---|---|---|
| **GO** | Every V-T beats every V-A on decided fraction; at least 3 newly confirmed hypotheses; new-precision ≥ 2/3; INVALID mean ≤ control + 1; no VALIDATED_DEFECT REFUTED | Confirm end to end with a single full first-11 run of the Investigator with the strict prompt (the real pipeline, not replay), then propose it as the Investigator's Verifier prompt for review. Never merged automatically. |
| **INCONCLUSIVE** | Decided fraction rises, but new-precision < 2/3, or the gain is mostly REFUTED that adjudication can't settle, or fewer than 3 newly confirmed hypotheses | Don't adopt. Next step: Finder coverage, or a variance experiment (majority vote). |
| **NO-GO** | No separation from the control range | The Verifier's standard isn't binding. Next candidate: Finder coverage. |
| **STOP (harmful)** | INVALID mean > control + 1, or a VALIDATED_DEFECT REFUTED in ≥ 2 runs | Reject the wording. Do not iterate the prompt on these cases (no tuning on the frozen set). |

## Implementation outline (for later)

**`investigation.verify`**
- Takes an optional `system` argument, defaulting to `VERIFIER_SYSTEM`.
- Adds `VERIFIER_SYSTEM_STRICT = VERIFIER_SYSTEM` with the frozen paragraph inserted, selected by `verifier_prompt="control" | "strict"`.
- The production default and artifact stay unchanged. Budget/artifact records `verifier_prompt` only when non-default.

**Verifier-input freeze**
- `_investigate_*` record the `shown` object per hypothesis when asked: `verifier_inputs_out`.
- `investigation_replay` gets `--verifier-input-out`.
- For two_round runs, store the final-round input. This experiment uses sequential, so there is one per hypothesis.

**`sydes.verify.verifier_replay`** (bench only)
- `--inputs X.verifier-input.json --prompt control|strict --repo-root --json OUT`.
- Writes the same result and investigation layout as `investigation_replay`.
- Lookups are copied from the input, and only Verifier usage is counted.

**Harness**
- `aacr.py verifier-replay --source-run E0 --prompt …`, mirroring `replay`.

**Tests** (scripted client):
- the strict prompt is used only when selected;
- the default `VERIFIER_SYSTEM` is byte-identical to `c54d97e`;
- the verifier-input file round-trips: the replayed `verify()` receives an identical `shown`;
- no Finder or Investigator call happens during Verifier replay;
- `check_confirmation` still downgrades a CONFIRMED that has no verified quotes under the strict prompt;
- leakage: no AACR text in the verifier input or the replay outputs.

**Constraints**
- Everything stays on `feat/investigator-prototype`.
- No merge, no release, no change to production defaults.
- No AACR references reach Sydes, and adjudication is not fed back.
- The prompt paragraph is not edited after the first run.
