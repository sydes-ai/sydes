"""One bounded, PR-level LLM read of a change — the semantic perspective.

Complementary to Sydes' structural/CBM analysis, never a replacement for it:
this module reasons over the diff and already-computed change context as a
whole, in exactly one LLM call, and returns a `ChangeSemanticAnalysis` —
hypotheses for a reviewer (and later, structural reconciliation) to pursue,
never proof. See `ChangeSemanticAnalysis` for the explicit boundary this
output is kept on the far side of: it can never create a PROVEN/INFERRED
impact, an `AffectedFlow`, a `VerificationObligation`, or move a verdict
toward VERIFIED — nothing in this module ever touches those types.

Reuses existing machinery end to end, in the same shape as its sibling
`llm_findings.py`: the provider-neutral `LLMClient`/`create_default_llm_client`
factory, `read_unified_diff` for bounded diff context (already used nowhere
else — this module is its first real caller), and the `ChangeSet`/
`ChangedFile`/`ChangedSymbol` representation `resolve_change_set` already
produces. No new diff parser, no new symbol extractor, no new provider
abstraction, no graph traversal, no CBM call.

Graceful degradation: if `change.symbols` is empty (a language/indexing gap
left changed-symbol extraction with nothing to attribute), this pass still
runs from `change.files` and the diff text alone — it never refuses to
answer for that reason.

Evidence discipline: every `behavior_changes[]` claim may carry literal
`citations` (file/line/quote), each re-checked against the real repository
by `_apply_verification` before `ChangeSemanticAnalysis.verification_state`
is set — a claim's own stated confidence never substitutes for this. The
model may separately, explicitly flag the whole analysis `indeterminate`
(a distinct axis from verification, with a fixed machine-readable reason)
when the real answer depends on something this repository cannot tell it —
deployment config, runtime-only state, an external system, or just too
little evidence in the diff. See `ChangeSemanticAnalysis` for both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sydes.impact.citation_check import verify_citation
from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    create_default_llm_client,
)
from sydes.verify.git_change import read_unified_diff
from sydes.verify.llm_findings import _extract_json_object
from sydes.verify.models import (
    INDETERMINATE_INSUFFICIENT_REPOSITORY_EVIDENCE,
    SEMANTIC_BOUNDARY_TYPES,
    SEMANTIC_INDETERMINATE_REASONS,
    SEMANTIC_VERIFICATION_INDETERMINATE,
    SEMANTIC_VERIFICATION_PARTIALLY_VERIFIED,
    SEMANTIC_VERIFICATION_UNVERIFIED,
    SEMANTIC_VERIFICATION_VERIFIED,
    ChangeSemanticAnalysis,
    ChangeSet,
    SemanticBehaviorChange,
    SemanticCitation,
    SemanticInvestigationHint,
    SemanticKeySymbol,
)

MAX_DIFF_CHARS = 12_000
MAX_PROMPT_CHARS = 20_000
MAX_FILES_IN_CONTEXT = 40
MAX_SYMBOLS_IN_CONTEXT = 40
MAX_BEHAVIOR_CHANGES = 6
MAX_IMPORTANT_SYMBOLS = 8
MAX_INVESTIGATION_HINTS = 6
MAX_LIST_ITEMS = 6


def _build_semantic_context(*, change: ChangeSet, diff_text: str) -> dict[str, Any]:
    """The bounded context payload for the semantic pass: changed files,
    changed symbols (possibly empty), and the diff itself. No source beyond
    what the diff's own context lines already carry, no graph traversal."""
    return {
        "version": "v1",
        "base": change.base,
        "includes_working_tree": change.includes_working_tree,
        "files": [
            {
                "path": item.path,
                "change_type": item.change_type,
                "role": item.role,
                "added_lines": item.added_lines,
                "removed_lines": item.removed_lines,
            }
            for item in change.files[:MAX_FILES_IN_CONTEXT]
        ],
        "symbols": [
            {
                "repo": item.repo,
                "file": item.file,
                "name": item.qualified_name or item.name,
                "kind": item.kind,
                "change_type": item.change_type,
                "lines": (
                    f"{item.start_line}-{item.end_line}"
                    if item.start_line is not None else None
                ),
                "decorators": item.decorators,
            }
            for item in change.symbols[:MAX_SYMBOLS_IN_CONTEXT]
        ],
        "diff": diff_text[:MAX_DIFF_CHARS],
    }


