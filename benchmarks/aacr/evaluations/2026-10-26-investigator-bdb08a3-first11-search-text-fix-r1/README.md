# Investigator + search_text fix on the frozen first-11 dev-laptop set (one run, 2026-10-26)

## What was run

- **Code:** `feat/investigator-prototype` at `bdb08a3`, which is `3b6bb91` (the search_text brace/`|` glob fix) cherry-picked onto `7cbe5ab`/`b9391e5`.
  - The product diff from `b9391e5` is only `src/sydes/recovery/tools.py` plus its tests.
- **Unchanged:** prompts, hypothesis cap (5), lookup budget (12, max 4 per hypothesis), reasoning effort, evidence packet, causal attribution, cases (`cases.json` is the frozen manifest), evaluator (AACR `ee9d7fef`, judge `gpt-5.2`) and the harness.
- **Command:** `review --sydes-ref bdb08a3 --sydes-arg=--investigator`, then `eval`, then `summarize`. Model `openai:gpt-5.6-sol`. One run.

**Comparisons:**
- (1) 0.4.0 frozen: `../../baselines/2026-10-25-sydes-0.4.0-dev-laptop-first11/`
- (2) Investigator b9391e5: `../2026-10-26-investigator-b9391e5-first11-r1/`
- (3) this run. **The primary comparison is (2) vs (3).**

**Read this first.** Each arm is a single run. The Finder produced a different hypothesis set in every PR between (2) and (3), and the count differs in 6 of 10. As shown below, no finding gained or lost between the runs traces to a search_text result that the fix changed. The AACR delta is run-to-run variance, not an effect of the fix.

## Validity

- **Valid:** 10. **Invalid:** 1, the same case as before.
  - `facebook__react-32499`, INVALID (provider errors): the Finder call timed out, giving 1 LLM call error and 1 investigator error.
  - It is excluded by the existing harness rule. Its artifacts are in `invalid/`.
- The 10 valid PRs carry 56 reference comments.

## AACR view (10 valid PRs)

| Run | Findings | Matched | Unmatched | File P | File R | Balanced | Legacy line P/R/F1 | Legacy semantic P/R/F1 |
|---|---|---|---|---|---|---|---|---|
| (1) 0.4.0, same 10 | 7 | 3 | 4 | 0.429 | 0.054 | 0.095 | — | — |
| (2) Investigator b9391e5 | 9 | 5 | 4 | 0.556 | 0.089 | 0.153 | 0.556/0.089/0.153 | 0.222/0.036/0.062 |
| (3) Investigator + fix | 5 | 3 | 2 | 0.600 | 0.054 | 0.099 | 0.400/0.036/0.066 | 0.400/0.036/0.066 |

The 0.4.0 frozen run over all 11 PRs: 7 findings, 3 matched, balanced 0.077.

## Product view

`adjudication.md` covers the unmatched CONFIRMED findings:
- gemini `commentJson.ts:113`: VALIDATED_DEFECT (same claim as in run (2)).
- gofr `handler.go:102`, POST `Response{Data:nil}` now 201 instead of 202: VALIDATED_DEFECT (new).

**Validated useful = 3 + 2 = 5.** Run (2) had 5 + 3 = 8; 0.4.0 had 3 matched.

## Per case: (2) b9391e5 → (3) fixed

