"""The `workshop leaf`'s launch shape (issue #534 PR-2): its sandbox profile, its lineage home,
its launch record, its argv, and the spawn seam's admission of its one writable profile.

The launch record is driven through the REAL `record_launch` — `_PureFakeConductor` stubs it,
so without these rows the first billed run would be the first thing to cross its validators.
The workshop templates are PR-3's: the rows that render a prompt patch a stand-in template
into the template map, carrying the identity block the validators read.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tools.llm_config as lc
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
from tools import operator_private_root, pure_leaf
from tools.orchestration_runtime import (
    build_readonly_bwrap_profile,
    claude_workshop_profile_kwargs,
    init_orchestration,
    record_launch,
    render_bwrap_command,
)
from tools.pure_leaf import (
    WORKSHOP_PROMPT_CONTRACT_VERSION,
    WORKSHOP_PROMPT_SENTINEL,
    workshop_mcp_config_json,
)
from tools.tests.test_pure_leaf_producer import _PureFakeConductor
from tools.tests.test_pure_leaf_wiring import (
    _mark_dependencies_ready,
    _preflight,
    _pure_request,
    _spawn_response,
)

_ORCH = "orch_001"
_REAL_TEMPLATE_LOADER = ort._load_launch_prompt_templates
_REAL_TEMPLATE_FILES = dict(ort._PROMPT_TEMPLATE_FILES)

#: A stand-in for PR-3's `workshop_generate_generate.txt`: the identity block the validators
#: bind, under the workshop sentinel, and the five document slots of the default shape.
_TEST_TEMPLATE = (
    f"{WORKSHOP_PROMPT_SENTINEL}: stand-in template (issue #534 PR-2 tests).\n\n"
    "<harness_capabilities>\n<target_profile>\n<ir_document>\n<tests_document>\n"
    "<runner_document>\n\n"
    "Target node_key: <node_key>\nTarget step: <step>\nTarget substep: <substep>\n"
    "orchestration_id: <orchestration_id>\nagent_run_id: <agent_run_id>\n"
    "prompt_contract_version: <prompt_contract_version>\n")
_TEST_REPAIR_TEMPLATE = (
    f"{WORKSHOP_PROMPT_SENTINEL}: stand-in repair template.\n\n<findings>\n\n"
    "Target node_key: <node_key>\nTarget step: <step>\nTarget substep: <substep>\n"
    "orchestration_id: <orchestration_id>\nagent_run_id: <agent_run_id>\n"
    "prompt_contract_version: <prompt_contract_version>\n")


def _with_test_templates(test: unittest.TestCase) -> None:
    real = ort._load_launch_prompt_templates()
    patched = {**real, "workshop generate.generate": _TEST_TEMPLATE,
               "workshop bundle repair": _TEST_REPAIR_TEMPLATE}
    for target, value in (("_load_launch_prompt_templates", lambda: patched),
                          ("_PROMPT_TEMPLATE_FILES",
                           {**ort._PROMPT_TEMPLATE_FILES,
                            "workshop generate.generate": "workshop_generate_generate.txt",
                            "workshop bundle repair": "workshop_bundle_repair.txt"})):
        patcher = mock.patch.object(ort, target, value)
        patcher.start()
        test.addCleanup(patcher.stop)


def _workshop_request(child: str, lineage: str | None = None, **overrides) -> dict:
    req = _pure_request("generate", leaf_mode="workshop",
                        prompt_contract_version=WORKSHOP_PROMPT_CONTRACT_VERSION,
                        agent_run_id=child, workshop_lineage_id=lineage or child)
    req.update(overrides)
    return req


class _Env(unittest.TestCase):
    """A checkout, a homes root and an operator credentials file, all temporary and apart."""

    def setUp(self) -> None:
        repo = tempfile.TemporaryDirectory()
        homes = tempfile.TemporaryDirectory()
        cred = tempfile.TemporaryDirectory()
        for d in (repo, homes, cred):
            self.addCleanup(d.cleanup)
        self.repo = Path(repo.name)
        self.homes = Path(homes.name) / "homes"
        self.credentials = Path(cred.name) / ".credentials.json"
        self.credentials.write_text('{"claudeAiOauth": {"accessToken": "x"}}', encoding="utf-8")
        os.chmod(self.credentials, 0o600)
        env = {"ATMOFAB_WORKFLOW_HOMES_ROOT": str(self.homes),
               "ATMOFAB_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT": "0",
               "ATMOFAB_ORCHESTRATION_ASSUME_BWRAP": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        cred_patch = mock.patch.object(operator_private_root, "claude_credentials_file",
                                       lambda: self.credentials)
        cred_patch.start()
        self.addCleanup(cred_patch.stop)
        init_orchestration(repo_root=self.repo, orchestration_id=_ORCH)

    def _prepare(self, lineage: str, *, resume: bool) -> dict | None:
        return ort._prepare_claude_workshop_home(self.repo, _ORCH, lineage, resume=resume)


class LineageHomeTests(_Env):
    def test_a_cold_preparation_creates_a_private_home_with_its_config_and_tree(self) -> None:
        iso = self._prepare("lin_a", resume=False)
        home = Path(iso["home"])
        self.assertEqual(home, (self.homes / _ORCH / "claude" / "lin_a").resolve())
        self.assertEqual(Path(iso["homes_root"]), self.homes.resolve())
        for path in (home, Path(iso["config"]), Path(iso["tree"])):
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o700, path)
        self.assertEqual(sorted(p.name for p in Path(iso["config"]).iterdir()),
                         [".credentials.json"])
        self.assertEqual(Path(iso["credentials_destination"]).read_bytes(), b"")
        self.assertEqual(Path(iso["credentials"]), self.credentials.resolve())
        meta = json.loads((self.repo / "workspace/orchestrations" / _ORCH
                           / "orchestration_meta.json").read_text())
        self.assertEqual(meta["claude_workflow_home"], str(self.homes / _ORCH / "claude"))
        # The record directory of `workshop_tree.LineagePaths` exists too.
        self.assertTrue((self.repo / "workspace/orchestrations" / _ORCH / "workshop"
                         / "lin_a").is_dir())

    def test_a_lineage_is_created_once(self) -> None:
        self._prepare("lin_a", resume=False)
        with self.assertRaises(ValueError) as ctx:
            self._prepare("lin_a", resume=False)
        self.assertIn("already exists", str(ctx.exception))

    def test_a_warm_preparation_finds_the_home_or_answers_none(self) -> None:
        cold = self._prepare("lin_a", resume=False)
        (Path(cold["config"]) / "projects").mkdir()
        self.assertEqual(self._prepare("lin_a", resume=True), cold)
        self.assertIsNone(self._prepare("lin_gone", resume=True))
        self.assertFalse((self.homes / _ORCH / "claude" / "lin_gone").exists())

    def test_a_warm_preparation_re_secures_the_lineage_home(self) -> None:
        cold = self._prepare("lin_a", resume=False)
        home = Path(cold["home"])
        os.chmod(home, 0o755)
        self._prepare("lin_a", resume=True)
        self.assertEqual(stat.S_IMODE(home.lstat().st_mode), 0o700)

    def test_a_warm_preparation_re_secures_the_config_home(self) -> None:
        """The CLI's own `config/` holds the lineage's transcripts: a drifted mode is tightened
        on the warm preparation, and a link in its place is refused."""
        cold = self._prepare("lin_a", resume=False)
        config = Path(cold["config"])
        os.chmod(config, 0o755)
        self._prepare("lin_a", resume=True)
        self.assertEqual(stat.S_IMODE(config.lstat().st_mode), 0o700)
        config.rename(config.with_name("config_real"))
        config.symlink_to(config.with_name("config_real"))
        with self.assertRaises(ValueError):
            self._prepare("lin_a", resume=True)

    def test_a_missing_credentials_file_is_refused(self) -> None:
        self.credentials.unlink()
        with self.assertRaises(ValueError) as ctx:
            self._prepare("lin_a", resume=False)
        self.assertIn("credentials file not found", str(ctx.exception))

    def test_a_lineage_id_must_be_a_plain_token(self) -> None:
        for bad in ("../x", "a/b", "", "x y"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._prepare(bad, resume=False)


class WorkshopProfileTests(_Env):
    def _profile(self, **over) -> tuple[dict, dict]:
        iso = self._prepare("lin_a", resume=False)
        kwargs = {**claude_workshop_profile_kwargs(iso), **over}
        profile = build_readonly_bwrap_profile(
            repo_root=self.repo, orchestration_id=_ORCH, agent_run_id="lin_a",
            backend_command="claude", backend_type="claude", **kwargs)
        return profile, iso

    def test_the_profile_binds_the_lineage_and_nothing_of_the_operators_home(self) -> None:
        profile, iso = self._profile()
        self.assertIs(profile["readonly"], False)
        self.assertEqual(profile["leaf_mode"], "workshop")
        self.assertEqual(profile["write_roots"], [])
        self.assertEqual(profile["runtime_rw_bind_paths"], [iso["config"], iso["tree"]])
        self.assertEqual(profile["workdir"], iso["tree"])
        self.assertEqual(profile["env"]["CLAUDE_CONFIG_DIR"], iso["config"])
        cmd = render_bwrap_command(profile=profile, command_argv=["claude", "-p"])
        self.assertEqual(cmd[cmd.index("--chdir") + 1], iso["tree"])
        binds = [(cmd[i + 1], cmd[i + 2]) for i, tok in enumerate(cmd) if tok == "--bind"]
        self.assertIn((iso["config"], iso["config"]), binds)
        self.assertIn((iso["tree"], iso["tree"]), binds)
        # The credential mapping is the one file bind, AFTER its parent `config/`.
        cred = (iso["credentials"], iso["credentials_destination"])
        self.assertIn(cred, binds)
        self.assertGreater(binds.index(cred), binds.index((iso["config"], iso["config"])))
        operator_claude = str(Path(os.environ.get("HOME", "/nonexistent")) / ".claude")
        self.assertFalse(any(src == operator_claude for src, _ in binds), binds)
        self.assertEqual(cmd[cmd.index("CLAUDE_CONFIG_DIR") - 1], "--setenv")
        self.assertEqual(cmd[cmd.index("CLAUDE_CONFIG_DIR") + 1], iso["config"])
        self.assertIn(str(self.repo), [cmd[i + 1] for i, t in enumerate(cmd) if t == "--tmpfs"])

    def test_the_builder_refuses_a_half_workshop_profile(self) -> None:
        iso = self._prepare("lin_b", resume=False)
        base = dict(repo_root=self.repo, orchestration_id=_ORCH, agent_run_id="lin_b",
                    backend_command="claude", backend_type="claude")
        kwargs = claude_workshop_profile_kwargs(iso)
        for broken in ({"readonly": True}, {"leaf_mode": None}, {"leaf_mode": "agentic"},
                       {"workdir": None}, {"credential_rw_mapping": None}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                build_readonly_bwrap_profile(**base, **{**kwargs, **broken})
        # A pure profile takes neither a workdir nor a credential mapping.
        for extra in ({"workdir": iso["tree"]},
                      {"credential_rw_mapping": (iso["credentials"],
                                                 iso["credentials_destination"])},
                      {"readonly": False}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                build_readonly_bwrap_profile(**base, **extra)

    def test_the_renderer_refuses_each_workshop_field_off_a_workshop_profile(self) -> None:
        profile, iso = self._profile()
        cases = {
            "pure profile naming CLAUDE_CONFIG_DIR": lambda p: (
                p.pop("leaf_mode"), p.pop("runtime_rw_credential_mapping"),
                p.update(readonly=True, workdir=p["repo_root"])),
            "pure profile with a credential mapping": lambda p: (
                p.pop("leaf_mode"), p["env"].pop("CLAUDE_CONFIG_DIR"),
                p.update(readonly=True, workdir=p["repo_root"])),
            "pure profile with another workdir": lambda p: (
                p.pop("leaf_mode"), p.pop("runtime_rw_credential_mapping"),
                p["env"].pop("CLAUDE_CONFIG_DIR"), p.update(readonly=True)),
            "workshop workdir outside its rw binds": lambda p: p.update(workdir="/tmp"),
            "workshop profile marked readonly": lambda p: p.update(readonly=True),
            "credential destination outside config": lambda p: p.update(
                runtime_rw_credential_mapping=[iso["credentials"], iso["credentials"]]),
            "CLAUDE_CONFIG_DIR naming another directory": lambda p: p["env"].update(
                CLAUDE_CONFIG_DIR="/tmp"),
            "unknown leaf_mode": lambda p: p.update(leaf_mode="agentic"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                broken = json.loads(json.dumps(profile))
                mutate(broken)
                with self.assertRaises(ValueError):
                    render_bwrap_command(profile=broken, command_argv=["claude"])


class WorkshopRecordLaunchTests(_Env):
    """The real `record_launch` with a workshop request."""

    def setUp(self) -> None:
        super().setUp()
        _mark_dependencies_ready(self.repo, _ORCH)
        _preflight(self.repo)
        _with_test_templates(self)

    def _launch(self, child: str, req: dict, *, prepare: bool = True,
                response: dict | None = None, **kw) -> dict:
        """Record one launch. A COLD workshop launch has its lineage home prepared first, as
        the conductor does in its own process before it records the launch (`prepare=False`
        leaves it absent)."""
        if (prepare and pure_leaf.is_workshop_request(req) and "workshop_lineage_id" not in kw
                and not (self.homes / _ORCH / "claude" / child).exists()):
            self._prepare(child, resume=False)
        prepared = ort.prepare_launch_request_payload(dict(req))
        req = dict(req, launch_prompt_full=ort.render_launch_prompt_text(prepared))
        return record_launch(repo_root=self.repo, orchestration_id=_ORCH,
                             parent_agent_run_id="orch_run_001", child_agent_run_id=child,
                             request_payload=req,
                             response_payload=response or _spawn_response(child), **kw)

    def test_a_cold_launch_whose_home_the_host_did_not_prepare_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._launch("ar_ws_cold", _workshop_request("ar_ws_cold"), prepare=False)
        self.assertIn("the host prepares it before recording the launch", str(ctx.exception))
        self.assertFalse((self._base() / "launches/ar_ws_cold.request.json").exists())
        self.assertFalse((self.homes / _ORCH / "claude" / "ar_ws_cold").exists())

    def _base(self) -> Path:
        return self.repo / "workspace/orchestrations" / _ORCH

    def _index_row(self, child: str) -> dict:
        doc = json.loads((self._base() / "session_run_index.json").read_text())
        return next(e for e in doc["entries"] if e["agent_run_id"] == child)

    def test_a_cold_workshop_launch_records_its_lineage_and_its_writable_profile(self) -> None:
        self._launch("ar_ws_cold", _workshop_request("ar_ws_cold"))
        profile = json.loads((self._base() / "sandbox_profiles/ar_ws_cold.json").read_text())
        self.assertEqual(profile["leaf_mode"], "workshop")
        self.assertIs(profile["readonly"], False)
        home = (self.homes / _ORCH / "claude" / "ar_ws_cold").resolve()
        self.assertEqual(profile["runtime_rw_bind_paths"],
                         [str(home / "config"), str(home / "tree")])
        response = json.loads((self._base() / "launches/ar_ws_cold.response.json").read_text())
        self.assertEqual(response["workshop_lineage_id"], "ar_ws_cold")
        self.assertEqual(response["claude_workflow_home"], str(home))
        self.assertEqual(self._index_row("ar_ws_cold")["workshop_lineage_id"], "ar_ws_cold")
        request = json.loads((self._base() / "launches/ar_ws_cold.request.json").read_text())
        self.assertEqual(request["allowed_output_paths"], [])
        prompt = (self._base() / "launches/ar_ws_cold.prompt.txt").read_text()
        self.assertTrue(prompt.startswith(WORKSHOP_PROMPT_SENTINEL))
        self.assertIn(f"prompt_contract_version: {WORKSHOP_PROMPT_CONTRACT_VERSION}", prompt)
        # The workshop profile passes the persisted-record audit the validator runs.
        import tools.validate_pipeline_semantics as vps
        self.assertEqual(vps._workshop_profile_violations(profile), [])

    def test_a_warm_launch_resumes_the_lineage_home_it_names(self) -> None:
        self._launch("ar_ws_cold", _workshop_request("ar_ws_cold"))
        _clear_active_child(self.repo)
        self._launch("ar_ws_warm", _workshop_request("ar_ws_warm", "ar_ws_cold"),
                     workshop_lineage_id="ar_ws_cold")
        profile = json.loads((self._base() / "sandbox_profiles/ar_ws_warm.json").read_text())
        home = (self.homes / _ORCH / "claude" / "ar_ws_cold").resolve()
        self.assertEqual(profile["workdir"], str(home / "tree"))
        self.assertEqual(self._index_row("ar_ws_warm")["workshop_lineage_id"], "ar_ws_cold")
        self.assertFalse((self.homes / _ORCH / "claude" / "ar_ws_warm").exists())

    def test_a_warm_launch_whose_home_is_gone_records_nothing(self) -> None:
        result = self._launch("ar_ws_warm", _workshop_request("ar_ws_warm", "ar_gone"),
                              workshop_lineage_id="ar_gone")
        self.assertEqual(result, {"workshop_lineage_home_missing": True,
                                  "workshop_lineage_id": "ar_gone"})
        self.assertFalse((self._base() / "launches/ar_ws_warm.request.json").exists())
        self.assertFalse((self._base() / "sandbox_profiles/ar_ws_warm.json").exists())
        self.assertFalse((self.homes / _ORCH / "claude" / "ar_gone").exists())
        # The terse projection keeps the sentinel whole, as it does the codex one.
        self.assertEqual(ort._project_terse_result("record-launch", result), result)

    def test_refusals(self) -> None:
        cases = {
            "no lineage id": (_workshop_request("ar_x", workshop_lineage_id=None), {}),
            "a cold launch naming another lineage": (
                _workshop_request("ar_x", "ar_other"), {}),
            "a warm launch whose request names another lineage": (
                _workshop_request("ar_x", "ar_a"), {"workshop_lineage_id": "ar_b"}),
            "a warm launch resuming itself": (
                _workshop_request("ar_x", "ar_x"), {"workshop_lineage_id": "ar_x"}),
            "a lineage id on a pure request": (
                _pure_request("generate", agent_run_id="ar_x"), {"workshop_lineage_id": "ar_a"}),
            "a lineage field on a pure request": (
                _pure_request("generate", agent_run_id="ar_x", workshop_lineage_id="ar_x"), {}),
            "the pure contract version": (
                _workshop_request("ar_x", prompt_contract_version=
                                  pure_leaf.PURE_PROMPT_CONTRACT_VERSION), {}),
            "a write path": (_workshop_request("ar_x", allowed_output_paths=["x"]), {}),
            "an HTTP provider named by the response": (
                _workshop_request("ar_x"),
                {"response": {**_spawn_response("ar_x"), "backend": "anthropic_api"}}),
            "a codex backend named by the response": (
                _workshop_request("ar_x"),
                {"response": {**_spawn_response("ar_x"), "backend": "codex"}}),
        }
        self._prepare("ar_x", resume=False)
        # A lineage that EXISTS, so the cold "starts its own lineage" check is the one that
        # refuses the row naming it, not the absence of its home (round 2: the row was masked).
        self._prepare("ar_other", resume=False)
        for label, (req, kw) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises((ValueError, RuntimeError)):
                    self._launch("ar_x", req, **kw)
                self.assertFalse((self._base() / "launches/ar_x.request.json").exists())

    def test_a_workshop_request_on_another_pair_is_refused(self) -> None:
        """Refused twice: the renderer finds no workshop template for the pair (it renders
        before validation), and the validator names the admissible pairs."""
        req = _workshop_request("ar_x", substep="verify")
        with self.assertRaises(ValueError) as ctx:
            ort.prepare_launch_request_payload(req)
        self.assertIn("workshop generate.verify", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            ort._validate_launch_request_payload(req)
        self.assertIn("leaf_mode=workshop is only valid for (generate, generate)",
                      str(ctx.exception))

    def test_the_prompt_and_the_request_must_agree_on_the_leaf_model(self) -> None:
        workshop = ort.prepare_launch_request_payload(_workshop_request("ar_x"))
        pure = ort.prepare_launch_request_payload(_pure_request("generate"))
        pure_prompt = ort.render_launch_prompt_text(pure)
        workshop_prompt = ort.render_launch_prompt_text(workshop)
        with self.assertRaises(ValueError):
            ort._validate_launch_prompt_text(workshop, pure_prompt)
        with self.assertRaises(ValueError):
            ort._validate_launch_prompt_text(pure, workshop_prompt)
        ort._validate_launch_prompt_text(workshop, workshop_prompt)
        # The identity VALUES are bound for a workshop prompt as for a pure one.
        with self.assertRaises(ValueError):
            ort._validate_launch_prompt_text(
                workshop, workshop_prompt.replace("Target step: generate", "Target step: x"))

    def test_the_repair_turn_renders_from_the_workshop_family(self) -> None:
        req = ort.prepare_launch_request_payload(_workshop_request(
            "ar_x", repair_findings="fix it", warm_resume=True, repair_strategy="reuse"))
        self.assertTrue(ort.render_launch_prompt_text(req).startswith(WORKSHOP_PROMPT_SENTINEL))

    def test_without_its_template_a_workshop_request_is_a_named_refusal(self) -> None:
        with mock.patch.object(ort, "_load_launch_prompt_templates", _REAL_TEMPLATE_LOADER), \
                mock.patch.object(ort, "_PROMPT_TEMPLATE_FILES", _REAL_TEMPLATE_FILES):
            with self.assertRaises(ValueError) as ctx:
                ort.render_launch_prompt_text(
                    ort.prepare_launch_request_payload(_workshop_request("ar_x")))
        self.assertIn("workshop generate.generate", str(ctx.exception))

    def test_the_marker_sets_of_the_two_modules_agree(self) -> None:
        import tools.validate_pipeline_semantics as vps
        prepared = ort.prepare_launch_request_payload(_workshop_request("ar_x"))
        self.assertEqual(set(ort._required_launch_prompt_markers(prepared)),
                         set(vps._required_launch_prompt_markers_for_role(
                             "substep", workshop=True)))


def _clear_active_child(repo: Path) -> None:
    path = ort._active_child_agent_run_id_path(repo, _ORCH)
    if path.exists():
        path.unlink()


class PreflightTests(unittest.TestCase):
    _HELP = ("Usage: claude\n  --mcp-config <configs...>  x\n"
             "  --permission-mode <mode>  x\n  --permission-prompts <target>  x\n"
             "  --allowedTools, --allowed-tools <tools...>  x\n")

    def _runner(self, help_text: str, rc: int = 0):
        import subprocess

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, rc, help_text, "")
        return run

    def test_the_required_options_are_the_workshop_argv_beyond_the_pure_one(self) -> None:
        """Derived from the two flag sets, and the derivation reads them: the set is exactly
        the four options today, and a flag added to the workshop argv joins it."""
        self.assertEqual(ort.CLAUDE_WORKSHOP_REQUIRED_OPTIONS,
                         ("--mcp-config", "--permission-mode", "--permission-prompts",
                          "--allowedTools"))
        original = pure_leaf.workshop_leaf_flags
        with mock.patch.object(pure_leaf, "workshop_leaf_flags",
                               lambda j: [*original(j), "--new-flag", "x"]):
            self.assertIn("--new-flag", ort._workshop_only_options())

    def test_the_check_requires_every_option_as_an_option_line(self) -> None:
        self.assertIs(ort._claude_workshop_flag_set_check("claude", self._runner(self._HELP))
                      ["pass"], True)
        for option in ort.CLAUDE_WORKSHOP_REQUIRED_OPTIONS:
            with self.subTest(dropped=option):
                text = "\n".join(line for line in self._HELP.splitlines()
                                 if not line.strip().startswith(option))
                # A mention inside another option's description does not answer for it.
                text += f"\n  --other  see {option}\n"
                check = ort._claude_workshop_flag_set_check("claude", self._runner(text))
                self.assertIs(check["pass"], False)
                self.assertIn(option, check["detail"])
        self.assertIs(ort._claude_workshop_flag_set_check(
            "claude", self._runner(self._HELP, rc=1))["pass"], False)

    def test_only_a_configuration_with_a_workshop_entry_takes_the_check(self) -> None:
        def probe(**kw):
            return {"probe_command": "claude", "checks": [], "status": "pass"}

        with tempfile.TemporaryDirectory() as tmp:
            for workshop in (False, True):
                body = "defaults: {provider: claude_cli}\n"
                if workshop:
                    body += ("phases: {generate: {substeps: {generate: {leaf: workshop, "
                             "max_wrapper_calls: 3, lineage_timeout_s: 60}}}}\n")
                path = Path(tmp) / f"llm_{workshop}.yaml"
                path.write_text(body, encoding="utf-8")
                with self.subTest(workshop=workshop), \
                        mock.patch.object(ort, "probe_execution_platform", probe), \
                        mock.patch.object(ort, "_preflight_allows_agent_launch",
                                          lambda p: True):
                    providers = ort.probe_all_providers(
                        config=lc.load_llm_config(path),
                        runner=self._runner("no options here"))
                    row = providers["claude"]
                    names = [c["name"] for c in row["checks"]]
                    self.assertEqual("claude_workshop_flag_set_available" in names, workshop)
                    self.assertEqual(row["launchable"], not workshop)
                    self.assertEqual(row["surfaces"][0].get("workshop"),
                                     True if workshop else None)
                    if workshop:
                        # The TTL re-probe reconstructs the requirement from the surface.
                        with mock.patch.object(ort, "_claude_workshop_flag_set_check",
                                               lambda cmd, runner: {
                                                   "name": "claude_workshop_flag_set_available",
                                                   "pass": False, "detail": ""}):
                            again = ort._reprobe_recorded_providers(providers, None)
                        self.assertIs(again["claude"]["launchable"], False)
                        self.assertTrue(again["claude"]["surfaces"][0]["workshop"])


class _WorkshopConfig:
    @staticmethod
    def config(tmp: Path) -> lc.LlmConfig:
        path = tmp / "llm_workshop.yaml"
        path.write_text(
            "defaults: {provider: claude_cli, model: opus}\n"
            "phases: {generate: {substeps: {generate: {provider: claude_cli, model: opus, "
            "effort: medium, leaf: workshop, max_wrapper_calls: 30, lineage_timeout_s: 2700}}}}\n",
            encoding="utf-8")
        return lc.load_llm_config(path)


class LeafCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.c = _PureFakeConductor(repo_root=self.repo, orchestration_id="o",
                                    orchestration_agent_run_id="orch", env={},
                                    llm_config=_WorkshopConfig.config(self.repo))
        self.entry = self.c.entry_for("generate", "generate")
        self.mcp = workshop_mcp_config_json("http://127.0.0.1:4242/mcp")

    def test_the_workshop_argv(self) -> None:
        argv = wc.Conductor.leaf_command(self.c, self.entry, session_id="sid",
                                         mcp_config_json=self.mcp)
        self.assertEqual(argv, ["claude", "--model", "opus", "--effort", "medium",
                                "--session-id", "sid",
                                *pure_leaf.workshop_leaf_flags(self.mcp), "-p"])
        self.assertNotIn("--safe-mode", argv)
        self.assertNotIn("", argv)

    def test_the_warm_prefix(self) -> None:
        argv = wc.Conductor.leaf_command(self.c, self.entry, session_id="new",
                                         resume_session_id="old", mcp_config_json=self.mcp)
        i = argv.index("--resume")
        self.assertEqual(argv[i:i + 3], ["--resume", "old", "--fork-session"])
        self.assertLess(i, argv.index("--restricted"))

    def test_the_entry_and_the_endpoint_must_agree(self) -> None:
        with self.assertRaises(ValueError):
            wc.Conductor.leaf_command(self.c, self.entry, session_id="s")
        pure = self.c.entry_for("generate", "verify")
        with self.assertRaises(ValueError):
            wc.Conductor.leaf_command(self.c, pure, session_id="s", mcp_config_json=self.mcp)

    def test_the_setting_surface_records_the_endpoint(self) -> None:
        surface = wc.Conductor._launch_setting_surface(
            self.c, "child", {"step": "generate", "substep": "generate"}, self.entry,
            mcp_config_json=self.mcp)
        self.assertEqual(surface["claude_mcp_config"],
                         [{"server": "wrapper", "url": "http://127.0.0.1:4242/mcp"}])
        self.assertEqual(surface["claude_tools"], ["Read,Write,Edit,Glob,Grep"])
        pure = wc.Conductor._launch_setting_surface(
            self.c, "child", {"step": "generate", "substep": "verify"},
            self.c.entry_for("generate", "verify"))
        self.assertNotIn("claude_mcp_config", pure)

    def test_a_command_prefix_carrying_its_own_mcp_config_is_recorded_as_before(self) -> None:
        """Round 1 (a regression against origin/main): a PURE entry whose `command:` carries
        `--mcp-config <file>` raised JSONDecodeError here, failing every launch at
        record-launch; origin/main records `claude_tools` alone."""
        cfg = self.repo / "llm_cmd.yaml"
        cfg.write_text("defaults: {provider: claude_cli, command: claude --mcp-config "
                       "/etc/x.json}\n", encoding="utf-8")
        c = _PureFakeConductor(repo_root=self.repo, orchestration_id="o",
                               orchestration_agent_run_id="orch", env={},
                               llm_config=lc.load_llm_config(cfg))
        entry = c.entry_for("generate", "verify")
        self.assertEqual(wc.Conductor._launch_setting_surface(
            c, "child", {"step": "generate", "substep": "verify"}, entry),
            {"claude_tools": [""]})

    def test_record_launch_passes_the_lineage_only_when_given(self) -> None:
        seen: list[list[str]] = []
        self.c.runtime = lambda argv, **kw: seen.append(list(argv)) or {}  # type: ignore
        wc.Conductor.record_launch(self.c, "child", {"node_key": "n", "step": "generate",
                                                     "substep": "generate"},
                                   self.entry, mcp_config_json=self.mcp)
        wc.Conductor.record_launch(self.c, "child2", {"node_key": "n", "step": "generate",
                                                      "substep": "generate"},
                                   self.entry, mcp_config_json=self.mcp,
                                   workshop_lineage_id="child")
        self.assertNotIn("--workshop-lineage-id", seen[0])
        self.assertEqual(seen[1][seen[1].index("--workshop-lineage-id") + 1], "child")
        response = json.loads(seen[0][seen[0].index("--response-json") + 1])
        self.assertEqual(response["claude_mcp_config"][0]["url"], "http://127.0.0.1:4242/mcp")

    def test_a_workshop_entry_fails_closed_until_its_loop_is_wired(self) -> None:
        from tools.tests.test_pure_leaf_producer import _write_node
        refs = _write_node(self.repo)
        outcome = self.c.run_substep(refs, "generate", "generate")
        self.assertEqual(outcome.status, "fail")
        self.assertEqual(outcome.infra_error[0], "workshop_leaf_not_wired")
        self.assertIn("leaf: workshop", outcome.infra_error[1])


class SpawnTimeoutTests(unittest.TestCase):
    """`spawn_leaf(timeout_seconds=, timeout_name=)` bounds THIS leaf and names the bound."""

    def test_the_marker_names_the_bound_that_killed_the_leaf(self) -> None:
        self.assertIn("leaf exceeded ATMOFAB_LEAF_TIMEOUT_SECONDS=5",
                      wc._leaf_timeout_marker(5, 6.0))
        self.assertIn("leaf exceeded lineage_timeout_s=7",
                      wc._leaf_timeout_marker(7, 8.0, "lineage_timeout_s"))

    def test_the_bound_reaches_the_kill(self) -> None:
        """A real child under the claude spawn path, with the environment cap far away and an
        explicit bound of one second: the kill fires at the explicit bound and the marker and
        the event name it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            events: list[tuple[str, dict]] = []
            c = _PureFakeConductor(repo_root=repo, orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=_WorkshopConfig.config(repo))
            c.emit = lambda name, **f: events.append((name, f))  # type: ignore[assignment]
            c._bwrap_enabled = lambda: False  # type: ignore[assignment]
            c.leaf_command = lambda *a, **k: ["sleep", "60"]  # type: ignore[assignment]
            entry = c.entry_for("generate", "verify")
            with mock.patch.dict(os.environ, {"ATMOFAB_LEAF_TIMEOUT_SECONDS": "3600"}):
                result = wc.Conductor.spawn_leaf(
                    c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, entry,
                    child_arid="child", timeout_seconds=1, timeout_name="lineage_timeout_s")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("leaf exceeded lineage_timeout_s=1", result.stderr)
            fired = [f for name, f in events if name == "leaf_timeout"]
            self.assertEqual(fired[0]["timeout_bound"], "lineage_timeout_s")
            self.assertEqual(fired[0]["timeout_seconds"], 1)

    def _claude_conductor(self, tmp: Path, argv: list[str]):
        c = _PureFakeConductor(repo_root=tmp, orchestration_id="o",
                               orchestration_agent_run_id="orch", env={},
                               llm_config=_WorkshopConfig.config(tmp))
        self.events: list[tuple[str, dict]] = []
        c.emit = lambda name, **f: self.events.append((name, f))  # type: ignore[assignment]
        c._bwrap_enabled = lambda: False  # type: ignore[assignment]
        c.leaf_command = lambda *a, **k: list(argv)  # type: ignore[assignment]
        return c

    def test_a_cancel_event_ends_the_leaf_and_says_so(self) -> None:
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            c = self._claude_conductor(Path(tmp), ["sleep", "60"])
            cancel = threading.Event()
            threading.Timer(0.5, cancel.set).start()
            t0 = time.monotonic()
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "verify"), child_arid="child", cancel_event=cancel)
            self.assertLess(time.monotonic() - t0, 30)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.endswith(wc.LEAF_CANCELLED_MARKER), result.stderr)
            self.assertNotIn("leaf_timeout", [name for name, _ in self.events])

    def test_an_unset_cancel_event_changes_nothing(self) -> None:
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            c = self._claude_conductor(Path(tmp), ["sh", "-c", "echo out; exit 3"])
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "verify"), child_arid="child",
                cancel_event=threading.Event())
            self.assertEqual((result.returncode, result.stdout.strip()), (3, "out"))
            self.assertNotIn(wc.LEAF_CANCELLED_MARKER, result.stderr)

    def test_the_wrapper_ending_the_lineage_ends_the_leaf(self) -> None:
        """How PR-3 composes the two halves: the endpoint's `on_end` sets the spawn's cancel
        event. The leaf here is a real process that calls the endpoint once and then hangs; the
        call is answered `ended: true` (the cap is spent), and the leaf is killed."""
        import sys
        import threading
        import time
        from types import SimpleNamespace

        from tools import workshop_mcp, workshop_tree
        from tools.workshop_tree import LineagePaths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = LineagePaths(homes_root=root / "h", orchestration_id="o", lineage_id="l",
                                 orchestration_dir=root / "orch")
            paths.home.mkdir(parents=True)
            paths.make_work_dirs()
            workshop_tree.write_lineage_record(paths, node_key="n",
                                               started_at_utc="2026-10-23T00:00:00Z")
            workshop_tree.end_lineage(paths, "max_wrapper_calls")
            cancel = threading.Event()
            server = workshop_mcp.WrapperMcpServer(
                SimpleNamespace(paths=paths, agent_run_id="child", max_wrapper_calls=1,
                                lineage_deadline=time.time() + 600),
                on_end=lambda _why: cancel.set()).start()
            self.addCleanup(server.stop)
            leaf = ("import http.client, json, time, urllib.parse\n"
                    f"u = urllib.parse.urlsplit({server.url!r})\n"
                    "c = http.client.HTTPConnection(u.hostname, u.port)\n"
                    "c.request('POST', u.path, json.dumps({'jsonrpc': '2.0', 'id': 1, "
                    "'method': 'tools/call', 'params': {'name': 'derive', 'arguments': {}}}), "
                    "{'Content-Type': 'application/json'})\n"
                    "print(c.getresponse().read().decode(), flush=True)\n"
                    "time.sleep(60)\n")
            c = self._claude_conductor(root, [sys.executable, "-c", leaf])
            t0 = time.monotonic()
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "verify"), child_arid="child", cancel_event=cancel)
            self.assertLess(time.monotonic() - t0, 30)
            self.assertIn('\\"ended\\": true', result.stdout)
            self.assertTrue(result.stderr.endswith(wc.LEAF_CANCELLED_MARKER), result.stderr)

    def test_a_cancelled_leaf_that_exits_zero_is_still_a_failed_launch(self) -> None:
        """A leaf that answers the termination with a clean exit and a whole envelope must not
        read as a finished turn: the pure loops accept on `returncode == 0`."""
        import threading
        script = "trap 'echo ANSWERED; exit 0' TERM; sleep 60 & wait"
        with tempfile.TemporaryDirectory() as tmp:
            c = self._claude_conductor(Path(tmp), ["sh", "-c", script])
            cancel = threading.Event()
            threading.Timer(0.5, cancel.set).start()
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "verify"), child_arid="child", cancel_event=cancel)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.strip(), "ANSWERED")  # the trap ran and exited 0
            self.assertTrue(result.stderr.endswith(wc.LEAF_CANCELLED_MARKER))

    def test_the_bound_still_fires_while_a_cancel_event_is_held(self) -> None:
        """PR-3 passes both on every workshop spawn: an event never set must not switch the
        deadline off."""
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            c = self._claude_conductor(Path(tmp), ["sleep", "60"])
            result = wc.Conductor.spawn_leaf(
                c, "", {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                c.entry_for("generate", "verify"), child_arid="child",
                timeout_seconds=1, timeout_name="lineage_timeout_s",
                cancel_event=threading.Event())
            self.assertIn("leaf exceeded lineage_timeout_s=1", result.stderr)
            self.assertNotIn(wc.LEAF_CANCELLED_MARKER, result.stderr)

    def test_a_cancel_event_is_a_claude_leafs(self) -> None:
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "llm.yaml"
            cfg.write_text("defaults: {provider: codex_cli, model: gpt-x}\n", encoding="utf-8")
            c = _PureFakeConductor(repo_root=Path(tmp), orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=lc.load_llm_config(cfg))
            with self.assertRaises(ValueError):
                wc.Conductor.spawn_leaf(c, "", {}, c.entry_for(None, None), child_arid="c",
                                        cancel_event=threading.Event())

    def test_an_explicit_bound_is_clamped_to_the_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = self._claude_conductor(Path(tmp), ["true"])
            seen: list[int] = []
            original = wc._wait_for_leaf_exit

            def spy(process, deadline, cancel_event):
                import time
                seen.append(int(deadline - time.monotonic()) if deadline else -1)
                return original(process, deadline, cancel_event)

            with mock.patch.object(wc, "_wait_for_leaf_exit", spy):
                wc.Conductor.spawn_leaf(c, "", {"PATH": os.environ.get("PATH", "")},
                                        c.entry_for("generate", "verify"), child_arid="child",
                                        timeout_seconds=10 ** 12)
            self.assertLessEqual(seen[0], wc.LEAF_TIMEOUT_MAX_SECONDS)

    def test_an_explicit_bound_is_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = _PureFakeConductor(repo_root=Path(tmp), orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=_WorkshopConfig.config(Path(tmp)))
            for bad in (0, -1, 1.5, True, "3"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    wc.Conductor.spawn_leaf(c, "", {}, c.entry_for("generate", "verify"),
                                            child_arid="c", timeout_seconds=bad)



class WorkshopRecordLaunchCliTests(WorkshopRecordLaunchTests):
    """The `record-launch` CLI carries `--workshop-lineage-id` through to `record_launch` and
    prints the warm sentinel whole."""

    def test_the_cli_flag_reaches_the_warm_check(self) -> None:
        import contextlib
        import io
        req = _workshop_request("ar_cli", "ar_gone")
        prepared = ort.prepare_launch_request_payload(dict(req))
        req["launch_prompt_full"] = ort.render_launch_prompt_text(prepared)
        req_file = self.repo / "req.json"
        req_file.write_text(json.dumps(req), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ort.main(["record-launch", "--repo-root", str(self.repo),
                           "--orchestration-id", _ORCH, "--parent-agent-run-id", "orch_run_001",
                           "--child-agent-run-id", "ar_cli", "--request-json-file",
                           str(req_file), "--response-json",
                           json.dumps({**_spawn_response("ar_cli"), "agent_run_id": "ar_cli",
                                       "started_at": "2026-10-23T00:00:00Z",
                                       "backend": "claude"}),
                           "--workshop-lineage-id", "ar_gone"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"workshop_lineage_home_missing": True,
                          "workshop_lineage_id": "ar_gone"})


class WorkshopLaunchRecordTests(unittest.TestCase):
    """The smaller record surfaces a workshop launch reaches."""

    def test_the_session_index_keeps_the_lineage_across_updates(self) -> None:
        doc: dict = {}
        common = dict(agent_session_id="s", context_id=None, agent_role="substep")
        ort._upsert_session_run_index_entry(doc, agent_run_id="a", status="running",
                                            workshop_lineage_id="lin", **common)
        self.assertEqual(doc["entries"][0]["workshop_lineage_id"], "lin")
        ort._upsert_session_run_index_entry(doc, agent_run_id="a", status="pass", **common)
        self.assertEqual(doc["entries"][0]["workshop_lineage_id"], "lin")
        ort._upsert_session_run_index_entry(doc, agent_run_id="a", status="pass",
                                            workshop_lineage_id="lin2", **common)
        self.assertEqual(doc["entries"][0]["workshop_lineage_id"], "lin2")
        ort._upsert_session_run_index_entry(doc, agent_run_id="b", status="running", **common)
        self.assertNotIn("workshop_lineage_id", doc["entries"][1])

    def test_a_workshop_request_reads_no_skill(self) -> None:
        _with_test_templates(self)
        prepared = ort.prepare_launch_request_payload(
            _workshop_request("ar_x", skill_name="x", skill_ref="skills/x/SKILL.md"))
        self.assertEqual((prepared["skill_name"], prepared["skill_ref"]), ("", ""))

    def test_a_deterministic_request_with_a_workshop_prompt_is_refused(self) -> None:
        """Only the workshop arm refuses this: the deterministic markers are all present and
        neither pure arm fires on a prompt that does not open with the pure sentinel."""
        req = ort.prepare_launch_request_payload({
            "node_key": "component/x/y@0.1.0", "step": "build", "deterministic": True,
            "orchestration_id": _ORCH, "agent_run_id": "ar_d", "parent_agent_run_id": "o",
            "ir_ref": "workspace/ir/x", "pipeline_ref": "workspace/pipelines/x",
            "agent_role": "step"})
        prompt = ort.render_launch_prompt_text(req)
        ort._validate_launch_prompt_text(req, prompt)
        with self.assertRaises(ValueError) as ctx:
            ort._validate_launch_prompt_text(req, WORKSHOP_PROMPT_SENTINEL + "\n" + prompt)
        self.assertIn("workshop", str(ctx.exception))

    def test_a_workshop_pass_row_declares_no_output_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            launches = repo / "workspace" / "orchestrations" / "o" / "launches"
            launches.mkdir(parents=True)
            (launches / "ar_w.request.json").write_text(json.dumps(
                {"leaf_mode": "workshop", "step": "generate", "substep": "generate"}),
                encoding="utf-8")
            row = {"agent_role": "substep", "status": "pass", "agent_run_id": "ar_w"}
            ort._validate_terminal_run_payload(repo, "o", {**row, "output_refs": []})
            for bad in ({"output_refs": ["x"]}, {}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    ort._validate_terminal_run_payload(repo, "o", {**row, **bad})

    def test_the_audit_attributes_a_workshop_launch(self) -> None:
        from tools import audit_orchestration as ao
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            launches = repo / "workspace" / "orchestrations" / "o" / "launches"
            launches.mkdir(parents=True)
            for arid, mode, substep in (("a", "workshop", "generate"), ("b", "pure", "verify"),
                                        ("c", "agentic", "judge")):
                (launches / f"{arid}.request.json").write_text(json.dumps(
                    {"leaf_mode": mode, "step": "validate" if arid == "c" else "generate",
                     "substep": substep}), encoding="utf-8")
            self.assertEqual(ao._pure_leaf_keys_that_ran(repo, "o"),
                             frozenset({"generate.generate", "generate.verify"}))

    def test_the_slim_repair_classifier_excludes_a_workshop_repair(self) -> None:
        import tools.validate_pipeline_semantics as vps
        shape = {"warm_resume": True, "repair_strategy": "reuse", "repair_findings": "x"}
        self.assertTrue(vps._launch_request_is_slim_repair(dict(shape)))
        self.assertFalse(vps._launch_request_is_slim_repair({**shape, "leaf_mode": "workshop"}))


class WorkshopSpawnArgvTests(unittest.TestCase):
    """`spawn_leaf` launches the workshop argv: the endpoint it is handed reaches `leaf_command`."""

    def test_the_endpoint_reaches_the_launched_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            c = _PureFakeConductor(repo_root=repo, orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=_WorkshopConfig.config(repo))
            c._bwrap_enabled = lambda: False  # type: ignore[assignment]
            seen: list[list[str]] = []

            class _Stop(Exception):
                pass

            def popen(argv, **kw):
                seen.append(list(argv))
                raise _Stop()

            mcp = workshop_mcp_config_json("http://127.0.0.1:7/mcp")
            with mock.patch.object(wc.subprocess, "Popen", popen), self.assertRaises(_Stop):
                wc.Conductor.spawn_leaf(c, "P", {}, c.entry_for("generate", "generate"),
                                        child_arid="child", session_id="sid",
                                        mcp_config_json=mcp)
            self.assertEqual(seen[0][seen[0].index("--mcp-config") + 1], mcp)

    def test_spawn_leaf_hands_the_codex_spawn_its_bound_and_its_name(self) -> None:
        """Through `spawn_leaf`, for both an explicit bound and none: the codex spawn gets the
        seconds and a NAME — the environment cap's when the caller names none."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = repo / "llm.yaml"
            cfg.write_text("defaults: {provider: codex_cli, model: gpt-x}\n", encoding="utf-8")
            c = _PureFakeConductor(repo_root=repo, orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=lc.load_llm_config(cfg))
            c._bwrap_enabled = lambda: False  # type: ignore[assignment]
            c.leaf_command = lambda *a, **k: ["sleep", "60"]  # type: ignore[assignment]
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            for kwargs, expected in (
                    ({"timeout_seconds": 1, "timeout_name": "lineage_timeout_s"},
                     "leaf exceeded lineage_timeout_s=1"),
                    ({}, "leaf exceeded ATMOFAB_LEAF_TIMEOUT_SECONDS=1")):
                with self.subTest(kwargs=kwargs), mock.patch.dict(
                        os.environ, {"ATMOFAB_LEAF_TIMEOUT_SECONDS":
                                     "1" if not kwargs else "3600"}):
                    result = wc.Conductor.spawn_leaf(c, "", env, c.entry_for(None, None),
                                                     child_arid="child", **kwargs)
                    self.assertIn(expected, result.stderr)

    def test_the_codex_spawn_takes_the_explicit_bound_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            cfg = repo / "llm.yaml"
            cfg.write_text("defaults: {provider: codex_cli, model: gpt-x}\n", encoding="utf-8")
            c = _PureFakeConductor(repo_root=repo, orchestration_id="o",
                                   orchestration_agent_run_id="orch", env={},
                                   llm_config=lc.load_llm_config(cfg))
            events: list[tuple[str, dict]] = []
            c.emit = lambda name, **f: events.append((name, f))  # type: ignore[assignment]
            with mock.patch.dict(os.environ, {"ATMOFAB_LEAF_TIMEOUT_SECONDS": "3600"}):
                result = wc.Conductor._spawn_codex_json_leaf(
                    c, ["sleep", "60"], {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    "child", prompt_text="", entry=c.entry_for(None, None),
                    timeout_seconds=1, timeout_name="lineage_timeout_s")
            self.assertIn("leaf exceeded lineage_timeout_s=1", result.stderr)
            fired = [f for name, f in events if name == "leaf_timeout"]
            self.assertEqual(fired[0]["timeout_bound"], "lineage_timeout_s")


if __name__ == "__main__":
    unittest.main()
