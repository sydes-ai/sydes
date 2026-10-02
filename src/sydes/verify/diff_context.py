"""Change context for the model stages: which parts of a large diff a model sees.

The previous construction cut the diff three times by raw character prefix (the diff reader,
then each stage's diff cap, then the prompt budget), in path order, after the file and symbol
lists had taken their share. On medium/large changes the model routinely saw a few kilobytes
of whichever files sorted first, or none at all, and the final cut could slice the JSON
context itself.

This module replaces those cuts with one policy:
- the budget is per model stage and can be sized to the model (`SYDES_LLM_CONTEXT_CHARS`);
- changed source files come first, then tests, then other files, then docs/generated files;
- every included file gets its first hunks before any file gets all of them, and a diff is
  only ever cut at a hunk boundary, with an explicit marker naming what was left out;
- oversized metadata (symbol lists, per-symbol regions) is trimmed before the diff is, and
  the serialized context is never sliced.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from sydes.ingest.file_roles import classify_candidate_file_role

#: Generated or vendored content: last, whatever its extension.
_GENERATED = re.compile(
    r"(\.lock$|lock\.json$|\.sum$|\.min\.(js|css)$|\.pb\.go$|_pb2(_grpc)?\.py$|\.snap$|"
    r"/migrations?/|/vendor/|/node_modules/|/dist/|/build/|\.svg$|\.png$|\.jpe?g$)"
)
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_HUNK = re.compile(r"^@@ ", re.M)


#: Prompt budget for hosted providers whose models take 128k+ tokens: about 25k tokens, so a
#: medium PR's changed source fits (Baserow #5507: 22k chars of source hunks, which a 20k
#: stage default cut to a third). Local/unknown providers keep the stage default.
HOSTED_CONTEXT_CHARS = 100_000
_HOSTED_PROVIDERS = frozenset({"openai", "anthropic"})


def context_chars(default: int, model_spec: str | None = None) -> int:
    """The prompt budget for a model stage: `SYDES_LLM_CONTEXT_CHARS` when set to a positive
    integer (size it to the model's context window), else the stage's built-in default, raised
    to `HOSTED_CONTEXT_CHARS` when the stage's provider is a hosted one."""
    raw = os.getenv("SYDES_LLM_CONTEXT_CHARS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value > 0:
        return value
    return max(default, HOSTED_CONTEXT_CHARS) if _hosted(model_spec) else default


def _hosted(model_spec: str | None) -> bool:
    from sydes.llm.client import _resolve_provider_and_model

    try:
        provider, _model, _settings = _resolve_provider_and_model(model_spec)
    except Exception:  # noqa: BLE001 - an unresolvable spec keeps the conservative default
        return False
    return provider in _HOSTED_PROVIDERS


def file_priority(path: str) -> int:
    """0 changed source, 1 tests, 2 other (config, templates, frontend), 3 docs/generated."""
    lower = path.lower()
    if _GENERATED.search("/" + lower) or lower.endswith(_DOC_SUFFIXES):
        return 3
    role = classify_candidate_file_role(path)
    if role.startswith("test"):
        return 1
    if role.startswith("docs"):
        return 3
    if role.startswith("source"):
        return 0
    return 2


@dataclass
class _FileDiff:
    path: str
    header: str
    hunks: list[str] = field(default_factory=list)
    index: int = 0

    @property
    def priority(self) -> int:
        return file_priority(self.path)


def _split(diff_text: str) -> list[_FileDiff]:
    files: list[_FileDiff] = []
    for i, block in enumerate(re.split(r"(?m)^(?=diff --git )", diff_text)):
        if not block.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        path = m.group(2) if m else f"<unknown-{i}>"
        starts = [h.start() for h in _HUNK.finditer(block)]
        if not starts:
            files.append(_FileDiff(path, block, [], i))
            continue
        header = block[: starts[0]]
        hunks = [block[s:e] for s, e in zip(starts, [*starts[1:], len(block)], strict=True)]
        files.append(_FileDiff(path, header, hunks, i))
    return files


def _cost(text: str) -> int:
    """Serialized size inside a JSON string (escapes included)."""
    return len(json.dumps(text, ensure_ascii=True)) - 2


def select_diff(diff_text: str, budget: int) -> str:
    """The prioritized, hunk-granular part of `diff_text` that fits in `budget` serialized
    characters, with markers for every omitted hunk or file. Unchanged when it all fits."""
    if budget <= 0 or not diff_text:
        return ""
    if _cost(diff_text) <= budget:
        return diff_text
    files = sorted(_split(diff_text), key=lambda f: (f.priority, f.index))
    taken: dict[int, int] = {}  # file position -> hunks included
    used = 0
    reserve = 200  # room for the omission summary

    def try_add(pos: int, piece: str) -> bool:
        nonlocal used
        c = _cost(piece)
        if used + c > budget - reserve:
            return False
        used += c
        return True

    # tier by tier (source, tests, other, docs): a lower tier gets nothing until every hunk of
    # the tiers above it is in. Within a tier, each file's first hunk first (breadth), then
    # the rest. A test file's or changelog's first hunk must not displace changed source.
    for tier in sorted({f.priority for f in files}):
        in_tier = [pos for pos, f in enumerate(files) if f.priority == tier]
        for pos in in_tier:
            f = files[pos]
            if try_add(pos, f.header + (f.hunks[0] if f.hunks else "")):
                taken[pos] = 1 if f.hunks else 0
        for pos in in_tier:
            f = files[pos]
            if pos not in taken:
                continue
            while taken[pos] < len(f.hunks) and try_add(pos, f.hunks[taken[pos]]):
                taken[pos] += 1
        if any(pos not in taken or taken[pos] < len(files[pos].hunks) for pos in in_tier):
            break  # this tier did not fit: lower tiers are omitted, not interleaved
    out: list[str] = []
    omitted_files: list[str] = []
    for pos, f in enumerate(files):
        if pos not in taken:
            omitted_files.append(f"{f.path} ({len(f.hunks)} hunk(s))")
            continue
        out.append(f.header + "".join(f.hunks[: taken[pos]]))
        left = len(f.hunks) - taken[pos]
        if left:
            out.append(f"... [{left} more hunk(s) of {f.path} not shown: context budget]\n")
    if omitted_files:
        out.append(
            "... [files changed but not shown (context budget; lowest priority first omitted): "
            + ", ".join(omitted_files[:40])
            + (f" and {len(omitted_files) - 40} more" if len(omitted_files) > 40 else "")
            + "]\n"
        )
    return "".join(out)


def fit_prompt(
    header: str,
    context: dict[str, Any],
    *,
    limit: int,
    full_diff: str,
    trims: list[Callable[[dict[str, Any]], bool]] | None = None,
    diff_share: float = 0.6,
) -> tuple[str, dict[str, Any]]:
    """Serialize `header` + `context` within `limit` characters. Metadata trims run first
    while metadata would leave the diff less than `diff_share` of the non-header budget;
    the diff then gets whatever remains, selected by `select_diff`. Never slices the
    serialized context. Returns the prompt and the context actually sent."""

    def render(payload: dict[str, Any]) -> str:
        return header + "\nContext:\n" + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    payload = dict(context)
    payload["diff"] = ""
    room = max(0, limit - len(header))
    for trim in trims or []:
        if len(render(payload)) - len(header) <= room * (1 - diff_share):
            break
        trim(payload)
    budget = limit - len(render(payload)) - 16
    for _ in range(6):
        payload["diff"] = select_diff(full_diff, budget)
        prompt = render(payload)
        if len(prompt) <= limit:
            return prompt, payload
        budget -= len(prompt) - limit + 64
    payload["diff"] = "... [diff not shown: context budget exhausted by metadata]\n"
    return render(payload), payload


def trim_list(key: str, keep: int, *, nested: str | None = None) -> Callable[[dict[str, Any]], bool]:
    """A trim step: keep only the first `keep` items of `payload[key]` (or of
    `payload[nested][key]`), adding a count of what was dropped."""

    def step(payload: dict[str, Any]) -> bool:
        holder = payload.get(nested) if nested else payload
        if not isinstance(holder, dict) or not isinstance(holder.get(key), list):
            return False
        items = holder[key]
        if len(items) <= keep:
            return False
        holder = dict(holder)
        holder[key] = items[:keep]
        holder[f"{key}_not_shown"] = len(items) - keep
        if nested:
            payload[nested] = holder
        else:
            payload.update(holder)
        return True

    return step


def order_by_priority(items: list[dict[str, Any]], path_key: str) -> list[dict[str, Any]]:
    """Stable reorder: source files' entries first, docs/generated last."""
    return sorted(items, key=lambda it: file_priority(str(it.get(path_key) or "")))


def is_source_path(path: str) -> bool:
    return file_priority(path) == 0 and bool(PurePosixPath(path).suffix)