def _bounded_prompt(context: dict[str, Any]) -> str:
    """Serialize the prompt, shrinking the diff first when over budget —
    same strategy as `llm_findings._bounded_prompt`."""
    payload = dict(context)
    prompt = _SEMANTIC_ANALYSIS_HEADER + "\nContext:\n" + json.dumps(
        payload, ensure_ascii=True, separators=(",", ":")
    )
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt

    diff = str(payload.get("diff") or "")
    overflow = len(prompt) - MAX_PROMPT_CHARS
    payload["diff"] = diff[: max(0, len(diff) - overflow - 200)] + "\n... [truncated]"
    prompt = _SEMANTIC_ANALYSIS_HEADER + "\nContext:\n" + json.dumps(
        payload, ensure_ascii=True, separators=(",", ":")
    )
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt

    payload["symbols"] = payload.get("symbols", [])[:10]
    prompt = _SEMANTIC_ANALYSIS_HEADER + "\nContext:\n" + json.dumps(
        payload, ensure_ascii=True, separators=(",", ":")
    )
    return prompt[:MAX_PROMPT_CHARS]


_SEMANTIC_ANALYSIS_HEADER = (
    "You are performing a PR-level SEMANTIC read of a backend code change — a "
    "complementary perspective to Sydes' own structural/graph analysis, not a "
    "replacement for it. Structural analysis may be incomplete, or the changed-symbol "
    "list below may be empty (a language/indexing gap); reason from whatever you are "
    "given — the diff and changed file list alone are enough to answer from.\n"
    "\n"
    "Answer, using only the evidence supplied below:\n"
    "1. What behavior appears to have changed?\n"
    "2. Which changed symbols/files matter most, and why?\n"
    "3. What concepts or areas of the system should structural analysis investigate?\n"
    "4. What kinds of boundaries are plausibly relevant?\n"
    "5. What local/static risks or invariants should a reviewer consider?\n"
    "6. What remains uncertain from the diff/context alone?\n"
    "\n"
    "Ground rules:\n"
    "- Distinguish what the diff directly supports, from what is merely likely, from "
    "what needs further investigation, from what cannot be established here at all — "
    "put the last category in `uncertainties` rather than guessing or inventing it.\n"
    "- Describe BEHAVIOR, not syntax: what a user or caller of the system would observe "
    "differently, not which statement was added. Never reuse example wording from these "
    "instructions themselves — describe THIS diff's own behavior, in its own terms.\n"
    "- Never claim a caller, route, or downstream system effect exists unless the "
    "supplied files/symbols/diff actually show it. Say it is uncertain instead of "
    "inventing one — this output is a hypothesis for later reconciliation against "
    "structural evidence, never proof, never a verified impact, never itself a "
    "discovered system boundary.\n"
    "- Every entry in `behavior_changes` should carry `citations`: a list of "
    "`{\"file\": \"...\", \"line\": <int>, \"quoted_text\": \"...\"}` entries, each a "
    "LITERAL, VERBATIM quote of real source text you were actually shown (from the diff "
    "or the supplied context) — never a paraphrase, a summary, or a description of what "
    "a line does. Each citation is re-checked against the real file before it counts for "
    "anything; a quote that does not actually appear at that file/line is worse than no "
    "citation at all. A `behavior_changes` entry with no verifiable citation is treated "
    "as unverified downstream, no matter how confident you are — so ground every claim "
    "you can, and where you genuinely cannot ground one, say so in `uncertainties` "
    "rather than asserting it uncited.\n"
    "- `likely_boundary_types` (both per-hint and overall) may ONLY contain values from "
    "this fixed set: api, callable, async, external, unknown — nothing else, and it is a "
    "hint for later investigation, not a discovery.\n"
    "- Keep every list short — a handful of items, not an exhaustive catalogue.\n"
    "\n"
    "Indeterminacy — read carefully, this is commonly under-used: being able to "
    "describe HOW a change would plausibly behave is not the same as knowing WHETHER it "
    "has any real observable effect in a given deployment. A confident, well-cited, "
    "completely accurate description of the MECHANISM is not, by itself, evidence that "
    "the question 'what does this change affect' has a repository-only answer — those "
    "are two separate questions, and you must answer the second one explicitly, every "
    "time, not just when you feel unsure.\n"
    "Apply this concrete test to every behavior_changes entry: does the code change "
    "itself GUARANTEE this behavior is exercised for a real user, or does it only fire "
    "IF some condition holds whose actual value you cannot see anywhere in the diff or "
    "context given to you — a config flag, which records/content/schema exist, whether a "
    "feature is enabled, what data a real installation has? If it's the latter — you can "
    "trace exactly what the code WOULD do, but not whether the precondition for it is "
    "ever true anywhere real — that is indeterminate, even though your mechanism trace "
    "is completely correct and fully citable. (Sketch, not a real case: a change that "
    "only takes effect when [some flag/setting/data you cannot observe from here] is "
    "true is indeterminate for exactly this reason, no matter how precisely you can "
    "describe what happens once it is.) When this applies, set "
    "`indeterminate.is_indeterminate` to true and choose the closest `reason` — you "
    "should still describe the mechanism in `behavior_changes`, cited as usual; the two "
    "fields are not in tension. Only set `reason` from this exact "
    "set: `deployment_config_required` (behavior depends on config/settings not in this "
    "repo), `runtime_only_behavior` (depends on state only known at runtime, e.g. "
    "feature flags, request data), `external_system_state_required` (depends on another "
    "service/system not in this repo), `insufficient_repository_evidence` (the diff/"
    "context alone just isn't enough to say). Leave `is_indeterminate` false, and "
    "`reason`/`detail` empty, when you're confident the question itself has a "
    "repository-only answer — do not set it defensively on every change.\n"
    "\n"
    "Return strict JSON only, matching exactly this shape:\n"
    '{"change_summary":"...",'
    '"behavior_changes":[{"description":"...","changed_symbols":["..."],"evidence":["..."],'
    '"citations":[{"file":"...","line":0,"quoted_text":"..."}],'
    '"confidence":0.0}],'
    '"important_symbols":[{"repo":"...","file":"...","symbol":"...","reason":"..."}],'
    '"investigation_hints":[{"description":"...","related_symbols":["..."],"concepts":["..."],'
    '"likely_boundary_types":["..."]}],'
    '"likely_boundary_types":["..."],"local_risks":["..."],"uncertainties":["..."],'
    '"indeterminate":{"is_indeterminate":false,"reason":null,"detail":"..."}}'
)


