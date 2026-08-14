#!/usr/bin/env python3
"""The Fortran language backend.

Submodules, by capability:

* `lines` — free-form logical-line scanning (comments, `&` continuations, `;` statements).
* `structure` — the tree-sitter-fortran structural front end the model gates read through.
* `signatures` — the language-neutral structured signature form <-> Fortran interface stanzas.
* `structure_differential` — a developer harness that diffs `structure` against a flang oracle.

`docs/BACKEND_BOUNDARY.md` states which capabilities are still inlined in the neutral core.
"""
