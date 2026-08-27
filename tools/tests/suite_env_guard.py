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

import contextlib

# The backend configuration homes, which carry no `METDSL_` prefix. `CODEX_HOME` cost 10
# failures when exported (measured on `165c26f` over `test_orchestration_runtime.py`);
# `CLAUDE_CONFIG_DIR` is its twin and cost 0 in the same measurement, included by symmetry
# so that a future test reading it cannot inherit the operator's.
BACKEND_CONFIG_HOME_ENV = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")

# Names the SUITE sets on purpose, and who sets them. They ARE exempt from the RATCHET
# below — that is what the table is for — but not from the strip: the hook removes an
# operator's value for these names like any other, and `STRIPPED_OPERATOR_ENV` is what the
# witness checks that against. So an operator exporting one of these is caught by the
# record, not by the ratchet. Both are process-global once set, hence visible to every test
# collected afterwards; that is recorded as a rough edge in TODO.md rather than fixed.
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

# Set by the hook, whether or not it stripped. False means the reader is not the module the
# hook wrote to — which is the defect this flag exists for, so it must not double as "the
# operator declined". That is `DECLINED`.
CONFIGURED = False

# Set when `--keep-operator-env` told the hook not to strip.
DECLINED = False


def operator_env_names_to_strip(environ) -> list[str]:
    """The ambient names a test must not be able to inherit.

    Defined ONCE and read by the hook, by the ratchet and by the witness, so none of them
    can drift into pinning a copy of the rule.

    BY PREFIX, from `orchestration_runtime.LEAF_ENV_ALLOWED_PREFIXES` — the same constant
    that decides which host names reach a leaf — so a `METDSL_*` knob added later is
    neutralized without anyone remembering this file.

    The candidate names are snapshotted BEFORE the import. What that guards against is the
    import DEFINING a strippable name, not the import CACHING an operator value. The second
    hazard has no defence in this function; what stands against it is
    `test_no_module_level_environment_read_defeats_the_guard`, which fails if any module the
    hook imports grows a module-scope environment read.

    NO COUNT is stated for how many names this covers, deliberately, and this paragraph
    has now been wrong twice for stating one. Three documents once said "the 17 METDSL_*
    names the tree reads"; reviewers counting differently got 21, 23, 25 and 27, each a
    correct answer to a different question about which files and which spellings count.
    What is checked instead is a PROPERTY, by
    `test_every_environment_name_the_tree_reads_is_stripped_or_declared`: every name the
    tree reads is either stripped here or declared in `MUST_BE_INHERITED`. That question
    can fail — verified on three uncovered names — which the count-shaped one could not.
    """
    present = list(environ)
    from tools.orchestration_runtime import LEAF_ENV_ALLOWED_PREFIXES

    names = {n for n in present if n.startswith(tuple(LEAF_ENV_ALLOWED_PREFIXES))}
    names.update(n for n in BACKEND_CONFIG_HOME_ENV if n in present)
    return sorted(names)


# Names the tree reads and the suite MUST inherit — process and interpreter facts, not
# per-run knobs an operator would export to change what a run does. Stripping one of these
# would break the suite rather than steady it.
MUST_BE_INHERITED = {
    "HOME": "the home directory itself; the hook guards resolve paths against it",
    "PYTHONPATH": "how `tools.` imports resolve at all under `python3 -m pytest`",
    "PYTHONDONTWRITEBYTECODE": "an interpreter setting the mutation sweeps rely on",
}


def undecided_environment_names(read_names) -> set[str]:
    """Names the tree reads that are neither stripped nor declared inheritable.

    Lives here, beside both tables, rather than as an expression inside the test that
    consumes it — an expression there is an assertion, and neutering an assertion inside a
    test survives every sweep by construction. As a function it is a mechanism with its own
    synthetic witness.
    """
    stripped = set(operator_env_names_to_strip({n: "x" for n in read_names}))
    return set(read_names) - stripped - set(MUST_BE_INHERITED)