| Case | H | L | Budget hit | C/R/U | Findings | Matched | Seconds | Calls | In / out tokens |
|---|---|---|---|---|---|---|---|---|---|
| browser-use-1482 | 3→**4** | 12→12 | Y→Y | 1/0/2→**0/0/4** | 1→**0** | 0→0 | 400→308 | 40→31 | 129k/20k→101k/18k |
| browser-use-3172 | 4→5 | 12→12 | Y→Y | 1/0/3→1/0/4 | 1→1 | 1→1 | 325→233 | 40→30 | 139k/17k→92k/15k |
| cline-1808 | 3→4 | 12→12 | Y→Y | 0/0/3→0/**2**/2 | 0→0 | 0→0 | 347→296 | 61→50 | 147k/17k→120k/17k |
| dbeaver-37564 | 4→4 | 12→12 | Y→Y | 0/1/3→0/0/4 | 0→0 | 0→0 | 598→638 | 23→22 | 57k/15k→57k/18k |
| dbeaver-38239 | 1→1 | 4→4 | N→N | 1/0/0→**0/0/1** | 1→**0** | 0→0 | 533→523 | 18→18 | 67k/10k→61k/8k |
| dbeaver-38915 | 4→3 | 12→12 | Y→Y | 1/0/3→**0/0/3** | 1→**0** | 1→**0** | 656→620 | 27→30 | 88k/17k→92k/15k |
| react-33059 | 3→3 | 12→12 | Y→Y | 0/0/3→0/0/3 | 0→0 | 0→0 | 274→270 | 34→43 | 68k/13k→89k/14k |
| gofr-1325 | 2→**3** | 8→**12** | N→**Y** | 1/0/1→**2**/0/1 | 1→**2** | 1→1 | 299→345 | 37→35 | 100k/17k→108k/19k |
| gemini-cli-8154 | 5→5 | 12→12 | Y→Y | 4/0/1→**2**/0/3 | 4→**2** | 2→**1** | 659→509 | 47→53 | 159k/30k→194k/29k |
| immich-16893 | 5→5 | 12→12 | Y→Y | 0/0/5→0/0/5 | 0→0 | 0→0 | 883→822 | 54→51 | 137k/23k→133k/21k |
| **Total** | 34→37 | 108→112 | 8→9 | 9/1/24→5/2/30 | 9→5 | 5→3 | | 381→363 | |

**Meaningful changes:**

| Case | Change | Cause |
|---|---|---|
| browser-use-1482 | Lost the validated test-mock finding (`requests` mocked; code uses `httpx`). | The Finder ranked it H4, and H4 got 0 lookups. |
| dbeaver-38915 | Lost a matched finding: `setDefaultCatalog` swallows the failure. | Evidence was retrieved: the superclass converts `SQLException` and the override catches without rethrowing. The Verifier still asked for a caller that relies on the return value. |
| dbeaver-38239 | Lost the PLAUSIBLE_UNVERIFIED binary-compatibility finding. | The Verifier now demands evidence of a downstream binary. |
| gemini-cli-8154 | Lost one matched finding (env leak through arrays) and one validated finding (MCP remove not deleted). | Both hit the 4-lookup cap one read short. They needed the MCP `args` array type and `saveSettings`. |
| gofr-1325 | Gained a validated finding. | A third hypothesis this run. |

## search_text: false negatives

| | b9391e5 | bdb08a3 (fixed) |
|---|---|---|
| search_text calls | 31 | 30 |
| Calls with a brace or `\|` path_glob | 9 (all returned nothing) | 8 brace, 0 `\|` |
| Empty results | 11 | 6 |
| False negatives (glob matched no files, or missed in-glob hits) | **9** | **0** |
| Genuine no-match | 2 | 6 |
| `(no files match path_glob …)` results | n/a | 0 |
| Errors / malformed | 0 | 0 |

- **How the empties were checked.** All 6 empty results in the fixed run were re-run with the fixed `RepoTools`. They were also checked with a brute-force regex over every file of a `git archive` PR-head snapshot. None has an in-glob hit.
  - react L2 has one hit, `fixtures/flight/playwright.config.js`, outside its `**/*{jest,Jest,test,Test}*` glob.
  - immich L2 has hits only under `server/`, outside `web/src/**/*.{ts,svelte}`.
- **Old failing shapes, replayed on the fixed tool** (`lookups.json` → `prior_false_negatives_replayed_on_fixed_tool`). Every one now returns hits:

| Shape | Lookups | Hits now |
|---|---|---|
| `**/*.{ts,tsx,md,json}` | cline L8 | 40 |
| `src/**/*.{ts,tsx}` | cline L10 | 11 |
| `{scripts,packages,.github}/**/*` | react L2 | 12 |
| `package.json\|scripts/**/*\|.github/**/*` | react L3 | 40 |
| `packages/cli/src/**/*.{ts,tsx}` | gemini L2 | 17 |
| `**/*.{ts,svelte,js}` | immich L2 | 14 |
| `web/src/**/*.{ts,svelte,js}` | immich L3 | 40 |
| `**/*.{ts,js,svelte,tsx,jsx}` | immich L6 | 3 |
| `**/*.{md,mdx,yml,yaml,ts,js,json}` | immich L8 | 13 |

