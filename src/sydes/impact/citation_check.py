"""Deterministic verification for a guide-proposed candidate's citations.

An `ImpactCandidate` from the semantic guide (`guide.py`) is a hypothesis,
never evidence on its own — that discipline is unchanged by this module.
What's new: the guide may now optionally attach `CandidateCitation`s (file +
line + a literal quote of the source text the claim rests on) to a
candidate, and this module re-reads the real file to confirm the quote
actually appears there, the same "re-check the citation, don't trust it"
discipline already proven out in `discover/layer2_shared.py`'s
`citation_verified` and the standalone claim-verifier experiment
(`experiments/claim_verifier/`).

This is deliberately record-only: verifying a citation never changes
whether a candidate is accepted, corroborated, or promoted to PROVEN — see
`interpreter.py`'s `_merge_candidates`, which is unchanged in that respect.
The result is recorded on `result.llm_candidate_log` for transparency and as
groundwork for a future, separately-decided promotion policy — not acted on
here. A candidate with no citations at all is unaffected by this module
(reported as `citation_provided=False`), and behaves exactly as before this
module existed.
"""

from __future__ import annotations

from pathlib import Path

#: Lines of surrounding context checked, before and after the cited line —
#: generous relative to the single-identifier checks in
#: discover/layer2_shared.py, since a guide citation is typically a
#: freeform quoted snippet (part of a statement, a decorator, a whole
#: line), not a single token to pin exactly.
_CONTEXT_WINDOW = 5

#: A citation whose quoted text, once whitespace-normalized, is shorter than
#: this is too easy to coincidentally match anywhere and is rejected before
#: ever reading the file — the same discipline as requiring a real,
#: specific reason elsewhere in this pipeline.
_MIN_CITATION_CHARS = 8


def _normalize(text: str) -> str:
    return " ".join(text.split())


def verify_citation(
    *, file: str, line: int, citation_text: str, repo_root: Path | None,
) -> tuple[bool, str]:
    """Re-read `file` at `repo_root` and confirm `citation_text` (whitespace-
    normalized) actually appears within a bounded window around `line`.

    Returns `(verified, reason)`. `reason` is always populated, including on
    success, so a caller can log it directly without a second branch.
    """
    if repo_root is None:
        return False, "no repo_root available to verify against"
    if not file or not isinstance(line, int) or line < 1:
        return False, "citation missing a usable file/line"
    normalized_citation = _normalize(citation_text)
    if len(normalized_citation) < _MIN_CITATION_CHARS:
        return False, "citation_text too short to verify meaningfully"

    path = (repo_root / file)
    try:
        resolved = path.resolve()
        if repo_root.resolve() not in resolved.parents and resolved != repo_root.resolve():
            return False, "citation file path escapes the repository root"
        if not resolved.is_file():
            return False, f"cited file does not exist: {file}"
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return False, f"could not read cited file: {exc}"

    if not (1 <= line <= len(lines)):
        return False, f"cited line {line} out of range for {file} ({len(lines)} lines)"

    start = max(0, line - 1 - _CONTEXT_WINDOW)
    end = min(len(lines), line + _CONTEXT_WINDOW)
    window = _normalize("\n".join(lines[start:end]))

    if normalized_citation in window:
        return True, f"citation confirmed near {file}:{line}"
    return False, f"quoted text not found near {file}:{line} (checked lines {start + 1}-{end})"
