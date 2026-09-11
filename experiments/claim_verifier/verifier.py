"""Phase B: a deterministic claim/citation verifier for Layer 2-shaped
edges -- the "meta-language / proof-term" idea made concrete and testable.

An edge (`{kind, user_file, user_symbol, used_file, used_symbol, line}`) is
a CLAIM: "user_symbol references used_symbol, and here is where." This
module does not re-derive that claim (the extractors already did) -- it
checks whether the claim's own citation actually holds up against the real
file on disk, the same way a proof assistant's kernel re-checks a proof
term instead of trusting the tactic that produced it.

Two independent checks, deliberately kept separate:

1. CITATION check (`verify_edge_citation`): does `used_symbol` actually
   appear, as a whole identifier (not a substring match -- "Duration"
   must not match inside "DurationLimitError"), within a small window of
   the cited line in the cited file? A fabricated or stale line/symbol
   fails this, not a hypothesis -- a direct re-read of the file.

2. CHAIN-COMPOSITION check (`verify_chain`): for a claimed multi-hop path,
   do adjacent edges actually connect -- is edge[i]'s `user_symbol` the
   same as edge[i+1]'s `used_symbol` (the standard shape this experiment's
   backward-BFS already assumes), AND, whenever both edges recorded a file
   for that shared name, is it the SAME file? Two different symbols that
   happen to share a bare name in different files is exactly the
   `SymbolIdentity` collision class this whole engagement has flagged
   before (see impact/interpreter.py's `_FactIndex`, keyed by full
   identity for this reason) -- this check exists specifically to catch
   that failure mode in a chain, not just to check each hop in isolation.
   When one side of a hop has no recorded file (READS_MEMBER edges never
   set `used_file` -- see member_access_extractor.py), the file-identity
   check for that hop is marked UNVERIFIED rather than silently passed.

Not wired into any production path.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

CONTEXT_WINDOW = 2       # lines BEFORE the cited line, for a decorator/
                         # annotation that precedes it.
MAX_STATEMENT_LINES = 15  # cap on how far a single call/signature can span
                          # forward -- same order of magnitude as
                          # deterministic_routes.py's own
                          # _MAX_DECORATOR_LOOKAHEAD precedent, not
                          # invented fresh for this check.


def _statement_span(lines: list[str], line_idx: int) -> tuple[int, int]:
    """The cited line plus however many further lines its own open
    parens/brackets span (a multi-line call or function signature), found
    by tracking paren depth rather than assuming a fixed window -- a real
    false-rejection was found empirically without this (a citation on a
    multi-line async call's opening line, with the actual argument several
    lines further down)."""
    start = max(0, line_idx - CONTEXT_WINDOW)
    depth = 0
    idx = line_idx
    while idx < len(lines) and idx < line_idx + MAX_STATEMENT_LINES:
        depth += lines[idx].count("(") + lines[idx].count("[") - lines[idx].count(")") - lines[idx].count("]")
        idx += 1
        if depth <= 0:
            break
    end = min(len(lines), max(idx, line_idx + 1))
    return start, end


@dataclass(frozen=True)
class CitationResult:
    edge: dict
    verified: bool
    reason: str


@dataclass(frozen=True)
class ChainHopResult:
    edge: dict
    citation: CitationResult
    adjacency_ok: bool
    file_identity: str  # "confirmed" | "unverified" | "mismatch" | "n/a (first hop)"


@dataclass(frozen=True)
class ChainResult:
    verified: bool
    hops: list = field(default_factory=list)
    reason: str = ""


def _whole_word_present(name: str, text: str) -> bool:
    if not name:
        return False
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def _phrase_present(phrase: str, text: str) -> bool:
    """Like `_whole_word_present`, but for a `receiver.member`-shaped
    phrase: word boundaries at the outer ends only, whitespace tolerated
    around the dot (`settings.max_output_duration_s` and
    `settings . max_output_duration_s` both count; a member access is
    never written with extra identifier characters glued to either end)."""
    parts = [re.escape(p) for p in phrase.split(".")]
    pattern = r"\s*\.\s*".join(parts)
    return re.search(rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])", text) is not None


def verify_edge_citation(edge: dict, repo_root: Path) -> CitationResult:
    """Re-read the actual cited file/line; confirm the claim's own literal
    evidence really appears there.

    For most edge kinds (`class_field_type_reference`,
    `call_argument_reference`, `function_parameter_type_reference`),
    `used_symbol` names a real identifier written verbatim at the site, so
    checking for it directly is correct.

    `reads_member` is different: `used_symbol` there is a RESOLVED type
    (`Settings`), not literal text -- the site itself only ever contains
    the receiver instance (`settings.max_output_duration_s`), never the
    type name. Verifying `used_symbol` against that kind of edge would
    always fail, for a reason that has nothing to do with whether the
    claim is true -- so this kind requires the extractor to additionally
    supply `citation_text` (the actual `receiver.member` phrase), which is
    what gets checked instead. An edge of this kind with no
    `citation_text` cannot be verified by this checker at all -- reported
    as such, not silently passed."""
    user_file = edge.get("user_file")
    line = edge.get("line")
    used_symbol = edge.get("used_symbol")
    citation_text = edge.get("citation_text")

    if not user_file or not isinstance(line, int):
        return CitationResult(edge, False, "missing user_file/line -- not a checkable citation")

    if edge.get("kind") == "reads_member" and not citation_text:
        return CitationResult(
            edge, False,
            "reads_member edge has no citation_text (receiver.member) -- "
            "used_symbol is a resolved type, not literal text, so it cannot be checked directly",
        )

    needle = citation_text or used_symbol
    if not needle:
        return CitationResult(edge, False, "no used_symbol or citation_text to check")

    path = (repo_root / user_file)
    if not path.is_file():
        return CitationResult(edge, False, f"cited file does not exist: {user_file}")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not (1 <= line <= len(lines)):
        return CitationResult(edge, False, f"cited line {line} out of range for {user_file} ({len(lines)} lines)")

    start, end = _statement_span(lines, line - 1)
    window_text = "\n".join(lines[start:end])

    present = _phrase_present(needle, window_text) if "." in needle else _whole_word_present(needle, window_text)
    if not present:
        return CitationResult(
            edge, False,
            f"'{needle}' not found near {user_file}:{line} (checked lines {start + 1}-{end}) "
            f"-- citation does not support the claim",
        )

    return CitationResult(edge, True, f"'{needle}' confirmed present near {user_file}:{line}")


def _resolves_via_one_hop_reexport(
    importing_file: str, symbol: str, target_file: str, repo_root: Path,
) -> bool:
    """True when `importing_file` imports `symbol` FROM a module that
    resolves to `target_file`, exactly one hop (a package `__init__.py`
    re-exporting a name from a sibling module, e.g.
    `from .schemas import OpenAISpeechRequest`). Deliberately as narrow as
    Layer 2's own one-hop cross-file resolution (`member_access_extractor.
    py`'s `_module_relative_to_file`) -- relative imports only, one hop,
    real ast.ImportFrom nodes only, never a name-string guess."""
    path = repo_root / importing_file
    if not path.is_file():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return False

    importing_dir = Path(importing_file).parent
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if symbol not in {alias.name for alias in node.names}:
            continue
        if node.level < 1:
            continue  # absolute imports: out of scope for this bounded check
        base = importing_dir
        for _ in range(node.level - 1):
            base = base.parent
        module = (node.module or "").replace(".", "/")
        candidate = (base / f"{module}.py") if module else (base / "__init__.py")
        if candidate.as_posix() == Path(target_file).as_posix():
            return True
    return False


def verify_chain(edges_in_order: list[dict], repo_root: Path) -> ChainResult:
    """`edges_in_order[i]` must connect to `edges_in_order[i+1]`: the thing
    edge i's user_symbol names must be the same thing edge i+1 claims as
    its used_symbol -- otherwise the "chain" is just a sequence of
    independently-plausible but disconnected claims strung together."""
    hops: list[ChainHopResult] = []

    for i, edge in enumerate(edges_in_order):
        citation = verify_edge_citation(edge, repo_root)

        if i == 0:
            adjacency_ok = True
            file_identity = "n/a (first hop)"
        else:
            prev = edges_in_order[i - 1]
            adjacency_ok = prev.get("user_symbol") == edge.get("used_symbol")
            prev_file = prev.get("user_file")
            this_used_file = edge.get("used_file")
            if this_used_file is None:
                file_identity = "unverified"
            elif prev_file == this_used_file:
                file_identity = "confirmed"
            elif _resolves_via_one_hop_reexport(this_used_file, edge.get("used_symbol", ""), prev_file, repo_root):
                file_identity = "confirmed_via_reexport"
            else:
                file_identity = "mismatch"

        hops.append(ChainHopResult(edge, citation, adjacency_ok, file_identity))

    all_citations_ok = all(h.citation.verified for h in hops)
    all_adjacency_ok = all(h.adjacency_ok for h in hops)
    no_file_mismatch = all(h.file_identity != "mismatch" for h in hops)

    verified = all_citations_ok and all_adjacency_ok and no_file_mismatch
    reason = "chain verified" if verified else _first_failure_reason(hops)
    return ChainResult(verified=verified, hops=hops, reason=reason)


def _first_failure_reason(hops: list[ChainHopResult]) -> str:
    for i, hop in enumerate(hops):
        if not hop.citation.verified:
            return f"hop {i}: citation failed -- {hop.citation.reason}"
        if not hop.adjacency_ok:
            return f"hop {i}: does not connect to previous hop (user_symbol/used_symbol mismatch)"
        if hop.file_identity == "mismatch":
            return f"hop {i}: same symbol name but DIFFERENT files across the hop -- likely a name collision, not a real chain"
    return "unknown failure"