- **How the brace searches were used in the fixed run:**
  - gemini L10 located `commands/mcp/remove.ts`, which the next lookup read.
  - 3172 L11 retrieved the `google-genai>=1.29.0,<2.0.0` pin.
  - immich L3, L11 and L12 retrieved the asset-URL helpers.
  - The Verifier treated genuine empties as non-proof. react H1: "absence of a search match does not prove that `.only` is honored".

## Repo-local UNKNOWNs (the 8 from b9391e5)

| b9391e5 UNKNOWN | Was a search false negative the cause? | Fixed run |
|---|---|---|
| 3172-H4: log disclosure | no (0 lookups) | Re-raised as H5; 0 lookups again |
| cline-H3: case-sensitive id | yes (L10) | Analogue H4 REFUTED, from `read_source` of `openai.ts` (not search). The id was already case-sensitive before the change. |
| 38915-H2: `initDefaultsFrom` | no | Same UNKNOWN (H3): 4 lookups, `initDefaultsFrom` body never read |
| 38915-H3: `DBUtils.findObject` | no | Same UNKNOWN (H2): 4 lookups, `findObject` never read |
| 37564-H1: InputStream ownership | no | Same UNKNOWN (H1): 4 lookups, contract not established |
| 33059-H1: CI runner config | yes (L2, L3) | Search now worked: L3 found `scripts/jest/jest-cli.js`, and L4 read its lines 1–240. Still UNKNOWN: the Jest invocation lies past that range and the cap was hit. |
| gemini-H5: legacy migration paths | no (0 lookups) | Not raised |
| immich-H5: SW registration timing | no (0 lookups) | Re-raised as H5; 0 lookups again |

- **Resolvable now:** 2 of 8 (cline-H3, 33059-H1) had a retrieval false negative as a cause, and both are now retrievable.
- **Resolved by retrieval:** 0. No UNKNOWN became CONFIRMED or REFUTED because a search now succeeded. The two cline REFUTEDs rest on `read_source`.
- The other 6 were never retrieval failures: 3 had zero lookups and 3 had the repo-local fact unread within 4 lookups.

## Lookup budget

| | b9391e5 | fixed |
|---|---|---|
| PRs hitting 12 | 8/10 | 9/10 |
| Hypotheses | 34 | 37 |
| Lookups | 108 | 112 |
| Lookups / hypothesis | 3.18 | 3.03 |
| Lookups / investigated hypothesis | 3.86 | 3.86 |
| Hypotheses with 0 lookups | 6 | **8** (1482-H4, 3172-H4/H5, 37564-H4, gemini H4/H5, immich H4/H5) |
| Non-productive lookups (empty, error or false negative) | 18 (9 FN, 2 no-match, 6 test/runtime, 1 callers error) | 19 (6 no-match, 10 test/runtime, 3 callers errors) |

- **Tool mix:**
  - b9391e5: read_source 57, search_text 31, find_symbol 12, tests_for 4, runtime_for 2, callers 2.
  - fixed: read_source 60, search_text 30, find_symbol 8, tests_for 6, runtime_for 4, callers 4.
- **Efficiency.** The fix removed the false-negative waste (9 to 0) and the blind retries that followed it. Total non-productive lookups did not fall: the freed lookups went to empty `tests_for`/`runtime_for` probes and CBM `callers` "function not found" errors.
- **Allocation.** Allocation is still strictly sequential, 4 per hypothesis, so H4/H5 are starved in 5 of 10 PRs.
  - Starved hypotheses in this run include a validated defect (1482 test mock, now H4).
  - immich-H5 was starved in both runs.
- **Total.** 9 of 10 PRs exhaust 12 lookups. The binding constraint is allocation, not size:
  - in the gemini and 33059 misses the hypothesis was one read short at its cap;
  - meanwhile 3 to 4 lookups per PR went to empty test/runtime probes.

**Answer: yes, lookup-budget allocation is now the clearest next bottleneck.** That means per-hypothesis ordering and the cap, not a larger total.

## Miss taxonomy (MODEL / CONTEXT / REASONING / CLAIM)

