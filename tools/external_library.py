"""External libraries (issue #644, the system tier's Part 3, P1): the declaration a node names,
where a site has it installed, and the identity a build key holds of it.

Three inputs meet here, each owned by someone else:

* **The declaration** — `spec/libraries/<library_id>.yaml`, Project data a person writes: the
  language whose sources may reference the library, the modules and external procedures a
  declaring source may use, and the link names in link order. A node names the libraries its
  sources reach in its `deps.yaml` under `dependencies.libraries`
  (`orchestration_runtime._parse_library_entries`).
* **The location** — a site's `libraries:` in the operator's `sites.yaml`
  (`execution_sites.SiteLibrary`): an include and a library directory, each optional. No config
  tool is asked: on the measured host the one on `PATH` described another compiler's build.
* **The knowledge** — what a flag, a link word, a load name or a symbol table is, which belongs
  to the compiler (`library_flags`, `library_binary` on the `compiler` axis). This module holds
  the names it is handed as opaque tokens.

The identity is the content hash of the declared files at the site — the file the linker takes
for each link name, the file the loader takes for it (its load name, beside it), and each
declared module file. It covers those files and not what they load in turn, and the build and
the run trust the loader to take the files the key hashed: nothing compares the two (the
operator's own installation is not policed; `AGENTS.md` §Development premises).

A node whose library is not usable on a target is `library unavailable`: decided before any
phase from human and operator data alone (`unavailable_reason`), never by a leaf.

Imports the registry and the derivation hashes; `orchestration_runtime`'s deps.yaml readers are
imported inside the functions that read a deps.yaml.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.backends import registry

LIBRARY_DECLARATION_VERSION = 1
#: Where the declarations live, relative to the repository root.
LIBRARIES_DIR = "spec/libraries"
_DECLARATION_KEYS = frozenset({"library_version", "library_id", "language", "modules",
                               "procedures", "link"})

#: The reasons a closure's library is unusable on a target, decided before any phase.
UNAVAILABLE_REASONS: tuple[str, ...] = ("library_language_mismatch", "library_not_at_site")
#: A site entry whose files do not resolve: an operator configuration defect, refused.
SITE_UNRESOLVED = "site_library_unresolved"
#: A closure that declares a library at a site the host cannot hash files at.
REMOTE_SITE = "library_at_remote_site"


class LibraryDeclarationError(ValueError):
    """A declaration that is missing or does not have the closed shape."""


#: A target whose compiler cannot reach a library (`registry.missing_capability_reason`).
CAPABILITY_MISSING = "library_capability_missing"


class LibraryUnusable(RuntimeError):
    """A closure's library cannot be built against here; `reason` is one of
    `UNAVAILABLE_REASONS`, `SITE_UNRESOLVED`, `REMOTE_SITE` or `CAPABILITY_MISSING`. The driver
    decides every one of them before any phase (`tools/run_workflow.py`), so a phase that meets
    one meets a defect of that decision."""

    def __init__(self, reason: str, library_id: str | None, detail: str) -> None:
        self.reason = reason
        self.library_id = library_id
        super().__init__(f"{reason}: "
                         + (f"library {library_id!r}: " if library_id else "") + detail)


class LibraryUnresolved(LibraryUnusable):
    """A library a site lists whose files cannot be found (`SITE_UNRESOLVED`)."""

    def __init__(self, library_id: str, detail: str) -> None:
        super().__init__(SITE_UNRESOLVED, library_id, detail)


@dataclass(frozen=True)
class LibraryDeclaration:
    library_id: str
    language: str
    modules: tuple[str, ...]
    procedures: tuple[str, ...]
    link: tuple[str, ...]
    #: sha256 of the declaration file's bytes.
    sha256: str


@dataclass(frozen=True)
class ResolvedLibrary:
    """A declared library located at a site: the directories, and the files the key hashes."""

    declaration: LibraryDeclaration
    include_dir: str | None
    lib_dir: str
    #: `(link name, file)` in link order: the file the linker takes.
    link_files: tuple[tuple[str, str], ...]
    #: `(load name, file)` for each shared link file: the file the loader takes.
    soname_files: tuple[tuple[str, str], ...]
    #: `(module, file)` for each declared module.
    module_files: tuple[tuple[str, str], ...]


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _string_list(doc: dict[str, Any], key: str, *, nonempty: bool) -> tuple[str, ...]:
    value = doc.get(key)
    if (not isinstance(value, list) or (nonempty and not value)
            or not all(isinstance(v, str) and v.strip() for v in value)
            or len(set(value)) != len(value)):
        raise LibraryDeclarationError(
            f"{key!r} must be a list of distinct non-empty strings"
            + (" with at least one entry" if nonempty else "") + f", got {value!r}")
    return tuple(v.strip() for v in value)


def declaration_path(repo_root: str | Path, library_id: str) -> Path:
    return Path(repo_root) / LIBRARIES_DIR / f"{library_id}.yaml"


def load_declaration(repo_root: str | Path, library_id: str) -> LibraryDeclaration:
    """The declaration of `library_id`; `LibraryDeclarationError` when it is missing or does not
    have the closed shape (`library_version`, `library_id` equal to the file's name, a declared
    `language`, `modules` / `procedures` lists, a non-empty `link` list)."""
    path = declaration_path(repo_root, library_id)
    try:
        raw = path.read_bytes()
        doc = yaml.safe_load(raw.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise LibraryDeclarationError(f"{path}: cannot be read ({exc})") from exc
    try:
        if not isinstance(doc, dict):
            raise LibraryDeclarationError("the document must be a mapping")
        unknown = sorted(set(doc) - _DECLARATION_KEYS, key=str)
        missing = sorted(_DECLARATION_KEYS - set(doc))
        if unknown or missing:
            raise LibraryDeclarationError(
                f"keys must be exactly {sorted(_DECLARATION_KEYS)} (unknown {unknown}, "
                f"missing {missing})")
        if doc["library_version"] != LIBRARY_DECLARATION_VERSION or isinstance(
                doc["library_version"], bool):
            raise LibraryDeclarationError(
                f"library_version must be {LIBRARY_DECLARATION_VERSION}")
        if doc["library_id"] != library_id:
            raise LibraryDeclarationError(
                f"library_id {doc['library_id']!r} is not the file's name {library_id!r}")
        language = doc["language"]
        reason = registry.unsupported_reason("language", language) if isinstance(
            language, str) else f"{language!r} is not a string"
        if reason is not None:
            raise LibraryDeclarationError(f"language: {reason}")
        return LibraryDeclaration(
            library_id=library_id, language=language,
            modules=_string_list(doc, "modules", nonempty=False),
            procedures=_string_list(doc, "procedures", nonempty=False),
            link=_string_list(doc, "link", nonempty=True),
            sha256=hashlib.sha256(raw).hexdigest())
    except LibraryDeclarationError as exc:
        raise LibraryDeclarationError(f"{path}: {exc}") from None


def declaration_violation(repo_root: str | Path, library_ids: Iterable[str]) -> str | None:
    """Spec-input bound: every library a deps.yaml names is declared under `spec/libraries/`
    with the closed shape. A node-identity defect no Compile re-author can repair."""
    for library_id in library_ids:
        try:
            load_declaration(repo_root, library_id)
        except LibraryDeclarationError as exc:
            return (f"library_declaration_invalid: deps.yaml names library {library_id!r}, and "
                    f"its declaration is not usable: {exc}")
    return None


def node_library_ids(repo_root: str | Path, spec_ref: str) -> list[str]:
    """The libraries `spec_ref`'s own deps.yaml declares, in declared order: none when there is
    no readable deps.yaml (the closure walk refuses a node without one before any phase,
    `dependency_deps_unreadable`); `LibraryDeclarationError` for a malformed `libraries`
    list."""
    from tools.orchestration_runtime import _parse_library_entries, _read_deps_yaml
    doc = _read_deps_yaml(Path(repo_root), spec_ref)
    if not isinstance(doc, dict):
        return []
    ids, well_formed = _parse_library_entries(doc)
    if not well_formed:
        raise LibraryDeclarationError(
            f"{spec_ref}/deps.yaml: `dependencies.libraries` is unreadable or malformed")
    return ids


def closure_library_ids(repo_root: str | Path, spec_ref: str,
                        closure_spec_refs: Sequence[str]) -> list[str]:
    """The libraries a build of `spec_ref` links: those the node itself declares, then those of
    its closure nodes in closure order, each once."""
    ordered: list[str] = []
    for ref in (spec_ref, *closure_spec_refs):
        for library_id in node_library_ids(repo_root, ref):
            if library_id not in ordered:
                ordered.append(library_id)
    return ordered


def closure_declarations(repo_root: str | Path, spec_ref: str,
                         closure_node_keys: Sequence[str]) -> list[LibraryDeclaration]:
    """The declarations of the libraries a build of the node at `spec_ref` links, in closure
    order: the node's own, then those of its pipeline closure (`pipeline_closure_nodes`, the
    target's harness included), each once. A closure node whose spec the catalog does not
    resolve contributes none: the closure walk resolved every member through the same catalog
    before any phase. `LibraryDeclarationError` when a declaration is unusable."""
    from tools.orchestration_runtime import resolve_spec_ref_for
    refs: list[str] = []
    for node_key in closure_node_keys:
        kind_rest = node_key.partition("@")[0]
        kind, _, spec_id = kind_rest.partition("/")
        ref = resolve_spec_ref_for(Path(repo_root), kind, spec_id)
        if ref is not None:
            refs.append(ref)
    return [load_declaration(repo_root, library_id)
            for library_id in closure_library_ids(repo_root, spec_ref, refs)]


def unavailable_reason(declaration: LibraryDeclaration, language: str,
                       site: Any) -> str | None:
    """Why `declaration` cannot be used by a build for a target of `language` at `site`, or
    None: `library_language_mismatch` (the library is for another language's sources) or
    `library_not_at_site` (the site does not list it)."""
    if declaration.language != language:
        return "library_language_mismatch"
    if site.library(declaration.library_id) is None:
        return "library_not_at_site"
    return None


def resolve(declaration: LibraryDeclaration, site: Any, compiler: str) -> ResolvedLibrary:
    """Locate `declaration` at `site` through `compiler`'s `library_flags` and
    `library_binary`. `LibraryUnresolved` when the site does not list it, a link name or a
    declared module file is not found, or a shared object's load-name file is not beside it."""
    entry = site.library(declaration.library_id)
    if entry is None:
        raise LibraryUnresolved(declaration.library_id,
                                f"site {site.site_id!r} does not list it")
    flags = registry.capability_module("compiler", compiler, "library_flags")
    binary = registry.capability_module("compiler", compiler, "library_binary")
    link_files: list[tuple[str, str]] = []
    soname_files: list[tuple[str, str]] = []
    lib_dir = entry.lib
    for name in declaration.link:
        path = flags.resolve(name, entry.lib)
        if path is None:
            raise LibraryUnresolved(
                declaration.library_id,
                f"link name {name!r} resolves to no file "
                + (f"in {entry.lib}" if entry.lib else "through the compiler's own search"))
        if lib_dir is None:
            lib_dir = os.path.normpath(os.path.dirname(str(path)))
        link_files.append((name, str(path)))
        load_name = binary.soname(path)
        if load_name is not None:
            beside = Path(os.path.dirname(str(path))) / load_name
            if not beside.is_file():
                raise LibraryUnresolved(
                    declaration.library_id,
                    f"{path} is loaded as {load_name!r}, and no such file is beside it")
            soname_files.append((load_name, str(beside)))
    module_files: list[tuple[str, str]] = []
    for module in declaration.modules:
        path = flags.module_file(module, entry.include)
        if path is None:
            raise LibraryUnresolved(
                declaration.library_id,
                f"module {module!r} has no module file "
                + (f"in {entry.include}" if entry.include else "(the site lists no include)"))
        module_files.append((module, str(path)))
    assert lib_dir is not None  # `link` is non-empty, so the loop set it
    return ResolvedLibrary(
        declaration=declaration, include_dir=entry.include, lib_dir=lib_dir,
        link_files=tuple(link_files), soname_files=tuple(soname_files),
        module_files=tuple(module_files))


def site_for_target(repo_root: str | Path, target_id: str) -> Any:
    """The site a run for `target_id` executes at, from the operator's `sites.yaml`: the one
    resolution every reader of a library's location uses (the conductor's Build and runs, the
    derivation key's probe, the driver's decision). Raises the loader's
    `SitesConfigError` / `TargetProfileError`."""
    from tools.execution_sites import load_sites
    return load_sites(Path(repo_root)).site_for(target_id)


def resolve_closure(repo_root: str | Path, target: Any,
                    declarations: Sequence[LibraryDeclaration]) -> list[ResolvedLibrary]:
    """Each declaration located at the site `target` runs at, in order; `[]` for none.
    `LibraryUnusable` when the site is remote (Part 3 serves the local site only: the host
    hashes the files it links), the target's compiler lacks `library_flags` or
    `library_binary`, a library is unavailable (`unavailable_reason`) or does not resolve."""
    if not declarations:
        return []
    from tools.checked_build import target_compiler
    site = site_for_target(repo_root, target.target_id)
    if not site.is_local:
        raise LibraryUnusable(
            REMOTE_SITE, None,
            f"target {target.target_id} runs at site {site.site_id!r}, and a closure that "
            f"declares a library is built at the local site only (the host hashes the files it "
            f"links)")
    compiler = target_compiler(target)
    for capability in ("library_flags", "library_binary"):
        reason = registry.missing_capability_reason("compiler", compiler, capability)
        if reason is not None:
            raise LibraryUnusable(CAPABILITY_MISSING, None, f"toolchain compiler {compiler}: {reason}")
    language = str(target.toolchain["language"])
    for declaration in declarations:
        reason = unavailable_reason(declaration, language, site)
        if reason is not None:
            raise LibraryUnusable(reason, declaration.library_id,
                                  f"target {target.target_id} (language {language}) at site "
                                  f"{site.site_id!r}")
    return [resolve(d, site, compiler) for d in declarations]


def loader_env(target: Any, dirs: Sequence[str], inherited_env: Any) -> dict[str, str]:
    """The loader-path overlay a run of a binary linking libraries in `dirs` takes: `{}` for
    none, else the target compiler's `library_binary.loader_env` — the directories prefixed to
    the loader variable's value in `inherited_env` (the environment the run would otherwise
    see: the host process's for a host run, the wrapper's base environment for a workshop
    run)."""
    if not dirs:
        return {}
    from tools.checked_build import target_compiler
    binary = registry.capability_module("compiler", target_compiler(target), "library_binary")
    inherited = (inherited_env or {}).get(str(binary.LOADER_VARIABLE))
    return dict(binary.loader_env(list(dirs), inherited))


def built_library_dirs(binary_meta: Any) -> tuple[str, ...]:
    """The library directories a Build recorded for the binary it linked
    (`binary_meta.json#library_dirs`); empty for a binary of a closure that declares none, and
    for one built before the field existed (which declared none either)."""
    dirs = binary_meta.get("library_dirs") if isinstance(binary_meta, dict) else None
    return tuple(str(d) for d in dirs) if isinstance(dirs, list) else ()


def declared_identity(declarations: Sequence[LibraryDeclaration]) -> list[dict[str, Any]]:
    """The key member without the site's files: ids and declaration hashes, in closure order.
    What a key computed without probing the host holds."""
    return [{"library_id": d.library_id, "declaration_sha256": d.sha256} for d in declarations]


def identity(resolved: Sequence[ResolvedLibrary]) -> list[dict[str, Any]]:
    """The build key's `libraries` member: per library, in closure order, the id, the
    declaration's hash and the content hash of every declared file at the site."""
    return [
        {"library_id": r.declaration.library_id,
         "declaration_sha256": r.declaration.sha256,
         "files": {
             "link": {name: _file_sha256(path) for name, path in r.link_files},
             "soname": {name: _file_sha256(path) for name, path in r.soname_files},
             "modules": {name: _file_sha256(path) for name, path in r.module_files},
         }}
        for r in resolved]


def include_dirs(resolved: Sequence[ResolvedLibrary]) -> tuple[str, ...]:
    """The include directories, in closure order, each once."""
    return tuple(dict.fromkeys(r.include_dir for r in resolved if r.include_dir))


def library_dirs(resolved: Sequence[ResolvedLibrary]) -> tuple[str, ...]:
    """The library directories, in closure order, each once: what the run's loader path is
    prefixed with, and what the linker searches for a library's own dependencies."""
    return tuple(dict.fromkeys(r.lib_dir for r in resolved))


def make_values(compiler: str, resolved: Sequence[ResolvedLibrary]) -> tuple[str, str]:
    """`(include value, link value)`: the words the compiler's `library_flags` gives, joined.
    Both empty when the closure declares no library."""
    if not resolved:
        return "", ""
    flags = registry.capability_module("compiler", compiler, "library_flags")
    files = [path for r in resolved for _name, path in r.link_files]
    return (" ".join(flags.compile_flags(include_dirs(resolved))),
            " ".join(flags.link_flags(files, library_dirs(resolved))))


def bind_roots(resolved: Sequence[ResolvedLibrary]) -> tuple[str, ...]:
    """The directories a sandbox binds read-only for a build and run of these libraries: each
    include and library directory, its real path when that differs, and the directory each
    declared file really lives in (a link name may be a symbolic link into another prefix)."""
    real_file_dirs = [os.path.dirname(os.path.realpath(path)) for r in resolved
                      for _name, path in (*r.link_files, *r.soname_files, *r.module_files)]
    roots: list[str] = []
    for d in (*include_dirs(resolved), *library_dirs(resolved), *real_file_dirs):
        for form in (d, os.path.realpath(d)):
            if form not in roots:
                roots.append(form)
    return tuple(roots)


def symbol_check(target: Any, objects: Sequence[Path], resolved: Sequence[ResolvedLibrary],
                 declared: Iterable[str]) -> tuple[dict[str, Any], str | None]:
    """The post-build library symbol checks over `objects`, the objects the node's own leaf
    authored (issue #644), against the libraries on its link line (`resolved`) and the ones
    the node itself declares (`declared`):

    * `library_symbol_not_declared` — an object imports a symbol a library the node does not
      declare exports. On a target whose harness links an I/O library every node links it, and
      a kernel that called it could rewrite a capture a predicate judges: the leaf would be
      closer to reporting done (`AGENTS.md` §Development premises).
    * `library_symbol_interposed` — an object defines a global symbol such a library exports,
      which takes the library's place at link time.
    * `library_declared_unused` — the node declares a library and its objects import no symbol
      it exports. A call behind a branch that is false at run time still imports the symbol
      and passes: whether the library is USED is `Generate.verify`'s to judge.

    Returns `(record, violation text or None)`; the record goes into `binary_meta.json`."""
    from tools.checked_build import target_compiler
    binary = registry.capability_module("compiler", target_compiler(target), "library_binary")
    declared_set = set(declared)
    imported: set[str] = set()
    defined: set[str] = set()
    for obj in objects:
        imported |= binary.imports(obj)
        defined |= binary.defines(obj)
    violations: list[str] = []
    for r in resolved:
        library_id = r.declaration.library_id
        exported = set().union(*(binary.exports(path) for _name, path in r.link_files))
        if library_id in declared_set:
            if not imported & exported:
                violations.append(
                    f"library_declared_unused: deps.yaml declares library {library_id!r} and "
                    f"the node's objects import no symbol it exports; call it, or remove the "
                    f"declaration")
            continue
        for code, verb, hits in (("library_symbol_not_declared", "import", imported & exported),
                                 ("library_symbol_interposed", "define", defined & exported)):
            if hits:
                violations.append(
                    f"{code}: the node's objects {verb} {sorted(hits)[:10]} of library "
                    f"{library_id!r}, which the node does not declare; a node reaches only the "
                    f"libraries its own deps.yaml declares")
    record = {
        "libraries": [r.declaration.library_id for r in resolved],
        "declared": sorted(declared_set),
        "objects": [Path(o).name for o in objects],
        "violations": violations,
    }
    return record, ("\n".join(violations) or None)
