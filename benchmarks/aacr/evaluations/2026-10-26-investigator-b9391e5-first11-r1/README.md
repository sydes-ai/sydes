# Investigator on the frozen first-11 dev-laptop set (one run, 2026-10-26)

| Field | Value |
|---|---|
| Sydes under test | commit `b9391e5414a11d2a62dbc537de801822edbfaa0a` (`feat/investigator-prototype`) |
| Build | `git archive`, run with `--sydes-arg=--investigator` (implies `--code-review`) |
| Model | `openai:gpt-5.6-sol` |
| Case set | the 11 cases in `cases.json`, a byte-identical copy of `baselines/2026-10-25-sydes-0.4.0-dev-laptop-first11/cases-completed.json` |
| Excluded | the interrupted 12th case of the 0.4.0 run |
| Evaluator | AACR `ee9d7fe`, unchanged; reader `ocr`; `line-k` 1; file-level and legacy scoring |
| Judge | `gpt-5.2` through `adapter/judge_relay.py` |
| Runs | one. Nothing in Sydes, prompts, budgets, cases, evaluator or harness was changed before it. |
| Comparison | the frozen Sydes 0.4.0 first-11 result |

Manual adjudication of the unmatched CONFIRMED findings is in `adjudication.md`.
Per-case numbers are in `per-case.json`.

## Validity

| Status | Cases |
|---|---|
| Valid | 10 |
| Invalid | 1: `facebook__react-32499` |

- **react-32499.** The Finder call timed out (`OpenAI request failed … Request timed out`).
- The harness marked it `INVALID(provider errors)` and excluded it from scoring; it was not retried. Its artifacts are in `invalid/` and `sydes/facebook__react@b045f18.*`.
- That case holds 16 of the 72 reference comments, so AACR scores this run against 56. On 0.4.0 it produced no findings.

## AACR view

| | 0.4.0, frozen (11 PRs) | 0.4.0, same 10 PRs\* | Investigator (10 valid PRs) |
|---|---|---|---|
| Reference comments | 72 | 56 | 56 |
| Submitted findings | 7 | 7 | 9 |
| Matched | 3 | 3 | 5 |
| Unmatched | 4 | 4 | 4 |
| File-level precision | 0.429 | 0.429 | **0.556** |
| File-level recall | 0.042 | 0.054 | **0.089** |
| Balanced score | 0.077 | 0.095 | **0.153** |
| Legacy line P / R / F1 | 0.286 / 0.028 / 0.051 | — | 0.556 / 0.089 / 0.153 |
| Legacy semantic P / R / F1 | 0.143 / 0.014 / 0.026 | — | 0.222 / 0.036 / 0.062 |

\* The same-10 column filters the frozen per-case judge output to the 10 valid
cases. It is not re-scored. The 0.4.0 run had no findings on react-32499, so
only the denominator changes.

## Product view