| Miss | Class (b9391e5 → fixed) | Why it remained |
|---|---|---|
| browser-use-1482: port `None` | MODEL → MODEL | Raised (H1). Lookups were attempted. The Pydantic runtime behaviour is not available repo-locally. |
| browser-use-3172: retry loop removed | MODEL → MODEL | Raised (H3). The fix retrieved the pin; the SDK's default retry policy is not available repo-locally. |
| cline-1808: Azure detection keyed on `deepseek` | MODEL → MODEL | Raised (H1/H2). Needs Azure endpoint facts, which are not available repo-locally. |
| dbeaver-38915: `getDefaultCatalog` null | CONTEXT → CONTEXT | Raised (H2). Lookup attempted but insufficient: `DBUtils.findObject` was never read in 4 lookups. |
| dbeaver-38915: `setDefaultCatalog` swallows failure (matched in (2)) | — → REASONING | Raised (H1). Evidence was retrieved, but the Verifier wanted proof that a caller relies on the return value. |
| dbeaver-37564: InputStream not closed | CONTEXT → CONTEXT | Raised (H1). Lookup attempted but insufficient: ownership contract not established. |
| react-33059: one-hook form fiber | CONTEXT → CONTEXT | Raised (H3). Lookup attempted but insufficient: reachability not shown. |
| gemini-8154: env leak through arrays (matched in (2)) | — → CONTEXT | Raised (H2). Lookup attempted but insufficient: the array-typed setting is repo-local and was not read before the cap. |
| immich-16893: top-level `BroadcastChannel` | CONTEXT → CONTEXT | Touched by H5. The right hypothesis got 0 lookups. |
| immich-16893: `event.origin` | REASONING → REASONING | The Finder never raised it. |
| gofr-1325: custom headers not applied | REASONING → REASONING | The Finder never raised it (3 hypotheses this run, none about headers). |
| dbeaver-38239: null varargs element | REASONING → REASONING | The Finder never raised it (1 hypothesis). |
| CLAIM | none → none | No confirmed finding overstates its evidence. |

No remaining miss is explained by a search_text false negative.

## Cost (same 10 valid PRs, per PR)

| | 0.4.0 | b9391e5 | fixed | fixed / b9391e5 | fixed / 0.4.0 |
|---|---|---|---|---|---|
| Seconds | 299.6 | 497.5 | 456.4 | 0.92× | 1.52× |
| LLM calls | 25.3 | 38.1 | 36.3 | 0.95× | 1.43× |
| Input tokens | 76.4k | 109.2k | 104.5k | 0.96× | 1.37× |
| Output tokens | 8.2k | 17.9k | 17.3k | 0.97× | 2.11× |
| Investigator calls (total) | — | 153 | 160 | | |
| Judge calls | — | 14 | 6 | | |

## Integrity

- **Leakage tests.** Harness and leakage tests (`benchmarks/aacr/tests`, including `test_no_leakage.py`), `tests/test_investigation.py` and `tests/test_recovery_tools_search_glob.py` all passed on `bdb08a3`: 37 passed, both before the run and after it.
- **References.** AACR reference comments were read only by AACR's evaluator, and afterwards for adjudication and miss analysis. Nothing from them reached Finder, Investigator or Verifier, and nothing was fed back.
- **Snapshots.** Verification used read-only `git archive` snapshots in a scratch directory. No target repo was modified.
- **Secrets.** The artifacts were scanned for secrets.

## Decision

- **Deterministic addition:** NO DETERMINISTIC ADDITION YET.
  - Remaining misses split across a Finder that never raised the defect (3), the 4-lookup cap or zero allocation (5), Verifier strictness (1), and facts that are not repo-local (3).
  - No single repo-local fact type recurs as unretrievable by the existing tools.
- **Next experiment:** lookup-budget allocation (per-hypothesis ordering and cap, same total of 12).
  - This is the repeated, cross-run signal: zero-lookup hypotheses (6 then 8), and hypotheses one read short at the cap.
- **Pinned dependency-source lookup:** not justified yet.
  - It would address 2 MODEL misses (Pydantic for 1482, google-genai for 3172). They recur, but only on the same 2 PRs.
  - It would not have recovered any finding lost between the runs.