@contextlib.contextmanager
def isolated_record():
    """Drive the strip without leaving the session's own record or flags behind.

    `STRIPPED_OPERATOR_ENV` and `CONFIGURED` are process-global. A test that drives the
    strip on a synthetic mapping sets both as a side effect, and `CONFIGURED` left up means
    the witness's "the hook never ran, or wrote to a different module" check can be
    satisfied by a SIBLING rather than by the hook, depending on execution order. Restoring
    both HERE makes that a mechanism with a witness instead of two lines of test cleanup
    that a sweep cannot reach.
    """
    global CONFIGURED
    record, configured = dict(STRIPPED_OPERATOR_ENV), CONFIGURED
    STRIPPED_OPERATOR_ENV.clear()
    try:
        yield
    finally:
        STRIPPED_OPERATOR_ENV.clear()
        STRIPPED_OPERATOR_ENV.update(record)
        CONFIGURED = configured


def undeclared_operator_env_names(environ) -> set[str]:
    """Strippable names present that nobody has claimed — the ratchet.

    Either the guard stopped stripping, or the suite grew a new process-global environment
    dependence without saying who sets it.
    """
    return set(operator_env_names_to_strip(environ)) - set(SUITE_OWNED_ENV)


def decline_strip() -> None:
    """The hook ran and was told not to strip (`--keep-operator-env`).

    `CONFIGURED` still goes up, because the question it answers is "did the hook run and
    write to the module I am reading" — a cross-module split, not a policy. Conflating the
    two cost a failure belonging to nothing: with the flag and an EMPTY environment, the
    early return left `CONFIGURED` False and the witness failed on a host with no knob set
    at all, which is precisely the class this whole change exists to remove. Measured: `-q
    --keep-operator-env` on a clean checkout gave `1 failed, 5293 passed, 1 skipped`.

    With the flag AND a knob set, what fails is the ratchet — and that failure does belong
    to the knob, which is what the flag's help promises.
    """
    global CONFIGURED, DECLINED
    CONFIGURED = True
    DECLINED = True


def strip_operator_env(environ) -> list[str]:
    """Remove the operator's per-run knobs; return the names removed.

    The name list is computed first — that is where the import lives and the only step that
    could raise for an ordinary reason — so such a failure happens before anything has been
    popped and leaves the environment whole.

    RECORDING AND REMOVING ARE VERIFIED TOGETHER, at the point of the strip. Doing one
    without the other leaves the environment correct and the record empty, or the record
    full and the environment poisoned, and each of those makes a different witness vacuous
    while the other still passes. It is not a hypothetical: this repository shipped the
    second shape for two commits, because a mutant applied in a worktree was carried back
    into the checkout by a `cp` — the file was untracked there, so the `git checkout` meant
    to revert it did nothing and said nothing. The check costs one dict lookup per name and
    fails the session loudly rather than leaving a guard that reports success.
    """
    global CONFIGURED
    names = operator_env_names_to_strip(environ)
    for name in names:
        STRIPPED_OPERATOR_ENV[name] = environ.pop(name)
        if name in environ:                       # see the docstring
            raise RuntimeError(
                f"the operator-environment guard recorded {name} without removing it")
    CONFIGURED = True
    return names


def restore_operator_env(environ) -> None:
    """Put back what was stripped.

    NOT "leaves the caller's environment as it found it": the suite ADDS names of its own
    — the four modules that `setdefault` METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK at
    import — and those were never in the record, so they survive this.

    UNWITNESSED and currently unreachable: no `pytest.main(` exists anywhere in this
    repository, so nothing observes the restoration and its mutant survives the suite. It
    is kept as defence in depth for a future in-process runner, and this sentence is the
    record that it is not a covered guarantee.
    """
    environ.update(STRIPPED_OPERATOR_ENV)
    STRIPPED_OPERATOR_ENV.clear()
