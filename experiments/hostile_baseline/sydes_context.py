"""Oracle-free Sydes structural facts for the 3 hostile cases, extracted
directly from real `sydes verify-change` runs against the real repos at
the exact target commit (see cases.py for shas / repo scoping).

Deliberately excludes each run's `pr_semantic_analysis` (LLM-hypothesis)
block: on hostile-h3-strapi, that pass hallucinated an entirely unrelated
"order expiration varies by sales channel" narrative at 0.9 confidence for
a CMS media-field diff -- a real, newly-discovered Sydes bug, not fixed as
part of this benchmark per this round's explicit "no new deterministic
work" instruction. Handing a hallucinated narrative to variant C would
unfairly bias the frontier-model comparison, so only the deterministic/
structural fields below (changed symbols, headline, verdict, risk,
runtime dependencies) are included -- the same "oracle-free structured
facts, no gt/expected_path language" discipline as the prior round's
GO_CONTEXT/JAVA_CONTEXT/RUST_CONTEXT.
"""

from __future__ import annotations

H1_CONTEXT = """
## Sydes structural analysis (oracle-free facts, not a verdict)

Sydes (a static change-impact tool) analyzed this diff structurally and
reports:
- 9 changed symbols identified across 2 source files: JobRepository.{startWorkers,watchWorkers,teardown,checkWorkers} and 3 anonymous `if` blocks in src/repositories/job.repository.ts; QueueService.{onBootstrap,onShutdown} and 1 anonymous `if` block in src/services/queue.service.ts.
- Verdict: VERIFICATION INCOMPLETE. Risk: MEDIUM. Reason given: "change could not be tied to any verifiable behavior."
- Headline: "9 changed symbol(s); no route flow resolved to them."
- 0 affected HTTP/route flows resolved. 0 accepted impacts (neither proven nor inferred). 0 symbols reported as unresolved-identity (the 9 symbols above were identified, they just don't reach any known entrypoint through Sydes' own structural graph).
- 2 runtime dependencies detected in the wider repo (unrelated to this diff specifically): Redis (used elsewhere for websocket adapter / caching).
This is Sydes' own structural result, not a ground truth oracle -- it may be incomplete.
""".strip()

H2_CONTEXT = """
## Sydes structural analysis (oracle-free facts, not a verdict)

Sydes (a static change-impact tool) analyzed this diff structurally and
reports:
- 0 changed symbols identified in any of the 3 changed files (server/src/bootstrap.ts, server/src/services/ai-metadata-jobs.ts, server/src/__tests__/bootstrap.test.ts) -- Sydes' symbol extractor did not recognize any top-level symbol change here (this diff is a pure deletion inside an existing object-literal-style service and an async function body; both are file/module changes without a named export changing).
- Verdict: VERIFICATION INCOMPLETE. Risk: MEDIUM. Reason given: "change could not be tied to any verifiable behavior."
- Headline: "0 changed symbol(s); no route flow resolved to them."
- 0 affected HTTP/route flows. 0 accepted impacts. 0 runtime dependencies detected for this diff.
This is Sydes' own structural result, not a ground truth oracle -- treat the "0 changed symbols" outcome as a real gap (a genuine miss), not evidence the diff has no effect.
""".strip()

H3_CONTEXT = """
## Sydes structural analysis (oracle-free facts, not a verdict)

Sydes (a static change-impact tool) analyzed this diff structurally and
reports:
- 1 changed symbol identified: getMediaAttributes (new function) in utils/src/content-types.ts. The other changed file (content-manager/server/src/services/document-metadata.ts) produced 0 recognized changed symbols (Sydes' extractor did not resolve the modified default-export service method as a named symbol change).
- Verdict: VERIFICATION INCOMPLETE. Risk: MEDIUM. Reason given: "change could not be tied to any verifiable behavior."
- 0 structural propagation path was established from the changed symbols to any entrypoint. 0 runtime dependencies, 0 affected boundaries detected.
- The test suite could not be run in this environment ("jest is declared ... but not installed").
This is Sydes' own structural result, not a ground truth oracle.
""".strip()

CONTEXT_BY_CASE = {
    "hostile-h1-immich": H1_CONTEXT,
    "hostile-h2-strapi": H2_CONTEXT,
    "hostile-h3-strapi": H3_CONTEXT,
}