def _as_str_list(raw: Any, *, cap: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:300])
        if len(out) >= cap:
            break
    return out


def _filtered_boundary_types(raw: Any) -> list[str]:
    """Keep only values in the fixed vocabulary, in first-seen order —
    anything else is silently dropped rather than invented into the set."""
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in SEMANTIC_BOUNDARY_TYPES and item not in seen:
            seen.append(item)
    return seen


#: A citation whose quoted text is shorter than this is too easy to
#: coincidentally match anywhere and is dropped before verification is even
#: attempted — same threshold and reasoning as
#: `impact.citation_check._MIN_CITATION_CHARS`.
_MIN_CITATION_CHARS = 8


def _parse_citation(raw: Any) -> SemanticCitation | None:
    """Malformed entries are dropped individually, never failing the whole
    behavior_change — same discipline as `_parse_behavior_change` for
    `behavior_changes` and `impact.guide._parse_citations` for the guide's
    own citations."""
    if not isinstance(raw, dict):
        return None
    file = raw.get("file")
    line = raw.get("line")
    quoted_text = raw.get("quoted_text")
    if not isinstance(file, str) or not file.strip():
        return None
    if not isinstance(line, int) or line < 1:
        return None
    if not isinstance(quoted_text, str) or len(quoted_text.strip()) < _MIN_CITATION_CHARS:
        return None
    return SemanticCitation(file=file.strip(), line=line, quoted_text=quoted_text)