| Unmatched CONFIRMED finding | Class |
|---|---|
| browser-use-1482, `tests/test_browser.py:180` | VALIDATED_DEFECT (test mocks `requests`, code uses `httpx`) |
| dbeaver-38239, `DBSObjectFilter.java:153` | PLAUSIBLE_UNVERIFIED (binary break is real; whether it matters is not established) |
| gemini-cli-8154, `commentJson.ts:58` | VALIDATED_DEFECT (MCP remove doesn't delete) |
| gemini-cli-8154, `commentJson.ts:113` | VALIDATED_DEFECT (stale env mapping overwrites a new literal) |

- **Validated useful findings = 5 matched + 3 validated = 8** out of 9 submitted.
- Invalid findings: 0.
- The baseline's 4 unmatched findings were not adjudicated.

## Per case

| Case | Valid | H | L | Budget hit | C/R/U | Findings | AACR matched | Unmatched C (class) | LLM calls | In | Out | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| browser-use-1482 | yes | 3 | 12 | yes | 1/0/2 | 1 | 0 | 1 (VALIDATED) | 40 | 128.6k | 20.1k | 400 |
| browser-use-3172 | yes | 4 | 12 | yes | 1/0/3 | 1 | 1 | 0 | 40 | 139.2k | 16.8k | 325 |
| cline-1808 | yes | 3 | 12 | yes | 0/0/3 | 0 | 0 | 0 | 61 | 147.4k | 17.3k | 347 |
| dbeaver-37564 | yes | 4 | 12 | yes | 0/1/3 | 0 | 0 | 0 | 23 | 57.3k | 15.4k | 598 |
| dbeaver-38239 | yes | 1 | 4 | no | 1/0/0 | 1 | 0 | 1 (PLAUSIBLE) | 18 | 67.1k | 9.7k | 533 |
| dbeaver-38915 | yes | 4 | 12 | yes | 1/0/3 | 1 | 1 | 0 | 27 | 88.0k | 16.8k | 656 |
| react-32499 | **INVALID** | 0 | 0 | — | — | 0 | — | — | 28 | 66.1k | 8.3k | 447 |
| react-33059 | yes | 3 | 12 | yes | 0/0/3 | 0 | 0 | 0 | 34 | 68.4k | 13.0k | 274 |
| gofr-1325 | yes | 2 | 8 | no | 1/0/1 | 1 | 1 | 0 | 37 | 100.2k | 16.6k | 299 |
| gemini-cli-8154 | yes | 5 | 12 | yes | 4/0/1 | 4 | 2 | 2 (VALIDATED ×2) | 47 | 158.7k | 30.4k | 659 |
| immich-16893 | yes | 5 | 12 | yes | 0/0/5 | 0 | 0 | 0 | 54 | 137.1k | 22.6k | 883 |

Totals over the 10 valid cases:
- 34 hypotheses: 9 CONFIRMED, 1 REFUTED, 24 UNKNOWN.
- All 9 CONFIRMED became findings.

## Silent PRs

| Comparison | PRs |
|---|---|
| Silent on 0.4.0, findings with the Investigator | dbeaver-38915 (matched), dbeaver-38239 (plausible) |
| 0.4.0 findings the Investigator lost | immich-16893 (0.4.0 had 1 unmatched finding; Investigator 0); browser-use-1482 (see below) |
| Silent in both | cline-1808, dbeaver-37564, react-33059 |

- **browser-use-1482:** 0.4.0's matched finding, the `chrome_remote_debugging_port=None` TypeError, was raised again as H1 but ended UNKNOWN. The PR still has a different, validated finding.
- **react-32499** is excluded as invalid.

## Lookups

| Tool | Calls |
|---|---|
| `read_source` | 57 |
| `search_text` | 31 |
| `find_symbol` | 12 |
| `tests_for` | 4 |
| `runtime_for` | 2 |
| `callers` | 2 |
| `callees` | 0 |
| `evidence` | 0 |

- **Total:** 108 lookups, 10.8 per PR.
- **12-lookup budget hit:** 8 of 10 PRs.
- **Every investigated hypothesis used its whole per-hypothesis cap of 4.** With a per-PR budget of 12, only the first 3 hypotheses get any lookups. 6 hypotheses got none: browser-use-3172 H4, dbeaver-38915 H4, gemini H4 and H5, immich H4 and H5. Gemini H4 was confirmed from packet evidence alone.
- **Empty results:** 17.
  - 6 `tests_for` / `runtime_for` lookups were empty: AACR checkouts have no test or runtime evidence.
  - 11 `search_text` calls returned `(no matches)`. **9 of those 11 were false negatives**; see "search_text glob false negatives".
- **Lookup errors:** 1. `callers` on `GenericExecutionContext.setDefaultCatalog` failed with CBM `trace_path` "function not found".

### search_text glob false negatives

`RepoTools.search_text` expands `path_glob` with `pathlib.Path.glob`, which supports
neither brace expansion (`**/*.{ts,tsx}`) nor `|` alternation. Such a glob matches
no files, and the tool returns `(no matches)`, which looks the same as a real
absence.

Checked with `git grep` at head:
- 9 of the 11 empty searches had matches in the repository: cline L8 and L10, react-33059 L2 and L3, gemini L2, and immich L2, L3, L6 and L8.
- immich L6 and L7 are the same regex: with the brace glob it returned nothing, without it 3 hits.
- The 2 genuine no-matches were cline L3 and gofr L8.

This is a defect in an existing tool (shared with AI recovery). It is not a missing
capability. It was not fixed for this run.

## UNKNOWN hypotheses by missing fact

| Category | Count | Hypotheses |
|---|---|---|
| Repo-local fact not retrieved | 8 | 3172-H4 (0 lookups), cline-H3 (glob false negative), 38915-H2 (`initDefaultsFrom`), 38915-H3 (`DBUtils.findObject`), 37564-H1 (InputStream ownership contract), 33059-H1 (CI test-runner config; glob false negatives), gemini-H5 (0 lookups), immich-H5 (0 lookups) |
| External service/API behaviour | 5 | 1482-H2 (Chrome flag precedence), 3172-H2 (which Gemini models accept `thinking_config`), cline-H1 and H2 (Azure endpoint URL formats), immich-H4 (service-worker lifetime) |
| Ambiguous semantics or contract | 5 | 37564-H2, 33059-H2, gofr-H2, immich-H1, immich-H2 |
| External dependency behaviour | 3 | 1482-H1 (`socket.connect_ex`/httpx with port `None`), 3172-H3 (google-genai default retry policy), 37564-H4 (JDK `Files.copy` race) |
| Insufficient runtime/test evidence | 3 | 38915-H4, 33059-H3, immich-H3 |
| Other | 0 | |

## External-dependency question

1. **UNKNOWNs needing third-party dependency behaviour: 2.**
   - 1482-H1 (httpx, plus the Python stdlib `socket`) and 3172-H3 (google-genai).
   - Both are in browser-use. One more, 37564-H4, needs JDK standard-library behaviour.
2. **UNKNOWNs needing external service/API behaviour: 5** (Chrome, the Gemini API, Azure ×2, browser service workers).
3. **UNKNOWNs resolvable from facts already in the repository: 8.**
   - 3 never got a lookup because of the budget.
   - 2 were hit by the glob false negative.
   - 3 asked for repo code that was never read within the 4-lookup cap.
4. **Does it justify a read-only pinned-dependency source lookup? No.**
   - Across this run and the smoke runs, third-party source would have mattered in 3 independent PRs from 2 repositories: ragflow (Trio) and browser-use 1482 and 3172.
   - Only 1 of those is a lost AACR match on this set (browser-use-1482).
   - Two other repo-local problems are larger: 8 UNKNOWNs in 6 PRs for facts already in the repo, and 6 hypotheses with no lookups.
   - Service/API facts (5) wouldn't be helped by dependency source.

## Miss taxonomy (sample of defect-oriented AACR misses)

The taxonomy uses the four required classes. For this sample:
- **MODEL** means the fact lives outside the repository, so the current tools can't retrieve it.
- **REASONING** includes defects the Finder never raised, even though the diff was in the packet.

| Miss | Class | Why |
|---|---|---|
| browser-use-1482: `chrome_remote_debugging_port=None` TypeError (0.4.0 matched it) | MODEL | Raised as H1; the Verifier wanted `socket.connect_ex(('localhost', None))` / httpx behaviour, which is outside the repo |
| browser-use-3172: retry loop removed with no replacement | MODEL | Raised as H3. Deciding it needs google-genai's default retry policy. The removed loop itself was in the diff. |
| cline-1808: Azure detection now keys on `deepseek` | MODEL | Raised as H1/H2; needs Azure endpoint URL facts |
| dbeaver-38915: `getDefaultCatalog` may return null | CONTEXT | Raised as H3; `DBUtils.findObject` is in the repo but wasn't read within 4 lookups |
| dbeaver-37564: InputStream from `getAdapter` not closed | CONTEXT | Raised as H1; the ownership contract is repo-local and wasn't retrieved |
| react-33059: one-hook form fiber misclassified | CONTEXT | Raised as H2; reachability lives in the reconciler, which wasn't read within the cap |
| immich-16893: top-level `BroadcastChannel` in `sw-messaging` | CONTEXT | Touched by H5, which got 0 lookups (budget) |
| immich-16893: `event.origin` undefined on BroadcastChannel messages | REASONING | Never hypothesised; the line was in the diff |
| gofr-1325: custom `Headers` not applied | REASONING | Never hypothesised (2 hypotheses); the diff was in the packet |
| dbeaver-38239: NPE on a null varargs element | REASONING | Never hypothesised (1 hypothesis) |
| CLAIM | — | No validated finding overstates its evidence. The dbeaver binary-compat impact is the one borderline case. |

## Cost (same 10 valid PRs)

| Per PR | Sydes 0.4.0 | Investigator | Ratio |
|---|---|---|---|
| Seconds | 299.6 | 497.5 | **1.66×** |
| LLM calls | 25.3 (253 total) | 38.1 (381 total) | **1.51×** |
| Input tokens | 76.4k | 109.2k | **1.43×** |
| Output tokens | 8.2k | 17.9k | **2.18×** |
| Investigator-only calls | — | 15.3 (153 total) | |
| Hypotheses | — | 3.4 | |
| Lookups | — | 10.8 | |

The frozen 0.4.0 numbers over all 11 PRs are 290 s, 25.7 calls, 76.8k input and
8.4k output tokens per PR.

## Integrity

- Harness and leakage tests (`benchmarks/aacr/tests`, including `test_no_leakage.py`) and `tests/test_investigation.py` passed on `b9391e5` before the run (26 passed) and again before committing.
- AACR reference comments were read only by AACR's evaluator, and afterwards for this adjudication and miss analysis.
- Nothing from the references was given to Sydes.

## Decision

- **Repo-local primitive:** no repeated investigation failed because a repo-local fact couldn't be retrieved with the existing tools. The repo-local failures came from three things, none of them a missing relation or analysis:
  - the lookup budget (CONTEXT);
  - the `search_text` glob false negative, a defect in an existing tool;
  - one CBM `callers` name-resolution error.
- **Recommendation: `NO DETERMINISTIC ADDITION YET`.**
- **Pinned dependency-source lookup:** not justified by this sample. It is a retrieval capability, not an analysis primitive.
