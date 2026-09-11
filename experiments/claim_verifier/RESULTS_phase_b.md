# Phase B — deterministic claim/citation verifier

Experiment only. `main` untouched. No file under `src/sydes/` modified.
Branch: `experiment/claim-verifier` (off `experiment/layer2-generic-edges`).

## Question

The "meta-language / proof-term" idea from the sci-fi discussion, made
concrete: can a claimed edge (declaration-reference, member-access) be
independently re-checked against the real file it cites, deterministically,
the way a proof assistant's kernel re-checks a proof term instead of
trusting the tactic that produced it? And does chain composition (several
edges strung into a reachability path) need its own, separate check beyond
verifying each edge alone?

## Method

1. Regenerated real Layer 2 edges FRESH from Kokoro-FastAPI's own
   `py-real-01-max-output-duration` branch content (not a stale JSON
   snapshot from a previous round) via `build_fixture.py`, reusing
   `experiments/layer2_generic_edges`'s existing extractors unmodified.
2. Built `verifier.py`: `verify_edge_citation` (does the cited file/line
   really contain the claimed evidence?) and `verify_chain` (do adjacent
   edges actually connect, and does a shared symbol name resolve to the
   SAME file across the hop — the `SymbolIdentity`-bridging risk flagged in
   the production-integration critique, checked directly rather than left
   as a concern).
3. Ran it against all 151 real edges, the known Settings→`create_speech`
   chain end-to-end, and a deliberately fabricated citation.

## Three real bugs found and fixed while building this — reported honestly, not smoothed over

**1. Branch/state ordering bug (in the experiment harness itself).** The
first run silently verified real citations against Kokoro-FastAPI's
`master` branch, after `build_fixture.py` had already restored it there —
rejecting dozens of genuinely correct edges for a reason that had nothing
to do with their validity. Fixed by having `run_verification.py` check out
the same branch the edges were extracted from before verifying, with the
same status-guard/restore discipline as `rerun_kokoro_cases_v2.py`.

**2. Fixed-size context window too narrow for real multi-line statements.**
A citation on a multi-line async call's opening line, with the actual
argument several lines further down (`tts_service.generate_audio_stream(\n
... writer=writer,\n)`), false-rejected. Fixed by tracking paren/bracket
depth from the cited line forward (`_statement_span`, capped at 15 lines —
same order of magnitude as `deterministic_routes.py`'s own
`_MAX_DECORATOR_LOOKAHEAD` precedent) instead of a fixed ±2-line window.

**3. A genuine schema gap for `reads_member` edges.** This edge kind's
`used_symbol` is a *resolved type* (`Settings`), not literal text — the
real site only ever contains the receiver instance
(`settings.max_output_duration_s`). Checking `used_symbol` directly against
the source can never succeed, for a reason unrelated to the claim's truth.
The current edge schema (as `member_access_extractor.edges_only` produces
it) discards the `receiver`/`member` fields a citation check actually
needs. Fixed *for this experiment* by carrying a `citation_text` field
(`f"{receiver}.{member}"`) alongside the resolved type — **this is a
concrete requirement for Phase D**: the production edge schema must keep
literal citation text, not just the resolved semantic fact, or a
production claim-verifier would have nothing to check for this kind.

## Results (after fixes)

| Check | Result |
|---|---:|
| Citation check, all 151 real edges | **151/151 verified, 0 rejected** |
| Known chain: `Settings → _within_input_limit → InputText → OpenAISpeechRequest → create_speech` | **verified end-to-end**, all 4 hops |
| Fabricated citation (`used_symbol` replaced with a nonexistent name) | **correctly rejected** |
| Chain with broken adjacency (two real, individually-valid citations, not actually connected) | **correctly rejected** ("does not connect to previous hop") |
| Chain with a genuine cross-file name collision (`helper` in two different files) | **correctly rejected** ("likely a name collision, not a real chain") |
| Chain hop resolved only through a package re-export (`structures/__init__.py` re-exporting `OpenAISpeechRequest` from `.schemas`) | **correctly confirmed** via a bounded one-hop import check, not silently passed or falsely rejected |

10 unit tests (`test_verifier.py`), all hermetic (`tmp_path` fixtures, no
git branch switching needed to run them), covering every case above plus
the substring-vs-whole-word guard (`Duration` must not match inside
`DurationLimitError` — the same string-coincidence class already fixed
once in `member_access_treesitter.py`'s `info` collision).

## What this validates about the proof-term idea

- A deterministic checker CAN distinguish a real, well-formed claim from a
  fabricated one, and does so on a real, non-trivial multi-hop chain, not
  a toy example.
- The chain-composition check catches a failure mode per-edge citation
  checking alone cannot: two individually-true citations that don't
  actually connect, or connect only by name coincidence across different
  files.
- The idea only works if the edge schema PRESERVES enough literal
  provenance to check against — a resolved semantic fact alone (as
  `reads_member`'s current production shape does) is not sufficient; this
  is a real, scoped requirement for Phase D, not a hypothetical concern.

## Recommendation

Feed directly into Phase D (`feature/layer2-evidence-integration`): every
edge kind must carry a `citation_text` (or equivalent literal-evidence)
field, not just resolved symbol names — and any promotion from Likely to
Established should be required to pass both `verify_edge_citation` (per
edge) and `verify_chain` (for the full path), reusing this exact checker
rather than re-inventing verification logic per edge kind.

**merge experiment? NO.** Branch `experiment/claim-verifier` pushed to
`sydes-ai/sydes`, no PR opened, `main` untouched.
