#!/usr/bin/env python3
"""Tests for `tools/execution_sites.py` — the operator's `sites.yaml` (issue #293, PR-1)."""

from __future__ import annotations

import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import execution_sites as es
from tools import host_execution
from tools.backends import registry

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "docs" / "examples" / "sites.example.yaml"

#: A remote site every case below starts from; each case changes one thing.
_BASE = """\
sites_version: 1
sites:
  box:
    host: box
    workdir: /scratch/jobs
    executes: [cpu]
    scheduler: none
"""


class _Repo:
    """A scratch repo root carrying `spec/targets/<id>.yaml` stubs (only the stems are read)."""

    def __init__(self, tmp: str, targets: tuple[str, ...] = ("t_cpu", "t_gpu")) -> None:
        self.root = Path(tmp)
        (self.root / "spec" / "targets").mkdir(parents=True)
        for target_id in targets:
            (self.root / "spec" / "targets" / f"{target_id}.yaml").write_text("{}\n")

    def load(self, text: str) -> es.SitesConfig:
        (self.root / es.DEFAULT_SITES_PATH).write_text(textwrap.dedent(text), encoding="utf-8")
        return es.load_sites(self.root)


def _profile(target_id: str, hardware_class: str) -> SimpleNamespace:
    return SimpleNamespace(target_id=target_id, hardware_class=hardware_class)


#: A scheduler record standing in for a batch scheduler, which PR-1 registers none of.
_BATCH = registry.Backend("scheduler", "zz_batch", None, core_provides=frozenset({"job_submit"}))


def _with_batch_scheduler():
    return mock.patch.dict(registry._BACKENDS, {("scheduler", "zz_batch"): _BATCH})


