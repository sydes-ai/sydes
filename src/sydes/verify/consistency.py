"""Cross-file consistency questions for code review.

Code review answers "does this patch introduce a defect?" from the supplied
evidence only, and deliberately stays silent about code it was not shown. The
class of defect it then misses is consistency across files: a concept changes
in one path, and a parallel path, a caller, or a reader of the same state is
not updated. This module finds, deterministically and within a fixed budget,
the places where that question is worth asking, and supplies the code that
answers it, so the review can judge it from evidence instead of guessing:

- partial wiring: a call added to some functions is absent from functions
  that call the same neighbouring helpers (parallel flows/adapters);
- discarded result: a changed function returns a value some call sites ignore;
- analogous calls: a new call sits next to similar calls whose arguments the
  reviewer should compare;
- shared state: state written by the change is read by unchanged code;
- removed or newly conditional behavior: a changed function lost a return path
  or now runs it only on success of a new dependency.

A candidate is a question with evidence, never a finding: the review decides.
Language-agnostic by construction (regex extents for brace languages and
Python); every generator is bounded in count and characters.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sydes.verify.source_files import RepoFiles

MAX_CANDIDATES = 8
_PER_KIND_CAP = {"partial_wiring": 3, "discarded_result": 2, "removed_behavior": 2,
                 "analogous_call": 2, "shared_state": 2}
MAX_EVIDENCE_CHARS = 1_400
MAX_TOTAL_CHARS = 14_000
#: A helper called from more functions than this is plumbing, not a sign that
#: two functions are parallel implementations of one concept.
MAX_ANCHOR_CALLERS = 12
NEIGHBOUR_WINDOW = 25

_TESTISH = re.compile(r"(?:^|[/._-])(?:tests?|specs?|fixtures?|mocks?|testdata|__tests__)(?:[/._-]|$)", re.I)
_BRACE_EXTS = {".go", ".java", ".ts", ".tsx", ".js", ".jsx", ".rs", ".kt", ".cs", ".php"}
_KEYWORDS = {
    "if", "for", "while", "switch", "return", "catch", "func", "function", "fn", "new", "match",
    "await", "defer", "go", "select", "sizeof", "typeof", "super", "this", "self", "print",
    "len", "make", "append", "map", "filter", "range", "string", "int", "str", "list", "dict",
    "set", "tuple", "isinstance", "Some", "Ok", "Err", "None", "assert", "expect", "it",
    "describe", "test", "require", "import", "from", "format", "panic", "unwrap", "clone",
    "to_string", "into", "iter", "collect", "push", "get", "join", "split", "join", "log",
}
_DEF_PATTERNS = {
    ".go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*\("),
    ".rs": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)"),
    ".java": re.compile(
        r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|default)\s+)+"
        r"[\w<>\[\],.?\s]+?\s+(?P<name>\w+)\s*\([^;]*$"
    ),
    ".py": re.compile(r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>\w+)\s*\("),
}
_TS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>\w+)\s*[(<]"
    r"|^\s*(?:export\s+)?(?:const|let)\s+(?P<arrow>\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>"
    r"|^\s*(?:export\s+)?(?:const|let)\s+(?P<proc>\w+)\s*=\s*[\w.]+\s*$"
    r"|^\s*(?:(?:public|private|protected|static|async|readonly)\s+)*(?P<method>\w+)\s*\([^;]*\)\s*(?::[^{;]+)?\{\s*$"
)
_CALL = re.compile(r"(?<![\w.])(?:[\w]+\.)*(?P<name>[A-Za-z_]\w*)\s*\(")
_STATE_WRITE = [
    re.compile(r"\.(?:update|insert|insertInto|delete|deleteFrom|upsert)\(\s*(?P<state>[A-Za-z_]\w+)\s*\)"),
    re.compile(r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(?P<state>[A-Za-z_]\w+)", re.I),
    re.compile(r"(?<![\w.])(?P<state>[A-Za-z_]\w{3,})\.(?:set|delete|clear|push|put|insert|remove)\("),
    re.compile(r"(?<![\w.])(?P<state>[A-Za-z_]\w{3,})\[[^\]]+\]\s*=[^=]"),
]


@dataclass(slots=True)
class Function:
    file: str
    name: str
    start: int
    end: int
    lines: list[str] = field(repr=False, default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.file}:{self.name}"

    def text(self, around: int | None = None, radius: int = 12) -> str:
        if around is None:
            body = self.lines
            offset = self.start
        else:
            lo = max(self.start, around - radius)
            hi = min(self.end, around + radius)
            body = self.lines[lo - self.start: hi - self.start + 1]
            offset = lo
        return "\n".join(f"{offset + i}: {line}" for i, line in enumerate(body))


@dataclass(slots=True)
class FileDiff:
    path: str
    added: dict[int, str] = field(default_factory=dict)  # new line -> text
    removed: list[str] = field(default_factory=list)


def parse_diff(diff_text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            current = None if path == "/dev/null" else files.setdefault(path, FileDiff(path))
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if match:
            new_line = int(match.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+"):
            current.added[new_line] = line[1:]
            new_line += 1
        elif line.startswith("-"):
            current.removed.append(line[1:])
        elif not line.startswith("\\"):
            new_line += 1
    return files


def _brace_end(lines: list[str], start: int) -> int:
    depth, opened = 0, False
    for index in range(start, len(lines)):
        text = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`|//.*$', "", lines[index])
        depth += text.count("{") - text.count("}")
        opened = opened or "{" in text
        if opened and depth <= 0:
            return index
        if not opened and index - start > 12:  # a declaration without a body
            return start
    return len(lines) - 1


def _python_end(lines: list[str], start: int, indent: int) -> int:
    end = start
    for index in range(start + 1, len(lines)):
        text = lines[index]
        if text.strip() and (len(text) - len(text.lstrip())) <= indent:
            break
        if text.strip():
            end = index
    return end


def functions_in(path: str, text: str) -> list[Function]:
    ext = Path(path).suffix
    lines = text.splitlines()
    out: list[Function] = []
    for index, line in enumerate(lines):
        if ext == ".py":
            match = _DEF_PATTERNS[".py"].match(line)
            if not match:
                continue
            end = _python_end(lines, index, len(match.group("indent")))
            name = match.group("name")
        elif ext in _BRACE_EXTS:
            if ext in {".ts", ".tsx", ".js", ".jsx"}:
                match = _TS_DEF.match(line)
                if not match:
                    continue
                name = next(g for g in (match.group("name"), match.group("arrow"),
                                        match.group("proc"), match.group("method")) if g)
                if name in _KEYWORDS:
                    continue
            else:
                pattern = _DEF_PATTERNS.get(ext)
                match = pattern.match(line) if pattern else None
                if not match:
                    continue
                name = match.group("name")
            end = _brace_end(lines, index)
            if match.group("proc") if ext in {".ts", ".tsx", ".js", ".jsx"} else False:
                # `export const x = builder` chains: the definition runs to the closing `);`
                end = next((j for j in range(index + 1, min(len(lines), index + 400))
                            if re.match(r"^\S", lines[j]) and lines[j].strip() not in {"", "})"}),
                           end)
        else:
            continue
        if end >= index:
            out.append(Function(path, name, index + 1, end + 1, lines[index:end + 1]))
    # nested definitions (a method inside a class/impl) are kept; the innermost
    # enclosing function is chosen by `_enclosing`
    return out


def _enclosing(functions: list[Function], line: int) -> Function | None:
    best = None
    for fn in functions:
        if fn.start <= line <= fn.end and (best is None or fn.start >= best.start):
            best = fn
    return best


def _calls(text: str) -> set[str]:
    return {m.group("name") for m in _CALL.finditer(text) if m.group("name") not in _KEYWORDS}


@dataclass(slots=True)
class Index:
    functions: dict[str, list[Function]]  # file -> functions
    by_name: dict[str, list[Function]]
    callers: dict[str, set[str]]  # callee name -> keys of functions calling it


def build_index(repo_files: RepoFiles, extensions: set[str]) -> Index:
    functions: dict[str, list[Function]] = {}
    by_name: dict[str, list[Function]] = defaultdict(list)
    callers: dict[str, set[str]] = defaultdict(set)
    for source in repo_files.files:
        if not source.is_app_source or source.extension not in extensions:
            continue
        fns = functions_in(source.path, source.text)
        functions[source.path] = fns
        for fn in fns:
            by_name[fn.name].append(fn)
            for callee in _calls("\n".join(fn.lines[1:])):
                callers[callee].add(fn.key)
    return Index(functions, dict(by_name), dict(callers))


@dataclass(slots=True)
class Candidate:
    id: str
    kind: str
    question: str
    changed_path: str
    analogous: list[dict[str, Any]]
    why_related: str
    evidence: list[str]
    confidence: str
    score: int = 0

    def as_context(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "question": self.question,
            "changed_path": self.changed_path, "analogous_paths": self.analogous,
            "why_related": self.why_related, "evidence": self.evidence,
            "confidence": self.confidence,
        }


def _clip(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…"


def _added_calls(fdiff: FileDiff, fns: list[Function]) -> list[tuple[str, Function, int]]:
    out = []
    for line_no, text in fdiff.added.items():
        fn = _enclosing(fns, line_no)
        if fn is None or line_no == fn.start:
            continue
        for callee in _calls(text):
            out.append((callee, fn, line_no))
    return out


def _specific(name: str) -> bool:
    """A helper name specific enough to say two functions do the same job."""
    return len(name) >= 8 and len(_tokens(name)) >= 2 and name.lower() not in _KEYWORDS


def _partial_wiring(diffs, index: Index, old_fns: dict[str, dict[str, Function]]) -> list[Candidate]:
    candidates = []
    seen: set[tuple[str, str]] = set()
    by_callee: dict[str, list[tuple[Function, int]]] = defaultdict(list)
    for path, fdiff in diffs.items():
        for callee, fn, line in _added_calls(fdiff, index.functions.get(path, [])):
            if callee not in index.by_name or callee == fn.name or not _specific(callee):
                continue
            before = old_fns.get(path, {}).get(fn.name)
            if before is not None and re.search(rf"\b{re.escape(callee)}\s*\(", "\n".join(before.lines)):
                continue  # not a new call: the line was only edited or moved
            by_callee[callee].append((fn, line))
    for callee, sites in by_callee.items():
        wired = {fn.key for fn, _ in sites}
        anchors: dict[str, int] = {}
        for fn, line in sites:
            window = "\n".join(fn.lines[max(0, line - fn.start - NEIGHBOUR_WINDOW): line - fn.start + NEIGHBOUR_WINDOW])
            for anchor in _calls(window):
                callers = index.callers.get(anchor, set())
                if (anchor != callee and anchor in index.by_name and _specific(anchor)
                        and 2 <= len(callers) <= MAX_ANCHOR_CALLERS):
                    anchors[anchor] = len(callers)
        shared: dict[str, list[str]] = defaultdict(list)
        for anchor in anchors:
            for key in index.callers.get(anchor, set()):
                if key not in wired:
                    shared[key].append(anchor)
        mine = _tokens(callee)

        def affinity(names: list[str]) -> tuple[int, int]:
            """Siblings sharing helpers whose names resemble the new call first."""
            return (max((len(_tokens(a) & mine) for a in names), default=0), len(names))

        for key, common in sorted(shared.items(), key=lambda kv: (tuple(-x for x in affinity(kv[1])), kv[0])):
            common = sorted(common, key=lambda a: (-len(_tokens(a) & mine), a))
            if len(common) < 2:
                continue
            if (callee, key) in seen:
                continue
            seen.add((callee, key))
            file, _, name = key.rpartition(":")
            sibling = next((f for f in index.functions.get(file, []) if f.name == name), None)
            if sibling is None:
                continue
            anchor_line = next(
                (sibling.start + i for i, text in enumerate(sibling.lines)
                 if re.search(rf"\b{re.escape(common[0])}\s*\(", text)),
                sibling.start,
            )
            first_fn, first_line = sites[0]
            candidates.append(Candidate(
                id="", kind="partial_wiring",
                question=(
                    f"This change adds a call to `{callee}` in {', '.join(sorted({f.name for f, _ in sites}))}. "
                    f"`{sibling.name}` calls the same neighbouring helpers ({', '.join(f'`{a}`' for a in common[:4])}) "
                    f"but not `{callee}`. Is `{sibling.name}` a parallel path that should also have changed, "
                    "or is the difference intentional?"
                ),
                changed_path=first_fn.key,
                analogous=[{"path": sibling.key, "updated": False}]
                + [{"path": f.key, "updated": True} for f in {f.key: f for f, _ in sites}.values()],
                why_related=f"shares {len(common)} neighbouring helper call(s) with the changed path(s)",
                evidence=[
                    _clip(first_fn.text(first_line, radius=10), 1_100),
                    _clip(sibling.text(anchor_line, radius=10), 1_100),
                ],
                confidence="high" if len(common) >= 3 else "medium",
                score=affinity(common)[0] * 10 + len(common),
            ))
    return candidates


_RETURN_HINT = {
    ".go": re.compile(r"\)\s*(?P<ret>\(?[\w\[\]*.,\s]+\)?)\s*\{\s*$"),
    ".rs": re.compile(r"->\s*(?P<ret>[^{]+)\{?\s*$"),
    ".py": re.compile(r"->\s*(?P<ret>[^:]+):\s*$"),
    ".ts": re.compile(r"\)\s*:\s*(?P<ret>[^{=]+?)\s*(?:\{|=>)\s*$"),
}


def _returns_value(fn: Function) -> str | None:
    ext = Path(fn.file).suffix
    ext = ".ts" if ext in {".tsx", ".js", ".jsx"} else ext
    head_lines = []
    for line in fn.lines[:20]:
        head_lines.append(line.strip())
        if "{" in line or (ext == ".py" and line.rstrip().endswith(":")):
            break
    header = " ".join(head_lines)
    header = header.split("{")[0] + "{" if "{" in header else header
    pattern = _RETURN_HINT.get(ext)
    if pattern:
        match = pattern.search(header)
        if match:
            ret = match.group("ret").strip()
            if ret and ret not in {"void", "None", "()", "Promise<void>", "error"} and not ret.startswith("Self"):
                return ret
    if ext == ".java":
        match = re.match(r"^\s*(?:(?:public|private|protected|static|final|synchronized)\s+)+(?P<ret>[\w<>\[\],.?]+)\s+\w+\s*\(", fn.lines[0])
        if match and match.group("ret") != "void":
            return match.group("ret")
    if ext == ".py" and any(re.match(r"^\s*return\s+\S", line) for line in fn.lines[1:]):
        return "a value"
    return None


def _discarded_results(diffs, index: Index, repo_files: RepoFiles) -> list[Candidate]:
    out = []
    changed_fns = {
        fn.key: fn
        for path, fdiff in diffs.items()
        for fn in index.functions.get(path, [])
        if any(fn.start <= line <= fn.end for line in fdiff.added)
    }
    texts = {f.path: f.text.splitlines() for f in repo_files.files if f.path in index.functions}
    for fn in changed_fns.values():
        ret = _returns_value(fn)
        if ret is None:
            continue
        statement = re.compile(rf"^\s*(?:await\s+|try\s+)?(?:[\w]+\.)*{re.escape(fn.name)}\s*\(")
        discarded = []
        for file in sorted({key.rpartition(":")[0] for key in index.callers.get(fn.name, set())}):
            for no, line in enumerate(texts.get(file, []), start=1):
                # `?`/unwrap/expect consume a Result; `let _ =` discards on purpose
                if statement.match(line) and not re.search(r"\)\s*(?:\.await)?\s*\?|\.(?:unwrap|expect)\(", line):
                    discarded.append((file, no, line.strip()))
        if not discarded:
            continue
        out.append(Candidate(
            id="", kind="discarded_result",
            question=(
                f"`{fn.name}` returns {ret}, and {len(discarded)} call site(s) do not use the returned "
                "value. Does any caller need it (for logging, state, or a decision), or is ignoring it "
                "correct?"
            ),
            changed_path=fn.key,
            analogous=[{"path": f"{f}:{n}", "updated": None} for f, n, _ in discarded[:6]],
            why_related="call sites of a changed function",
            evidence=[_clip(fn.text() if len(fn.lines) < 40 else "\n".join(fn.lines[:12]))]
            + [f"{f}:{n}: {text}" for f, n, text in discarded[:6]],
            confidence="medium",
        ))
        if len(out) >= 2:
            break
    return out


def _tokens(name: str) -> set[str]:
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)
    return {p.lower() for p in parts if len(p) > 2}


def _analogous_calls(diffs, index: Index) -> list[Candidate]:
    out = []
    for path, fdiff in diffs.items():
        for callee, fn, line in _added_calls(fdiff, index.functions.get(path, [])):
            if callee not in index.by_name:
                continue
            mine = _tokens(callee)
            if len(mine) < 2:
                continue
            lo, hi = line - fn.start - 15, line - fn.start + 15
            neighbours = []
            for offset, text in enumerate(fn.lines[max(0, lo): hi], start=fn.start + max(0, lo)):
                if offset in fdiff.added:
                    continue
                for other in _calls(text):
                    if other != callee and len(_tokens(other) & mine) >= 2 and other in index.by_name:
                        neighbours.append((offset, other))
            if not neighbours:
                continue
            new_call = not any(re.search(rf"\b{re.escape(callee)}\s*\(", t)
                               for t in fdiff.removed)
            out.append(Candidate(
                id="", kind="analogous_call",
                question=(
                    f"The new call to `{callee}` sits next to analogous existing calls "
                    f"({', '.join(sorted({f'`{n}`' for _, n in neighbours}))}). Compare what each call "
                    "receives: does the new call pass the values its purpose requires (the same "
                    "state/error/decision the neighbouring calls use for the same role)?"
                ),
                changed_path=fn.key,
                analogous=[{"path": f"{fn.file}:{n}", "updated": False} for n, _ in neighbours[:4]],
                why_related="similar names called in the same block",
                evidence=[_clip(fn.text(line, radius=16))],
                confidence="medium" if new_call else "low",
                score=len({n for _, n in neighbours}),
            ))
    # one question per callee: its first new site
    unique: dict[str, Candidate] = {}
    for candidate in out:
        callee = candidate.question.split("`")[1]
        unique.setdefault(callee, candidate)
    return list(unique.values())


def _shared_state(diffs, index: Index, repo_files: RepoFiles) -> list[Candidate]:
    written: dict[str, tuple[str, int]] = {}
    for path, fdiff in diffs.items():
        if _TESTISH.search(path):
            continue
        for line_no, text in fdiff.added.items():
            for pattern in _STATE_WRITE:
                for match in pattern.finditer(text):
                    state = match.group("state")
                    if state.lower() not in _KEYWORDS and len(state) > 3:
                        written.setdefault(state, (path, line_no))
    out = []
    changed_files = set(diffs)
    texts = {f.path: f.text for f in repo_files.files}
    shared = {}
    for state, where in written.items():
        writer_text = texts.get(where[0], "")
        # state other code can reach: imported into the writer, or exported/public there
        if re.search(rf"import[^\n]*\b{re.escape(state)}\b|from\s+\S+\s+import[^\n]*\b{re.escape(state)}\b"
                     rf"|export\s+(?:const|let|var|class|function)\s+{re.escape(state)}\b"
                     rf"|^{re.escape(state)}\s*=|^var\s+{re.escape(state)}\b|\bpub\s+static\s+{re.escape(state)}\b",
                     writer_text, re.M):
            shared[state] = where
    for state, (path, line_no) in list(shared.items())[:3]:
        readers = []
        for source in repo_files.files:
            if not source.is_app_source or source.path in changed_files or _TESTISH.search(source.path):
                continue
            if state not in source.text:
                continue
            # a reader of *this* state names it through an import or a qualifier,
            # not merely as the same word (`entries`, `projects`)
            if not re.search(
                rf"(?:import[^\n]*\b{re.escape(state)}\b|\b\w+\.{re.escape(state)}\b(?!\s*\()|from\s+\S+\s+import[^\n]*\b{re.escape(state)}\b)",
                source.text,
            ):
                continue
            for no, line in enumerate(source.text.splitlines(), start=1):
                if re.search(rf"\b{re.escape(state)}\b", line):
                    fn = _enclosing(index.functions.get(source.path, []), no)
                    readers.append((source.path, no, fn.name if fn else "", line.strip()))
                    break
            if len(readers) >= 6:
                break
        if not readers:
            continue
        writer = _enclosing(index.functions.get(path, []), line_no)
        out.append(Candidate(
            id="", kind="shared_state",
            question=(
                f"This change writes `{state}` (in {writer.name if writer else path}). Unchanged code "
                f"elsewhere reads `{state}`. Do those readers still see consistent values after this "
                "write — the same rows/keys, in the same shape, at the time they read them?"
            ),
            changed_path=writer.key if writer else path,
            analogous=[{"path": f"{p}:{fn or n}", "updated": False} for p, n, fn, _ in readers],
            why_related=f"same state name `{state}`",
            evidence=[_clip(writer.text(line_no) if writer else fdiff_line(diffs, path, line_no))]
            + [f"{p}:{n}: {text}" for p, n, _, text in readers],
            confidence="low",
        ))
    return out


def fdiff_line(diffs, path: str, line_no: int) -> str:
    return f"{path}:{line_no}: {diffs[path].added.get(line_no, '')}"


def _git_show(repo_root: Path, rev: str, path: str) -> str | None:
    try:
        out = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=repo_root, capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


_RETURN_STMT = re.compile(r"\breturn\b[^;\n]*;?")
_DEFAULT_RETURN = re.compile(
    r"^return\s*(?:null|None|nil|false|False|undefined|\[\]|\{|\(\)|Ok\(|Err\(|;|$)"
)


def _returns(body: str) -> list[str]:
    return [m.group(0).strip() for m in _RETURN_STMT.finditer(body)]


def _removed_behavior(diffs, index: Index, old_fns: dict[str, dict[str, Function]]) -> list[Candidate]:
    """A changed function that lost a return path: fewer return statements than
    before, or a default/fallback-shaped return that no longer exists. An
    edited return (same count, no default lost) is a change, not a removal."""
    out = []
    for path, fdiff in diffs.items():
        if not any(_RETURN_STMT.search(line) for line in fdiff.removed):
            continue
        for fn in index.functions.get(path, []):
            if not any(fn.start <= line <= fn.end for line in fdiff.added):
                continue
            old = old_fns.get(path, {}).get(fn.name)
            if old is None:
                continue
            before = _returns("\n".join(old.lines[1:]))
            after = _returns("\n".join(fn.lines[1:]))
            gone = [r for r in before if r not in after]
            defaults = [r for r in gone if _DEFAULT_RETURN.match(r)]
            if not gone or (len(after) >= len(before) and not defaults):
                continue
            listed = defaults + [r for r in gone if r not in defaults]
            out.append(Candidate(
                id="", kind="removed_behavior",
                question=(
                    f"`{fn.name}` no longer has the return path(s) {', '.join(f'`{g}`' for g in listed[:2])} "
                    "it had before. Is the behavior those paths provided (a fallback, a default, a "
                    "result for a failure case) still provided for every input that used to reach them?"
                ),
                changed_path=fn.key,
                analogous=[{"path": f"{path}:{fn.name} (before)", "updated": True}],
                why_related="same function before and after the change",
                evidence=["BEFORE:\n" + _clip(old.text(), 1_100), "AFTER:\n" + _clip(fn.text(), 1_100)],
                confidence="medium" if defaults else "low",
                score=len(defaults) * 10 + len(gone),
            ))
    return out


def consistency_candidates(
    *, diff_text: str, repo_files: RepoFiles, repo_root: Path, base: str
) -> tuple[list[Candidate], list[str]]:
    """Bounded consistency questions for this change, with their evidence."""
    diffs = parse_diff(diff_text)
    app_paths = {f.path for f in repo_files.files if f.is_app_source}
    diffs = {p: d for p, d in diffs.items() if p in app_paths}
    if not diffs:
        return [], ["consistency: no changed application source"]
    extensions = {Path(p).suffix for p in diffs}
    index = build_index(repo_files, extensions)
    old_fns: dict[str, dict[str, Function]] = {}
    for path in diffs:
        old_text = _git_show(repo_root, base, path)
        old_fns[path] = {fn.name: fn for fn in functions_in(path, old_text)} if old_text else {}
    generators = [
        ("partial_wiring", lambda: _partial_wiring(diffs, index, old_fns)),
        ("discarded_result", lambda: _discarded_results(diffs, index, repo_files)),
        ("removed_behavior", lambda: _removed_behavior(diffs, index, old_fns)),
        ("analogous_call", lambda: _analogous_calls(diffs, index)),
        ("shared_state", lambda: _shared_state(diffs, index, repo_files)),
    ]
    notes: list[str] = []
    found: list[Candidate] = []
    for name, generate in generators:
        try:
            produced = generate()
        except Exception as exc:  # a heuristic must never break the review
            notes.append(f"consistency: {name} skipped: {type(exc).__name__}: {exc}")
            continue
        rank = {"high": 0, "medium": 1, "low": 2}
        produced.sort(key=lambda c: (rank.get(c.confidence, 3), -c.score))
        found.extend(produced[:_PER_KIND_CAP[name]])
        notes.append(f"consistency: {name}={len(produced)}")
    kept: list[Candidate] = []
    total = 0
    for candidate in found:
        size = sum(len(e) for e in candidate.evidence) + len(candidate.question)
        if len(kept) >= MAX_CANDIDATES or total + size > MAX_TOTAL_CHARS:
            notes.append(f"consistency: dropped {candidate.kind} for budget")
            continue
        total += size
        candidate.id = f"C{len(kept) + 1}"
        kept.append(candidate)
    notes.append(f"consistency: {len(kept)} candidate(s), {total} chars")
    return kept, notes
