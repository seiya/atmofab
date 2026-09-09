#!/usr/bin/env python3
"""Unit tests for tools/dependency_graph.build_dependency_graph.

The builder is a pure function of `deps.yaml` + `spec_catalog.yaml`; these tests
seed a synthetic registry on disk and assert the derived graph (all_nodes /
topo_level / transitive_deps / via) and the fail-closed error taxonomy.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.dependency_graph import build_dependency_graph
from tools.orchestration_runtime import _load_spec_catalog


def _write_catalog(repo_root: Path, entries: list[dict]) -> None:
    lines = ["catalog_version: 0.2.0", "updated_at: 2026-06-18", "specs:"]
    for e in entries:
        lines.append(f"  - spec_kind: {e['spec_kind']}")
        lines.append(f"    spec_id: {e['spec_id']}")
        lines.append(f"    spec_version: \"{e['spec_version']}\"")
        lines.append(f"    deps_path: {e['deps_path']}")
    (repo_root / "spec" / "registry").mkdir(parents=True, exist_ok=True)
    (repo_root / "spec" / "registry" / "spec_catalog.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _write_deps(repo_root: Path, spec_ref: str, spec_kind: str, spec_id: str,
                components: list[tuple[str, str]] | None = None,
                profiles: list[tuple[str, str]] | None = None,
                infrastructure: list[tuple[str, str]] | None = None) -> None:
    d = repo_root / spec_ref
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"spec_id: {spec_id}", f"spec_kind: {spec_kind}", "dependencies:"]
    lines.append("  components:")
    for cid, c in (components or []):
        lines.append(f"    - component_id: {cid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not components:
        lines[-1] = "  components: []"
    lines.append("  profiles:")
    for pid, c in (profiles or []):
        lines.append(f"    - profile_id: {pid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not profiles:
        lines[-1] = "  profiles: []"
    if infrastructure is not None:
        lines.append("  infrastructure:")
        for iid, c in infrastructure:
            lines.append(f"    - infrastructure_id: {iid}")
            lines.append(f"      version_constraint: \"{c}\"")
        if not infrastructure:
            lines[-1] = "  infrastructure: []"
    (d / "deps.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class BuildDependencyGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        _load_spec_catalog.cache_clear()

    def tearDown(self) -> None:
        _load_spec_catalog.cache_clear()

    # --- chain: top -> mid -> base (base leaf) ---
    def _seed_chain(self, repo_root: Path) -> None:
        _write_catalog(repo_root, [
            {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
             "deps_path": "spec/component/top/deps.yaml"},
            {"spec_kind": "component", "spec_id": "mid", "spec_version": "0.1.0",
             "deps_path": "spec/component/mid/deps.yaml"},
            {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
             "deps_path": "spec/component/base/deps.yaml"},
        ])
        _write_deps(repo_root, "spec/component/top", "component", "top",
                    components=[("mid", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/mid", "component", "mid",
                    components=[("base", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/base", "component", "base")

    def test_chain_topo_levels_and_transitive_via(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_chain(repo)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(err)
            self.assertEqual(graph["node_key"], "component/top@0.1.0")
            self.assertEqual(graph["generated_by"], "conductor")
            # height: base=0, mid=1, top=2
            self.assertEqual(graph["all_nodes"], [
                {"node_key": "component/base@0.1.0", "topo_level": 0},
                {"node_key": "component/mid@0.1.0", "topo_level": 1},
                {"node_key": "component/top@0.1.0", "topo_level": 2},
            ])
            # base is transitive (reached via mid); mid is direct (not listed).
            self.assertEqual(graph["transitive_deps"], [
                {"node_key": "component/base@0.1.0", "via": ["component/mid@0.1.0"]},
            ])
            # host_direct reconstruction = all_nodes - self - transitive = {mid}
            all_nk = {n["node_key"] for n in graph["all_nodes"]}
            trans_nk = {d["node_key"] for d in graph["transitive_deps"]}
            self.assertEqual(all_nk - {"component/top@0.1.0"} - trans_nk,
                             {"component/mid@0.1.0"})

    def test_leaf_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # No catalog needed for a leaf (empty deps).
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/base",
                target_node_key="component/base@0.1.0")
            self.assertIsNone(err)
            self.assertEqual(graph["all_nodes"],
                             [{"node_key": "component/base@0.1.0", "topo_level": 0}])
            self.assertEqual(graph["transitive_deps"], [])

    # --- diamond: a -> b, c ; b -> d ; c -> d ; d leaf ---
    def _seed_diamond(self, repo_root: Path) -> None:
        _write_catalog(repo_root, [
            {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
             "deps_path": "spec/problem/a/deps.yaml"},
            {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
             "deps_path": "spec/component/b/deps.yaml"},
            {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
             "deps_path": "spec/component/c/deps.yaml"},
            {"spec_kind": "component", "spec_id": "d", "spec_version": "0.1.0",
             "deps_path": "spec/component/d/deps.yaml"},
        ])
        _write_deps(repo_root, "spec/problem/a", "problem", "a",
                    components=[("b", ">=0.1.0 <1.0.0"), ("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/b", "component", "b",
                    components=[("d", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/c", "component", "c",
                    components=[("d", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/d", "component", "d")

    def test_diamond_via_is_lex_min_and_l6_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_diamond(repo)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/a",
                target_node_key="problem/a@0.3.0")
            self.assertIsNone(err)
            # heights: d=0, b=1, c=1, a=2
            self.assertEqual(graph["all_nodes"], [
                {"node_key": "component/d@0.1.0", "topo_level": 0},
                {"node_key": "component/b@0.1.0", "topo_level": 1},
                {"node_key": "component/c@0.1.0", "topo_level": 1},
                {"node_key": "problem/a@0.3.0", "topo_level": 2},
            ])
            # b, c are direct; only d is transitive. via = lex-min of
            # [b] vs [c] -> [component/b@0.1.0]. (Builder does not raise L6.)
            self.assertEqual(graph["transitive_deps"], [
                {"node_key": "component/d@0.1.0", "via": ["component/b@0.1.0"]},
            ])

    def test_profile_and_component_mixed(self) -> None:
        """Issue #175: an adopted `profile` is DATA, not a node. It never enters `all_nodes`;
        the components it selects become the adopting node's own direct dependencies, and the
        adoption is recorded in the separate `profiles` key."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.2.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
                {"spec_kind": "component", "spec_id": "co", "spec_version": "0.1.0",
                 "deps_path": "spec/component/co/deps.yaml"},
                {"spec_kind": "component", "spec_id": "own", "spec_version": "0.1.0",
                 "deps_path": "spec/component/own/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        components=[("own", ">=0.1.0")], profiles=[("pr", ">=0.2.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr",
                        components=[("co", ">=0.1.0")])
            _write_deps(repo, "spec/component/co", "component", "co")
            _write_deps(repo, "spec/component/own", "component", "own")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(err)
            self.assertEqual({n["node_key"] for n in graph["all_nodes"]},
                             {"problem/p@0.1.0", "component/own@0.1.0",
                              "component/co@0.1.0"})
            # The profile-selected component is DIRECT, not transitive: the host directly-
            # required set is `all_nodes - self - transitive_deps`, and it must contain `co`.
            self.assertEqual(graph["transitive_deps"], [])
            self.assertEqual(graph["profiles"], [{
                "node_key": "profile/pr@0.2.0",
                "profile_id": "pr",
                "profile_version": "0.2.0",
                "version_constraint": ">=0.2.0",
                "components": [
                    {"component_id": "co", "version_constraint": ">=0.1.0"},
                ],
            }])

    def test_a_node_adopting_no_profile_records_an_empty_profiles_key(self) -> None:
        # The key is always present, so `_validate_profile_selection` can tell "adopts none"
        # from "sidecar predates the key" (which it refuses).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_chain(repo)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(err)
            self.assertEqual(graph["profiles"], [])

    def test_a_component_adopting_a_profile_is_expanded_too(self) -> None:
        """The expansion runs on EVERY visited node, not just the target. Applying it only to
        the target would leave a profile node in the closure of any dependency that adopts
        one — a fail-open the target's own sidecar would then record."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "component", "spec_id": "mid", "spec_version": "0.1.0",
                 "deps_path": "spec/component/mid/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.2.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        components=[("mid", ">=0.1.0")])
            _write_deps(repo, "spec/component/mid", "component", "mid",
                        profiles=[("pr", ">=0.2.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr",
                        components=[("base", ">=0.1.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(err)
            self.assertEqual({n["node_key"] for n in graph["all_nodes"]},
                             {"problem/p@0.1.0", "component/mid@0.1.0",
                              "component/base@0.1.0"})
            # `base` reaches the target THROUGH mid, so it is transitive — the profile is not
            # on the path because it is not a node at all.
            self.assertEqual(graph["transitive_deps"], [
                {"node_key": "component/base@0.1.0", "via": ["component/mid@0.1.0"]},
            ])
            # Only the TARGET's adoptions reach the sidecar; mid's are mid's own business.
            self.assertEqual(graph["profiles"], [])

    def _seed_two_profiles(self, repo: Path, con_a: str, con_b: str) -> None:
        _write_catalog(repo, [
            {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
             "deps_path": "spec/problem/p/deps.yaml"},
            {"spec_kind": "profile", "spec_id": "pa", "spec_version": "0.1.0",
             "deps_path": "spec/profile/pa/deps.yaml"},
            {"spec_kind": "profile", "spec_id": "pb", "spec_version": "0.1.0",
             "deps_path": "spec/profile/pb/deps.yaml"},
            {"spec_kind": "component", "spec_id": "co", "spec_version": "0.2.0",
             "deps_path": "spec/component/co/deps.yaml"},
            {"spec_kind": "component", "spec_id": "co", "spec_version": "0.5.0",
             "deps_path": "spec/component/co/deps.yaml"},
        ])
        _write_deps(repo, "spec/problem/p", "problem", "p",
                    profiles=[("pa", ">=0.1.0"), ("pb", ">=0.1.0")])
        _write_deps(repo, "spec/profile/pa", "profile", "pa", components=[("co", con_a)])
        _write_deps(repo, "spec/profile/pb", "profile", "pb", components=[("co", con_b)])
        _write_deps(repo, "spec/component/co", "component", "co")

    def test_two_profiles_selecting_one_component_intersect_to_one_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # `<0.5.0` and `>=0.1.0` both admit 0.2.0 — one node, pinned to the intersection's
            # highest member (NOT the catalog's highest, 0.5.0, which pa excludes).
            self._seed_two_profiles(repo, "<0.5.0", ">=0.1.0")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(err)
            self.assertEqual({n["node_key"] for n in graph["all_nodes"]},
                             {"problem/p@0.1.0", "component/co@0.2.0"})
            self.assertEqual([r["profile_id"] for r in graph["profiles"]], ["pa", "pb"])

    def test_two_profiles_disagreeing_on_a_component_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_two_profiles(repo, "<0.5.0", ">=0.5.0")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_component_conflict")
            self.assertIn("component/co", err["detail"])

    def test_declaring_a_profile_selected_component_directly_is_refused(self) -> None:
        """Issue #175 D5: a component has exactly ONE source. Declaring it both directly and
        through an adopted profile is the drift this refusal exists to prevent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
                {"spec_kind": "component", "spec_id": "co", "spec_version": "0.1.0",
                 "deps_path": "spec/component/co/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        components=[("co", ">=0.1.0")], profiles=[("pr", ">=0.1.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr",
                        components=[("co", ">=0.1.0")])
            _write_deps(repo, "spec/component/co", "component", "co")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_component_declared_twice")
            self.assertIn("component/co", err["detail"])
            self.assertIn("profile/pr", err["detail"])

    def test_a_profile_adopting_a_profile_is_refused_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "outer", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/outer/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "inner", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/inner/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        profiles=[("outer", ">=0.1.0")])
            _write_deps(repo, "spec/profile/outer", "profile", "outer",
                        profiles=[("inner", ">=0.1.0")])
            _write_deps(repo, "spec/profile/inner", "profile", "inner")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_nesting_unsupported")
            self.assertIn("inner", err["detail"])

    def test_a_profile_declaring_a_harness_is_refused(self) -> None:
        """A profile builds nothing. A harness declared on it would enter the closure of every
        ADOPTING node through an edge that node never wrote."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
                {"spec_kind": "infrastructure", "spec_id": "h", "spec_version": "0.1.0",
                 "deps_path": "spec/infrastructure/h/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        profiles=[("pr", ">=0.1.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr",
                        infrastructure=[("h", ">=0.1.0")])
            _write_deps(repo, "spec/infrastructure/h", "infrastructure", "h")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_declares_infrastructure")

    def test_an_unreadable_registry_at_the_expansion_is_not_a_profile_reason(self) -> None:
        """The `SpecCatalogCorruption` guard the expansion call site carries. A round-5 census
        found it unwitnessed, and swallowing it is not a crash but a MISCLASSIFICATION: the
        expansion would then report `profile_unresolvable` — a `_PROFILE_EXPANSION_REASONS`
        member, hence the STALE side of `_dependency_resolution_freshness` — for a node whose
        registry could not be READ, which is the FRESH side. That is the one-keystroke class
        `_profile_expansion_failure` exists to prevent, one frame outside the constructor it
        guards, so `test_every_expansion_refusal_uses_a_declared_reason` cannot reach it."""
        import tools.orchestration_runtime as ort
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p", profiles=[("pr", ">=0.1.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr")
            calls = {"n": 0}
            real = ort._load_spec_catalog

            def _corrupt_on_the_expansion_read(*args, **kwargs):
                # The FIRST read is the expansion's; later ones belong to edge resolution.
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ort.SpecCatalogCorruption("spec_catalog.yaml is unreadable")
                return real(*args, **kwargs)

            with mock.patch.object(ort, "_load_spec_catalog", _corrupt_on_the_expansion_read):
                graph, err = build_dependency_graph(
                    repo, target_spec_ref="spec/problem/p",
                    target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "spec_catalog_corrupt")
            self.assertNotIn(err["reason"], ort._PROFILE_EXPANSION_REASONS)
            self.assertIn(err["reason"], ort._UNREADABLE_CLOSURE_REASONS)

    def test_an_unresolvable_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        profiles=[("pr", ">=9.0.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_unresolvable")

    def test_a_profile_with_an_unreadable_deps_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        profiles=[("pr", ">=0.1.0")])
            # The catalog entry resolves the directory, but no deps.yaml is written there.
            (repo / "spec" / "profile" / "pr").mkdir(parents=True, exist_ok=True)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_deps_unreadable")

    def test_a_profile_with_a_malformed_deps_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.1.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        profiles=[("pr", ">=0.1.0")])
            (repo / "spec" / "profile" / "pr").mkdir(parents=True, exist_ok=True)
            # `profiles:` missing -> `_parse_dep_entries` marks it malformed.
            (repo / "spec" / "profile" / "pr" / "deps.yaml").write_text(
                "spec_id: pr\nspec_kind: profile\ndependencies:\n  components: []\n",
                encoding="utf-8")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "profile_deps_malformed")

    def test_version_pins_highest_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
                 "deps_path": "spec/component/top/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.2.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=0.1.0 <1.0.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(err)
            self.assertIn({"node_key": "component/base@0.2.0", "topo_level": 0},
                          graph["all_nodes"])

    # --- error taxonomy ---
    def test_cycle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
                 "deps_path": "spec/component/c/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/b", "component", "b",
                        components=[("c", ">=0.1.0")])
            _write_deps(repo, "spec/component/c", "component", "c",
                        components=[("b", ">=0.1.0")])
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/b",
                target_node_key="component/b@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_cycle")

    def test_unresolvable_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
                 "deps_path": "spec/component/top/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=9.0.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_unresolvable")

    def test_version_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # top -> base(==0.1.0); top -> mid -> base(==0.2.0): incompatible pins.
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
                 "deps_path": "spec/component/top/deps.yaml"},
                {"spec_kind": "component", "spec_id": "mid", "spec_version": "0.1.0",
                 "deps_path": "spec/component/mid/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.2.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", "==0.1.0"), ("mid", ">=0.1.0")])
            _write_deps(repo, "spec/component/mid", "component", "mid",
                        components=[("base", "==0.2.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_version_conflict")

    def test_malformed_deps_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            d = repo / "spec/component/top"
            d.mkdir(parents=True)
            # Unknown key -> malformed schema.
            (d / "deps.yaml").write_text(
                "spec_id: top\nspec_kind: component\ndependencies:\n  widgets: []\n",
                encoding="utf-8")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_deps_malformed")

    def test_spec_ref_unresolved_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Catalog has base's version but no deps_path/controlled_spec_path -> no spec dir.
            (repo / "spec" / "registry").mkdir(parents=True, exist_ok=True)
            (repo / "spec" / "registry" / "spec_catalog.yaml").write_text(
                "catalog_version: 0.2.0\nupdated_at: 2026-06-18\nspecs:\n"
                "  - spec_kind: component\n    spec_id: top\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/component/top/deps.yaml\n"
                "  - spec_kind: component\n    spec_id: base\n    spec_version: \"0.1.0\"\n",
                encoding="utf-8")
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=0.1.0")])
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_spec_ref_unresolved")

    def test_identity_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # top requires `mid` as BOTH a component and an infrastructure, and the catalog
            # resolves both to the SAME spec dir (deps_path): an identity conflict. The rule
            # is "one directory required under two kinds", so it is stated with two kinds that
            # are both closure NODES — a `profile` is expanded away before an edge is recorded
            # and so can no longer witness it (issue #175).
            (repo / "spec" / "registry").mkdir(parents=True, exist_ok=True)
            (repo / "spec" / "registry" / "spec_catalog.yaml").write_text(
                "catalog_version: 0.2.0\nupdated_at: 2026-06-18\nspecs:\n"
                "  - spec_kind: component\n    spec_id: top\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/component/top/deps.yaml\n"
                "  - spec_kind: component\n    spec_id: mid\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/shared/deps.yaml\n"
                "  - spec_kind: infrastructure\n    spec_id: mid\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/shared/deps.yaml\n",
                encoding="utf-8")
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("mid", ">=0.1.0")],
                        infrastructure=[("mid", ">=0.1.0")])
            _write_deps(repo, "spec/shared", "component", "mid")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_identity_conflict")

    def test_catalog_corrupt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Non-leaf target (has a dep edge) but NO catalog on disk -> SpecCatalogCorruption.
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=0.1.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "spec_catalog_corrupt")

    def test_catalog_corrupt_on_resolve_spec_ref_fails_closed(self) -> None:
        # The catalog resolves versions (cached) but is deleted before resolve_spec_ref_for
        # re-reads it -> SpecCatalogCorruption must be caught and returned as an error, not
        # escape as an uncaught exception into the conductor.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_chain(repo)
            import tools.orchestration_runtime as ort
            from tools.orchestration_runtime import SpecCatalogCorruption

            def _boom(*a, **k):
                raise SpecCatalogCorruption("catalog vanished mid-traversal")

            orig = ort.resolve_spec_ref_for
            ort.resolve_spec_ref_for = _boom
            try:
                graph, err = build_dependency_graph(
                    repo, target_spec_ref="spec/component/top",
                    target_node_key="component/top@0.1.0")
            finally:
                ort.resolve_spec_ref_for = orig
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "spec_catalog_corrupt")

    def test_include_via_false_preserves_the_node_sets(self) -> None:
        """The R6-lite freshness comparison reads the NODE SETS (`all_nodes` + the
        `transitive_deps` membership) but never the `via` paths, whose enumeration is
        exponential on a wide diamond. Skipping `via` must leave both node sets identical, or
        freshness would compare a different closure than the sidecar recorded."""
        cases = [
            (self._seed_chain, "spec/component/top", "component/top@0.1.0"),
            (self._seed_diamond, "spec/problem/a", "problem/a@0.3.0"),
        ]
        for seed, spec_ref, node_key in cases:
            with self.subTest(seed=seed.__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    _load_spec_catalog.cache_clear()
                    seed(repo)
                    kwargs = dict(target_spec_ref=spec_ref, target_node_key=node_key)
                    full, err_full = build_dependency_graph(repo, **kwargs)
                    _load_spec_catalog.cache_clear()
                    lite, err_lite = build_dependency_graph(
                        repo, include_via=False, **kwargs)
                    self.assertIsNone(err_full)
                    self.assertIsNone(err_lite)
                    self.assertEqual(full["all_nodes"], lite["all_nodes"])
                    self.assertEqual(full["node_key"], lite["node_key"])
                    # Membership is preserved (a set difference); only the paths are dropped.
                    self.assertEqual([d["node_key"] for d in full["transitive_deps"]],
                                     [d["node_key"] for d in lite["transitive_deps"]])
                    self.assertTrue(all(d["via"] == [] for d in lite["transitive_deps"]))
                    if seed is self._seed_diamond:
                        # Sanity: the full build really does have a `via` block to skip.
                        self.assertTrue(any(d["via"] for d in full["transitive_deps"]))

    def test_all_nodes_alone_does_not_identify_a_closure(self) -> None:
        """`topo_level` is a node's HEIGHT, so `a->b, a->c, b->c` and `a->b->c` share every
        `(node_key, topo_level)` pair. They differ only in whether `c` is direct or transitive.
        This is why `_closure_signature` compares the `transitive_deps` membership too — a
        deps.yaml edit that only moves an edge must still re-certify the node."""
        shapes = {}
        for label, a_deps in (("wide", ["b", "c"]), ("chain", ["b"])):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                _load_spec_catalog.cache_clear()
                _write_catalog(repo, [
                    {"spec_kind": "component", "spec_id": s, "spec_version": "0.1.0",
                     "deps_path": f"spec/component/{s}/deps.yaml"} for s in ("a", "b", "c")])
                _write_deps(repo, "spec/component/a", "component", "a",
                            components=[(d, ">=0.1.0 <1.0.0") for d in a_deps])
                _write_deps(repo, "spec/component/b", "component", "b",
                            components=[("c", ">=0.1.0 <1.0.0")])
                _write_deps(repo, "spec/component/c", "component", "c")
                graph, err = build_dependency_graph(
                    repo, target_spec_ref="spec/component/a",
                    target_node_key="component/a@0.1.0")
                self.assertIsNone(err)
                shapes[label] = graph
        self.assertEqual(shapes["wide"]["all_nodes"], shapes["chain"]["all_nodes"])
        self.assertEqual([d["node_key"] for d in shapes["wide"]["transitive_deps"]], [])
        self.assertEqual([d["node_key"] for d in shapes["chain"]["transitive_deps"]],
                         ["component/c@0.1.0"])

    def test_missing_deps_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/nope",
                target_node_key="component/nope@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_deps_unreadable")


if __name__ == "__main__":
    unittest.main()
