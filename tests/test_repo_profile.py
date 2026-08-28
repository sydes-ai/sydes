"""Increment B: persistent repository architecture memory (`repo_profile` v1).

A small, deterministic set of repository facts — package roles, frameworks,
test/tooling roots, public/internal surfaces — built once from manifests,
persisted per workspace, and retrieved a handful at a time into boundary
reasoning's evidence packet.

These tests pin the two properties that make it safe and useful: extraction
stays conservative (an ambiguous package is `unknown`, never guessed), and
a repo fact is context for inference, never structural proof.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.discover.repo_map import build_repo_map
from sydes.core.models import RepoRef
from sydes.verify.models import AffectedBoundary, ChangedSymbol, ChangeSet, ChangeVerificationResult
from sydes.verify.repo_profile import (
    CONFIDENCE_STRONG,
    ROLE_APPLICATION,
    ROLE_BACKEND,
    ROLE_FRONTEND,
    ROLE_LIBRARY,
    ROLE_TESTS,
    ROLE_TOOLING,
    ROLE_UNKNOWN,
    RepoFact,
    RepoPackage,
    RepoProfile,
    build_repo_profile,
    get_or_build_repo_profile,
    load_repo_profile,
    save_repo_profile,
)

REPO = "app"


def write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def profile_for(root: Path, *, commit: str | None = "c1") -> RepoProfile:
    """Build a profile the way the analyzer does — through `repo_map`, so
    the manifest list comes from the existing shared walk, not a new one."""
    repo_map = build_repo_map(RepoRef(name=REPO, root=str(root)))
    return build_repo_profile(
        repo_root=root, repo_identity=REPO, observed_commit=commit, repo_map=repo_map,
    )


def package_named(profile: RepoProfile, path: str) -> RepoPackage:
    return next(item for item in profile.packages if item.path == path)


# --------------------------------------------------------------------------
# 1-3. Package discovery and role classification
# --------------------------------------------------------------------------

def test_multiple_manifests_produce_correct_package_roots(tmp_path: Path) -> None:
    write(tmp_path, "packages/core/package.json",
          json.dumps({"name": "@x/core", "dependencies": {"express": "^4"}}))
    write(tmp_path, "packages/ui/package.json",
          json.dumps({"name": "@x/ui", "dependencies": {"react": "^18"}}))
    write(tmp_path, "services/api/pyproject.toml",
          '[project]\nname = "api"\ndependencies = ["fastapi>=0.1"]\n')

    profile = profile_for(tmp_path)

    assert {item.path for item in profile.packages} == {
        "packages/core", "packages/ui", "services/api",
    }
    assert package_named(profile, "packages/core").name == "@x/core"


def test_backend_and_frontend_roles_come_from_dependency_evidence(tmp_path: Path) -> None:
    write(tmp_path, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"express": "^4"}}))
    write(tmp_path, "packages/admin-ui/package.json",
          json.dumps({"name": "admin-ui", "dependencies": {"@angular/core": "^17"}}))

    profile = profile_for(tmp_path)

    assert package_named(profile, "packages/core").role == ROLE_BACKEND
    assert package_named(profile, "packages/admin-ui").role == ROLE_FRONTEND
    assert set(profile.frameworks) == {"Express", "Angular"}
    # The evidence names the actual dependency, not the directory.
    assert any("express" in item.lower()
               for item in package_named(profile, "packages/core").evidence)


def test_directory_name_alone_never_establishes_a_backend_role(tmp_path: Path) -> None:
    """A package sitting in a backend-sounding directory, with a manifest
    carrying no framework or publish signal, must stay `unknown`."""
    write(tmp_path, "server/backend/package.json",
          json.dumps({"name": "looks-like-backend", "private": True}))

    profile = profile_for(tmp_path)

    assert package_named(profile, "server/backend").role == ROLE_UNKNOWN


def test_ambiguous_package_stays_unknown_rather_than_guessed(tmp_path: Path) -> None:
    write(tmp_path, "packages/mystery/package.json",
          json.dumps({"name": "mystery", "private": True, "dependencies": {"lodash": "^4"}}))

    profile = profile_for(tmp_path)

    assert package_named(profile, "packages/mystery").role == ROLE_UNKNOWN


def test_rust_bin_and_lib_targets_map_to_application_and_library(tmp_path: Path) -> None:
    write(tmp_path, "crates/server/Cargo.toml",
          '[package]\nname = "server"\n\n[[bin]]\nname = "server"\n')
    write(tmp_path, "crates/shared/Cargo.toml",
          '[package]\nname = "shared"\n\n[lib]\nname = "shared"\n')

    profile = profile_for(tmp_path)

    assert package_named(profile, "crates/server").role == ROLE_APPLICATION
    assert package_named(profile, "crates/shared").role == ROLE_LIBRARY


# --------------------------------------------------------------------------
# Increment B.1: `repo_map` batch-shape normalization
#
# `StructuralFacts.repo_map` on the real `analyze_change` path is always
# `build_repo_map_batch()`'s `{"repos": [...]}` wrapper, never the bare
# single-repo shape `build_repo_map()` returns directly. Before this fix,
# `build_repo_profile` read `manifests`/`extension_counts`/`folders`
# straight off the top-level dict, which only exist on the single-repo
# shape — so every real profile silently saw `packages=[]`,
# `frameworks=[]`, no matter how many manifests actually existed.
# --------------------------------------------------------------------------

def test_single_repo_repo_map_remains_supported(tmp_path: Path) -> None:
    """The un-normalized shape `build_repo_map()` returns directly must keep
    working exactly as before — this is not a breaking schema change."""
    write(tmp_path, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"express": "^4"}}))
    repo_map = build_repo_map(RepoRef(name=REPO, root=str(tmp_path)))

    profile = build_repo_profile(
        repo_root=tmp_path, repo_identity=REPO, observed_commit="c1", repo_map=repo_map,
    )

    assert package_named(profile, "packages/core").role == ROLE_BACKEND
    assert profile.frameworks == ["Express"]


def test_batch_repo_map_selects_the_matching_repo_by_name(tmp_path: Path) -> None:
    """The real shape: `{"repos": [...]}`. Extraction must reach into it."""
    write(tmp_path, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"django": "^4"}}))
    single = build_repo_map(RepoRef(name=REPO, root=str(tmp_path)))
    batch = {"version": "v1", "repos": [single]}

    profile = build_repo_profile(
        repo_root=tmp_path, repo_identity=REPO, observed_commit="c1", repo_map=batch,
    )

    assert package_named(profile, "packages/core").role == ROLE_BACKEND
    assert profile.frameworks == ["Django"]
    assert profile.packages  # the original bug: this was always []


def test_two_repos_in_a_batch_do_not_contaminate_each_other(tmp_path: Path) -> None:
    """A multi-repo batch must profile ONLY the requested repo's manifests —
    never silently fall back to the first entry in `repos`."""
    root_a = tmp_path / "service_a"
    root_b = tmp_path / "service_b"
    write(root_a, "package.json", json.dumps({"name": "a", "dependencies": {"express": "^4"}}))
    write(root_b, "package.json", json.dumps({"name": "b", "dependencies": {"react": "^18"}}))
    batch = {
        "version": "v1",
        "repos": [
            build_repo_map(RepoRef(name="service_a", root=str(root_a))),
            build_repo_map(RepoRef(name="service_b", root=str(root_b))),
        ],
    }

    profile_a = build_repo_profile(
        repo_root=root_a, repo_identity="service_a", observed_commit="c1", repo_map=batch,
    )
    profile_b = build_repo_profile(
        repo_root=root_b, repo_identity="service_b", observed_commit="c1", repo_map=batch,
    )

    assert profile_a.frameworks == ["Express"]
    assert profile_b.frameworks == ["React"]
    assert package_named(profile_a, "").role == ROLE_BACKEND
    assert package_named(profile_b, "").role == ROLE_FRONTEND


def test_no_matching_repo_name_yields_a_conservative_empty_profile(tmp_path: Path) -> None:
    write(tmp_path, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"express": "^4"}}))
    batch = {"repos": [build_repo_map(RepoRef(name="other_repo", root=str(tmp_path)))]}

    profile = build_repo_profile(
        repo_root=tmp_path, repo_identity=REPO, observed_commit="c1", repo_map=batch,
    )

    assert profile.packages == []
    assert profile.frameworks == []
    assert profile.languages == []


def test_batch_shape_persists_and_reloads_with_full_extraction(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    store = tmp_path / "store"
    write(repo, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"fastapi": ">=0.1"}}))
    batch = {"repos": [build_repo_map(RepoRef(name=REPO, root=str(repo)))]}

    profile, notes = get_or_build_repo_profile(
        repo_root=repo, repo_identity=REPO, workspace_id="ws-b1",
        observed_commit="c1", repo_map=batch, root=store,
    )
    assert profile is not None
    assert profile.packages  # rebuilt correctly from the batch shape
    reloaded = load_repo_profile("ws-b1", store)

    assert reloaded is not None
    assert reloaded.to_dict() == profile.to_dict()
    assert any("rebuilt" in note for note in notes)


def test_normalizing_the_repo_map_makes_no_llm_or_cbm_call(tmp_path: Path) -> None:
    """The fix is pure dict selection — confirm it stays that way."""
    import inspect

    from sydes.verify import repo_profile as module

    source = inspect.getsource(module._normalize_repo_map_for_repo)
    assert "create_default_llm_client" not in source
    assert "CBMClient" not in source
    assert "call_tool" not in source

    write(tmp_path, "packages/core/package.json", json.dumps({"name": "core"}))
    batch = {"repos": [build_repo_map(RepoRef(name=REPO, root=str(tmp_path)))]}
    profile = build_repo_profile(repo_root=tmp_path, repo_identity=REPO, repo_map=batch)
    assert profile.packages


# --------------------------------------------------------------------------
# 4. Test roots, via the existing shared classifier
# --------------------------------------------------------------------------

def test_test_roots_are_recorded_using_existing_file_role_logic(tmp_path: Path) -> None:
    write(tmp_path, "src/app.py", "x = 1\n")
    write(tmp_path, "tests/test_app.py", "def test_x(): pass\n")

    profile = profile_for(tmp_path)

    assert "tests" in profile.test_roots


def test_a_package_rooted_at_a_test_root_takes_the_tests_role(tmp_path: Path) -> None:
    write(tmp_path, "tests/package.json", json.dumps({"name": "e2e", "private": True}))

    profile = profile_for(tmp_path)

    assert package_named(profile, "tests").role == ROLE_TESTS


# --------------------------------------------------------------------------
# 5. Public / internal surface hints
# --------------------------------------------------------------------------

def test_publishable_library_manifest_becomes_a_public_surface_fact(tmp_path: Path) -> None:
    write(tmp_path, "packages/sdk/package.json",
          json.dumps({"name": "sdk", "main": "index.js", "exports": {".": "./index.js"}}))

    profile = profile_for(tmp_path)

    assert package_named(profile, "packages/sdk").role == ROLE_LIBRARY
    hints = profile.public_surface_hints
    assert len(hints) == 1
    assert hints[0].path == "packages/sdk"
    assert hints[0].confidence == CONFIDENCE_STRONG


def test_go_internal_directory_becomes_an_internal_surface_fact(tmp_path: Path) -> None:
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "internal/store/store.go", "package store\n")

    profile = profile_for(tmp_path)

    assert any(item.path == "internal" for item in profile.internal_surface_hints)


def test_generic_visibility_alone_produces_no_surface_fact(tmp_path: Path) -> None:
    """A private package with public-looking source is not a public
    surface — only an explicit publish/visibility declaration is."""
    write(tmp_path, "packages/thing/package.json",
          json.dumps({"name": "thing", "private": True, "main": "index.js"}))

    profile = profile_for(tmp_path)

    assert profile.public_surface_hints == []


# --------------------------------------------------------------------------
# 6-7. Persistence and commit refresh
# --------------------------------------------------------------------------

def test_profile_survives_save_and_reload_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write(repo, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"django": "^4"}}))
    store = tmp_path / "store"

    profile = profile_for(repo)
    saved = save_repo_profile(profile, "ws1", store)
    assert saved is not None and saved.name == "repo_profile.json"

    reloaded = load_repo_profile("ws1", store)
    assert reloaded is not None
    assert reloaded.to_dict() == profile.to_dict()


def test_a_matching_commit_reuses_the_persisted_profile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write(repo, "packages/core/package.json", json.dumps({"name": "core"}))
    store = tmp_path / "store"
    repo_map = build_repo_map(RepoRef(name=REPO, root=str(repo)))

    first, first_notes = get_or_build_repo_profile(
        repo_root=repo, repo_identity=REPO, workspace_id="ws1",
        observed_commit="c1", repo_map=repo_map, root=store,
    )
    second, second_notes = get_or_build_repo_profile(
        repo_root=repo, repo_identity=REPO, workspace_id="ws1",
        observed_commit="c1", repo_map=repo_map, root=store,
    )

    assert any("rebuilt" in note for note in first_notes)
    assert any("reused" in note for note in second_notes)
    assert first is not None and second is not None
    assert second.observed_commit == "c1"


def test_a_changed_commit_triggers_a_rebuild_and_never_serves_stale_facts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write(repo, "packages/core/package.json", json.dumps({"name": "core"}))
    store = tmp_path / "store"

    get_or_build_repo_profile(
        repo_root=repo, repo_identity=REPO, workspace_id="ws1", observed_commit="c1",
        repo_map=build_repo_map(RepoRef(name=REPO, root=str(repo))), root=store,
    )
    # The repository actually changes: a new frontend package appears.
    write(repo, "packages/ui/package.json",
          json.dumps({"name": "ui", "dependencies": {"react": "^18"}}))
    refreshed, notes = get_or_build_repo_profile(
        repo_root=repo, repo_identity=REPO, workspace_id="ws1", observed_commit="c2",
        repo_map=build_repo_map(RepoRef(name=REPO, root=str(repo))), root=store,
    )

    assert any("rebuilt" in note for note in notes)
    assert refreshed is not None
    assert refreshed.observed_commit == "c2"
    assert package_named(refreshed, "packages/ui").role == ROLE_FRONTEND


def test_a_profile_written_by_a_different_schema_is_not_trusted(tmp_path: Path) -> None:
    store = tmp_path / "store"
    profile = RepoProfile(repo_identity=REPO, observed_commit="c1")
    save_repo_profile(profile, "ws1", store)
    target = store / "workspaces" / "ws1" / "repo_profile.json"
    payload = json.loads(target.read_text())
    payload["schema_version"] = "v99"
    target.write_text(json.dumps(payload))

    assert load_repo_profile("ws1", store) is None


# --------------------------------------------------------------------------
# 8-10. Retrieval
# --------------------------------------------------------------------------

def _profile_with_packages() -> RepoProfile:
    return RepoProfile(
        repo_identity=REPO,
        packages=[
            RepoPackage(path="packages/core", name="core", role=ROLE_BACKEND,
                        manifest="packages/core/package.json"),
            RepoPackage(path="packages/admin-ui", name="admin-ui", role=ROLE_FRONTEND,
                        manifest="packages/admin-ui/package.json"),
            RepoPackage(path="packages/mystery", role=ROLE_UNKNOWN,
                        manifest="packages/mystery/package.json"),
        ],
        architecture_facts=[
            RepoFact(key="framework:Django", value="uses Django", source="manifest",
                     confidence=CONFIDENCE_STRONG),
        ],
    )


def test_lookup_returns_the_containing_package_fact_first(tmp_path: Path) -> None:
    profile = _profile_with_packages()

    found = profile.lookup(files=["packages/core/src/service.py"], limit=3)

    assert found
    assert found[0].path == "packages/core"
    assert "backend" in found[0].value


def test_lookup_ranks_relevant_facts_above_unrelated_ones() -> None:
    profile = _profile_with_packages()

    found = profile.lookup(
        files=["packages/admin-ui/src/page.ts"], concepts=["admin ui rendering"], limit=3,
    )

    assert found[0].path == "packages/admin-ui"


def test_lookup_omits_unknown_roles_and_respects_the_cap_without_duplicates() -> None:
    profile = _profile_with_packages()

    found = profile.lookup(
        files=["packages/core/a.py", "packages/core/b.py", "packages/admin-ui/c.ts"],
        symbols=["core"], concepts=["django"], limit=2,
    )

    assert len(found) == 2
    assert len({item.key for item in found}) == 2
    assert all("mystery" not in item.path for item in found)


def test_lookup_returns_nothing_when_no_fact_is_relevant() -> None:
    profile = _profile_with_packages()

    assert profile.lookup(files=["totally/unrelated/path.rb"], limit=5) == []


# --------------------------------------------------------------------------
# Precision hardening: lookup() must not spend its small retrieval budget on
# unrelated test/tooling/fixture facts, and sibling packages must not rank
# via lexical overlap alone.
# --------------------------------------------------------------------------

def _profile_with_test_and_tooling_packages() -> RepoProfile:
    return RepoProfile(
        repo_identity=REPO,
        packages=[
            RepoPackage(path="packages/core", name="core", role=ROLE_BACKEND,
                        manifest="packages/core/package.json"),
            RepoPackage(path="packages/dashboard", name="dashboard", role=ROLE_FRONTEND,
                        manifest="packages/dashboard/package.json"),
            # A nested fixture package that declares itself "library" in its
            # own manifest despite living under a recognized test root —
            # the case existing package-role data alone would get wrong.
            RepoPackage(path="packages/dashboard/vite/tests/fixtures-esm", name="fixtures-esm",
                        role=ROLE_LIBRARY, kind="publishable",
                        manifest="packages/dashboard/vite/tests/fixtures-esm/package.json"),
            RepoPackage(path="packages/tools/lint", name="lint", role=ROLE_TOOLING,
                        manifest="packages/tools/lint/package.json"),
        ],
        public_surface_hints=[
            RepoFact(key="public_surface:packages/core", value="is a publishable library package",
                     source="manifest", confidence=CONFIDENCE_STRONG, path="packages/core"),
        ],
    )


def test_production_file_returns_containing_package_role_first() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(files=["packages/core/src/service/foo.ts"], limit=5)

    assert found
    assert found[0].path == "packages/core"
    assert "backend" in found[0].value


def test_unrelated_test_fixture_package_is_not_returned() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(files=["packages/core/src/service/foo.ts"], limit=5)

    assert all("fixtures-esm" not in item.path for item in found)


def test_unrelated_tooling_package_is_not_returned() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(files=["packages/core/src/service/foo.ts"], limit=5)

    assert all("tools/lint" not in item.path for item in found)


def test_queried_test_file_can_retrieve_its_own_test_package_facts() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(
        files=["packages/dashboard/vite/tests/fixtures-esm/index.ts"], limit=5,
    )

    assert any("fixtures-esm" in item.path for item in found)


def test_sibling_package_without_containment_does_not_outrank_containing_package() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(
        files=["packages/core/src/service/foo.ts"], concepts=["dashboard frontend"], limit=5,
    )

    assert found[0].path == "packages/core"
    assert all(item.path != "packages/dashboard" for item in found)


def test_public_surface_fact_for_containing_package_survives() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(files=["packages/core/src/api.py"], limit=5)

    assert any(item.key == "public_surface:packages/core" for item in found)


def test_limit_and_dedup_are_unchanged_by_the_new_filtering() -> None:
    profile = _profile_with_test_and_tooling_packages()

    found = profile.lookup(
        files=["packages/core/a.py", "packages/core/b.py"], limit=1,
    )

    assert len(found) == 1


def test_zero_result_behavior_remains_conservative_with_new_filtering() -> None:
    profile = _profile_with_test_and_tooling_packages()

    assert profile.lookup(files=["totally/unrelated/path.rb"], limit=5) == []


# --------------------------------------------------------------------------
# 11-13. Boundary-reasoning integration and failure behavior
# --------------------------------------------------------------------------

def _packet_with(profile: RepoProfile | None) -> dict:
    from sydes.code_intelligence.base import StructuralFacts
    from sydes.impact.models import AffectedEntrypoint, ImpactResult
    from sydes.verify.boundary_reasoning import build_reasoning_packet

    change = ChangeSet(
        base="main", head="abc", files=[],
        symbols=[ChangedSymbol(id="1", repo=REPO, file="packages/core/src/svc.py", name="helper")],
    )
    impact_result = ImpactResult(affected=[
        AffectedEntrypoint(repo=REPO, symbol="handler", qualified_name="core.handler",
                           file="packages/core/src/svc.py"),
    ])
    return build_reasoning_packet(
        change=change, impact_result=impact_result, deterministic_boundaries=[],
        semantic_analysis=None,
        facts=StructuralFacts(symbol_index={"repos": []}, backend="cbm"),
        repo=REPO, repo_profile=profile,
    )


def test_packet_includes_only_a_few_retrieved_facts_not_the_whole_profile() -> None:
    profile = _profile_with_packages()

    packet = _packet_with(profile)

    assert packet["repo_context"]
    assert len(packet["repo_context"]) <= 6
    # The relevant package is present; the unrelated frontend one is not.
    joined = " ".join(packet["repo_context"])
    assert "packages/core" in joined
    assert "admin-ui" not in joined
    # It is a list of short strings, never the serialized profile.
    assert all(isinstance(item, str) for item in packet["repo_context"])


def test_boundary_reasoning_is_unchanged_when_no_profile_exists() -> None:
    assert _packet_with(None)["repo_context"] == []


def test_a_broken_profile_lookup_does_not_break_packet_construction() -> None:
    class Exploding:
        def lookup(self, **_kwargs):
            raise RuntimeError("profile is corrupt")

    packet = _packet_with(Exploding())

    assert packet["repo_context"] == []
    assert packet["boundary_candidates"]  # everything else still built


def test_profile_construction_failure_is_reported_not_raised(tmp_path: Path) -> None:
    """A repo root that cannot be read must yield a diagnostic and `None`,
    never an exception into the verification pipeline."""
    profile, notes = get_or_build_repo_profile(
        repo_root=tmp_path / "does-not-exist", repo_identity=REPO,
        workspace_id="ws1", observed_commit="c1",
        repo_map={"manifests": ["packages/core/package.json"]}, root=tmp_path / "store",
    )

    # Missing manifests are simply skipped; the profile is still valid.
    assert profile is not None
    assert profile.packages == []
    assert notes


# --------------------------------------------------------------------------
# 14-15. Soundness and cost
# --------------------------------------------------------------------------

def test_repo_context_alone_cannot_create_a_boundary_flow_obligation_or_verified() -> None:
    """A repo fact is not a graph edge. Even a profile full of confident
    architecture facts leaves every verification-facing structure empty."""
    from sydes.verify.analyzer import _compute_summary

    result = ChangeVerificationResult(
        change=ChangeSet(base="main", head="abc", files=[], symbols=[]),
    )
    result.summary = _compute_summary(result)

    assert result.affected_boundaries == []
    assert result.affected_flows == []
    assert result.accepted_impacts == []
    assert result.summary.counts.obligations == 0
    assert result.summary.verdict != "VERIFIED"


def test_a_repo_fact_is_never_itself_an_established_boundary(tmp_path: Path) -> None:
    """The profile produces `RepoFact`s only. It never imports — and so can
    never construct — any verification-facing type, which is why a repo
    fact cannot become proof no matter how strong its confidence."""
    from sydes.verify import repo_profile as module

    for forbidden in ("AffectedBoundary", "AffectedFlow", "VerificationObligation",
                      "AcceptedImpact", "ChangeVerificationResult"):
        assert not hasattr(module, forbidden)

    write(tmp_path, "packages/sdk/package.json",
          json.dumps({"name": "sdk", "main": "index.js"}))
    profile = profile_for(tmp_path)
    found = profile.lookup(files=["packages/sdk/index.js"], limit=5)

    assert found  # a strong, confident public-surface fact exists ...
    assert all(isinstance(item, RepoFact) for item in found)  # ... and it is only ever a fact


def test_building_and_retrieving_a_profile_makes_no_provider_calls(tmp_path: Path) -> None:
    """Increment B adds zero LLM calls: the module never imports a client."""
    import inspect

    from sydes.verify import repo_profile as module

    source = inspect.getsource(module)
    assert "create_default_llm_client" not in source
    assert "LLMRequest" not in source
    assert "CBMClient" not in source

    write(tmp_path, "packages/core/package.json",
          json.dumps({"name": "core", "dependencies": {"flask": "^3"}}))
    profile = profile_for(tmp_path)
    assert profile.lookup(files=["packages/core/a.py"], limit=3)