def _parse_behavior_change(raw: Any) -> SemanticBehaviorChange | None:
    if not isinstance(raw, dict):
        return None
    description = str(raw.get("description") or "").strip()
    if not description:
        return None
    confidence = raw.get("confidence")
    confidence_value: float | None = None
    if isinstance(confidence, int | float):
        confidence_value = max(0.0, min(1.0, float(confidence)))
    raw_citations = raw.get("citations")
    citations = [
        item for item in (
            _parse_citation(entry) for entry in raw_citations or []
        ) if item is not None
    ] if isinstance(raw_citations, list) else []
    return SemanticBehaviorChange(
        description=description[:400],
        changed_symbols=_as_str_list(raw.get("changed_symbols"), cap=MAX_LIST_ITEMS),
        evidence=_as_str_list(raw.get("evidence"), cap=MAX_LIST_ITEMS),
        confidence=confidence_value,
        citations=citations,
    )


def _parse_key_symbol(raw: Any) -> SemanticKeySymbol | None:
    if not isinstance(raw, dict):
        return None
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        return None

    def _opt_str(value: Any) -> str | None:
        return str(value).strip()[:300] or None if isinstance(value, str) else None

    return SemanticKeySymbol(
        repo=_opt_str(raw.get("repo")),
        file=_opt_str(raw.get("file")),
        symbol=_opt_str(raw.get("symbol")),
        reason=reason[:300],
    )


def _parse_investigation_hint(raw: Any) -> SemanticInvestigationHint | None:
    if not isinstance(raw, dict):
        return None
    description = str(raw.get("description") or "").strip()
    if not description:
        return None
    return SemanticInvestigationHint(
        description=description[:300],
        related_symbols=_as_str_list(raw.get("related_symbols"), cap=MAX_LIST_ITEMS),
        concepts=_as_str_list(raw.get("concepts"), cap=MAX_LIST_ITEMS),
        likely_boundary_types=_filtered_boundary_types(raw.get("likely_boundary_types")),
    )


def _parse_indeterminate(raw: Any) -> tuple[bool, str | None, str]:
    """Parse the top-level `indeterminate` object.

    Returns `(is_indeterminate, reason, detail)`. A malformed or absent
    object, or a `reason` outside `SEMANTIC_INDETERMINATE_REASONS`, is
    treated as "not indeterminate" — the model gets no benefit of the doubt
    for a badly-shaped indeterminacy claim, the same fail-closed discipline
    `impact.guide.parse_guide_decision` uses for its own strict fields.
    Exception: `is_indeterminate=true` with a missing/invalid `reason` still
    counts as indeterminate, falling back to
    `INDETERMINATE_INSUFFICIENT_REPOSITORY_EVIDENCE` — the model's explicit
    "I can't determine this" is trusted even when it picked (or omitted) the
    wrong reason code, since silently discarding that signal entirely would
    be worse than defaulting its reason.
    """
    if not isinstance(raw, dict):
        return False, None, ""
    is_indeterminate = bool(raw.get("is_indeterminate"))
    if not is_indeterminate:
        return False, None, ""
    reason = raw.get("reason")
    if not isinstance(reason, str) or reason not in SEMANTIC_INDETERMINATE_REASONS:
        reason = INDETERMINATE_INSUFFICIENT_REPOSITORY_EVIDENCE
    detail = raw.get("detail")
    detail_value = detail.strip()[:400] if isinstance(detail, str) else ""
    return True, reason, detail_value


