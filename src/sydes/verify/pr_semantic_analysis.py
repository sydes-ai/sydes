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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    create_default_llm_client,
)
from sydes.verify.git_change import read_unified_diff
from sydes.verify.llm_findings import _extract_json_object
from sydes.verify.models import (
    SEMANTIC_BOUNDARY_TYPES,
    ChangeSemanticAnalysis,
    ChangeSet,
    SemanticBehaviorChange,
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
    "- Describe BEHAVIOR, not syntax. Prefer 'order expiration can now vary by sales "
    "channel' over 'the code adds an if statement checking sales_channel'.\n"
    "- Never claim a caller, route, or downstream system effect exists unless the "
    "supplied files/symbols/diff actually show it. Say it is uncertain instead of "
    "inventing one — this output is a hypothesis for later reconciliation against "
    "structural evidence, never proof, never a verified impact, never itself a "
    "discovered system boundary.\n"
    "- `likely_boundary_types` (both per-hint and overall) may ONLY contain values from "
    "this fixed set: api, callable, async, external, unknown — nothing else, and it is a "
    "hint for later investigation, not a discovery.\n"
    "- Keep every list short — a handful of items, not an exhaustive catalogue.\n"
    "\n"
    "Return strict JSON only, matching exactly this shape:\n"
    '{"change_summary":"...",'
    '"behavior_changes":[{"description":"...","changed_symbols":["..."],"evidence":["..."],'
    '"confidence":0.0}],'
    '"important_symbols":[{"repo":"...","file":"...","symbol":"...","reason":"..."}],'
    '"investigation_hints":[{"description":"...","related_symbols":["..."],"concepts":["..."],'
    '"likely_boundary_types":["..."]}],'
    '"likely_boundary_types":["..."],"local_risks":["..."],"uncertainties":["..."]}'
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
    return SemanticBehaviorChange(
        description=description[:400],
        changed_symbols=_as_str_list(raw.get("changed_symbols"), cap=MAX_LIST_ITEMS),
        evidence=_as_str_list(raw.get("evidence"), cap=MAX_LIST_ITEMS),
        confidence=confidence_value,
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


def parse_semantic_analysis(raw: dict[str, Any]) -> ChangeSemanticAnalysis:
    """Parse and conservatively validate one semantic-analysis response.

    Never raises on a malformed field — an individual malformed list item is
    dropped, not treated as a reason to discard the whole result, since a
    partially-useful hypothesis is still more useful than none. The caller
    (`generate_pr_semantic_analysis`) is the one place that treats "the
    response was not even a JSON object at all" as unavailable.
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

    return ChangeSemanticAnalysis(
        change_summary=str(raw.get("change_summary") or "").strip()[:800],
        behavior_changes=behavior_changes,
        important_symbols=important_symbols,
        investigation_hints=investigation_hints,
        likely_boundary_types=_filtered_boundary_types(raw.get("likely_boundary_types")),
        local_risks=_as_str_list(raw.get("local_risks"), cap=MAX_LIST_ITEMS),
        uncertainties=_as_str_list(raw.get("uncertainties"), cap=MAX_LIST_ITEMS),
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
    return analysis, [f"pr_semantic_analysis_prompt_chars={len(prompt)}"]
