#!/usr/bin/env python3
"""Execution sites: WHERE `Validate.execute` runs a binary, read from the operator's `./sites.yaml`
(issue #293).

A target profile says what a run builds FOR; it does not say which machine runs the result, and
it must not: the profile's sha256 enters the validate key, and a site is a record of where the
evidence was produced, not an input that changes what the evidence has to show. Host names are
also machine-local facts (`AGENTS.md` §Canonical record placement). So the site description is a
separate, gitignored file the operator creates by copying `docs/examples/sites.example.yaml`,
beside `./llm.yaml`, and a missing file means every target runs at the `local` site.

"Can this run execute a binary built for this hardware class" has two halves. The registry half —
does this repository have the code, `execution` on the class's record — is
`target_profile.hardware_violations`. The machine half — does some site the operator configured
actually have such a machine — is `site_violations` here: the site a target maps to must list the
class in its `executes`. The local site's default is `LOCAL_DEFAULT_EXECUTES`.

The loader follows `tools/llm_config.py`: a closed document shape, a repeated key refused, and
every refusal NAMED (`SitesConfigError.rule`, one of `SITES_CONFIG_RULES`), because this is a
file an operator writes by hand. Values that reach a remote shell — `host`, `workdir`,
`scheduler_directives` — are refused at load when they carry a character the build-runtime
server refuses in a value for the same reason (`_SHELL_ACTIVE_CHARS`), read from the server
rather than copied.

Nothing calls `load_sites` yet: the driver and the conductor are wired to it in a later pull
request of issue #293, and until then this module changes no run.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tools.backends import registry
from tools.derivation import canonical_json_bytes, sha256_hex
from tools.host_execution import LOCAL_SITE
from tools.target_profile import NON_EXECUTING_PHASES, TOKEN_PATTERN, list_target_ids

SITES_VERSION = 1
#: The file the driver reads, resolved against the repository root.
DEFAULT_SITES_PATH = "sites.yaml"
#: The hardware classes the `local` site executes when `sites.yaml` does not say: the class
#: `Validate.execute` launches in-process on this host (`tools/backends/registry.py`'s `hardware`
#: records say which class that is). A host that can run another class lists it under `local:`.
LOCAL_DEFAULT_EXECUTES: tuple[str, ...] = ("cpu",)
#: The scheduler a site uses when it submits nothing: the job script runs in the foreground.
DIRECT_SCHEDULER = "none"

#: Every refusal this loader can raise. `docs/ORCHESTRATION.md` §Execution sites lists them and
#: `tools/tests/test_execution_sites.py` compares that list with this set.
SITES_CONFIG_RULES: frozenset[str] = frozenset({
    "sites_config_unreadable",
    "sites_config_not_a_mapping",
    "sites_config_duplicate_key",
    "sites_config_version",
    "sites_config_unknown_key",
    "sites_config_missing_field",
    "sites_config_invalid_field",
    "sites_config_shell_active_value",
    "sites_config_scheduler_unknown",
    "sites_config_scheduler_field_misplaced",
    "sites_config_executes_unknown_class",
    "sites_config_unknown_site",
    "sites_config_unknown_target",
})

_TOP_KEYS = frozenset({"sites_version", "sites", "targets"})
_REMOTE_REQUIRED = frozenset({"host", "workdir", "executes", "scheduler"})
_REMOTE_OPTIONAL = frozenset({"scheduler_directives", "queue_timeout_sec"})
#: The fields that only mean something to a batch scheduler; a `none` site may not carry them.
_SCHEDULER_ONLY = ("scheduler_directives", "queue_timeout_sec")
_LOCAL_KEYS = frozenset({"executes"})
#: An ssh destination the host hands to ssh AND scp unchanged: `[user@]name`. `:` and `/` are
#: refused because scp reads `host:path` at the first `:` and treats a `/` before it as a local
#: path — `scp f 'a/b:/x'` is a local copy — so ssh and scp would reach different places; a
#: leading `-` is an ssh option, and one of those runs a command. A destination this refuses
#: (an IPv6 literal, a port) is written as an ssh alias in ssh's own configuration.
HOST_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]*")


class SitesConfigError(ValueError):
    """A named `sites.yaml` rejection. `rule` is one of `SITES_CONFIG_RULES`; `where` is a dotted
    path into the document, empty for a whole-document failure — except for
    `sites_config_duplicate_key`, raised while YAML is still being parsed, where it is the
    repeated key alone."""

    def __init__(self, rule: str, message: str, *, where: str = "") -> None:
        assert rule in SITES_CONFIG_RULES, rule
        self.rule = rule
        self.where = where
        super().__init__(f"{rule}: {message}" + (f" (at {where})" if where else ""))


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that refuses a repeated mapping key: YAML keeps the last one silently,
    and two `gpu_box:` blocks would send a run to a machine the operator cannot see in the file."""


