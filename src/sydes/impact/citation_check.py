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

This is the same function `pr_semantic_analysis._verify_citation` calls for
`SemanticCitation`s — the one place both callers' "quoted text not found"
failures actually come from. A real evaluation run
(`sydes-examples/nestjs-boilerplate#1`) showed the model is usually right
about *what* text exists but sometimes wrong about exactly *where* (a stale
line from before the diff, an off-by-one, a citation for a multi-line
statement pinned to one line of it) — a plain fixed ±5-line window rejects
all of those as fabrications even though the quoted text is real and
present nearby. `_locate_nearest_span` below recovers those cases with a
bounded, hint-centered search (the same shape as `graph_path._locate_and_
cite`'s `_SEARCH_WINDOW_LINES`/forward-preference approach, adapted here to
a substring search rather than a whole-identifier one, and to a plain
deterministic re-read rather than an agent's own tool call). Genuinely
fabricated text is never found by widening the search window — no radius
invents text that was never in the file — so this recovers real citations
without weakening the check for wrong ones.
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

#: How far from the model's claimed line the bounded recovery search looks
#: before giving up, when the exact-line window (`_CONTEXT_WINDOW`) misses.
#: Wide enough to catch a citation drifted by a stale pre-diff line number
#: or pinned to the wrong line of a multi-line statement; still a small,
#: fixed cost per citation regardless of file size.
_RECOVERY_RADIUS_LINES = 200

#: A whole-file fallback scan (when the bounded radius above still finds
#: nothing) only runs for files at or under this many lines — "targeted
#: reads, not unbounded ones" for very long files. A citation that has
#: drifted further than `_RECOVERY_RADIUS_LINES` from its claimed line in a
#: file this large is rare enough that refusing to chase it further is the
#: safer default; it still just fails closed, the same as before this
#: module existed.
_MAX_FULL_FILE_SCAN_LINES = 20_000


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _window_text(lines: list[str], center: int) -> str:
    """Whitespace-normalized text of the small context window around
    1-indexed `center` (clamped to the file)."""
    start = max(1, center - _CONTEXT_WINDOW)
    end = min(len(lines), center + _CONTEXT_WINDOW)
    return _normalize("\n".join(lines[start - 1 : end]))


def _expanding_offsets(radius: int):
    """0, -1, +1, -2, +2, ... up to `radius` — checks the hinted line
    itself first, then walks outward symmetrically so the first match
    found is always the one nearest the hint, without a separate
    distance-comparison pass over every candidate."""
    yield 0
    for step in range(1, radius + 1):
        yield -step
        yield step


def _locate_nearest_span(
    lines: list[str], normalized_citation: str, hint_line: int, radius: int,
) -> tuple[int, int] | None:
    """Search outward from `hint_line` (1-indexed, clamped into the file)
    for a context window whose normalized text contains the citation.
    Returns the first (nearest-to-hint) match's (start, end) 1-indexed
    span, or `None` if the citation is not found anywhere in range —
    including when it is not in the file at all, which no radius changes.
    """
    n = len(lines)
    if n == 0:
        return None
    clamped_hint = max(1, min(hint_line, n))
    for offset in _expanding_offsets(radius):
        center = clamped_hint + offset
        if center < 1 or center > n:
            continue
        if normalized_citation in _window_text(lines, center):
            return max(1, center - _CONTEXT_WINDOW), min(n, center + _CONTEXT_WINDOW)
    return None


def verify_citation(
    *, file: str, line: int, citation_text: str, repo_root: Path | None,
) -> tuple[bool, str]:
    """Re-read `file` at `repo_root` and confirm `citation_text` (whitespace-
    normalized) actually appears there — at the claimed line first, then via
    a bounded search outward from it if that exact window misses.

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

    if not lines:
        return False, f"cited file is empty: {file}"

    # Exact-line attempt first: the common, correct case is unaffected by
    # anything below it, including its exact `reason` text on success.
    if 1 <= line <= len(lines) and normalized_citation in _window_text(lines, line):
        return True, f"citation confirmed near {file}:{line}"

    # Bounded recovery: the claimed line may be stale, off-by-one, or
    # pointing at the wrong line of a multi-line statement, while the text
    # itself is real. Search outward from the claimed line (clamped into
    # the file so an out-of-range line still anchors a search near the
    # nearest real one, e.g. end-of-file) before ever falling back to a
    # full-file scan, and only run that full scan for files small enough
    # that doing so is still a targeted, bounded cost.
    found = _locate_nearest_span(lines, normalized_citation, line, _RECOVERY_RADIUS_LINES)
    if found is None and len(lines) <= _MAX_FULL_FILE_SCAN_LINES:
        found = _locate_nearest_span(lines, normalized_citation, line, len(lines))
    if found is not None:
        found_start, found_end = found
        return True, (
            f"citation recovered near {file}:{found_start}-{found_end} "
            f"(model cited line {line}; the real text was located there instead)"
        )

    if not (1 <= line <= len(lines)):
        return False, f"cited line {line} out of range for {file} ({len(lines)} lines)"
    start = max(1, line - _CONTEXT_WINDOW)
    end = min(len(lines), line + _CONTEXT_WINDOW)
    return False, f"quoted text not found near {file}:{line} (checked lines {start}-{end})"
