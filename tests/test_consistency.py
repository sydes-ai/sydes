"""Cross-file consistency questions (5-language evaluation).

Reviewers caught, and Sydes missed, defects of one shape: a concept changes in
one path and a parallel path, a caller, or a fallback is not updated
(weave-os/router #1573: a new hook wired into two of three proxy paths, its
result discarded; superset #8052: a lookup lost its fallback). These fixtures
encode the structural patterns only; the module asks questions, never answers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sydes.verify.consistency import consistency_candidates, functions_in
from sydes.verify.source_files import load_repo_files


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, before: dict[str, str], after: dict[str, str]) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for files in (before, after):
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    base = _git(root, "rev-parse", "HEAD~1").strip()
    return root, base, _git(root, "diff", base, "HEAD")


PROXY = '''package proxy

func (s *Service) ProxyMessages() {{
	s.recordCommittedStreamFailure()
	s.recordRescuedPrimaryFailure()
	s.disableProviderAfterOverload(){messages}
}}

func (s *Service) ProxyChat() {{
	s.recordCommittedStreamFailure()
	s.recordRescuedPrimaryFailure()
	s.disableProviderAfterOverload(){chat}
}}
'''
GEMINI = '''package proxy

func (s *Service) ProxyGemini() {
	s.recordCommittedStreamFailure()
	s.recordRescuedPrimaryFailure()
	s.disableProviderAfterOverload()
}
'''
HELPERS = '''package proxy

func (s *Service) recordCommittedStreamFailure() {}
func (s *Service) recordRescuedPrimaryFailure() {}
func (s *Service) disableProviderAfterOverload() {}
'''
HOOK = '''
func (s *Service) demoteAfterUnrescuedStall(
	reason string,
) string {
	return reason
}
'''


def test_a_hook_added_to_some_parallel_paths_asks_about_the_others(tmp_path: Path) -> None:
    root, base, diff = _repo(
        tmp_path,
        {"proxy/service.go": PROXY.format(messages="", chat=""), "proxy/gemini.go": GEMINI,
         "proxy/helpers.go": HELPERS},
        {"proxy/service.go": PROXY.format(messages="\n\ts.demoteAfterUnrescuedStall(\"m\")",
                                          chat="\n\ts.demoteAfterUnrescuedStall(\"c\")"),
         "proxy/helpers.go": HELPERS + HOOK},
    )
    questions, _ = consistency_candidates(diff_text=diff, repo_files=load_repo_files("app", root),
                                          repo_root=root, base=base)
    wiring = [q for q in questions if q.kind == "partial_wiring"]
    assert wiring and "`ProxyGemini`" in wiring[0].question
    assert {a["path"] for a in wiring[0].analogous} >= {"proxy/gemini.go:ProxyGemini"}
    # the hook returns a value that both new call sites drop
    discarded = [q for q in questions if q.kind == "discarded_result"]
    assert discarded and "returns string, and 2 call site(s)" in discarded[0].question
    # a question, never a verdict
    assert all("?" in q.question for q in questions)


def test_a_removed_default_return_asks_whether_the_fallback_survives(tmp_path: Path) -> None:
    before = '''export async function linkedWorkspace(db, projectId, n) {
  const pr = db.find(projectId, n);
  if (!pr) return { workspaceId: null };
  return { workspaceId: pr.workspace };
}
'''
    after = '''export async function linkedWorkspace(db, projectId, n) {
  const repo = await resolveRepo(projectId);
  const rows = db.findByRepo(repo, n);
  return { workspaceId: rows[0].workspace };
}
'''
    root, base, diff = _repo(tmp_path, {"src/linked.ts": before}, {"src/linked.ts": after})
    questions, _ = consistency_candidates(diff_text=diff, repo_files=load_repo_files("app", root),
                                          repo_root=root, base=base)
    removed = [q for q in questions if q.kind == "removed_behavior"]
    assert removed and "`return { workspaceId: null };`" in removed[0].question
    assert removed[0].evidence[0].startswith("BEFORE:") and "if (!pr)" in removed[0].evidence[0]


def test_unrelated_changes_ask_nothing(tmp_path: Path) -> None:
    root, base, diff = _repo(
        tmp_path,
        {"app/a.py": "def total(xs):\n    return sum(xs)\n"},
        {"app/a.py": "def total(xs):\n    return sum(x for x in xs)\n"},
    )
    questions, notes = consistency_candidates(diff_text=diff, repo_files=load_repo_files("app", root),
                                              repo_root=root, base=base)
    assert questions == [] and any("0 candidate" in n for n in notes)


def test_function_extents_for_brace_and_indent_languages() -> None:
    go = functions_in("x.go", "package x\n\nfunc (s *S) A(\n\tv int,\n) int {\n\treturn v\n}\n")
    py = functions_in("x.py", "class C:\n    def m(self):\n        return 1\n\n    def n(self):\n        pass\n")
    assert [(f.name, f.start, f.end) for f in go] == [("A", 3, 7)]
    assert [(f.name, f.start, f.end) for f in py] == [("m", 2, 3), ("n", 5, 6)]