def _no_duplicate_keys(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            raise SitesConfigError(
                "sites_config_unknown_key",
                f"mapping key {key!r} is a {type(key).__name__}; only scalar keys are "
                f"meaningful in this document") from None
        if key in seen:
            raise SitesConfigError(
                "sites_config_duplicate_key",
                f"key {key!r} is defined more than once in the same mapping; YAML keeps only "
                f"the last", where=str(key))
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: _no_duplicate_keys(loader, node))


def _shell_active_chars() -> frozenset[str]:
    """The build-runtime server's set of characters a shell acts on, reached the way
    `tools/host_prerequisites.py` reaches that module, so the two refusals cannot drift."""
    mcp_dir = str(Path(__file__).resolve().parents[1] / "mcp_servers")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import build_runtime_server

    return frozenset(build_runtime_server._SHELL_ACTIVE_CHARS)


@dataclass(frozen=True)
class Site:
    """One execution site. The `local` site has no `host` and no `workdir`, and runs `none`."""

    site_id: str
    executes: tuple[str, ...]
    host: str | None = None
    workdir: str | None = None
    scheduler: str = DIRECT_SCHEDULER
    scheduler_directives: tuple[str, ...] = ()
    queue_timeout_sec: int | None = None

    @property
    def is_local(self) -> bool:
        return self.site_id == LOCAL_SITE


@dataclass(frozen=True)
class SitesConfig:
    """A loaded `sites.yaml`. `path` and `sha256` are None when no file exists, which is the
    configuration "every target runs at the local site"."""

    sites: dict[str, Site]
    targets: dict[str, str] = field(default_factory=dict)
    path: Path | None = None
    sha256: str | None = None

    def site_for(self, target_id: str) -> Site:
        """The site a run for `target_id` executes at: the one `targets:` maps it to, else local."""
        return self.sites[self.targets.get(target_id, LOCAL_SITE)]


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SitesConfigError("sites_config_invalid_field",
                               f"must be a non-empty string, got {value!r}", where=where)
    return value


def _remote_safe(value: str, where: str, *, spaces: bool) -> str:
    """Refuse a value a remote shell would act on or misread: a character in the server's
    shell-active set, anything that is not printable ASCII (a NUL ends an argv element, a
    non-breaking space reads as a space to a person and not to a shell), and a plain space
    where `spaces` is False (it is admissible inside a directive, never in a host or a path)."""
    bad = sorted(set(value) & _shell_active_chars())
    bad += sorted({c for c in value if not (c.isascii() and c.isprintable())})
    if not spaces and " " in value:
        bad.append(" ")
    if bad:
        raise SitesConfigError(
            "sites_config_shell_active_value",
            f"{value!r} carries {', '.join(repr(c) for c in bad)}, which a remote shell would "
            f"act on or misread", where=where)
    return value


def _token(value: Any, where: str) -> str:
    if not (isinstance(value, str) and TOKEN_PATTERN.fullmatch(value)):
        raise SitesConfigError("sites_config_invalid_field",
                               f"{value!r} is not a lowercase token ({TOKEN_PATTERN.pattern})",
                               where=where)
    return value


def _check_keys(obj: dict, required: frozenset[str], optional: frozenset[str], where: str) -> None:
    for unknown in sorted(set(obj) - required - optional, key=str):
        raise SitesConfigError("sites_config_unknown_key", f"unknown key {unknown!r}",
                               where=f"{where}.{unknown}" if where else str(unknown))
    for missing in sorted(required - set(obj)):
        raise SitesConfigError("sites_config_missing_field", f"missing required key {missing!r}",
                               where=where)