class LoadTests(unittest.TestCase):

    def test_no_file_is_the_local_site_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = es.load_sites(Path(tmp))
        self.assertIsNone(cfg.path)
        self.assertIsNone(cfg.sha256)
        self.assertEqual(set(cfg.sites), {"local"})
        site = cfg.site_for("anything")
        self.assertTrue(site.is_local)
        self.assertEqual(site.executes, es.LOCAL_DEFAULT_EXECUTES)
        self.assertIsNone(site.host)
        self.assertEqual(site.scheduler, es.DIRECT_SCHEDULER)

    def test_the_local_site_id_has_one_owner(self) -> None:
        self.assertEqual(es.LOCAL_SITE, host_execution.LOCAL_SITE)
        self.assertIs(es.LOCAL_SITE, host_execution.LOCAL_SITE)

    def test_the_example_loads(self) -> None:
        """`docs/examples/sites.example.yaml` is what an operator copies; it must load against
        this checkout's own targets and registry."""
        cfg = es.load_sites(REPO_ROOT, path=EXAMPLE)
        self.assertEqual(cfg.path, EXAMPLE)
        self.assertRegex(cfg.sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertIn("local", cfg.sites)
        remote = [s for s in cfg.sites.values() if not s.is_local]
        self.assertTrue(remote)
        for site in remote:
            self.assertTrue(site.host)
            self.assertTrue(site.workdir.startswith("/"))
        # Every hardware class the registry declares is executed by some site in the example.
        executed = {c for s in cfg.sites.values() for c in s.executes}
        self.assertEqual(executed, set(registry.backend_ids("hardware")))
        self.assertTrue(cfg.targets)
        for target_id, site_id in cfg.targets.items():
            self.assertEqual(cfg.site_for(target_id).site_id, site_id)

    def test_a_remote_site_and_a_target_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Repo(tmp).load(_BASE + "targets:\n  t_cpu: box\n")
        site = cfg.site_for("t_cpu")
        self.assertEqual(site, es.Site(site_id="box", executes=("cpu",), host="box",
                                       workdir="/scratch/jobs", scheduler="none"))
        self.assertFalse(site.is_local)
        self.assertTrue(cfg.site_for("t_gpu").is_local)
        self.assertEqual(cfg.targets, {"t_cpu": "box"})

    def test_the_local_site_takes_executes_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            cfg = repo.load("sites_version: 1\nsites:\n  local:\n    executes: [cpu, gpu]\n")
            self.assertEqual(cfg.site_for("t_gpu").executes, ("cpu", "gpu"))
            with self.assertRaises(es.SitesConfigError) as ctx:
                repo.load("sites_version: 1\nsites:\n  local:\n    host: h\n")
            self.assertEqual(ctx.exception.rule, "sites_config_unknown_key")
            self.assertEqual(ctx.exception.where, "sites.local.host")

    def test_empty_sections_and_an_empty_local_say_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            for text in ("sites_version: 1\nsites:\ntargets:\n",
                         "sites_version: 1\nsites:\n  local:\n"):
                with self.subTest(text=text):
                    cfg = repo.load(text)
                    self.assertEqual(set(cfg.sites), {"local"})
                    self.assertEqual(cfg.targets, {})
                    self.assertEqual(cfg.sites["local"].executes, es.LOCAL_DEFAULT_EXECUTES)
            # A null body is admissible for `local` only.
            with self.assertRaises(es.SitesConfigError) as ctx:
                repo.load("sites_version: 1\nsites:\n  box:\n")
            self.assertEqual(ctx.exception.where, "sites.box")

    def test_a_relative_path_is_resolved_against_the_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            (repo.root / "alt.yaml").write_text(_BASE, encoding="utf-8")
            cfg = es.load_sites(repo.root, path="alt.yaml")
        self.assertEqual(cfg.path, repo.root / "alt.yaml")
        self.assertIn("box", cfg.sites)

    def test_a_relative_repo_root_finds_the_default_file(self) -> None:
        """Codex, round 2: the default path was `repo_root / "sites.yaml"` and then, being
        relative, joined to `repo_root` again — so a relative root read `root/root/sites.yaml`
        and silently returned the local-only configuration."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            (repo.root / es.DEFAULT_SITES_PATH).write_text(_BASE, encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(repo.root.parent)
            try:
                cfg = es.load_sites(Path(repo.root.name))
            finally:
                os.chdir(cwd)
        self.assertIn("box", cfg.sites)
        self.assertEqual(cfg.path, Path(repo.root.name) / es.DEFAULT_SITES_PATH)

    def test_a_named_path_that_does_not_exist_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(es.SitesConfigError) as ctx:
            es.load_sites(Path(tmp), path="no_such.yaml")
        self.assertEqual(ctx.exception.rule, "sites_config_unreadable")

    def test_the_sha_follows_the_target_mapping(self) -> None:
        """The sha is the provenance of where each target ran: a changed `targets:` with the
        same sites must change it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            a = repo.load(_BASE + "targets:\n  t_cpu: box\n").sha256
            b = repo.load(_BASE + "targets:\n  t_cpu: local\n").sha256
            c = repo.load(_BASE).sha256
        self.assertEqual(len({a, b, c}), 3)

    def test_a_file_without_a_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(es.SitesConfigError) as ctx:
            _Repo(tmp).load(_BASE.replace("sites_version: 1\n", ""))
        self.assertEqual(ctx.exception.rule, "sites_config_missing_field")

    def test_a_file_that_is_not_utf8_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            (repo.root / es.DEFAULT_SITES_PATH).write_bytes(b"sites_version: 1\n# \xff\xfe\n")
            with self.assertRaises(es.SitesConfigError) as ctx:
                es.load_sites(repo.root)
        self.assertEqual(ctx.exception.rule, "sites_config_unreadable")

    def test_the_sha_ignores_comments_and_key_order_and_follows_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            a = repo.load(_BASE).sha256
            reordered = ("# note\nsites:\n  box:\n    scheduler: none\n    executes: [cpu]\n"
                         "    workdir: /scratch/jobs\n    host: box\nsites_version: 1\n")
            self.assertEqual(repo.load(reordered).sha256, a)
            self.assertNotEqual(repo.load(_BASE.replace("/scratch/jobs", "/scratch/j2")).sha256,
                                a)

    def test_a_batch_scheduler_site_carries_its_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _with_batch_scheduler():
            cfg = _Repo(tmp).load(_BASE.replace("scheduler: none", "scheduler: zz_batch") +
                                  "    scheduler_directives: ['--partition=a b', '--time=1']\n"
                                  "    queue_timeout_sec: 60\n")
        site = cfg.sites["box"]
        self.assertEqual(site.scheduler, "zz_batch")
        # A space is admissible inside a directive, and nowhere else.
        self.assertEqual(site.scheduler_directives, ("--partition=a b", "--time=1"))
        self.assertEqual(site.queue_timeout_sec, 60)


#: One document per rule: `rule -> (document text, expected `where`)`. The set of keys is
#: compared with `SITES_CONFIG_RULES`, so a rule the loader declares and no case reaches fails.
_REFUSALS: dict[str, tuple[str, str]] = {
    "sites_config_unreadable": ("sites_version: [1\n", ""),
    "sites_config_not_a_mapping": ("- a\n- b\n", ""),
    "sites_config_duplicate_key": (_BASE + "  box:\n    host: other\n", "box"),
    "sites_config_version": (_BASE.replace("sites_version: 1", "sites_version: 2"),
                             "sites_version"),
    "sites_config_unknown_key": (_BASE + "    port: 22\n", "sites.box.port"),
    "sites_config_missing_field": (_BASE.replace("    workdir: /scratch/jobs\n", ""),
                                   "sites.box"),
    "sites_config_invalid_field": (_BASE.replace("executes: [cpu]", "executes: []"),
                                   "sites.box.executes"),
    "sites_config_shell_active_value": (_BASE.replace("host: box", "host: 'box;x'"),
                                        "sites.box.host"),
    "sites_config_scheduler_unknown": (_BASE.replace("scheduler: none", "scheduler: nosuch"),
                                       "sites.box.scheduler"),
    "sites_config_scheduler_field_misplaced": (_BASE + "    queue_timeout_sec: 60\n",
                                               "sites.box.queue_timeout_sec"),
    "sites_config_executes_unknown_class": (_BASE.replace("[cpu]", "[cpu, tpu]"),
                                            "sites.box.executes[1]"),
    "sites_config_unknown_site": (_BASE + "targets:\n  t_cpu: nowhere\n", "targets.t_cpu"),
    "sites_config_unknown_target": (_BASE + "targets:\n  t_none: box\n", "targets.t_none"),
}


class RefusalTests(unittest.TestCase):

    def _refuse(self, text: str, **repo_kwargs) -> es.SitesConfigError:
        with tempfile.TemporaryDirectory() as tmp, \
                self.assertRaises(es.SitesConfigError) as ctx:
            _Repo(tmp, **repo_kwargs).load(text)
        return ctx.exception

    def test_every_declared_rule_is_reached_and_named(self) -> None:
        self.assertEqual(set(_REFUSALS), es.SITES_CONFIG_RULES)
        for rule, (text, where) in _REFUSALS.items():
            with self.subTest(rule=rule):
                exc = self._refuse(text)
                self.assertEqual(exc.rule, rule, str(exc))
                self.assertEqual(exc.where, where, str(exc))
                self.assertTrue(str(exc).startswith(f"{rule}: "), str(exc))

    def test_an_unknown_rule_name_cannot_be_raised(self) -> None:
        with self.assertRaises(AssertionError):
            es.SitesConfigError("sites_config_no_such_rule", "x")

    def test_every_shell_active_character_is_refused_in_host_and_workdir(self) -> None:
        for ch in sorted(es._shell_active_chars()) + [" "]:
            for field, value in (("host", f"b{ch}x"), ("workdir", f"/s/j{ch}x")):
                with self.subTest(field=field, ch=ch):
                    body = _BASE.replace("host: box", f"host: {value!r}" if field == "host"
                                         else "host: box")
                    if field == "workdir":
                        body = body.replace("workdir: /scratch/jobs", f"workdir: {value!r}")
                    exc = self._refuse(body)
                    self.assertEqual(exc.rule, "sites_config_shell_active_value", str(exc))
                    self.assertEqual(exc.where, f"sites.box.{field}")

    def test_a_directive_is_refused_on_a_shell_active_character_but_not_a_space(self) -> None:
        with _with_batch_scheduler():
            base = _BASE.replace("scheduler: none", "scheduler: zz_batch")
            for ch in sorted(es._shell_active_chars()):
                with self.subTest(ch=ch):
                    exc = self._refuse(base + f"    scheduler_directives: [{('--a' + ch)!r}]\n")
                    self.assertEqual(exc.rule, "sites_config_shell_active_value")
                    self.assertEqual(exc.where, "sites.box.scheduler_directives[0]")

    def test_the_shell_active_set_is_the_servers_own(self) -> None:
        """Read from the server, not copied: a character the server starts refusing is refused
        here too."""
        import build_runtime_server  # on sys.path once `_shell_active_chars` has run

        self.assertEqual(es._shell_active_chars(), frozenset(build_runtime_server._SHELL_ACTIVE_CHARS))
        with mock.patch.object(build_runtime_server, "_SHELL_ACTIVE_CHARS",
                               set(build_runtime_server._SHELL_ACTIVE_CHARS) | {"%"}):
            exc = self._refuse(_BASE.replace("host: box", "host: 'b%x'"))
        self.assertEqual(exc.rule, "sites_config_shell_active_value")

    def test_a_host_ssh_and_scp_would_read_differently_is_refused(self) -> None:
        """`:` and `/` move scp's idea of the host (`scp f 'a/b:/x'` is a local copy), and a
        leading `-` is an ssh option; each is refused, and a plain destination is not."""
        # `-v` and `-Jx` carry no character outside the word class, so they fail on the
        # leading `-` alone (`-oX=y` would also fail on its `=`).
        for host in ("-v", "-Jx", "u@-v", "-u@h", "-oX=y", "a:b", "2001:db8::1", "foo/bar",
                     "ssh://u@h:2222", "@h", "u@", "x@.", ".h", "a@b@c", "a%b"):
            with self.subTest(host=host):
                exc = self._refuse(_BASE.replace("host: box", f"host: {host!r}"))
                self.assertEqual(exc.rule, "sites_config_invalid_field", str(exc))
                self.assertEqual(exc.where, "sites.box.host")
        for host in ("user@host", "login.example.org", "user.name@node-1", "h_1", "gpu1",
                     "9host", "U@Host.Example.COM", "10.0.0.1"):
            with self.subTest(accepted=host), tempfile.TemporaryDirectory() as tmp:
                cfg = _Repo(tmp).load(_BASE.replace("host: box", f"host: {host!r}"))
                self.assertEqual(cfg.sites["box"].host, host)

    def test_a_character_that_is_not_printable_ascii_is_refused_in_every_remote_value(
            self) -> None:
        import yaml

        for escaped in ("\\0", "\\v", "\\x7f", "\\xa0", "\\u00e9"):
            # The probe must reach the loader as ONE decoded character, not as a backslash —
            # a backslash is shell-active on its own and would be refused for that instead.
            decoded = yaml.safe_load(f'"{escaped}"')
            self.assertEqual(len(decoded), 1, escaped)
            self.assertNotEqual(decoded, "\\")
            with _with_batch_scheduler():
                cases = {
                    "sites.box.host": _BASE.replace("host: box", f'host: "b{escaped}x"'),
                    "sites.box.workdir": _BASE.replace("workdir: /scratch/jobs",
                                                       f'workdir: "/s{escaped}x"'),
                    "sites.box.scheduler_directives[0]": (
                        _BASE.replace("scheduler: none", "scheduler: zz_batch")
                        + f'    scheduler_directives: ["--a{escaped}"]\n'),
                }
                for where, text in cases.items():
                    with self.subTest(char=escaped, where=where):
                        exc = self._refuse(text)
                        self.assertEqual(exc.rule, "sites_config_shell_active_value", str(exc))
                        self.assertEqual(exc.where, where)

    def test_a_workdir_must_be_absolute_below_root_without_dotdot(self) -> None:
        for workdir in ("scratch/jobs", "/", "//", "/./", "/scratch/../jobs", "/scratch/..",
                        "/scratch/a:b"):
            with self.subTest(workdir=workdir):
                exc = self._refuse(_BASE.replace("/scratch/jobs", workdir))
                self.assertEqual(exc.rule, "sites_config_invalid_field", str(exc))
                self.assertEqual(exc.where, "sites.box.workdir")

    def test_a_none_site_carries_no_scheduler_only_field(self) -> None:
        for key, value in (("scheduler_directives", "['--a']"), ("queue_timeout_sec", "60")):
            with self.subTest(key=key):
                exc = self._refuse(_BASE + f"    {key}: {value}\n")
                self.assertEqual(exc.rule, "sites_config_scheduler_field_misplaced")
                self.assertEqual(exc.where, f"sites.box.{key}")

    def test_field_types(self) -> None:
        with _with_batch_scheduler():
            batch = _BASE.replace("scheduler: none", "scheduler: zz_batch")
            cases = {
                "sites.box.host": _BASE.replace("host: box", "host: ''"),
                "sites.box.executes[0]": _BASE.replace("[cpu]", "[CPU]"),
                "sites.box.executes": _BASE.replace("[cpu]", "[cpu, cpu]"),
                "sites.box.scheduler": _BASE.replace("scheduler: none", "scheduler: 3"),
                "sites.box.queue_timeout_sec": batch + "    queue_timeout_sec: true\n",
                "sites.box.scheduler_directives": batch + "    scheduler_directives: '--a'\n",
                "sites.box.scheduler_directives[0]": batch + "    scheduler_directives: ['']\n",
                "sites.Box": _BASE.replace("  box:", "  Box:"),
                "sites": "sites_version: 1\nsites: [a]\n",
                "targets": _BASE + "targets: [a]\n",
            }
            for where, text in cases.items():
                with self.subTest(where=where):
                    exc = self._refuse(text)
                    self.assertEqual(exc.rule, "sites_config_invalid_field", str(exc))
                    self.assertEqual(exc.where, where)
            exc = self._refuse(batch + "    queue_timeout_sec: 0\n")
            self.assertEqual(exc.where, "sites.box.queue_timeout_sec")

    def test_a_scheduler_the_registry_declares_but_nothing_implements_is_refused(self) -> None:
        named_only = registry.Backend("scheduler", "zz_named", None)
        with mock.patch.dict(registry._BACKENDS, {("scheduler", "zz_named"): named_only}):
            self.assertIsNone(registry.unsupported_reason("scheduler", "zz_named"))
            exc = self._refuse(_BASE.replace("scheduler: none", "scheduler: zz_named"))
        self.assertEqual(exc.rule, "sites_config_scheduler_unknown")

    def test_an_unhashable_key_is_refused_by_name_with_its_line(self) -> None:
        exc = self._refuse(_BASE + "? [a, b]\n: 1\n")
        self.assertEqual(exc.rule, "sites_config_unknown_key", str(exc))
        self.assertEqual(exc.where, "")
        self.assertIn("at line 8 is a list", str(exc))

    def test_a_remote_site_body_that_is_not_a_mapping_is_refused(self) -> None:
        for body in ("[x]", "box", "3", "null"):
            with self.subTest(body=body):
                exc = self._refuse(f"sites_version: 1\nsites:\n  box: {body}\n")
                self.assertEqual(exc.rule, "sites_config_invalid_field", str(exc))
                self.assertEqual(exc.where, "sites.box")

    def test_an_unknown_top_level_key_is_named_at_its_own_path(self) -> None:
        exc = self._refuse(_BASE + "extra: 1\n")
        self.assertEqual(exc.rule, "sites_config_unknown_key")
        self.assertEqual(exc.where, "extra")

    def test_a_nested_duplicate_key_is_refused(self) -> None:
        exc = self._refuse(_BASE + "    host: other\n")
        self.assertEqual(exc.rule, "sites_config_duplicate_key")
        self.assertEqual(exc.where, "host")

    def test_the_version_is_the_integer_one(self) -> None:
        for version in ("true", "1.0", "'1'", "2", "0"):
            with self.subTest(version=version):
                exc = self._refuse(f"sites_version: {version}\n")
                self.assertEqual(exc.rule, "sites_config_version", str(exc))

    def test_a_target_mapped_to_a_value_that_is_not_a_site_id_is_named(self) -> None:
        for value in ("[box]", "{a: b}", "3", "null"):
            with self.subTest(value=value):
                exc = self._refuse(_BASE + f"targets:\n  t_cpu: {value}\n")
                self.assertEqual(exc.rule, "sites_config_unknown_site", str(exc))
                self.assertEqual(exc.where, "targets.t_cpu")

    def test_a_hardware_class_the_registry_declares_but_nothing_implements_is_refused(
            self) -> None:
        """The `hardware` twin of the scheduler row below: `executes` asks whether the class is
        implemented, not merely declared."""
        named_only = registry.Backend("hardware", "zz_named", None)
        with mock.patch.dict(registry._BACKENDS, {("hardware", "zz_named"): named_only}):
            self.assertIsNone(registry.unsupported_reason("hardware", "zz_named"))
            exc = self._refuse(_BASE.replace("[cpu]", "[cpu, zz_named]"))
        self.assertEqual(exc.rule, "sites_config_executes_unknown_class")
        self.assertEqual(exc.where, "sites.box.executes[1]")

    def test_a_blank_directive_is_refused(self) -> None:
        with _with_batch_scheduler():
            for blank in ("''", "'   '"):
                with self.subTest(directive=blank):
                    exc = self._refuse(_BASE.replace("scheduler: none", "scheduler: zz_batch")
                                       + f"    scheduler_directives: [{blank}]\n")
                    self.assertEqual(exc.rule, "sites_config_invalid_field")
                    self.assertEqual(exc.where, "sites.box.scheduler_directives[0]")

    def test_a_path_that_exists_but_is_not_a_readable_file_is_refused_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            target = repo.root / es.DEFAULT_SITES_PATH
            target.mkdir()
            with self.assertRaises(es.SitesConfigError) as ctx:
                es.load_sites(repo.root)
            self.assertEqual(ctx.exception.rule, "sites_config_unreadable")
            target.rmdir()
            target.symlink_to(repo.root / "no_such_file.yaml")
            self.assertFalse(target.exists())
            with self.assertRaises(es.SitesConfigError) as ctx:
                es.load_sites(repo.root)
            self.assertEqual(ctx.exception.rule, "sites_config_unreadable")

    def test_a_target_may_map_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Repo(tmp).load(_BASE + "targets:\n  t_cpu: local\n")
        self.assertTrue(cfg.site_for("t_cpu").is_local)


class SiteViolationTests(unittest.TestCase):
    """The machine half of the "can this run execute" gate (`test_target_profile.py`'s
    `test_the_execution_half_is_asked_of_a_run_that_reaches_validate_only` is the registry
    half, and this mirrors its phase cases)."""

    def test_asked_of_a_run_that_reaches_validate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _Repo(tmp)
            cfg = repo.load(_BASE.replace("[cpu]", "[gpu]") + "targets:\n  t_gpu: box\n")
            local_only = repo.load("sites_version: 1\n")
            (repo.root / es.DEFAULT_SITES_PATH).unlink()
            no_file = es.load_sites(repo.root)
        gpu = _profile("t_gpu", "gpu")
        for until in ("Compile", "Generate", "Build", "build", " BUILD "):
            with self.subTest(until_phase=until):
                self.assertEqual(es.site_violations(local_only, gpu, until_phase=until), [])
        for until in ("Validate", "validate", None, "", "Buld"):
            with self.subTest(until_phase=until):
                violations = es.site_violations(local_only, gpu, until_phase=until)
                self.assertEqual(len(violations), 1, violations)
                self.assertEqual(violations, [
                    ("hardware.class: gpu is not executed at the site target t_gpu runs at: "
                    "sites.yaml maps target t_gpu to no site, so it runs at local, which "
                    "executes cpu; the sites that execute gpu: none")])
                self.assertEqual(es.site_violations(no_file, gpu, until_phase=until), [
                    ("hardware.class: gpu is not executed at the site target t_gpu runs at: "
                    "there is no sites.yaml, so target t_gpu runs at local, which executes "
                    "cpu; the sites that execute gpu: none")])
                # The site that WOULD run it is the answer.
                self.assertEqual(es.site_violations(cfg, gpu, until_phase=until), [])
        # This host's own class runs locally at every end.
        for until in ("Build", "Validate", None):
            self.assertEqual(es.site_violations(local_only, _profile("t_cpu", "cpu"),
                                                until_phase=until), [])

    def test_an_explicit_local_mapping_and_an_overridden_local_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Repo(tmp).load("sites_version: 1\nsites:\n  local:\n    executes: [gpu]\n"
                                  "targets:\n  t_cpu: local\n")
        violations = es.site_violations(cfg, _profile("t_cpu", "cpu"), until_phase="Validate")
        self.assertEqual(violations, [
            ("hardware.class: cpu is not executed at the site target t_cpu runs at: sites.yaml "
            "maps target t_cpu to local, which executes gpu; the sites that execute cpu: none")])

    def test_a_mapped_site_that_does_not_execute_the_class_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Repo(tmp).load(_BASE + "targets:\n  t_gpu: box\n")
        violations = es.site_violations(cfg, _profile("t_gpu", "gpu"), until_phase="Validate")
        self.assertEqual(violations, [
            ("hardware.class: gpu is not executed at the site target t_gpu runs at: sites.yaml "
            "maps target t_gpu to box, which executes cpu; the sites that execute gpu: none")])
        # A class the local site runs is still refused when the target is mapped elsewhere:
        # the mapping is the operator's statement of where it runs — and the sites that DO run
        # the class are named, because the repair is usually the mapping, not a new site.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Repo(tmp).load(_BASE.replace("[cpu]", "[gpu]") + "targets:\n  t_cpu: box\n")
        violations = es.site_violations(cfg, _profile("t_cpu", "cpu"), until_phase="Validate")
        self.assertEqual(violations, [
            ("hardware.class: cpu is not executed at the site target t_cpu runs at: sites.yaml "
            "maps target t_cpu to box, which executes gpu; the sites that execute cpu: local")])

    def test_an_unmapped_target_is_told_which_declared_site_would_run_it(self) -> None:
        """Round 2, both axes: an unmapped gpu target beside a declared gpu site read "gpu is
        executed by no site" — false, and it pointed the operator at adding a site."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Repo(tmp).load(_BASE.replace("[cpu]", "[gpu]")
                                  + "  box2:\n    host: box2\n    workdir: /s\n"
                                  "    executes: [cpu, gpu]\n    scheduler: none\n")
        violations = es.site_violations(cfg, _profile("t_gpu", "gpu"), until_phase="Validate")
        self.assertEqual(violations, [
            ("hardware.class: gpu is not executed at the site target t_gpu runs at: sites.yaml "
            "maps target t_gpu to no site, so it runs at local, which executes cpu; the sites "
            "that execute gpu: box, box2")])


class DocumentTests(unittest.TestCase):

    def _section(self) -> str:
        doc = (REPO_ROOT / "docs" / "ORCHESTRATION.md").read_text(encoding="utf-8")
        match = re.search(r"^### Execution sites\n(.*?)^### ", doc, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(match, "docs/ORCHESTRATION.md has no §Execution sites")
        return match.group(1)

    def test_the_canonical_section_lists_exactly_the_declared_rules(self) -> None:
        """One statement of the rule set, compared with the code rather than restated: the
        bullet that opens "Load is fail-closed" inside §Execution sites must name every rule in
        `SITES_CONFIG_RULES` and no other `sites_config_*` token."""
        bullets = [b for b in self._section().split("\n- ")
                   if b.lstrip("- ").startswith("Load is fail-closed")]
        self.assertEqual(len(bullets), 1, "the rule bullet must appear exactly once")
        named = set(re.findall(r"`(sites_config_[a-z_]+)`", bullets[0]))
        self.assertEqual(named, set(es.SITES_CONFIG_RULES))

    def test_the_rule_reader_sees_a_dropped_rule(self) -> None:
        """Witness for the reader above: the section with one rule deleted reads short."""
        section = self._section().replace("`sites_config_unknown_site` and ", "")
        bullet = next(b for b in section.split("\n- ")
                      if b.lstrip("- ").startswith("Load is fail-closed"))
        named = set(re.findall(r"`(sites_config_[a-z_]+)`", bullet))
        self.assertEqual(set(es.SITES_CONFIG_RULES) - named, {"sites_config_unknown_site"})


if __name__ == "__main__":
    unittest.main()