def parse_semantic_analysis(raw: dict[str, Any]) -> ChangeSemanticAnalysis:
    """Parse and conservatively validate one semantic-analysis response.

    Never raises on a malformed field — an individual malformed list item is
    dropped, not treated as a reason to discard the whole result, since a
    partially-useful hypothesis is still more useful than none. The caller
    (`generate_pr_semantic_analysis`) is the one place that treats "the
    response was not even a JSON object at all" as unavailable.

    Purely structural: this does not touch the filesystem, so it cannot yet
    verify `behavior_changes[].citations` or compute the final
    `verification_state` — those need `repo_root` and are applied afterward
    by `_apply_verification` (called from `generate_pr_semantic_analysis`).
    An indeterminate self-report is the one exception: parsed here directly,
    since it needs no repository access and — per `_apply_verification` —
    always takes precedence over whatever citation verification finds.
    """
    behavior_changes = [
        item for item in (
            _parse_behavior_change(entry) for entry in raw.get("behavior_changes", []) or []
        ) if item is not None
    ][:MAX_BEHAVIOR_CHANGES]
    important_symbols = [
        item for item in (
            _parse_key_symbol(entry) for entry in raw.get("important_symbols", []) or []
        ) if item is not None
    ][:MAX_IMPORTANT_SYMBOLS]
    investigation_hints = [
        item for item in (
            _parse_investigation_hint(entry) for entry in raw.get("investigation_hints", []) or []
        ) if item is not None
    ][:MAX_INVESTIGATION_HINTS]
    is_indeterminate, indeterminate_reason, indeterminate_detail = _parse_indeterminate(
        raw.get("indeterminate")
    )

    return ChangeSemanticAnalysis(
        change_summary=str(raw.get("change_summary") or "").strip()[:800],
        behavior_changes=behavior_changes,
        important_symbols=important_symbols,
        investigation_hints=investigation_hints,
        likely_boundary_types=_filtered_boundary_types(raw.get("likely_boundary_types")),
        local_risks=_as_str_list(raw.get("local_risks"), cap=MAX_LIST_ITEMS),
        uncertainties=_as_str_list(raw.get("uncertainties"), cap=MAX_LIST_ITEMS),
        verification_state=(
            SEMANTIC_VERIFICATION_INDETERMINATE if is_indeterminate
            else SEMANTIC_VERIFICATION_UNVERIFIED
        ),
        indeterminate_reason=indeterminate_reason,
        indeterminate_detail=indeterminate_detail,
    )


def generate_pr_semantic_analysis(
    *,
    change: ChangeSet,
    repo_root: Path,
    model_spec: str | None = None,
    llm_client: LLMClient | None = None,
) -> tuple[ChangeSemanticAnalysis | None, list[str]]:
    """Run the one bounded PR-level semantic-analysis LLM call.

    Returns `(None, notes)` whenever a usable result could not be produced —
    no client/provider failure/unparseable output — with `notes` explaining
    why, in the same "`<name>` unavailable: ..." convention
    `_build_impact_guide` already uses. Never raises `LLMClientError` itself:
    a failed semantic pass must never crash the surrounding `verify-change`
    run, only leave `pr_semantic_analysis` absent and the reason visible.

    Exactly one LLM call, reasoning over the whole change — never one call
    per changed symbol, no agent loop, no retry beyond whatever the shared
    client already does. `temperature=None` at both the client and the
    request (matching the impact guide's own fix for the same issue) so no
    provider sees a hard-coded `temperature=0` this call didn't ask for.
    """
    client = llm_client
    if client is None:
        try:
            client = create_default_llm_client(
                model_spec=model_spec, temperature=None, stage="pr_semantic_analysis",
            )
        except LLMClientError as exc:
            return None, [f"pr_semantic_analysis unavailable: {exc}"]

    diff_text = read_unified_diff(repo_root=repo_root, base_rev=change.merge_base or change.base)
    context = _build_semantic_context(change=change, diff_text=diff_text)
    prompt = _bounded_prompt(context)

    try:
        response = client.generate(LLMRequest(prompt=prompt, temperature=None))
    except LLMClientError as exc:
        return None, [f"pr_semantic_analysis unavailable: {exc}"]

    raw = _extract_json_object(response.text)
    if raw is None:
        return None, ["pr_semantic_analysis unavailable: model output was not valid JSON."]

    analysis = parse_semantic_analysis(raw)
    analysis = _apply_verification(analysis, repo_root=repo_root)
    return analysis, [f"pr_semantic_analysis_prompt_chars={len(prompt)}"]


