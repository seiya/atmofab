#!/usr/bin/env python3
"""The rule and the record for the suite's operator-environment guard (issue #84).

A PLAIN MODULE and not `conftest.py`, and the reason is a defect this file exists to
close. `tools/tests` has no `__init__.py`, so pytest imports the conftest under the module
name `conftest` (rootdir insertion) while a test doing `from tools.tests.conftest import
...` re-executes the same file as a SECOND module object. The hook then populates one
module's `STRIPPED_OPERATOR_ENV` and the witness reads the other's, which is empty
forever — measured: the witness's primary assertion iterated nothing on every host,
including the poisoned one it was written for, and a mutant that recorded a name without
stripping it survived. A module reached only by its dotted name is imported once, from
both sides.

`CONFIGURED` is the witness's guard against that split coming back in another shape: it
is set by the hook, so a witness that finds it False is reading a module the hook never
touched, whatever the reason.
"""

from __future__ import annotations

# The backend configuration homes, which carry no `METDSL_` prefix. `CODEX_HOME` cost 10
# failures when exported (measured on `165c26f` over `test_orchestration_runtime.py`);
# `CLAUDE_CONFIG_DIR` is its twin and cost 0 in the same measurement, included by symmetry
# so that a future test reading it cannot inherit the operator's.
BACKEND_CONFIG_HOME_ENV = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")

# Names the SUITE sets on purpose, and who sets them. NOT exemptions from the guard — the
# guard is about the OPERATOR's environment, and these are set after it has run. Both are
# process-global, so both are visible to every test collected afterwards; that is recorded
# as a rough edge in TODO.md rather than fixed.
SUITE_OWNED_ENV = {
    "METDSL_WORKFLOW_HOMES_ROOT":
        "the `_redirect_workflow_homes_root` fixture in tools/tests/conftest.py, per test",
    "METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK":
        "a module-level `os.environ.setdefault` in test_orchestration_runtime.py and the "
        "three test_pure_leaf_* modules, so it appears once any of them is imported",
}

# Populated by the conftest hook; a witness reads it rather than inferring the guard from a
# side effect. Name -> the value the operator had exported.
STRIPPED_OPERATOR_ENV: dict[str, str] = {}

# Set by the hook. False means the reader is not the module the hook wrote to.
CONFIGURED = False


def operator_env_names_to_strip(environ) -> list[str]:
    """The ambient names a test must not be able to inherit.

    Defined ONCE and read by the hook, by the ratchet and by the witness, so none of them
    can drift into pinning a copy of the rule.

    BY PREFIX, from `orchestration_runtime.LEAF_ENV_ALLOWED_PREFIXES` — the same constant
    that decides which host names reach a leaf — so a `METDSL_*` knob added later is
    neutralized without anyone remembering this file.

    The candidate names are snapshotted BEFORE the import. What that guards against is the
    import DEFINING a strippable name, not the import CACHING an operator value; nothing in
    `orchestration_runtime` reads the environment at module level today (measured by
    spying on `os.environ` through the import), and the second hazard has no defence here.
    """
    present = list(environ)
    from tools.orchestration_runtime import LEAF_ENV_ALLOWED_PREFIXES

    names = {n for n in present if n.startswith(tuple(LEAF_ENV_ALLOWED_PREFIXES))}
    names.update(n for n in BACKEND_CONFIG_HOME_ENV if n in present)
    return sorted(names)


def undeclared_operator_env_names(environ) -> set[str]:
    """Strippable names present that nobody has claimed — the ratchet.

    Either the guard stopped stripping, or the suite grew a new process-global environment
    dependence without saying who sets it.
    """
    return set(operator_env_names_to_strip(environ)) - set(SUITE_OWNED_ENV)


def strip_operator_env(environ) -> list[str]:
    """Remove the operator's per-run knobs; return the names removed.

    The name list is computed first — that is where the import lives and the only step that
    can raise — so a failure happens before anything has been popped and leaves the
    environment whole. The pops themselves cannot fail.
    """
    global CONFIGURED
    names = operator_env_names_to_strip(environ)
    for name in names:
        if name in SUITE_OWNED_ENV:
            STRIPPED_OPERATOR_ENV[name] = environ[name]
            continue
        STRIPPED_OPERATOR_ENV[name] = environ.pop(name)
    CONFIGURED = True
    return names


def restore_operator_env(environ) -> None:
    """Put them back, so an in-process pytest run leaves its caller's environment alone.

    UNWITNESSED and currently unreachable: no `pytest.main(` exists anywhere in this
    repository, so nothing observes the restoration and its mutant survives the suite. It
    is kept as defence in depth for a future in-process runner, and this sentence is the
    record that it is not a covered guarantee.
    """
    environ.update(STRIPPED_OPERATOR_ENV)
    STRIPPED_OPERATOR_ENV.clear()
