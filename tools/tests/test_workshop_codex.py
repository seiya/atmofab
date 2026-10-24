"""The `workshop leaf` on `codex_cli` (issue #534 step 2 PR-2): its lineage home, its sandbox
profile and the spawn seam's admission of it, its launch record through the REAL
`record_launch`, its preflight row, its argv, and the cancel event on the codex spawn.

The real CLI under the real profile is `test_bwrap_simulation.CodexWorkshopLeafCliTests`
(MX1-MX5); the loop on a codex entry is `test_workshop_producer.CodexWorkshopLoopTests`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools import workshop_tree
from tools.orchestration_runtime import (
    build_readonly_bwrap_profile,
    claude_workshop_profile_kwargs,
    codex_workshop_profile_kwargs,
    record_launch,
    render_bwrap_command,
    write_preflight,
)
from tools.pure_leaf import workshop_mcp_config_json
from tools.tests.test_pure_leaf_producer import _PureFakeConductor
from tools.tests.test_pure_leaf_wiring import _mark_dependencies_ready, _spawn_response
from tools.tests.test_workshop_launch import (
    _ORCH,
    _clear_active_child,
    _Env,
    _with_test_templates,
    _workshop_request,
)

_MCP = workshop_mcp_config_json("http://127.0.0.1:4242/mcp")


def _codex_config(tmp: Path) -> lc.LlmConfig:
    path = tmp / "llm_codex_workshop.yaml"
    path.write_text(
        "defaults: {provider: codex_cli, model: gpt-x}\n"
        "phases: {generate: {substeps: {generate: {provider: codex_cli, model: gpt-x, "
        "effort: medium, leaf: workshop, max_wrapper_calls: 30, lineage_timeout_s: 2700}}}}\n",
        encoding="utf-8")
    return lc.load_llm_config(path)


class _CodexEnv(_Env):
    """`_Env` plus an operator codex home holding a (placeholder) `auth.json`."""

    def setUp(self) -> None:
        super().setUp()
        origin = tempfile.TemporaryDirectory()
        self.addCleanup(origin.cleanup)
        self.origin = Path(origin.name)
        (self.origin / "auth.json").write_text("{}\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.origin)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _prepare_codex(self, lineage: str, *, resume: bool) -> dict | None:
        return ort._prepare_codex_workflow_home(self.repo, _ORCH, lineage, resume=resume,
                                                workshop=True)


class CodexLineageHomeTests(_CodexEnv):
    def test_a_cold_preparation_lays_out_the_lineage_with_its_codex_home(self) -> None:
        iso = self._prepare_codex("lin_a", resume=False)
        home = Path(iso["home"])
        self.assertEqual(home, (self.homes / _ORCH / "codex" / "lin_a").resolve())
        self.assertEqual(Path(iso["homes_root"]), self.homes.resolve())
        paths = workshop_tree.LineagePaths(
            homes_root=Path(iso["homes_root"]), orchestration_id=_ORCH, lineage_id="lin_a",
            orchestration_dir=self.repo / "workspace/orchestrations" / _ORCH, backend="codex")
        self.assertEqual((paths.home, paths.config, paths.tree),
                         (home, Path(iso["config"]), Path(iso["tree"])))
        for path in (home, paths.config, paths.tree, paths.staged, paths.syntax,
                     paths.snapshot):
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o700, path)
        self.assertEqual(sorted(p.name for p in paths.config.iterdir()),
                         ["auth.json", "config.toml"])
        self.assertEqual(Path(iso["auth_destination"]).read_bytes(), b"")
        self.assertEqual(Path(iso["auth"]), (self.origin / "auth.json").resolve())
        # The TREE (the leaf's cwd) is the project marked untrusted, not the checkout.
        self.assertEqual(Path(iso["config_toml"]).read_text(),
                         f'[projects."{paths.tree}"]\ntrust_level = "untrusted"\n')
        meta = json.loads((self.repo / "workspace/orchestrations" / _ORCH
                           / "orchestration_meta.json").read_text())
        self.assertEqual(meta["codex_workflow_home"], str(self.homes / _ORCH / "codex"))

    def test_a_lineage_is_created_once_and_required_warm(self) -> None:
        self._prepare_codex("lin_a", resume=False)
        with self.assertRaises(ValueError):
            self._prepare_codex("lin_a", resume=False)
        self.assertIsNone(self._prepare_codex("lin_gone", resume=True))
        self.assertFalse((self.homes / _ORCH / "codex" / "lin_gone").exists())
        self.assertEqual(self._prepare_codex("lin_a", resume=True)["lineage_id"], "lin_a")

    def test_a_pure_and_a_workshop_lineage_share_the_container(self) -> None:
        pure = ort._prepare_codex_workflow_home(self.repo, _ORCH, "lin_pure", resume=False)
        workshop = self._prepare_codex("lin_ws", resume=False)
        self.assertEqual(Path(pure["home"]).parent, Path(workshop["home"]).parent)
        # The pure lineage home IS its CODEX_HOME; the workshop one holds it as `config/`.
        self.assertEqual(Path(pure["config"]).name, "config.toml")
        self.assertEqual(Path(workshop["config"]), Path(workshop["home"]) / "config")

    def test_the_backend_is_one_of_the_lineage_backends(self) -> None:
        self.assertTrue(set(workshop_tree.LINEAGE_BACKENDS)
                        <= set(ort.WORKFLOW_BACKEND_HOME_DIRNAMES))
        self.assertEqual(set(workshop_tree.LINEAGE_BACKENDS), set(ort.WORKSHOP_HOME_PREPARERS))
        with self.assertRaises(ValueError):
            workshop_tree.LineagePaths(homes_root=self.homes, orchestration_id=_ORCH,
                                       lineage_id="l", orchestration_dir=self.repo,
                                       backend="http")


class CodexWorkshopProfileTests(_CodexEnv):
    def _profile(self, **over) -> tuple[dict, dict]:
        iso = self._prepare_codex("lin_a", resume=False)
        kwargs = {**codex_workshop_profile_kwargs(iso), **over}
        profile = build_readonly_bwrap_profile(
            repo_root=self.repo, orchestration_id=_ORCH, agent_run_id="lin_a",
            backend_command="codex", backend_type="codex", **kwargs)
        return profile, iso

    def test_the_profile_binds_the_lineage_and_remounts_the_two_host_files(self) -> None:
        profile, iso = self._profile()
        self.assertEqual((profile["readonly"], profile["leaf_mode"], profile["write_roots"]),
                         (False, "workshop", []))
        self.assertEqual(profile["runtime_rw_bind_paths"], [iso["config"], iso["tree"]])
        self.assertNotIn("runtime_rw_credential_mapping", profile)
        self.assertEqual(profile["env"]["CODEX_HOME"], iso["config"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", profile["env"])
        cmd = render_bwrap_command(profile=profile, command_argv=["codex", "exec"])
        self.assertEqual(cmd[cmd.index("--chdir") + 1], iso["tree"])
        binds = [(cmd[i + 1], cmd[i + 2]) for i, tok in enumerate(cmd) if tok == "--bind"]
        ro = [(cmd[i + 1], cmd[i + 2]) for i, tok in enumerate(cmd) if tok == "--ro-bind"]
        config_bind = cmd.index("--bind", cmd.index(iso["config"]) - 1)
        for pair in ((iso["auth"], iso["auth_destination"]),
                     (iso["config_toml"], iso["config_toml"])):
            self.assertIn(pair, ro)
            # Remounted AFTER the writable `config/` bind, which would otherwise cover it.
            position = next(i for i, tok in enumerate(cmd)
                            if tok == "--ro-bind" and (cmd[i + 1], cmd[i + 2]) == pair)
            self.assertGreater(position, config_bind)
        self.assertFalse(any(dst.endswith(".credentials.json") for _, dst in binds), binds)
        self.assertEqual(cmd[cmd.index("CODEX_HOME") + 1], iso["config"])
        import tools.validate_pipeline_semantics as vps
        self.assertEqual(vps._workshop_profile_violations(profile), [])

    def test_the_builder_refuses_a_credential_mapping_and_two_homes(self) -> None:
        iso = self._prepare_codex("lin_b", resume=False)
        base = dict(repo_root=self.repo, orchestration_id=_ORCH, agent_run_id="lin_b",
                    backend_command="codex", backend_type="codex")
        kwargs = codex_workshop_profile_kwargs(iso)
        for label, broken in (
                ("a credential mapping", {"credential_rw_mapping": ("/x", "/y")}),
                ("no workdir", {"workdir": None}),
                ("both homes", {"env_overrides": {"CODEX_HOME": iso["config"],
                                                  "CLAUDE_CONFIG_DIR": iso["config"]}}),
                ("no home", {"env_overrides": {}})):
            with self.subTest(case=label), self.assertRaises(ValueError):
                build_readonly_bwrap_profile(**base, **{**kwargs, **broken})

    def test_the_renderer_refuses_each_other_codex_shape(self) -> None:
        profile, iso = self._profile()
        tree_auth = str(Path(iso["tree"]) / "auth.json")
        cases = {
            "a credential mapping": lambda p: p.update(
                runtime_rw_credential_mapping=[iso["auth"], iso["auth_destination"]]),
            "auth.json mapped into the tree": lambda p: (
                Path(tree_auth).write_text(""),
                p.update(runtime_ro_bind_mappings=[[iso["auth"], tree_auth],
                                                   [iso["config_toml"], iso["config_toml"]]])),
            "a mapping missing": lambda p: p.update(
                runtime_ro_bind_mappings=[[iso["auth"], iso["auth_destination"]]]),
            "CODEX_HOME naming the tree": lambda p: p["env"].update(CODEX_HOME=iso["tree"]),
            "CODEX_HOME outside the rw binds": lambda p: p["env"].update(CODEX_HOME="/tmp"),
            "both homes": lambda p: p["env"].update(CLAUDE_CONFIG_DIR=iso["config"]),
            "no home": lambda p: p["env"].pop("CODEX_HOME"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                broken = json.loads(json.dumps(profile))
                mutate(broken)
                with self.assertRaises(ValueError):
                    render_bwrap_command(profile=broken, command_argv=["codex"])


class CodexAdmissionTests(_CodexEnv):
    """`_admissible_leaf_profile`: a codex workshop profile for a codex workshop entry, and
    neither family's profile for the other's entry."""

    def test_each_family_s_profile_is_admitted_for_its_own_entry_only(self) -> None:
        codex_iso = self._prepare_codex("lin_c", resume=False)
        claude_iso = self._prepare("lin_k", resume=False)
        common = dict(repo_root=self.repo, orchestration_id=_ORCH)
        codex_profile = build_readonly_bwrap_profile(
            agent_run_id="lin_c", backend_command="codex", backend_type="codex",
            **common, **codex_workshop_profile_kwargs(codex_iso))
        claude_profile = build_readonly_bwrap_profile(
            agent_run_id="lin_k", backend_command="claude", backend_type="claude",
            **common, **claude_workshop_profile_kwargs(claude_iso))
        codex_entry = _codex_config(self.repo).entry_for("generate", "generate")
        claude_entry = lc.load_llm_config(self._claude_yaml()).entry_for("generate", "generate")
        admit = wc._admissible_leaf_profile
        self.assertTrue(admit(codex_profile, codex_entry, self.repo))
        self.assertTrue(admit(claude_profile, claude_entry, self.repo))
        self.assertFalse(admit(codex_profile, claude_entry, self.repo))
        self.assertFalse(admit(claude_profile, codex_entry, self.repo))
        for label, mutate in (
                ("the claude home declared too",
                 lambda p: p["env"].update(CLAUDE_CONFIG_DIR=codex_iso["config"])),
                ("a credential mapping",
                 lambda p: p.update(runtime_rw_credential_mapping=["/x", "/y"])),
                ("a mapping missing",
                 lambda p: p.update(runtime_ro_bind_mappings=p["runtime_ro_bind_mappings"][:1])),
                ("CODEX_HOME on the tree",
                 lambda p: p["env"].update(CODEX_HOME=codex_iso["tree"]))):
            with self.subTest(case=label):
                broken = json.loads(json.dumps(codex_profile))
                mutate(broken)
                self.assertFalse(admit(broken, codex_entry, self.repo))

    def _claude_yaml(self) -> Path:
        path = self.repo / "llm_claude_ws.yaml"
        path.write_text("defaults: {provider: claude_cli}\nphases: {generate: {substeps: "
                        "{generate: {leaf: workshop, max_wrapper_calls: 3, "
                        "lineage_timeout_s: 60}}}}\n", encoding="utf-8")
        return path


class CodexWorkshopRecordLaunchTests(_CodexEnv):
    """The real `record_launch` with a codex workshop request."""

    def setUp(self) -> None:
        super().setUp()
        _mark_dependencies_ready(self.repo, _ORCH)
        write_preflight(repo_root=self.repo, orchestration_id=_ORCH, payload={
            "status": "pass", "sandbox_runtime": "bwrap", "sandbox_enforced": True,
            "can_launch_step_agents": True, "can_launch_substep_agents": True,
            "backend": "codex",
            "checks": [{"name": n, "pass": True} for n in (
                "codex_version_available", "codex_exec_json_streaming",
                "codex_exec_pure_isolation_flags", "codex_exec_resume", "codex_prompt_stdin",
                "codex_home_writable", "sandbox_bwrap_available", "sandbox_bwrap_userns",
                "sandbox_bwrap_exec")]})
        _with_test_templates(self)

    def _base(self) -> Path:
        return self.repo / "workspace/orchestrations" / _ORCH

    def _request(self, child: str, lineage: str | None = None) -> dict:
        return _workshop_request(child, lineage, workshop_provider="codex_cli",
                                 agent_model="gpt-x")

    def _launch(self, child: str, req: dict, *, prepare: bool = True, **kw) -> dict:
        if (prepare and "workshop_lineage_id" not in kw
                and not (self.homes / _ORCH / "codex" / child).exists()):
            self._prepare_codex(child, resume=False)
        prepared = ort.prepare_launch_request_payload(dict(req))
        req = dict(req, launch_prompt_full=ort.render_launch_prompt_text(prepared))
        return record_launch(repo_root=self.repo, orchestration_id=_ORCH,
                             parent_agent_run_id="orch_run_001", child_agent_run_id=child,
                             request_payload=req,
                             response_payload={"backend": "codex", **_spawn_response(child)},
                             **kw)

    def test_a_cold_codex_workshop_launch_records_its_lineage_and_profile(self) -> None:
        self._launch("ar_cx_cold", self._request("ar_cx_cold"))
        home = (self.homes / _ORCH / "codex" / "ar_cx_cold").resolve()
        profile = json.loads((self._base() / "sandbox_profiles/ar_cx_cold.json").read_text())
        self.assertEqual(profile["runtime_rw_bind_paths"],
                         [str(home / "config"), str(home / "tree")])
        self.assertEqual(profile["env"]["CODEX_HOME"], str(home / "config"))
        self.assertEqual(profile["workdir"], str(home / "tree"))
        response = json.loads((self._base() / "launches/ar_cx_cold.response.json").read_text())
        self.assertEqual(response["workshop_lineage_id"], "ar_cx_cold")
        self.assertEqual(response["codex_workflow_home"], str(home))
        # The pure thread-lineage layout was NOT prepared for this launch.
        self.assertNotIn("codex_lineage_id", response)
        self.assertFalse((home / "config.toml").exists())
        import tools.validate_pipeline_semantics as vps
        request = json.loads((self._base() / "launches/ar_cx_cold.request.json").read_text())
        self.assertEqual(vps._workshop_profile_violations(profile, request, self.repo), [])

    def test_a_warm_launch_resumes_the_lineage_home_it_names(self) -> None:
        self._launch("ar_cx_cold", self._request("ar_cx_cold"))
        _clear_active_child(self.repo)
        self._launch("ar_cx_warm", self._request("ar_cx_warm", "ar_cx_cold"),
                     workshop_lineage_id="ar_cx_cold")
        profile = json.loads((self._base() / "sandbox_profiles/ar_cx_warm.json").read_text())
        home = (self.homes / _ORCH / "codex" / "ar_cx_cold").resolve()
        self.assertEqual(profile["workdir"], str(home / "tree"))
        self.assertFalse((self.homes / _ORCH / "codex" / "ar_cx_warm").exists())

    def test_a_warm_launch_whose_home_is_gone_records_nothing(self) -> None:
        result = self._launch("ar_cx_warm", self._request("ar_cx_warm", "ar_gone"),
                              workshop_lineage_id="ar_gone")
        self.assertEqual(result, {"workshop_lineage_home_missing": True,
                                  "workshop_lineage_id": "ar_gone"})
        self.assertFalse((self._base() / "launches/ar_cx_warm.request.json").exists())
        self.assertFalse((self.homes / _ORCH / "codex" / "ar_gone").exists())

    def test_a_cold_launch_whose_home_the_host_did_not_prepare_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            self._launch("ar_cx_cold", self._request("ar_cx_cold"), prepare=False)
        self.assertFalse((self._base() / "launches/ar_cx_cold.request.json").exists())

    def test_a_pure_codex_lineage_id_on_a_workshop_launch_is_refused(self) -> None:
        self._prepare_codex("ar_cx_cold", resume=False)
        with self.assertRaises(ValueError):
            self._launch("ar_cx_cold", self._request("ar_cx_cold"),
                         codex_lineage_id="ar_cx_cold")
        self.assertFalse((self._base() / "launches/ar_cx_cold.request.json").exists())

    def test_the_request_names_this_launch_s_provider(self) -> None:
        with self.assertRaises(ValueError):
            self._launch("ar_cx_cold", _workshop_request("ar_cx_cold", agent_model="gpt-x"))
        self.assertFalse((self._base() / "launches/ar_cx_cold.request.json").exists())


class CodexPreflightTests(unittest.TestCase):
    _EXEC_HELP = ("Usage: codex exec\n  -m, --model <MODEL>\n  -c, --config <key=value>\n"
                  "  -s, --sandbox <SANDBOX_MODE>\n      [possible values: read-only, "
                  "workspace-write, danger-full-access]\n      --skip-git-repo-check\n"
                  "      --ignore-rules\n      --json\n")
    _MCP_HELP = "Usage: codex mcp add\n      --url <URL>\n"

    def _runner(self, exec_help: str, mcp_help: str, rc: int = 0):
        def run(argv, **kw):
            text = mcp_help if "mcp" in argv else exec_help
            return subprocess.CompletedProcess(argv, rc, text, "")
        return run

    def test_the_row_requires_each_option_the_mode_and_the_url_route(self) -> None:
        check = ort._codex_workshop_flag_set_check
        self.assertIs(check("codex", self._runner(self._EXEC_HELP, self._MCP_HELP))["pass"],
                      True)
        for option in ort.CODEX_WORKSHOP_EXEC_REQUIRED_OPTIONS:
            with self.subTest(dropped=option):
                text = "\n".join(ln for ln in self._EXEC_HELP.splitlines()
                                 if option not in ln)
                text += f"\n      --other  see {option}\n"
                result = check("codex", self._runner(text, self._MCP_HELP))
                self.assertIs(result["pass"], False)
                self.assertIn(option, result["detail"])
        result = check("codex", self._runner(
            self._EXEC_HELP.replace("workspace-write, ", ""), self._MCP_HELP))
        self.assertIs(result["pass"], False)
        self.assertIn("workspace-write", result["detail"])
        # Another option's description naming the mode does not answer for `--sandbox`'s values
        # (codex-cli 0.164.0's `--approve-for-me` says "the workspace-write sandbox").
        mentioned = (self._EXEC_HELP.replace("workspace-write, ", "")
                     + "      --approve-for-me\n          using the workspace-write sandbox\n")
        self.assertIs(check("codex", self._runner(mentioned, self._MCP_HELP))["pass"], False)
        # Nor do the possible values of the NEXT option, when `--sandbox` lists none of its own.
        borrowed = (self._EXEC_HELP.replace(
            "      [possible values: read-only, workspace-write, danger-full-access]\n", "")
            + "      --color <COLOR>\n      [possible values: always, workspace-write]\n")
        self.assertNotIn("possible values: read-only", borrowed)
        self.assertIs(check("codex", self._runner(borrowed, self._MCP_HELP))["pass"], False)
        result = check("codex", self._runner(self._EXEC_HELP, "Usage: codex mcp add\n"))
        self.assertIs(result["pass"], False)
        self.assertIn("mcp add --url", result["detail"])
        self.assertIs(check("codex", self._runner(self._EXEC_HELP, self._MCP_HELP, rc=1))
                      ["pass"], False)

    def test_the_required_options_are_the_cold_argv_s(self) -> None:
        """Every option the cold workshop argv spells is certified by the row, and every
        option the warm argv spells by `codex_exec_resume` (the option-set pin)."""
        with tempfile.TemporaryDirectory() as tmp:
            c = _PureFakeConductor(repo_root=Path(tmp), orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=_codex_config(Path(tmp)))
            entry = c.entry_for("generate", "generate")
            cold = wc.Conductor.leaf_command(c, entry, mcp_config_json=_MCP)
            warm = wc.Conductor.leaf_command(c, entry, resume_session_id="tid",
                                             mcp_config_json=_MCP)
        self.assertEqual(sorted({t for t in cold if t.startswith("--")}),
                         sorted(ort.CODEX_WORKSHOP_EXEC_REQUIRED_OPTIONS))
        self.assertEqual(sorted({t for t in warm if t.startswith("--")}),
                         sorted(ort.CODEX_EXEC_RESUME_REQUIRED_FLAGS))
        self.assertIn(ort.CODEX_WORKSHOP_SANDBOX_MODE, cold)

    def test_each_cli_s_workshop_surface_takes_its_own_row(self) -> None:
        def probe(**kw):
            return {"probe_command": kw.get("agent_command") or kw["backend"], "checks": [],
                    "status": "pass"}

        seen: list[tuple[str, str]] = []

        def fake(name):
            def check(cmd, runner):
                seen.append((name, cmd))
                return {"name": name, "pass": True, "detail": ""}
            return check

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm.yaml"
            path.write_text(
                "defaults: {provider: claude_cli}\nphases: {generate: {substeps: {generate: "
                "{provider: codex_cli, model: gpt-x, leaf: workshop, max_wrapper_calls: 3, "
                "lineage_timeout_s: 60}}}}\n", encoding="utf-8")
            with mock.patch.object(ort, "probe_execution_platform", probe), \
                    mock.patch.object(ort, "_preflight_allows_agent_launch", lambda p: True), \
                    mock.patch.object(ort, "_claude_workshop_flag_set_check",
                                      fake("claude_workshop_flag_set_available")), \
                    mock.patch.object(ort, "_codex_workshop_flag_set_check",
                                      fake("codex_workshop_flag_set_available")):
                providers = ort.probe_all_providers(config=lc.load_llm_config(path))
                self.assertEqual([name for name, _ in seen],
                                 ["codex_workshop_flag_set_available"])
                names = {t: [c["name"] for c in row["checks"]] for t, row in providers.items()}
                self.assertNotIn("claude_workshop_flag_set_available", names["claude"])
                self.assertIn("codex_workshop_flag_set_available", names["codex"])
                seen.clear()
                ort._reprobe_recorded_providers(providers, None)
                self.assertEqual([name for name, _ in seen],
                                 ["codex_workshop_flag_set_available"])

    def test_a_backend_with_no_row_fails_it(self) -> None:
        self.assertIs(ort._workshop_flag_set_check("anthropic_api", "x", None)["pass"], False)


class CodexLeafCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.c = _PureFakeConductor(repo_root=Path(tmp.name), orchestration_id="o",
                                    orchestration_agent_run_id="orch", env={},
                                    llm_config=_codex_config(Path(tmp.name)))
        self.entry = self.c.entry_for("generate", "generate")

    def test_the_cold_and_warm_argv(self) -> None:
        overrides = ["--config", 'approval_policy="never"', "--config", "features.apps=false",
                     "--config", 'mcp_servers.wrapper.url="http://127.0.0.1:4242/mcp"',
                     "--config", 'mcp_servers.wrapper.default_tools_approval_mode="approve"']
        self.assertEqual(
            wc.Conductor.leaf_command(self.c, self.entry, session_id="s", mcp_config_json=_MCP),
            ["codex", "exec", "--model", "gpt-x", "--config", 'model_reasoning_effort="medium"',
             "--ignore-rules", "--sandbox", "workspace-write", "--skip-git-repo-check",
             *overrides, "--json", "-"])
        self.assertEqual(
            wc.Conductor.leaf_command(self.c, self.entry, session_id="s",
                                      resume_session_id="thread-1", mcp_config_json=_MCP),
            ["codex", "exec", "resume", "--model", "gpt-x", "thread-1",
             "--config", 'model_reasoning_effort="medium"', "--ignore-rules",
             "--config", 'sandbox_mode="workspace-write"', "--skip-git-repo-check",
             *overrides, "--json", "-"])

    def test_the_entry_and_the_endpoint_must_agree(self) -> None:
        with self.assertRaises(ValueError):
            wc.Conductor.leaf_command(self.c, self.entry)
        with self.assertRaises(ValueError):
            wc.Conductor.leaf_command(self.c, self.c.entry_for(None, None),
                                      mcp_config_json=_MCP)

    def test_the_endpoint_must_be_a_loopback_wrapper_url(self) -> None:
        for bad in ("{}", "not json", json.dumps({"mcpServers": {"wrapper": {
                "url": "http://example.com/mcp"}}})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                wc.Conductor.leaf_command(self.c, self.entry, mcp_config_json=bad)

    def test_the_setting_surface_records_the_codex_endpoint(self) -> None:
        surface = wc.Conductor._launch_setting_surface(
            self.c, "child", {"step": "generate", "substep": "generate"}, self.entry,
            mcp_config_json=_MCP)
        self.assertEqual(surface, {"codex_mcp_config": [
            {"server": "wrapper", "url": "http://127.0.0.1:4242/mcp"}]})


class CodexWorkshopSpawnTests(unittest.TestCase):
    """`spawn_leaf(cancel_event=)` on a codex entry ends the leaf's process group."""

    def _conductor(self, tmp: Path, argv: list[str]):
        c = _PureFakeConductor(repo_root=tmp, orchestration_id="o",
                               orchestration_agent_run_id="orch", env={},
                               llm_config=_codex_config(tmp))
        self.events: list[tuple[str, dict]] = []
        c.emit = lambda name, **f: self.events.append((name, f))  # type: ignore[assignment]
        c._bwrap_enabled = lambda: False  # type: ignore[assignment]
        c.leaf_command = lambda *a, **k: list(argv)  # type: ignore[assignment]
        c._bind_codex_thread = lambda arid, tid: None  # type: ignore[assignment]
        return c

    def test_a_cancel_event_ends_the_codex_leaf_and_its_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child.pid"
            leaf = ("import json, os, subprocess, sys, time\n"
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}), "
                    "flush=True)\n"
                    f"p = subprocess.Popen(['sleep', '60'])\n"
                    f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
                    "time.sleep(60)\n")
            c = self._conductor(Path(tmp), [sys.executable, "-c", leaf])
            cancel = threading.Event()
            threading.Timer(1.0, cancel.set).start()
            t0 = time.monotonic()
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "generate"), child_arid="child", cancel_event=cancel,
                mcp_config_json=_MCP)
            self.assertLess(time.monotonic() - t0, 30)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.endswith(wc.LEAF_CANCELLED_MARKER), result.stderr)
            self.assertNotIn("leaf_timeout", [name for name, _ in self.events])
            # The tool subprocess the leaf started went with its group.
            pid = int(marker.read_text())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail(f"the leaf's subprocess {pid} outlived the cancel")

    def test_an_unset_cancel_event_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            leaf = ("import json\n"
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}))\n"
                    "print(json.dumps({'type': 'item.completed', 'item': {"
                    "'type': 'agent_message', 'text': 'done'}}))\n"
                    "print(json.dumps({'type': 'turn.completed', 'usage': {"
                    "'input_tokens': 1, 'output_tokens': 2}}))\n")
            c = self._conductor(Path(tmp), [sys.executable, "-c", leaf])
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "generate"), child_arid="child",
                cancel_event=threading.Event(), mcp_config_json=_MCP)
            self.assertEqual((result.returncode, result.stdout), (0, "done"))
            self.assertNotIn(wc.LEAF_CANCELLED_MARKER, result.stderr)

    def test_items_are_counted_from_the_stream_not_the_capped_capture(self) -> None:
        """`items_completed` is counted by the read loop: a leaf whose JSONL outgrows the raw
        capture (`LEAF_RAW_STDOUT_CAPTURE_MAX_CHARS`) still reports every completed item."""
        n, width = 301, 20_000
        self.assertGreater(n * width, wc.LEAF_RAW_STDOUT_CAPTURE_MAX_CHARS)
        with tempfile.TemporaryDirectory() as tmp:
            leaf = ("import json\n"
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 't1'}))\n"
                    f"for i in range({n - 1}):\n"
                    "    print(json.dumps({'type': 'item.completed', 'item': {"
                    f"'type': 'command_execution', 'aggregated_output': 'x' * {width}}}}}))\n"
                    "print(json.dumps({'type': 'item.completed', 'item': {"
                    "'type': 'agent_message', 'text': 'done'}}))\n"
                    "print(json.dumps({'type': 'turn.completed', 'usage': {"
                    "'input_tokens': 1, 'output_tokens': 2}}))\n")
            c = self._conductor(Path(tmp), [sys.executable, "-c", leaf])
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "generate"), child_arid="child",
                cancel_event=threading.Event(), mcp_config_json=_MCP)
            self.assertEqual((result.returncode, result.stdout), (0, "done"))
            self.assertLess(result.raw_stdout.count('"type": "item.completed"'), n)
            self.assertEqual(result.items_completed, n)

    def test_the_bound_still_fires_while_a_cancel_event_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = self._conductor(Path(tmp), ["sleep", "60"])
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "generate"), child_arid="child",
                timeout_seconds=1, timeout_name="lineage_timeout_s",
                cancel_event=threading.Event(), mcp_config_json=_MCP)
            self.assertIn("leaf exceeded lineage_timeout_s=1", result.stderr)
            self.assertNotIn(wc.LEAF_CANCELLED_MARKER, result.stderr)


if __name__ == "__main__":
    unittest.main()