def _executes(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SitesConfigError("sites_config_invalid_field",
                               f"must be a non-empty list of hardware classes, got {value!r}",
                               where=where)
    classes = tuple(_token(c, f"{where}[{i}]") for i, c in enumerate(value))
    if len(set(classes)) != len(classes):
        raise SitesConfigError("sites_config_invalid_field",
                               f"lists a hardware class more than once: {list(classes)}",
                               where=where)
    for i, cls in enumerate(classes):
        reason = registry.unimplemented_reason("hardware", cls)
        if reason is not None:
            raise SitesConfigError("sites_config_executes_unknown_class", reason,
                                   where=f"{where}[{i}]")
    return classes


def _remote_site(site_id: str, body: dict, where: str) -> Site:
    _check_keys(body, _REMOTE_REQUIRED, _REMOTE_OPTIONAL, where)
    host = _remote_safe(_string(body["host"], f"{where}.host"), f"{where}.host", spaces=False)
    if not HOST_PATTERN.fullmatch(host):
        raise SitesConfigError(
            "sites_config_invalid_field",
            f"{host!r} is not an ssh destination ssh and scp both read unchanged "
            f"({HOST_PATTERN.pattern}): ':' and '/' change what scp reads as the host, and a "
            f"leading '-' is an ssh option; name anything else as an ssh alias",
            where=f"{where}.host")
    workdir = _remote_safe(_string(body["workdir"], f"{where}.workdir"), f"{where}.workdir",
                           spaces=False)
    segments = [seg for seg in workdir.split("/") if seg not in ("", ".")]
    if not workdir.startswith("/") or ".." in segments or not segments:
        raise SitesConfigError(
            "sites_config_invalid_field",
            f"{workdir!r} must be an absolute path below '/', with no '..' segment: each job "
            f"is created and removed beneath it", where=f"{where}.workdir")
    executes = _executes(body["executes"], f"{where}.executes")
    scheduler = _token(body["scheduler"], f"{where}.scheduler")
    reason = registry.unimplemented_reason("scheduler", scheduler)
    if reason is not None:
        raise SitesConfigError("sites_config_scheduler_unknown", reason,
                               where=f"{where}.scheduler")
    if scheduler == DIRECT_SCHEDULER:
        for key in _SCHEDULER_ONLY:
            if key in body:
                raise SitesConfigError(
                    "sites_config_scheduler_field_misplaced",
                    f"{key!r} is read by a batch scheduler, and this site submits nothing "
                    f"(scheduler: {DIRECT_SCHEDULER})", where=f"{where}.{key}")
    directives: tuple[str, ...] = ()
    if "scheduler_directives" in body:
        raw = body["scheduler_directives"]
        if not isinstance(raw, list):
            raise SitesConfigError("sites_config_invalid_field",
                                   f"must be a list of strings, got {raw!r}",
                                   where=f"{where}.scheduler_directives")
        directives = tuple(
            _remote_safe(_string(d, f"{where}.scheduler_directives[{i}]"),
                         f"{where}.scheduler_directives[{i}]", spaces=True)
            for i, d in enumerate(raw))
    queue_timeout: int | None = None
    if "queue_timeout_sec" in body:
        raw = body["queue_timeout_sec"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise SitesConfigError("sites_config_invalid_field",
                                   f"must be an integer >= 1, got {raw!r}",
                                   where=f"{where}.queue_timeout_sec")
        queue_timeout = raw
    return Site(site_id=site_id, executes=executes, host=host, workdir=workdir,
                scheduler=scheduler, scheduler_directives=directives,
                queue_timeout_sec=queue_timeout)


def _parse(doc: Any, repo_root: Path) -> tuple[dict[str, Site], dict[str, str]]:
    if not isinstance(doc, dict):
        raise SitesConfigError("sites_config_not_a_mapping",
                               f"the document must be a mapping, got {type(doc).__name__}")
    _check_keys(doc, frozenset({"sites_version"}), _TOP_KEYS - {"sites_version"}, "")
    version = doc["sites_version"]
    if type(version) is not int or version != SITES_VERSION:
        raise SitesConfigError("sites_config_version",
                               f"must be {SITES_VERSION}, got {version!r}", where="sites_version")
    sites: dict[str, Site] = {LOCAL_SITE: Site(site_id=LOCAL_SITE, executes=LOCAL_DEFAULT_EXECUTES)}
    raw_sites = doc.get("sites", {})
    if raw_sites is None:
        raw_sites = {}
    if not isinstance(raw_sites, dict):
        raise SitesConfigError("sites_config_invalid_field",
                               f"must be a mapping of site id to site, got {raw_sites!r}",
                               where="sites")
    for site_id, body in raw_sites.items():
        where = f"sites.{site_id}"
        _token(site_id, where)
        if site_id == LOCAL_SITE and body is None:
            # `local:` with nothing under it says nothing, and the default stands.
            continue
        if not isinstance(body, dict):
            raise SitesConfigError("sites_config_invalid_field",
                                   f"must be a mapping, got {body!r}", where=where)
        if site_id == LOCAL_SITE:
            _check_keys(body, frozenset(), _LOCAL_KEYS, where)
            if "executes" in body:
                sites[LOCAL_SITE] = Site(site_id=LOCAL_SITE,
                                         executes=_executes(body["executes"], f"{where}.executes"))
            continue
        sites[site_id] = _remote_site(site_id, body, where)
    raw_targets = doc.get("targets", {})
    if raw_targets is None:
        raw_targets = {}
    if not isinstance(raw_targets, dict):
        raise SitesConfigError("sites_config_invalid_field",
                               f"must be a mapping of target id to site id, got {raw_targets!r}",
                               where="targets")
    declared_targets = set(list_target_ids(repo_root)) if raw_targets else set()
    targets: dict[str, str] = {}
    for target_id, site_id in raw_targets.items():
        where = f"targets.{target_id}"
        if target_id not in declared_targets:
            raise SitesConfigError(
                "sites_config_unknown_target",
                f"{target_id!r} names no profile under spec/targets/ (declared: "
                f"{', '.join(sorted(declared_targets)) or 'none'})", where=where)
        if not isinstance(site_id, str) or site_id not in sites:
            raise SitesConfigError(
                "sites_config_unknown_site",
                f"{site_id!r} is not a site this file declares (declared: "
                f"{', '.join(sorted(sites))})", where=where)
        targets[target_id] = site_id
    return sites, targets


def load_sites(repo_root: Path, *, path: str | Path | None = None) -> SitesConfig:
    """Load the operator's site configuration, `<repo_root>/sites.yaml` unless `path` says
    otherwise. A missing file is the configuration with the local site only; a path that exists
    and cannot be read as a file — a directory, a dangling symlink — is not missing, and anything
    the loader cannot read or does not admit raises `SitesConfigError`. A `targets:` mapping reads
    `spec/targets/`, whose own malformation raises `target_profile.TargetProfileError`."""
    repo_root = Path(repo_root)
    p = Path(path) if path is not None else repo_root / DEFAULT_SITES_PATH
    if not p.is_absolute():
        p = repo_root / p
    if not os.path.lexists(p):
        return SitesConfig(
            sites={LOCAL_SITE: Site(site_id=LOCAL_SITE, executes=LOCAL_DEFAULT_EXECUTES)})
    try:
        doc = yaml.load(p.read_text(encoding="utf-8"), Loader=_NoDuplicateKeyLoader)
    except SitesConfigError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SitesConfigError("sites_config_unreadable", f"{p}: {exc}") from exc
    sites, targets = _parse(doc, repo_root)
    return SitesConfig(sites=sites, targets=targets, path=p,
                       sha256=sha256_hex(canonical_json_bytes(doc)))


def site_violations(config: SitesConfig, profile: Any, *,
                    until_phase: str | None = None) -> list[str]:
    """The machine half of the "can this run execute" gate: for a run that reaches `Validate`
    (every `until_phase` not in `NON_EXECUTING_PHASES`, None included), the site `profile`'s
    target maps to must list the profile's hardware class in its `executes`. Empty otherwise."""
    if str(until_phase or "").strip().lower() in NON_EXECUTING_PHASES:
        return []
    site = config.site_for(profile.target_id)
    hardware_class = profile.hardware_class
    if hardware_class in site.executes:
        return []
    local = config.sites[LOCAL_SITE]
    if site.is_local:
        mapping = f"sites.yaml maps target {profile.target_id} to no site, so it runs locally"
    else:
        mapping = (f"sites.yaml maps target {profile.target_id} to {site.site_id} "
                   f"(executes {', '.join(site.executes)})")
    return [(f"hardware.class: {hardware_class} is executed by no site: {mapping}; the local "
             f"site executes {', '.join(local.executes)}")]
