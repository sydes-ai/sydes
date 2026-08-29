"""Symbol attribution span: which source lines semantically belong to a
changed symbol, for the sole purpose of deciding whether a PR changed it.

CBM's own `[start_line, end_line]` is the base span, and it is correct for
the declaration keyword through the body. It is not always correct for the
declaration's own ATTACHED METADATA: a decorator (Python, TypeScript) or an
outer attribute (Rust) sitting immediately above the declaration keyword is,
semantically, part of the declaration — changing it changes the symbol's
behavior — but the extractor records the span starting at the keyword
itself, excluding it. Empirically confirmed against CBM's own index for all
three languages below; Java was also checked and already includes its
annotations in CBM's span (no widening needed there); Go's leading doc
comments are deliberately NOT treated as attached metadata here — that is a
separate, deferred question about comment-based framework annotations
(swaggo/gin-swagger style), not this invariant.

This module owns exactly one question — the widened start line to use for
attribution purposes — and nothing about parsing, corroboration, impact
inference, or report rendering. `attribute_changed_symbols` in
`verify/analyzer.py` is the only caller.

Deliberately conservative: a construct this module cannot confidently
recognize (a decorator/attribute spanning multiple lines, an unrecognized
language, a file that could not be read) falls back to the original,
unwidened span rather than guessing. Widening never touches `end_line` and
never crosses a blank line or a non-matching line — that line IS the
syntactic boundary, not a heuristic distance limit.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#: Suffix -> language label used only to select this module's own
#: attachment rule. Deliberately independent of
#: `code_intelligence.cbm._language_for` (which drives CBM query dispatch
#: and reports "unknown" for Go/Rust/Java today) — this table exists solely
#: to decide which attached-metadata syntax, if any, applies here.
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript",
    ".mjs": "typescript", ".cjs": "typescript",
    ".rs": "rust",
}

#: Languages with a supported attachment rule below. Any other language
#: (Go, Java, or anything unrecognized) is a deliberate no-op: Java already
#: includes annotations in CBM's own span, and Go's leading comments are
#: explicitly out of scope for this change.
_SUPPORTED_LANGUAGES = frozenset({"python", "typescript", "rust"})


def language_for_attribution(file_path: str) -> str:
    """The language label this module's own rules key off of, from a bare
    file suffix — never CBM's per-symbol `language` field, which reports
    "unknown" for languages this module does not (yet) touch anyway."""
    suffix = PurePosixPath(file_path.replace("\\", "/")).suffix.lower()
    return _LANGUAGE_BY_SUFFIX.get(suffix, "unknown")


#: A line the scan consumes as attached metadata, widening the span.
_LINE_MATCH = "match"
#: A single-line comment the scan looks *past* without widening or
#: stopping — see `_classify_line`'s docstring for why this exists.
_LINE_COMMENT = "comment"
#: Anything else: a blank line, ordinary code, or an unrelated statement.
#: This is the syntactic boundary the scan halts at.
_LINE_STOP = "stop"


def _classify_line(stripped: str, language: str) -> str:
    """Classify one already-stripped source line for the backward scan.

    Single-line decorators/attributes only, by design: one whose argument
    list continues onto further lines (an unbalanced `(`/`[` at end of
    line) is exactly the case this module cannot confidently recognize
    without either bracket-depth tracking or string-literal awareness —
    both would turn a small, deterministic check into a fragile lexical
    parser. Left unrecognized on purpose: such a line classifies as
    `_LINE_STOP`, so the scan simply stops one line short of the true
    decorator start — never a false positive, only a known, documented gap.

    A single-line comment (`//` in TypeScript/Rust) is classified
    separately from a hard stop: real-world Rust structs commonly carry a
    `///` doc comment directly between their attributes and the item
    itself (confirmed against `lemmy`'s own `Post` struct), and TypeScript
    conventionally places a JSDoc/plain comment above decorators the same
    way. The comment's own line is never counted as attached metadata (it
    cannot, by itself, widen the span) — it is only looked *past*, so a
    stacked run of attributes/decorators *above* a comment is still found.
    Python has no such convention (a docstring lives inside the body, never
    between a decorator and the `def`/`class` line it decorates), so a `#`
    comment there remains a plain stop, not a documented exception.
    """
    if not stripped:
        return _LINE_STOP
    if language == "python":
        if stripped.startswith("@") and len(stripped) > 1 and (stripped[1].isalpha() or stripped[1] == "_"):
            return _LINE_MATCH
        return _LINE_STOP
    if language == "typescript":
        if stripped.startswith("@") and len(stripped) > 1 and (stripped[1].isalpha() or stripped[1] == "_"):
            return _LINE_MATCH
        if stripped.startswith("//"):
            return _LINE_COMMENT
        return _LINE_STOP
    if language == "rust":
        if stripped.startswith("#["):
            return _LINE_MATCH
        if stripped.startswith("//"):
            return _LINE_COMMENT
        return _LINE_STOP
    return _LINE_STOP


def symbol_attribution_span(
    *,
    start_line: int | None,
    end_line: int | None,
    file_lines: list[str] | None,
    language: str,
) -> tuple[int | None, int | None]:
    """Widen `start_line` upward across contiguous, immediately-attached
    leading decorator/attribute lines, per `language`'s deterministic rule.

    Returns the original `(start_line, end_line)`, completely unchanged,
    whenever:
    - `start_line` is not a positive line number,
    - `file_lines` is not supplied (no source available to check),
    - `language` has no attachment rule here (Go, Java, anything else),
    - the nearest non-comment line above `start_line` does not match that
      language's single-line attached-metadata syntax — including a blank
      line, an unrelated statement/expression, another declaration's own
      body, or a multi-line decorator's continuation line (see
      `_classify_line`'s docstring for why the latter is deliberately
      unrecognized).

    A single-line comment directly above `start_line` (or between two
    attached-metadata lines) is looked past rather than treated as a stop —
    see `_classify_line` — but never itself counted as part of the widened
    span.

    `file_lines` is the file's raw source, split on newlines, 1-indexed by
    convention everywhere else in Sydes (`file_lines[0]` is line 1).
    """
    if not isinstance(start_line, int) or start_line <= 0 or not file_lines:
        return start_line, end_line
    if language not in _SUPPORTED_LANGUAGES:
        return start_line, end_line

    widened = start_line
    probe = widened - 1
    while probe >= 1 and probe <= len(file_lines):
        stripped = file_lines[probe - 1].strip()
        kind = _classify_line(stripped, language)
        if kind == _LINE_MATCH:
            widened = probe
        elif kind != _LINE_COMMENT:
            break
        probe -= 1
    return widened, end_line
