# AACR-Bench harness for Sydes Code Review

[AACR-Bench](https://github.com/alibaba/aacr-bench) (Alibaba, Apache-2.0; paper
[arXiv:2601.19494](https://arxiv.org/abs/2601.19494)) is a published benchmark of
real pull requests with expert-verified review comments. This harness uses it to
measure **one part of Sydes only: Code Review / bug-finding quality**.

**AACR results are not a score for the Sydes product.** AACR doesn't measure
blast radius, route and flow mapping, runtime evidence, test mapping,
verification gaps or DiffGenome. Those are tested on the separate cases in
[`../sydes-cases/manifest.json`](../sydes-cases/manifest.json), which are never
mixed into AACR scoring.

## What is scored, and what is excluded

Submitted: only Sydes' Code Review findings, the `code_findings` of a
`sydes verify-change` result.

Excluded from the submission (some kept in telemetry, never scored):
- the change summary;
- blast radius and impact;
- established routes and flows;
- runtime mappings;
- test mappings and evidence;
- verification status;
- framework gaps;
- the semantic pass's `local_risks`, which are not Code Review findings even
  though the GitHub comment shows P0/P1 ones under "Code review".

Sydes is **not** tuned to produce style or maintainability comments to raise AACR scores.

## AACR provenance (pinned version)

| Item | Value |
|---|---|
| Repository | `alibaba/aacr-bench` |
| Pinned commit | `ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df` (2026-10-15) |
| Evaluator code | `evaluation/` at that commit; `judge.py`/`evaluate.py` last changed in `4f00a6f` |
| Licence | Apache-2.0 |
| Positive samples | `dataset/positive_samples.json`: **196 PRs, 1,506 reference comments**, 50 repositories |
| Negative samples | `dataset/negative_samples.json`: 155 PRs (151 also in the positive file), 639 comments. AACR's docs don't describe them, and its converter reads only the positive file. OpenQodex's docs treat them as rejected comments. |
| Paper and README vs repository | Both say **200 PRs** and the paper says **1,505 comments**; the pinned repository has **196 PRs and 1,506 comments**. These numbers use the repository. |

Languages (positive samples): C++ 33, Java 30, TypeScript 29, Go 22, Python 21,
C 19, JavaScript 11, PHP 11, C# 10, Rust 10.

Reference comments by category:

| Category | Comments |
|---|---|
| Code Defect | 710 |
| Maintainability and Readability | 626 |
| Performance | 117 |
| Security Vulnerability | 53 |

By context: 754 diff-level, 518 file-level, 234 repository-level. By author:
1,114 marked AI-generated (and expert-verified), 392 human-written.

**The ground truth is more than defects.** Sydes Code Review looks for defects, so
the 626 maintainability and readability comments, and many of the performance
ones, are outside what it tries to find. Read recall with that in mind: a
defect-only reviewer's reachable recall is well below 100%.

## Adapter and data flow

```
AACR case            cases/<subset>-supported.json: repo, PR, base, head (no reference comments)
  -> Sydes review    adapter/aacr.py review: clone, check out head, diff against the review base,
                     run `sydes verify-change` from the released package
  -> Code Review     adapter/to_aacr.py: code_findings only
     extraction
  -> AACR format     results/<run>/aacr/<instance>.json in AACR's generic-comment shape (its `ocr`
                     reader): {path, start_line, end_line, content}
  -> evaluator       adapter/aacr.py eval: AACR's own pipeline, unchanged, at the pinned commit,
                     with default (file-level) and legacy (--use-deprecated-score) scoring
```

**Review base.** AACR gives `source_commit` (base) and `target_commit` (head). In
all three `dev-smoke` cases the base is not an ancestor of the head, so the review
diff is taken against their merge base, the three-dot `base...head` diff AACR's own
reviewers use. Several merge bases: newest committer date first, then the smallest
commit id (OpenQodex's rule). On these cases the merge-base diff is 11–16 changed
lines, matching AACR's `change_line_count`; diffing from `source_commit` directly
would have pulled in up to 76,000 unrelated lines.

**Field mapping.**
- `path` ← finding `file`
- `start_line` = `end_line` ← finding `line`. Sydes reports one line, so no range
  is invented from evidence spans, and a missing line stays `null`, which AACR
  reads as the whole file.
- `content` ← title, explanation, `Impact:` and `Fix:`.
- `severity` is archived; AACR doesn't score it.
- A failed or timed-out Sydes run still writes an empty result file, so the case
  stays in AACR's denominators.

**Judge.** The model is `gpt-5.2` (served as `gpt-5.2-2025-12-11`), the model
OpenQodex used for its AACR board. AACR's `judge.py` sends `temperature`, `top_p`,
`max_tokens` and `extra_body.chat_template_kwargs`, and OpenAI rejects
`chat_template_kwargs` (HTTP 400). `adapter/judge_relay.py` is a loopback-only
relay that changes exactly four request parameters:
- removes `chat_template_kwargs`;
- removes `temperature`;
- removes `top_p`;
- renames `max_tokens` to `max_completion_tokens`.

This is the same rewrite OpenQodex's relay applies. Prompts, messages, responses,
labels, scoring and evaluator code are untouched, and AACR's evaluator gets a
placeholder key while the real one stays in the relay. Other settings: reader
`ocr`, `line-k` 1, one evaluation round.

**No leakage.** The review stage reads only repository, PR number and commits.
The case files hold no reference fields, and Sydes sees only the checkout.
Reference comments, AACR's converted dataset and evaluator outputs enter only at
`eval`. `tests/test_no_leakage.py` runs the real `review` stage with git and Sydes
stubbed and fails if it opens anything but the case file and its own outputs.

**Sydes under test.** The released PyPI package `sydes==0.4.0` with
`openai:gpt-5.6-sol`, run with the normal command,
`--llm-policy auto --impact-guide auto --code-review`, through `uv tool run`. It
is never the working tree. The harness clears `VIRTUAL_ENV`/`PYTHONPATH`, so
Sydes can't mistake the harness's own environment for the target repository's.

Files: `adapter/aacr.py` (stages `prepare`, `review`, `eval`, `validate`,
`summarize`, `variance`), `adapter/to_aacr.py`, `adapter/judge_relay.py`,
`adapter/select_cases.py`, `config/aacr.json`, `cases/*.json`, `tests/`,
`baselines/`.

## Case selection

The base is OpenQodex's published AACR split, `benchmark/splits/aacr-v1.json` at
`openqodex/openqodex` commit `92d2063` (Apache-2.0). It assigns whole repository
groups to dev or test by `sha256(salt, group)`:
- `dev-smoke` = the smallest development PR per language;
- `dev-laptop` = 12 development PRs from each third by change size.

**Inclusion rule (declared in advance):** keep the subset's cases, in its order,
whose `project_main_language` is one of Python, TypeScript, JavaScript, Java, Go
or Rust. Nothing else is filtered, and nothing is chosen by Sydes' results.
`adapter/select_cases.py` regenerates both files, and `aacr.py validate` checks
each case against AACR's own converted repo, base and head.

### `dev-smoke` (3 of 5 OpenQodex cases)

| Case ID | Repo | Language | Base | Head |
|---|---|---|---|---|
| `cline__cline-1808` | cline/cline | TypeScript | `4d8bdf2` | `cb4d5c9` |
| `infiniflow__ragflow-6691` | infiniflow/ragflow | Python | `0a42e57` | `a63caa1` |
| `linera-io__linera-protocol-3151` | linera-io/linera-protocol | Rust | `ad50802` | `8fb32bb` |

### `dev-laptop` (23 of 36 OpenQodex cases)

Python: `browser-use__browser-use-1482`, `browser-use__browser-use-3172`,
`vllm-project__vllm-19756`. TypeScript: `cline__cline-1808`,
`google-gemini__gemini-cli-8154`, `immich-app__immich-16893`,
`immich-app__immich-17879`, `immich-app__immich-18043`. JavaScript:
`facebook__react-32499`, `facebook__react-33059`, `mrdoob__three.js-30076`. Java:
`dbeaver__dbeaver-37564`, `dbeaver__dbeaver-38239`, `dbeaver__dbeaver-38915`,
`kestra-io__kestra-7191`. Go: `gofr-dev__gofr-1325`,
`microsoft__typescript-go-1000`, `microsoft__typescript-go-691`,
`ollama__ollama-8938`, `ollama__ollama-9654`, `ollama__ollama-9746`. Rust:
`juspay__hyperswitch-7353`, `linera-io__linera-protocol-3151`.

Excluded by the language rule: 13 C, C++, C# and PHP cases. Full base and head
SHAs are in `cases/*.json`.

## Baseline: Sydes 0.4.0, `dev-smoke`, 3 identical runs (2026-10-25)

Artifacts: `baselines/2026-10-25-sydes-0.4.0-dev-smoke/` holds, per run, AACR's
metrics files (`eval-file_level/`, `eval-legacy/`, with `evaluator.log`), the
submitted result files, telemetry, the judge counts and the filtered dataset,
plus `variance.json`.

| Metric (AACR-native unless marked) | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Reference comments (`expected_notes`) | 10 | 10 | 10 |
| Findings submitted (`generated_notes`) | 1 | 1 | 0 |
| Precision, file-level (`file_semantic_match_rate`) | 1.000 | 1.000 | 0.000 |
| Recall, file-level (`file_semantic_recall_rate`) | 0.100 | 0.100 | 0.000 |
| Balanced score | 0.182 | 0.182 | 0.000 |
| Legacy semantic precision / recall / F1 | 1.0 / 0.1 / 0.182 | 1.0 / 0.1 / 0.182 | 0 / 0 / 0 |
| Legacy line precision / recall / F1 | 1.0 / 0.1 / 0.182 | 1.0 / 0.1 / 0.182 | 0 / 0 / 0 |
| Noise rate (derived: unmatched ÷ submitted) | 0.0 | 0.0 | n/a (none submitted) |

**Issue recall** is OpenQodex's own human-ruled metric, not an AACR field, so it isn't reported.

The only finding: `infiniflow__ragflow-6691`, `graphrag/entity_resolution.py:114`
(P1), a loop variable captured late by a scheduled lambda. In runs 1 and 2 the
judge matched it to the same reference comment. `cline-1808` and
`linera-protocol-3151` got no findings in any run.

### Variance (same inputs, 3 runs)

Findings are matched across runs by file and line (within 3 lines), never by
ground truth.

| Case | Findings per run | 3/3 | 2/3 | 1/3 |
|---|---|---|---|---|
| `cline__cline-1808` | 0, 0, 0 | 0 | 0 | 0 |
| `infiniflow__ragflow-6691` | 1, 1, 0 | 0 | **1** (same file and line, reworded title) | 0 |
| `linera-io__linera-protocol-3151` | 0, 0, 0 | 0 | 0 | 0 |

The metric range is balanced score 0.000–0.182: the single defect Sydes found
appears in 2 of 3 runs. That's the run-to-run variance (bucket D) seen elsewhere,
and it isn't fixed here.

### Cost and latency (released Sydes, all stages)

| Per PR | cline (TS) | ragflow (Py) | linera (Rust) | Mean of 9 reviews |
|---|---|---|---|---|
| Wall time | 96–130 s | 202–245 s | 76–102 s | 144 s |
| LLM calls | 21–34 | 31–46 | 12–17 | 26 |
| Input tokens | 61k–90k | 106k–135k | 41k–47k | 79k |
| Output tokens | 5.2k–6.7k | 9.8k–11.6k | 3.7k–5.4k | 7.1k (3.7k reasoning) |

Aggregate per 3-case run: about 430 s, 233k–244k input and 21k output tokens.

**Most of this cost is outside Code Review.** Of the roughly 26 calls per PR,
Code Review is 1. AI recovery accounts for 8–22, the impact guide up to 8, and
semantic analysis, route discovery and boundary reasoning 1 each. The judge used
3 `gpt-5.2` calls per run (about 1.3k tokens).

Estimated USD cost: unavailable. Sydes reports none without a configured price,
and no verified price for `gpt-5.6-sol` is recorded here. Set
`SYDES_AACR_PRICE_IN` / `SYDES_AACR_PRICE_OUT` to compute one.

## Comparison with OpenQodex

OpenQodex publishes AACR numbers. Its configuration differs from ours:
- AACR commit `68a5697`, not `ee9d7fe`;
- the full 36-case `dev-laptop` subset, including C, C++, C# and PHP;
- legacy scoring;
- judge `gpt-5.2` on Azure.

Those numbers are therefore **not directly comparable** reference points, not
something Sydes is ranked against. A comparable run needs the same AACR commit
and the same cases, in the same scoring mode.

## Limitations

- AACR measures **only Code Review**, not Sydes' overall quality.
- **Unsupported languages are excluded:** C, C++, C# and PHP.
- **No runtime evidence** for most AACR cases: the benchmark repositories' test
  environments aren't installed, and Sydes reports that honestly.
- **Incomplete ground truth:** a valid finding AACR doesn't list counts against precision.
- **Non-defect ground truth** (about 47% maintainability and readability) lowers
  the recall a defect-only reviewer can reach.
- **Small sample:** 3 PRs × 3 runs is a pipeline baseline, not a quality estimate.
  Use `dev-laptop` for that.

## Running it

From the Sydes repository root, with `OPENAI_API_KEY` set:

```bash
A="uv run --no-project --python 3.12 python benchmarks/aacr/adapter/aacr.py"
$A prepare
$A validate --cases benchmarks/aacr/cases/dev-smoke-supported.json
$A review   --cases benchmarks/aacr/cases/dev-smoke-supported.json --run baseline-r1
$A eval     --cases benchmarks/aacr/cases/dev-smoke-supported.json --run baseline-r1
$A validate --cases benchmarks/aacr/cases/dev-smoke-supported.json --run baseline-r1
$A summarize --run baseline-r1
$A variance --runs baseline-r1 baseline-r2 baseline-r3
uv run --no-project --python 3.12 --with pytest python -m pytest benchmarks/aacr/tests
```

Clones are cached in `~/.cache/sydes-aacr` (`SYDES_AACR_CACHE`), and run outputs
go to `results/` (git-ignored).