def _verify_behavior_change_citations(
    behavior_change: SemanticBehaviorChange, *, repo_root: Path,
) -> SemanticBehaviorChange:
    """Re-check every citation on one `behavior_change` against the real
    repository, reusing `impact.citation_check.verify_citation` — the same
    "re-read the real file, don't trust the quote" discipline already
    proven out for the impact-guide's own candidate citations. Returns a
    new `SemanticBehaviorChange` (Pydantic models are immutable-by-
    convention here) with `citations_verified`/`citation_notes` populated;
    everything else is unchanged."""
    if not behavior_change.citations:
        return behavior_change
    verified_count = 0
    notes: list[str] = []
    for citation in behavior_change.citations:
        verified, reason = verify_citation(
            file=citation.file, line=citation.line,
            citation_text=citation.quoted_text, repo_root=repo_root,
        )
        if verified:
            verified_count += 1
        notes.append(reason)
    return behavior_change.model_copy(
        update={"citations_verified": verified_count, "citation_notes": notes}
    )


def _compute_verification_state(
    behavior_changes: list[SemanticBehaviorChange],
) -> str:
    """Deterministic, evidence-only rollup across every `behavior_change`'s
    (already-verified) citations — never the model's own confidence.
    `verified` only when EVERY citation on EVERY claim checked out (and at
    least one citation exists at all); `unverified` when none did (including
    no citations supplied anywhere); `partially_verified` otherwise. Callers
    only reach this when the analysis is NOT already indeterminate — see
    `_apply_verification`, which checks that first and short-circuits."""
    total = sum(len(item.citations) for item in behavior_changes)
    verified = sum(item.citations_verified for item in behavior_changes)
    if total == 0:
        return SEMANTIC_VERIFICATION_UNVERIFIED
    if verified == total:
        return SEMANTIC_VERIFICATION_VERIFIED
    if verified == 0:
        return SEMANTIC_VERIFICATION_UNVERIFIED
    return SEMANTIC_VERIFICATION_PARTIALLY_VERIFIED


def _apply_verification(
    analysis: ChangeSemanticAnalysis, *, repo_root: Path,
) -> ChangeSemanticAnalysis:
    """The one place citation verification and `verification_state` rollup
    happen — called once, right after parsing, from
    `generate_pr_semantic_analysis`. Kept separate from `parse_semantic_analysis`
    itself so parsing stays filesystem-free and directly testable (see
    `test_pr_semantic_analysis.py`'s parsing tests, none of which touch disk).

    An indeterminate self-report (already recorded by `parse_semantic_analysis`)
    always wins: citation verification still runs and is still recorded on
    each `behavior_change` for transparency, but it never overwrites
    `verification_state` away from `indeterminate` — the model's structured
    "the answer depends on something this repo can't tell you" is a
    different question from "did your cited evidence check out," and a
    well-cited mechanism-level trace does not resolve that question either
    way.
    """
    verified_changes = [
        _verify_behavior_change_citations(item, repo_root=repo_root)
        for item in analysis.behavior_changes
    ]
    verification_state = (
        analysis.verification_state
        if analysis.verification_state == SEMANTIC_VERIFICATION_INDETERMINATE
        else _compute_verification_state(verified_changes)
    )
    return analysis.model_copy(
        update={"behavior_changes": verified_changes, "verification_state": verification_state}
    )
