#!/usr/bin/env python3
"""Unit tests for tools/backends/language/fortran/signatures (Objective B language backend).

The correctness contract is a round-trip driven by the REAL published interfaces (not a synthetic
fixture — a hand-built struct could pass while the real §5.1 shape breaks; see the fixture-fiction
lesson): loading the real harness structured §5.1 block and rendering/reparsing it through Fortran
must preserve the struct and the exact NORMALIZED stanza lines the current gates compare. The same must hold for
`the fortran backend runner's _HARNESS_V3_INTERFACE`, the third hardcoded copy of the harness signatures the
renderer pin uses. Drift tests confirm the structured form keeps the gate's discriminating power
(a changed intent / rank / type / name changes the normalized index).
"""

from __future__ import annotations

import copy
import re
import unittest
from pathlib import Path
from unittest import mock

from tools import structured_signatures
from tools.backends.language.fortran import signatures as fortran_signatures
from tools.backends.language.fortran.lines import normalize_fortran_line
from tools.backends.language.fortran.signatures import (
    SignatureParseError,
    canonicalize_end_line,
    declaration_atoms,
    load_structured_signatures,
    normalized_stanza_index,
    parse_interface_stanzas,
    parse_signatures_from_fortran,
    render_interface_to_fortran,
    render_module_parameter_to_fortran,
    render_signatures_to_fortran,
    render_symbol_to_fortran,
    stanza_atoms,
)
from tools.backends.language.fortran.runner import _HARNESS_V3_INTERFACE, _HARNESS_V3_PARAMETERS
from tools.validate_pipeline_semantics import _FENCED_BLOCK_RE

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_SPEC = (
    REPO_ROOT
    / "spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md"
)
CONTROLLED_SPEC_DOC = REPO_ROOT / "docs/CONTROLLED_SPEC.md"


def _real_section51_struct() -> dict:
    md = HARNESS_SPEC.read_text(encoding="utf-8")
    section = md.split("### 5.1", 1)[1]
    m = _FENCED_BLOCK_RE.search(section)
    assert m, "harness controlled_spec §5.1 fenced block not found"
    struct, err = load_structured_signatures(m.group(1))
    assert err is None, err
    return struct


def _real_section51_block() -> str:
    return render_signatures_to_fortran(_real_section51_struct())


class RoundTripRealArtifactsTest(unittest.TestCase):
    """parse -> render preserves every symbol's normalized stanza lines on REAL interfaces."""

    def _assert_round_trip(self, block: str) -> dict:
        struct = parse_signatures_from_fortran(block)
        rendered = render_signatures_to_fortran(struct)
        orig = normalized_stanza_index(block)
        rend = normalized_stanza_index(rendered)
        self.assertEqual(set(orig), set(rend), "symbol set changed under round-trip")
        for sym in orig:
            self.assertEqual(
                orig[sym], rend[sym], f"normalized stanza lines drifted for {sym}"
            )
        return struct

    def test_round_trip_harness_controlled_spec_section51(self) -> None:
        published = _real_section51_struct()
        struct = self._assert_round_trip(render_signatures_to_fortran(published))
        # sanity on the parsed shape (the real harness surface)
        self.assertEqual(len(struct["procedures"]), 13)
        self.assertEqual(len(struct["types"]), 5)
        self.assertEqual(
            {mp["name"] for mp in struct["module_parameters"]}, {"dp", "case_id_len"}
        )

    def test_round_trip_backend_runner_hardcoded_copy(self) -> None:
        # The third copy of the signatures (the fortran backend runner's _HARNESS_V3_INTERFACE) must lower and
        # round-trip identically, so B.3 can single-source the renderer pin through this backend.
        self._assert_round_trip(_HARNESS_V3_INTERFACE)

    def test_struct_is_stable_under_reparse(self) -> None:
        struct = parse_signatures_from_fortran(_real_section51_block())
        reparsed = parse_signatures_from_fortran(render_signatures_to_fortran(struct))
        self.assertEqual(struct, reparsed, "structured form not stable under render->parse")

    def test_language_neutral_vocabulary(self) -> None:
        # The abstract vocabulary must carry no Fortran spelling (`character`, `type(...)`).
        struct = parse_signatures_from_fortran(_real_section51_block())
        specs = [a["spec"] for p in struct["procedures"] for a in p["args"]]
        specs += [
            p["result"]["spec"] for p in struct["procedures"] if p["result"]
        ]
        specs += [c["spec"] for t in struct["types"] for c in t["components"]]
        for spec in specs:
            self.assertIn(spec["type"], {"real", "integer", "logical", "string", "derived"})


class DriftDiscriminationTest(unittest.TestCase):
    """A semantic drift in the struct changes the rendered normalized index (gate keeps its teeth)."""

    def setUp(self) -> None:
        self.struct = parse_signatures_from_fortran(_real_section51_block())
        self.base = normalized_stanza_index(render_signatures_to_fortran(self.struct))

    def _index_after(self, mutate) -> dict:
        s = copy.deepcopy(self.struct)
        mutate(s)
        return normalized_stanza_index(render_signatures_to_fortran(s))

    def test_intent_change_is_detected(self) -> None:
        def m(s):
            proc = next(p for p in s["procedures"] if p["name"].endswith("parse_cases"))
            proc["args"][0]["intent"] = "inout"  # was in
        self.assertNotEqual(self.base, self._index_after(m))

    def test_rank_change_is_detected(self) -> None:
        def m(s):
            proc = next(p for p in s["procedures"] if p["name"].endswith("emit_array_r1"))
            proc["args"][0]["rank"] = 2  # was 1
        self.assertNotEqual(self.base, self._index_after(m))

    def test_type_change_is_detected(self) -> None:
        def m(s):
            proc = next(p for p in s["procedures"] if p["name"].endswith("emit_int"))
            proc["args"][0]["spec"]["type"] = "real"  # was integer
        self.assertNotEqual(self.base, self._index_after(m))

    def test_component_reorder_is_detected_for_types(self) -> None:
        # A derived type's component LAYOUT is part of the §5 compatibility contract; a reorder
        # changes the ordered rendering (the type gate compares ordered lists, not sets).
        def m(s):
            t = next(t for t in s["types"] if t["name"].endswith("h_named"))
            t["components"].reverse()
        rendered = render_signatures_to_fortran(self._mutated(m))
        # The normalized *set* is order-insensitive, so assert on the ordered rendered lines.
        base_lines = _type_lines(render_signatures_to_fortran(self.struct), "h_named")
        drift_lines = _type_lines(rendered, "h_named")
        self.assertNotEqual(base_lines, drift_lines)

    def _mutated(self, mutate) -> dict:
        s = copy.deepcopy(self.struct)
        mutate(s)
        return s


class FailClosedTest(unittest.TestCase):
    def test_unparseable_type_spec_raises(self) -> None:
        with self.assertRaises(SignatureParseError):
            parse_signatures_from_fortran(
                "subroutine foo(x)\n  frobnicate :: x\nend subroutine foo\n"
            )


class FortranStanzaParserTests(unittest.TestCase):
    """Generated .f90 still uses the stanza splitter; retain its legacy fail-closed coverage."""

    def test_unterminated_stanza_errors(self) -> None:
        with self.assertRaisesRegex(SignatureParseError, "unterminated"):
            parse_signatures_from_fortran(
                "subroutine hx__foo(a)\n  integer, intent(in) :: a\n")

    def test_duplicate_stanza_errors(self) -> None:
        dup = (
            "function hx__foo(a) result(s)\n  integer, intent(in) :: a\n"
            "  character(len=:), allocatable :: s\nend function hx__foo\n"
            "function hx__foo(a, b) result(s)\n  integer, intent(in) :: a\n"
            "  integer, intent(in) :: b\n  character(len=:), allocatable :: s\n"
            "end function hx__foo\n")
        with self.assertRaisesRegex(SignatureParseError, "duplicate"):
            parse_signatures_from_fortran(dup)

    def test_bare_end_does_not_swallow_following_procedure(self) -> None:
        block = (
            "function hx__a(x) result(s)\n  real, intent(in) :: x\n  real :: s\nend\n"
            "function hx__b(y) result(s)\n  real, intent(in) :: y\n  real :: s\n"
            "end function hx__b\n")
        ops, _types, _ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(set(ops), {"hx__a", "hx__b"})
        # ... and it is ACCEPTED, not merely found: a bare `end` legally closes a module
        # procedure, so the termination branch must mark the stanza closed. Deleting that one
        # assignment left `set(ops)` unchanged and turned this legal input into an
        # `unterminated procedure interface` refusal, with nothing watching.
        self.assertEqual(errors, [])
        # The bare `end` line itself lands in the stanza (it is appended before the next header
        # terminates it) — recorded as measured, not as expected: this is `origin/main` behaviour
        # and the branch does not change it.
        self.assertEqual(
            ops["hx__a"],
            ["function hx__a(x) result(s)", "real, intent(in) :: x", "real :: s", "end"])

    def test_duplicate_derived_type_errors(self) -> None:
        # The duplicate check has a branch per stanza kind, and only the PROCEDURE branch was
        # pinned (`test_duplicate_stanza_errors`): deleting the derived-type branch left the whole
        # suite green while a §5.1 block declaring `hx__t` twice silently kept the second stanza —
        # the "a malformed first copy hides behind a correct second" fail-open the parser's own
        # docstring says it exists to prevent. Two reviewers found the gap independently.
        dup = (
            "type :: hx__t\n  integer :: a\nend type hx__t\n"
            "type :: hx__t\n  real :: b\nend type hx__t\n")
        with self.assertRaisesRegex(SignatureParseError, "duplicate signature for symbol 'hx__t'"):
            parse_signatures_from_fortran(dup)

    def test_no_space_endtype_closes_a_type_stanza(self) -> None:
        # The `endfunction` half of the no-space rule was pinned; the `endtype` half was not, in
        # BOTH regexes that carry it. Tightening either `end\s*` to `end\s+` kept the suite green
        # while refusing source gfortran accepts — the over-rejection direction.
        block = ("type :: hx__t\n  integer :: a\nendtype hx__t\n"
                 "type :: hx__u\n  real :: b\nend type hx__u\n")
        struct = parse_signatures_from_fortran(block)
        self.assertEqual({t["name"] for t in struct["types"]}, {"hx__t", "hx__u"})
        # ... and the closing line canonicalizes the same way with or without the space, which is
        # what makes the two spellings compare equal at the gates (`_END_STMT_RE`).
        self.assertEqual(canonicalize_end_line("endtype hx__t"), "end type")
        self.assertEqual(canonicalize_end_line("end type hx__t"), "end type")

    #: Headers that MUST lower today. Every combination of these is required to be taken by BOTH
    #: patterns; `_MUST_STAY_LOWERED_*` below keeps the lists from being eroded, which is what
    #: actually bounds the test. Two earlier versions bounded nothing: a `checked > 20` floor
    #: against an actual 288, then this product with no pin on the lists it multiplies — cutting
    #: all four to one entry each took it to a single combination with the suite green.
    _MUST_LOWER_PREFIXES = ("", "pure ", "elemental ", "recursive ", "pure elemental ",
                            "recursive pure ", "PURE ", "Elemental ")
    _MUST_LOWER_KEYWORDS = ("subroutine", "function", "SUBROUTINE", "Function")
    _MUST_LOWER_NAMES = ("hx__foo", "A1_b", "x")
    _HEADER_TAILS = ("(a)", "(a, b) result(s)", "()")

    #: Tokens NEITHER pattern accepts today. Each is a shape one pattern could plausibly grow
    #: without the other; while neither has it, the test asserts both REFUSE it, so a one-sided
    #: widening is caught on the day it happens rather than whenever someone thinks to probe it.
    _DIVERGENCE_PREFIXES = ("impure ", "module ")
    _DIVERGENCE_KEYWORDS = ("operator", "submodule", "interface", "procedure")

    #: Tokens that must stay PROBED — in the must-lower lists or the divergence lists, either is
    #: fine, but not deleted. Without this, emptying the divergence lists and widening one pattern
    #: is invisible: a census constructed exactly that pair and the suite stayed green. Moving a
    #: token from one list to the other is the deliberate act this permits; dropping it is not.
    _MUST_STAY_PROBED_PREFIXES = ("impure ", "module ")
    _MUST_STAY_PROBED_KEYWORDS = ("operator", "submodule", "interface", "procedure")

    #: ... and the same for the must-lower side, which had no bound at all: shrinking all four
    #: lists to one entry each took 288 combinations down to 1 with the suite green — the same
    #: erosion the `checked > 20` floor allowed, in the mechanism written to replace it.
    _MUST_STAY_LOWERED_PREFIXES = ("", "pure ", "elemental ", "recursive ", "pure elemental ")
    _MUST_STAY_LOWERED_KEYWORDS = ("subroutine", "function")
    #: A tail with a `result(...)` clause and a MULTI-TOKEN prefix are named explicitly because
    #: neither appears in the real §5.1 corpus, so the corpus half cannot stand in for them. The
    #: first floor kept the lists from collapsing to one entry but still allowed 288 combinations
    #: down to 60 — enough to drop both, and a one-sided narrowing of the splitter's prefix
    #: repetition then passed.
    _MUST_STAY_LOWERED_TAILS = ("(a, b) result(s)",)

    def _assert_patterns_agree(self, header: str) -> bool:
        """Assert both patterns answer the same on a WELL-FORMED header; return whether they took it.

        Agreement in BOTH directions, because both disagreements change behaviour:

        * parser-only — a signature the gates lower but the splitter never files as a stanza, so it
          is never compared;
        * splitter-only — a stanza with no lowering. Measured: adding `impure` to the splitter
          alone turns `impure subroutine hx__f(a)` from silently ignored into a hard
          `SignatureParseError`.

        The asymmetry the patterns legitimately have — the splitter has no end anchor, so it also
        matches a malformed tail — is kept out by probing well-formed headers only. An earlier
        version pinned that asymmetry over identifier shapes no compiler accepts (`a$b`), which
        refused two legitimate changes: giving the splitter an end anchor (the change that would
        make the patterns fully agree, i.e. the property this test is named for) and widening both
        identifier classes symmetrically. It was removed rather than repaired — pinning behaviour
        over source no compiler accepts buys nothing and costs the improvement.

        RECORDED COST of that removal: a ONE-SIDED widening of the splitter's identifier class is
        no longer observed, because this helper probes well-formed headers only and the splitter is
        unanchored. The consequence is a `SignatureParseError` on a header gfortran rejects anyway,
        which is why the trade was taken; stated rather than left for the next census to rediscover.
        """
        from tools.backends.language.fortran.signatures import (
            _IFACE_PROC_START, _PROC_HEADER_RE)

        lowered = _PROC_HEADER_RE.match(header)
        found = _IFACE_PROC_START.match(header)
        self.assertEqual(
            lowered is not None, found is not None,
            f"the two procedure-header patterns disagree on {header!r}: "
            f"the parser {'accepts' if lowered else 'rejects'} it and the splitter "
            f"{'accepts' if found else 'rejects'} it. A header only one of them takes is either a "
            f"signature that is never compared or a stanza that cannot be lowered.")
        if lowered is None:
            return False
        self.assertEqual(lowered.group(1).lower(), found.group(1).lower(),
                         f"the two patterns read a different keyword out of {header!r}")
        self.assertEqual(lowered.group(2), found.group(2),
                         f"the two patterns read a different symbol name out of {header!r}")
        return True

    def test_the_probe_vocabulary_keeps_the_tokens_it_was_built_for(self) -> None:
        prefixes = set(self._MUST_LOWER_PREFIXES) | set(self._DIVERGENCE_PREFIXES)
        keywords = set(self._MUST_LOWER_KEYWORDS) | set(self._DIVERGENCE_KEYWORDS)
        for token in self._MUST_STAY_PROBED_PREFIXES:
            self.assertIn(token, prefixes,
                          f"{token!r} stopped being probed by the header-pair test; a one-sided "
                          f"widening on it would now be invisible")
        for token in self._MUST_STAY_PROBED_KEYWORDS:
            self.assertIn(token, keywords,
                          f"{token!r} stopped being probed by the header-pair test; a one-sided "
                          f"widening on it would now be invisible")
        for token in self._MUST_STAY_LOWERED_PREFIXES:
            self.assertIn(token, self._MUST_LOWER_PREFIXES,
                          f"{token!r} stopped being exercised as a header that must lower")
        for token in self._MUST_STAY_LOWERED_KEYWORDS:
            self.assertIn(token, self._MUST_LOWER_KEYWORDS,
                          f"{token!r} stopped being exercised as a header that must lower")
        self.assertTrue(any(p != p.lower() for p in self._MUST_LOWER_PREFIXES)
                        and any(k != k.lower() for k in self._MUST_LOWER_KEYWORDS),
                        "the must-lower vocabulary lost its upper-cased entries, so the patterns' "
                        "case-insensitivity is no longer exercised here")
        for tail in self._MUST_STAY_LOWERED_TAILS:
            self.assertIn(tail, self._HEADER_TAILS,
                          f"{tail!r} stopped being exercised; it appears in no real §5.1 header, "
                          f"so nothing else covers it")
        self.assertGreaterEqual(len(self._MUST_LOWER_NAMES), 2)
        self.assertGreaterEqual(len(self._HEADER_TAILS), 2)

    def test_the_two_procedure_header_patterns_agree(self) -> None:
        # Every MUST-LOWER combination has to be taken by both patterns — that is the bound,
        # stated as a rule rather than as a count — and every divergence probe has to be REFUSED
        # by both. The second half is what gives the divergence vocabulary teeth: an earlier
        # version only rode them through a one-way implication, so emptying those lists hid a
        # one-sided widening entirely.
        for prefix in self._MUST_LOWER_PREFIXES:
            for keyword in self._MUST_LOWER_KEYWORDS:
                for name in self._MUST_LOWER_NAMES:
                    for tail in self._HEADER_TAILS:
                        header = f"{prefix}{keyword} {name}{tail}"
                        self.assertTrue(
                            self._assert_patterns_agree(header),
                            f"a header that must lower does not: {header!r} — either the lowering "
                            f"pattern narrowed, or this vocabulary is stale")

        for prefix in self._DIVERGENCE_PREFIXES:
            for keyword in self._MUST_LOWER_KEYWORDS:
                for name in self._MUST_LOWER_NAMES:
                    self.assertFalse(
                        self._assert_patterns_agree(f"{prefix}{keyword} {name}(a)"),
                        f"{prefix!r} is listed as a token NEITHER pattern accepts, but both now do "
                        f"— move it to _MUST_LOWER_PREFIXES deliberately")
        for keyword in self._DIVERGENCE_KEYWORDS:
            for name in self._MUST_LOWER_NAMES:
                self.assertFalse(self._assert_patterns_agree(f"{keyword} {name}(a)"),
                                 f"{keyword!r} is listed as accepted by neither pattern, but both "
                                 f"now accept it")
        # The real §5.1 headers, as the corpus half: whatever the vocabulary above misses, the
        # published surface still has to satisfy the same agreement.
        for header in _real_section51_block().splitlines():
            self._assert_patterns_agree(header)

    def test_stanza_headers_are_case_insensitive(self) -> None:
        # Fortran keywords and identifiers are case-insensitive, so all four stanza patterns carry
        # `re.IGNORECASE`. Dropping it from any of them left the suite green.
        #
        # The first version of this test asserted only the symbol sets and `errors == []`, which
        # does NOT observe `_IFACE_PROC_END`: with the end line unmatched, the FOLLOWING type
        # header terminates the procedure stanza, so the symbol sets and the error list are
        # unchanged and only the stanza CONTENTS differ. A reviewer caught that the test did not
        # pin the property it is named for. Both halves are asserted now.
        block = ("SUBROUTINE Hx__Foo(a)\n  INTEGER, INTENT(IN) :: a\nEND SUBROUTINE Hx__Foo\n"
                 "TYPE :: Hx__T\n  INTEGER :: a\nEND TYPE Hx__T\n")
        ops, types, _ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ops), ["Hx__Foo"])
        self.assertEqual(sorted(types), ["Hx__T"])
        # The `end` line is NOT part of a procedure stanza (it is part of a type stanza), so an
        # unmatched upper-case `END SUBROUTINE` shows up here as an extra atom.
        self.assertEqual(ops["Hx__Foo"], ["SUBROUTINE Hx__Foo(a)", "INTEGER, INTENT(IN) :: a"])
        self.assertEqual(types["Hx__T"], ["TYPE :: Hx__T", "INTEGER :: a", "END TYPE Hx__T"])

        # ... and with nothing after it to rescue the termination, an upper-cased procedure is
        # reported unterminated: a fail-closed refusal of legal Fortran, the over-rejection
        # direction.
        lone = "PURE SUBROUTINE Hx__Bar(a)\n  REAL, INTENT(IN) :: a\nENDSUBROUTINE Hx__Bar\n"
        ops_lone, _types_lone, _ifaces_lone, errors_lone = parse_interface_stanzas(lone)
        self.assertEqual(errors_lone, [])
        self.assertEqual(ops_lone["Hx__Bar"], ["PURE SUBROUTINE Hx__Bar(a)", "REAL, INTENT(IN) :: a"])

        # The fifth pattern, `_END_STMT_RE`, is reached through the ATOMS rather than through the
        # stanza split: an upper-cased closing line must canonicalize to `end type` like any other,
        # or a type stanza written in capitals compares unequal to the same type written in lower
        # case and the gate refuses correct source. Case-blind here means over-rejection there.
        self.assertEqual(stanza_atoms(types["Hx__T"]), stanza_atoms(
            ["type :: hx__t", "  integer :: a", "end type hx__t"]))

    def test_duplicate_symbol_across_stanza_KINDS_errors(self) -> None:
        # The duplicate check is one shared `seen` set across both kinds, and both duplicate tests
        # used same-kind pairs. Splitting it per kind left the suite green while a symbol declared
        # BOTH as a type and as a procedure passed silently — and the gates merge the two dicts
        # (`{**op_stanzas, **type_stanzas}`), so the procedure stanza disappears from the §5.1 side
        # and goes unpinned. Third branch of the same rule; a reviewer found it had no witness.
        dup = ("type :: hx__x\n  integer :: a\nend type hx__x\n"
               "subroutine hx__x(a)\n  integer, intent(in) :: a\nend subroutine hx__x\n")
        with self.assertRaisesRegex(SignatureParseError, "duplicate signature for symbol 'hx__x'"):
            parse_signatures_from_fortran(dup)

    def test_case_insensitive_lowering_of_declarations_and_parameters(self) -> None:
        # `parse_interface_stanzas` is case-blind by its own patterns; the LOWERING below it has
        # two more (`_INTENT_RE`, `_MODULE_PARAM_RE`) that carry the same rule and that no test
        # observed. Their harms point in opposite directions, so both are asserted here.
        #
        #  * `_INTENT_RE` case-blind => `INTENT(IN)` is an "unsupported declaration attribute"
        #    and a legal upper-cased source is REFUSED.
        #  * `_MODULE_PARAM_RE` case-blind => the module-parameter list comes back EMPTY, which is
        #    fail-open: those entries are the pin that catches a `case_id_len = 64 -> 32` drift.
        struct = parse_signatures_from_fortran(
            "INTEGER, PARAMETER :: DP = REAL64\n"
            "SUBROUTINE Hx__Foo(a)\n  REAL(DP), INTENT(IN) :: a\nEND SUBROUTINE Hx__Foo\n")
        self.assertEqual([mp["name"] for mp in struct["module_parameters"]], ["DP"])
        self.assertEqual(struct["module_parameters"][0]["value"], "float64")
        (proc,) = struct["procedures"]
        self.assertEqual(proc["args"][0]["intent"], "in")

    def test_an_attributed_type_header_opens_a_stanza(self) -> None:
        # `type, public :: t` — an attributed derived-type header — occurs 16 times in the real
        # in-tree corpus and had NO test input, so deleting the attribute group from
        # `_TYPE_HEADER_RE` left the whole suite green. The consequence on a real run is
        # over-rejection, not bypass: the splitter returns no stanza for the header, and
        # `_validate_generated_signatures` then reports a published type as missing
        # from a source that declares it correctly. Found by a witness census; it is the sharpest
        # gap this branch's own sweeps did not reach, because the decision is in code the move
        # relocated without changing.
        block = ("type, public :: hx__t\n  integer :: a\nend type hx__t\n"
                 "type, public, abstract :: hx__u\n  integer :: b\nend type hx__u\n")
        _ops, types, _ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(types), ["hx__t", "hx__u"])

    def test_a_multi_name_type_line_is_not_a_type_header(self) -> None:
        # `_TYPE_HEADER_RE`'s trailing anchor. Removing it survives the suite, and the harm is a
        # false stanza OPEN: `type :: a, b` is not a derived-type definition, but an unanchored
        # pattern reads it as one named `a` and swallows what follows into its stanza. Absent from
        # the corpus; the anchor is cheap to observe, so it is observed rather than recorded.
        _ops, types, _ifaces, errors = parse_interface_stanzas(
            "type :: hx__a, hx__b\n  integer :: x\nend type hx__a\n")
        self.assertEqual(types, {})
        self.assertEqual(errors, [])

    def test_a_component_declaration_is_not_a_type_header(self) -> None:
        # `_TYPE_HEADER_RE` is now the ONE owner of "what a type header is" — the stanza splitter
        # and `_parse_type` share it — so what it accepts is worth an explicit test: a component
        # declaration must not open a stanza and swallow the rest of the enclosing type.
        #
        # MEASURED, and deliberately NOT pinning the pattern's `[^:()]` class: what keeps a
        # component out is the missing comma before `::`, not the paren exclusion. What the paren
        # exclusion actually decides is every attribute list CONTAINING parentheses — `extends(b)`,
        # `bind(c)`, `public, bind(c)` — each of which is a legal type header the pattern silently
        # refuses (no stanza, no error). An earlier version of this comment named `extends` as the
        # only such input, which a reviewer measured false. None of those forms appears in the
        # corpus today; pinning the class here would freeze the refusal, so the ledger records it
        # for the language area instead.
        block = ("type :: hx__outer\n"
                 "  type(hx__inner), allocatable :: parts(:)\n"
                 "  integer :: n\n"
                 "end type hx__outer\n")
        _ops, types, _ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(types), ["hx__outer"])
        self.assertEqual(len(types["hx__outer"]), 4)

    def test_a_line_of_exotic_blanks_produces_no_atom(self) -> None:
        # The scanner and the normalizer disagree about what "blank" means, deliberately: the
        # scanner uses gfortran's blank set (space, tab, form feed) so that a U+00A0 stays CONTENT
        # the way the compiler reads it, while the normalizer erases Python's `\s`, which is
        # wider. A line holding only such a character therefore survives the scan and normalizes
        # to the empty string, and `stanza_atoms` must drop it: an empty atom breaks the ordered
        # stanza comparison.
        #
        # The guard is live — a census labelled it unreachable and a reviewer disproved that by
        # construction — but its consequence is bounded, and that was measured too: gfortran
        # rejects all four characters (`Error: Invalid character in name`), so no source carrying
        # one can be certified. This pins the behaviour, not a wrong-verdict path.
        for blank in ("\xa0", "\v", "\x85", "\u2028"):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(normalize_fortran_line(blank), "")
                self.assertEqual(
                    stanza_atoms(["type :: hx__t", blank, "  integer :: a", "end type hx__t"]),
                    stanza_atoms(["type :: hx__t", "  integer :: a", "end type hx__t"]))

    def test_atoms_fold_a_tab_like_any_other_whitespace(self) -> None:
        # `normalize_fortran_line` erases `\s+`, not just spaces. Narrowing it to ` +` left the
        # suite green while a tab-formatted source (gfortran accepts tabs as a blank) compared
        # unequal to §5.1 — over-rejection.
        self.assertEqual(stanza_atoms(["integer,\tintent(in) :: a"]),
                         stanza_atoms(["integer, intent(in) :: a"]))

    def test_type_missing_end_type_is_unterminated(self) -> None:
        block = (
            "type :: hx__a\n  integer :: x\n"
            "type :: hx__b\n  integer :: y\nend type hx__b\n")
        with self.assertRaisesRegex(SignatureParseError, "unterminated.*hx__a"):
            parse_signatures_from_fortran(block)

    def test_no_space_end_keyword_closes_stanza(self) -> None:
        block = (
            "function hx__a(x) result(s)\n  real, intent(in) :: x\n"
            "  real :: s\nendfunction hx__a\n"
            "function hx__b(y) result(s)\n  real, intent(in) :: y\n"
            "  real :: s\nend function hx__b\n")
        struct = parse_signatures_from_fortran(block)
        self.assertEqual({p["name"] for p in struct["procedures"]}, {"hx__a", "hx__b"})


def _type_lines(block: str, suffix: str) -> list[str]:
    _ops, types, _ifaces, _errs = parse_interface_stanzas(block)
    name = next(n for n in types if n.endswith(suffix))
    return [normalize_fortran_line(ln) for ln in types[name] if normalize_fortran_line(ln)]


class InterfaceBlockStanzaTests(unittest.TestCase):
    """`parse_interface_stanzas` reads an `interface` / `abstract interface` block: a procedure
    header inside one is a PROTOTYPE and lands in the third dict (``iface_stanzas``), never in
    ``op_stanzas`` (issue #266). Before this, a prototype was read as a published procedure —
    measured at `021d6165`: the prototype entered `op_stanzas` with its `import` / `implicit none`
    lines as atoms, and a same-named definition was a `duplicate signature` error.

    What is PINNED: the routing (prototype -> iface dict, not op dict), the two noise lines
    dropped, the generic listing dropped, the shared duplicate check, the unterminated-block
    error, and that an `end interface` outside a block closes nothing. What is SAMPLED: the
    spellings of each construct."""

    _BLOCK = (
        "integer, parameter :: dp = real64\n"
        "abstract interface\n"
        "  subroutine rhs_2d(u, dudt)\n"
        "    import :: dp\n"
        "    implicit none\n"
        "    real(dp), intent(in) :: u(:,:)\n"
        "    real(dp), intent(out) :: dudt(:,:)\n"
        "  end subroutine rhs_2d\n"
        "end interface\n"
        "subroutine hx__advance(u, rhs, u_next)\n"
        "  real(dp), intent(in) :: u(:,:)\n"
        "  procedure(rhs_2d) :: rhs\n"
        "  real(dp), intent(out) :: u_next(:,:)\n"
        "end subroutine hx__advance\n")

    def test_abstract_interface_prototypes_land_in_iface_stanzas_not_ops(self) -> None:
        ops, types, ifaces, errors = parse_interface_stanzas(self._BLOCK)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ops), ["hx__advance"])
        self.assertEqual(types, {})
        self.assertEqual(sorted(ifaces), ["rhs_2d"])
        self.assertEqual(ifaces["rhs_2d"][0], "subroutine rhs_2d(u, dudt)")

    def test_import_and_implicit_none_are_not_atoms_of_a_prototype(self) -> None:
        _ops, _types, ifaces, _errors = parse_interface_stanzas(self._BLOCK)
        atoms = stanza_atoms(ifaces["rhs_2d"])
        self.assertFalse(any(a.startswith(("import", "implicit")) for a in atoms),
                         atoms)
        # ... while the ABI lines are kept (the drop is by line kind, not a truncation).
        self.assertEqual(len(atoms), 3, atoms)
        # And the spellings the leaf may write for the same plumbing: bare `import`, `import, all`,
        # `implicit none (type, external)`.
        for noise in ("import", "import, all", "implicit none (type, external)"):
            with self.subTest(noise=noise):
                block = self._BLOCK.replace("    import :: dp\n", f"    {noise}\n")
                _o, _t, ifaces2, errs = parse_interface_stanzas(block)
                self.assertEqual(errs, [])
                self.assertEqual(stanza_atoms(ifaces2["rhs_2d"]), atoms)

    def test_a_use_statement_inside_a_prototype_is_an_atom(self) -> None:
        # Only the two scope statements are noise; anything else a prototype body carries is
        # ABI-adjacent and stays (a `use` rebinding the kind is exactly what the set-equality
        # pin must see). A round-1 mutant widening the noise pattern to `use` survived.
        block = self._BLOCK.replace("    import :: dp\n",
                                    "    use, intrinsic :: iso_fortran_env, only: dp => real32\n")
        _o, _t, ifaces, errs = parse_interface_stanzas(block)
        self.assertEqual(errs, [])
        self.assertTrue(any(a.startswith("use") for a in stanza_atoms(ifaces["rhs_2d"])),
                        stanza_atoms(ifaces["rhs_2d"]))

    def test_generic_interface_module_procedure_lines_are_dropped(self) -> None:
        block = ("interface hx__gen\n  module procedure hx__real\n  procedure hx__int\n"
                 "end interface hx__gen\n"
                 "subroutine hx__real(a)\n  real, intent(in) :: a\nend subroutine hx__real\n")
        ops, _types, ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(ifaces, {})
        self.assertEqual(sorted(ops), ["hx__real"])

    def test_prototype_sharing_a_defined_name_is_a_duplicate_error(self) -> None:
        # The decoy defence `_validate_generated_signatures` relies on: a prototype of a name the
        # module also DEFINES is refused by the splitter, whichever comes first.
        proto = ("interface\n  subroutine hx__advance(u)\n    real, intent(in) :: u\n"
                 "  end subroutine hx__advance\nend interface\n")
        defn = "subroutine hx__advance(u, v)\n  real, intent(in) :: u, v\nend subroutine hx__advance\n"
        for order, text in (("prototype first", proto + defn), ("definition first", defn + proto)):
            with self.subTest(order=order):
                _ops, _types, _ifaces, errors = parse_interface_stanzas(text)
                self.assertTrue(any("duplicate signature for symbol 'hx__advance'" in e
                                    for e in errors), errors)

    def test_unterminated_interface_block_errors(self) -> None:
        block = "abstract interface\n  subroutine rhs(u)\n    real, intent(in) :: u\n  end subroutine rhs\n"
        _ops, _types, ifaces, errors = parse_interface_stanzas(block)
        self.assertTrue(any("unterminated interface block" in e for e in errors), errors)
        self.assertEqual(sorted(ifaces), ["rhs"])  # the prototype itself was read

    def test_unterminated_prototype_errors_and_the_block_still_closes(self) -> None:
        block = ("abstract interface\n  subroutine rhs(u)\n    real, intent(in) :: u\n"
                 "end interface\n"
                 "subroutine hx__f(a)\n  real, intent(in) :: a\nend subroutine hx__f\n")
        ops, _types, ifaces, errors = parse_interface_stanzas(block)
        self.assertTrue(any("unterminated interface prototype 'rhs'" in e for e in errors), errors)
        self.assertFalse(any("unterminated interface block" in e for e in errors), errors)
        self.assertEqual(sorted(ops), ["hx__f"])  # the procedure after the block is still read
        self.assertEqual(sorted(ifaces), ["rhs"])

    def test_end_interface_does_not_close_a_procedure_stanza(self) -> None:
        # Outside a block, `end interface` is an ordinary non-header line: it neither closes the
        # enclosing procedure stanza nor errors.
        block = ("subroutine hx__f(a)\n  real, intent(in) :: a\n  end interface\n"
                 "  real :: local\nend subroutine hx__f\n")
        ops, _types, ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(ifaces, {})
        self.assertIn("real :: local", ops["hx__f"])

    def test_an_interface_block_inside_a_procedure_body_yields_prototypes(self) -> None:
        # Generated source: an explicit interface for an external, written inside a procedure
        # body. The block terminates the procedure stanza (the declarations before it are kept)
        # and its prototype is a prototype, not a second published procedure.
        block = ("subroutine hx__f(a)\n  real, intent(in) :: a\n"
                 "  interface\n    subroutine ext(x)\n      real, intent(in) :: x\n"
                 "    end subroutine ext\n  end interface\n"
                 "  call ext(a)\nend subroutine hx__f\n")
        ops, _types, ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ops), ["hx__f"])
        self.assertEqual(sorted(ifaces), ["ext"])
        self.assertIn("real, intent(in) :: a", ops["hx__f"])

    def test_a_variable_named_interface_opens_no_block(self) -> None:
        # Keywords are not reserved words: `interface = 3` / `interface(2) = 1` are assignments.
        block = ("subroutine hx__f(a)\n  real, intent(in) :: a\n  interface = 3\n"
                 "  interface(2) = 1\nend subroutine hx__f\n")
        ops, _types, ifaces, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(ifaces, {})
        self.assertIn("interface = 3", ops["hx__f"])

    def test_the_block_opener_spellings(self) -> None:
        # Each opener form the pattern admits, one per subtest; a `module procedure` listing may
        # follow a generic opener and is dropped.
        for opener in ("interface", "abstract interface", "interface hx__gen",
                       "interface operator(+)", "interface assignment(=)", "ABSTRACT INTERFACE"):
            with self.subTest(opener=opener):
                block = (f"{opener}\n  subroutine p(u)\n    real, intent(in) :: u\n"
                         "  end subroutine p\nend interface\n")
                ops, _types, ifaces, errors = parse_interface_stanzas(block)
                self.assertEqual(errors, [])
                self.assertEqual(ops, {})
                self.assertEqual(sorted(ifaces), ["p"])

    def test_normalized_stanza_index_includes_prototypes(self) -> None:
        index = normalized_stanza_index(self._BLOCK)
        self.assertEqual(set(index), {"hx__advance", "rhs_2d"})


class MalformedStructFailClosedTest(unittest.TestCase):
    """A leaf-fabricated malformed signature struct must raise SignatureParseError (clean fail-closed
    the gate turns into a repairable violation), NEVER an uncaught KeyError/TypeError/AttributeError
    that crashes the gate with a Python traceback."""

    def _proc(self) -> dict:
        # a minimal VALID function to mutate into each malformed shape
        return {
            "kind": "function", "name": "hx__f",
            "args": [{"name": "x", "rank": 0, "intent": "in",
                      "spec": {"type": "real", "kind": "dp"}}],
            "result": {"name": "s", "rank": 0,
                       "spec": {"type": "string", "len": "deferred", "alloc": True}},
        }

    def test_valid_baseline_renders(self) -> None:
        render_symbol_to_fortran(self._proc())  # must not raise

    def test_function_null_result_raises(self) -> None:
        p = self._proc(); p["result"] = None
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_derived_spec_missing_name_raises(self) -> None:
        p = self._proc(); p["args"][0]["spec"] = {"type": "derived"}
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_string_spec_missing_len_raises(self) -> None:  # closes F2 fail-open (no silent len=*)
        p = self._proc(); p["args"][0]["spec"] = {"type": "string"}
        with self.assertRaisesRegex(SignatureParseError, "len"):
            render_symbol_to_fortran(p)

    def test_spec_not_a_mapping_raises(self) -> None:
        p = self._proc(); p["args"][0]["spec"] = "real"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_rank_wrong_type_raises(self) -> None:
        p = self._proc(); p["args"][0]["rank"] = "1"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_unknown_entity_key_raises(self) -> None:  # closes F4 (typo silently defaulting)
        p = self._proc(); p["args"][0]["rankk"] = 1
        with self.assertRaisesRegex(SignatureParseError, "unknown key"):
            render_symbol_to_fortran(p)

    def test_bad_intent_value_raises(self) -> None:
        p = self._proc(); p["args"][0]["intent"] = "sideways"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_intent_on_result_raises(self) -> None:
        p = self._proc(); p["result"]["intent"] = "out"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_subroutine_with_result_raises(self) -> None:
        p = self._proc(); p["kind"] = "subroutine"  # keeps a `result` -> illegal
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_module_parameter_missing_value_raises(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp"}], "types": [], "procedures": []})

    def test_whole_struct_non_mapping_procedure_raises(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"procedures": ["not a mapping"], "types": [], "module_parameters": []})

    def test_omitted_args_renders_no_arg_procedure_not_crash(self) -> None:
        # validation tolerates an omitted args list; render must too (no KeyError on proc["args"]).
        out = render_signatures_to_fortran(
            {"module_parameters": [], "types": [],
             "procedures": [{"kind": "subroutine", "name": "hx__f"}]})
        self.assertIn("subroutine hx__f()", out)

    def test_omitted_components_renders_empty_type_not_crash(self) -> None:
        out = render_signatures_to_fortran(
            {"module_parameters": [], "procedures": [],
             "types": [{"name": "hx__opaque"}]})
        self.assertIn("type :: hx__opaque", out)
        self.assertIn("end type hx__opaque", out)


class ExplicitDimsTest(unittest.TestCase):
    """A signature can express a fixed dimension bound (e.g. coef(3)), not only assumed-shape (:)."""

    def test_fixed_dim_round_trips(self) -> None:
        block = ("subroutine hx__g(coef)\n"
                 "  real(dp), intent(in) :: coef(3)\n"
                 "end subroutine hx__g\n")
        struct = parse_signatures_from_fortran(block)
        self.assertEqual(struct["procedures"][0]["args"][0]["dims"], ["3"])
        rendered = render_signatures_to_fortran(struct)
        self.assertEqual(normalized_stanza_index(block), normalized_stanza_index(rendered))

    def test_assumed_shape_carries_no_dims_key(self) -> None:
        block = "subroutine hx__h(a)\n  real(dp), intent(in) :: a(:,:)\nend subroutine hx__h\n"
        arg = parse_signatures_from_fortran(block)["procedures"][0]["args"][0]
        self.assertNotIn("dims", arg)
        self.assertEqual(arg["rank"], 2)

    def test_dims_rank_disagreement_fails_closed(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran({"module_parameters": [], "types": [], "procedures": [
                {"kind": "subroutine", "name": "hx__g", "args": [
                    {"name": "c", "rank": 2, "dims": ["3"],
                     "spec": {"type": "real", "kind": "dp"}}]}]})


class Round2HardeningTest(unittest.TestCase):
    """Second-pass review fixes: bounded rank, empty-type symmetry, identifier/token injection guard,
    boolean parameter value."""

    def _arg(self, **over) -> dict:
        ent = {"name": "x", "rank": 0, "intent": "in", "spec": {"type": "real", "kind": "dp"}}
        ent.update(over)
        return {"kind": "subroutine", "name": "hx__f", "args": [ent]}

    def test_out_of_range_rank_fails_closed_not_oom(self) -> None:
        # An unbounded rank would amplify one int into a multi-GB string; it must fail closed fast.
        with self.assertRaisesRegex(SignatureParseError, "rank"):
            render_symbol_to_fortran(self._arg(rank=500_000_000))
        with self.assertRaisesRegex(SignatureParseError, "rank"):
            render_symbol_to_fortran(self._arg(rank=50))

    def test_empty_derived_type_round_trips(self) -> None:
        # parse and validate must agree: an empty (opaque tag) type is Fortran-legal and was
        # accepted by the pre-B Fortran-fence gate, so it must not false-reject now.
        block = "type :: hx__opaque\nend type hx__opaque\n"
        struct = parse_signatures_from_fortran(block)
        self.assertEqual(struct["types"][0]["components"], [])
        rendered = render_signatures_to_fortran(struct)  # must not raise
        self.assertEqual(normalized_stanza_index(block), normalized_stanza_index(rendered))

    def test_name_with_structural_chars_rejected(self) -> None:
        # A name carrying `end subroutine` / a newline could split into a second stanza.
        with self.assertRaisesRegex(SignatureParseError, "identifier"):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f\nend subroutine hx__f\nsubroutine hx__evil",
                 "args": []})

    def test_dims_token_injection_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(self._arg(rank=1, dims=["3) :: evil ! "]))

    def test_string_len_injection_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(self._arg(spec={"type": "string", "len": "4) :: evil"}))

    def test_boolean_parameter_value_rejected(self) -> None:
        with self.assertRaisesRegex(SignatureParseError, "boolean"):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "value": True}],
                 "types": [], "procedures": []})

    def test_non_integer_parameter_base_rejected(self) -> None:
        # `base` other than integer is silently dropped by the renderer -> fail closed.
        with self.assertRaisesRegex(SignatureParseError, "base"):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "base": "real", "value": "64"}],
                 "types": [], "procedures": []})

    def test_integer_alloc_flag_rejected(self) -> None:
        # `alloc: 1` (1 == True) must not slip the boolean check and render by truthiness.
        with self.assertRaisesRegex(SignatureParseError, "alloc"):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f",
                 "args": [{"name": "x", "spec": {"type": "string", "len": "deferred", "alloc": 1}}]})

    def test_integer_parameter_value_accepted(self) -> None:
        render_signatures_to_fortran(
            {"module_parameters": [{"name": "case_id_len", "value": 64}],
             "types": [], "procedures": []})  # must not raise

    def test_fortran_expression_parameter_value_rejected(self) -> None:
        # The Fortran-expression pass-through was removed: a module-parameter value has a neutral
        # form only as a number or the float64/float32 kind tokens, so the portable-kind idiom
        # `selected_real_kind(15, 307)` now fails closed on parse (no neutral form).
        block = "integer, parameter :: dp = selected_real_kind(15, 307)\n"
        with self.assertRaisesRegex(SignatureParseError, "no neutral form"):
            parse_signatures_from_fortran(block)

    def test_parameter_value_with_double_colon_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "value": "real64 :: evil"}],
                 "types": [], "procedures": []})

    def test_mixed_type_unknown_keys_fails_closed_not_crash(self) -> None:
        # A YAML mapping mixing an int key with string keys must not crash `sorted(...)` on the
        # unknown-key path; it must fail closed.
        with self.assertRaisesRegex(SignatureParseError, "unknown key"):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f",
                 "args": [{"name": "x", 1: "z", "q": "z", "spec": {"type": "real", "kind": "dp"}}]})

    def test_dims_comma_injection_rejected(self) -> None:
        # `dims: ['3,4']` is one entry (rank passes) but would render the rank-2 `(3,4)`.
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f",
                 "args": [{"name": "a", "rank": 1, "dims": ["3,4"],
                           "spec": {"type": "real", "kind": "dp"}}]})

    def test_parameter_value_semicolon_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "value": "real64; integer evil"}],
                 "types": [], "procedures": []})

    def test_parameter_value_character_literal_rejected(self) -> None:
        # `iachar('A')` and `iachar('a')` are different integers, but the value pin folds case and
        # whitespace — so a character literal in a module-parameter value would let ABI drift pass.
        # Fail closed at the grammar for both quote forms.
        for bad in ("iachar('A')", 'iachar("A")', "'a b'"):
            with self.assertRaises(SignatureParseError):
                render_signatures_to_fortran(
                    {"module_parameters": [{"name": "wp", "value": bad}],
                     "types": [], "procedures": []})

    def test_present_null_top_key_fails_closed(self) -> None:
        # `module_parameters: null` (present but null) must fail closed — silently emptying it
        # would drop the dp/case_id_len value pins and let a drifted parameter pass.
        struct, err = load_structured_signatures(
            "module_parameters: null\ntypes: []\nprocedures: []\n")
        self.assertIsNotNone(err)
        self.assertIn("must be a list", err)

    def test_absent_top_key_is_empty(self) -> None:
        # An ABSENT key legitimately means that category is empty (a pruned §5.1 may omit types).
        struct, err = load_structured_signatures(
            "procedures:\n- {kind: subroutine, name: hx__f, args: []}\n")
        self.assertIsNone(err)
        self.assertEqual(struct["types"], [])
        self.assertEqual(struct["module_parameters"], [])

    def test_implicit_result_function_round_trips(self) -> None:
        # `function f(x)` (no result clause) has the function NAME as its result variable; rendering
        # `result(f)` would be invalid Fortran (result name must differ from the function name).
        block = ("function hx__f(x)\n"
                 "  real(dp), intent(in) :: x\n"
                 "  real(dp) :: hx__f\n"
                 "end function hx__f\n")
        struct = parse_signatures_from_fortran(block)
        rendered = render_signatures_to_fortran(struct)
        self.assertNotIn("result(", rendered)
        self.assertEqual(normalized_stanza_index(block), normalized_stanza_index(rendered))

    def test_unsupported_declaration_attribute_rejected(self) -> None:
        # `dimension(:)` / `optional` / `pointer` / `value` are not modeled; silently dropping them
        # would change the ABI (a `dimension(:)` arg parsed as a scalar).
        for decl in ("real, dimension(:), intent(in) :: x", "real, optional, intent(in) :: x",
                     "real, pointer :: x", "real, value :: x"):
            with self.assertRaisesRegex(SignatureParseError, "unsupported declaration attribute"):
                parse_signatures_from_fortran(
                    f"subroutine hx__g(x)\n  {decl}\nend subroutine hx__g\n")

    def test_unhashable_type_value_fails_closed_not_crash(self) -> None:
        # `type: []` / `type: {}` is unhashable; a raw `not in frozenset` would TypeError and escape
        # the callers' `except SignatureParseError`, crashing the gate instead of failing closed.
        for bad_type in ([], {}, 3):
            with self.assertRaisesRegex(SignatureParseError, "spec.type"):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "spec": {"type": bad_type}}]})

    def test_unhashable_intent_value_fails_closed_not_crash(self) -> None:
        for bad_intent in ([], {}):
            with self.assertRaisesRegex(SignatureParseError, "intent"):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "intent": bad_intent,
                               "spec": {"type": "real", "kind": "dp"}}]})

    def test_inapplicable_type_field_fails_closed(self) -> None:
        # A field the renderer drops for this type would let §5.1 and the IR differ yet render equal.
        cases = [
            {"type": "real", "len": "case_id_len"},   # len ignored on real
            {"type": "real", "name": "foo"},          # name ignored on real
            {"type": "string", "len": ":", "kind": "dp"},   # kind ignored on string
            {"type": "string", "len": ":", "name": "foo"},  # name ignored on string
            {"type": "derived", "name": "hx__t", "len": ":"},  # len ignored on derived
        ]
        for spec in cases:
            with self.assertRaisesRegex(SignatureParseError, "not applicable"):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "spec": spec}]})

    def test_full_form_spec_with_none_inapplicable_fields_accepted(self) -> None:
        # The full-form struct parse_signatures_from_fortran emits carries kind/len/name=None for
        # inapplicable fields; None must NOT trip the inapplicable-field guard.
        render_symbol_to_fortran(
            {"kind": "subroutine", "name": "hx__f", "args": [
                {"name": "x", "rank": 0, "intent": "in",
                 "spec": {"type": "real", "kind": "dp", "len": None, "name": None, "alloc": False}}]})


class ProcedureTypedArgumentTest(unittest.TestCase):
    """The neutral vocabulary's `interfaces:` section and `{type: procedure, interface: <name>}`
    argument spec (issue #266). The correctness contract is the round-trip
    (`parse(render(x)) == x` over the normalized stanza index) and a fail-closed matrix that
    raises `SignatureParseError` — never a `KeyError` — for every malformed shape.

    PINNED: the round-trip through an abstract-interface block; each refusal below by its
    message; that every member of `_VALID_SPEC_TYPES` has an inapplicable-field row (the
    `KeyError` mutant of PR-1). SAMPLED: the argument spellings of each malformed shape."""

    def _struct(self) -> dict:
        real = {"type": "real", "kind": "dp"}
        return {
            "module_parameters": [{"name": "dp", "base": "integer", "value": "float64"}],
            "types": [],
            "interfaces": [{
                "kind": "subroutine", "name": "rhs_2d",
                "args": [{"name": "u", "rank": 2, "intent": "in", "spec": dict(real)},
                         {"name": "dudt", "rank": 2, "intent": "out", "spec": dict(real)}],
                "result": None}],
            "procedures": [{
                "kind": "subroutine", "name": "hx__advance",
                "args": [{"name": "u", "rank": 2, "intent": "in", "spec": dict(real)},
                         {"name": "rhs", "spec": {"type": "procedure", "interface": "rhs_2d"}},
                         {"name": "dt", "rank": 0, "intent": "in", "spec": dict(real)},
                         {"name": "u_next", "rank": 2, "intent": "out", "spec": dict(real)}],
                "result": None}],
        }

    def _assert_raises(self, struct: dict, pattern: str) -> None:
        with self.assertRaisesRegex(SignatureParseError, pattern):
            render_signatures_to_fortran(struct)

    def test_procedure_arg_round_trips(self) -> None:
        rendered = render_signatures_to_fortran(self._struct())
        self.assertIn("abstract interface", rendered)
        self.assertIn("procedure(rhs_2d) :: rhs", rendered)
        self.assertNotIn("import", rendered)          # the canonical form carries no plumbing
        self.assertNotIn("implicit none", rendered)
        parsed = parse_signatures_from_fortran(rendered)
        self.assertEqual(normalized_stanza_index(rendered),
                         normalized_stanza_index(render_signatures_to_fortran(parsed)))
        self.assertEqual([i["name"] for i in parsed["interfaces"]], ["rhs_2d"])
        rhs = parsed["procedures"][0]["args"][1]
        self.assertEqual(rhs["spec"]["type"], "procedure")
        self.assertEqual(rhs["spec"]["interface"], "rhs_2d")
        self.assertEqual((rhs["rank"], rhs["intent"]), (0, None))
        # stable under a second parse (the full-form struct is a fixed point)
        self.assertEqual(parsed, parse_signatures_from_fortran(render_signatures_to_fortran(parsed)))

    def test_render_interface_to_fortran_yields_one_prototype_stanza(self) -> None:
        text = render_interface_to_fortran(self._struct()["interfaces"][0])
        ops, types, ifaces, errors = parse_interface_stanzas(text)
        self.assertEqual((errors, ops, types), ([], {}, {}))
        self.assertEqual(sorted(ifaces), ["rhs_2d"])

    def test_render_interface_to_fortran_fails_closed_on_a_malformed_entry(self) -> None:
        # The single-entry renderer validates before it renders (a round-1 mutant deleting that
        # call survived): a malformed entry is a SignatureParseError, never a KeyError, and a
        # nested procedure-typed argument is refused there too.
        with self.assertRaisesRegex(SignatureParseError, "kind must be"):
            render_interface_to_fortran({"name": "rhs", "args": []})
        with self.assertRaisesRegex(SignatureParseError, "allowed only for a procedure's argument"):
            render_interface_to_fortran({"kind": "subroutine", "name": "rhs", "args": [
                {"name": "f", "spec": {"type": "procedure", "interface": "g"}}]})

    def test_intent_on_procedure_arg_fails_closed(self) -> None:
        s = self._struct(); s["procedures"][0]["args"][1]["intent"] = "in"
        self._assert_raises(s, "intent is not applicable to a procedure-typed argument")
        with self.assertRaisesRegex(SignatureParseError, "intent is not applicable"):
            parse_signatures_from_fortran(
                render_signatures_to_fortran(self._struct()).replace(
                    "procedure(rhs_2d) :: rhs", "procedure(rhs_2d), intent(in) :: rhs"))

    def test_dims_or_rank_on_procedure_arg_fails_closed(self) -> None:
        s = self._struct(); s["procedures"][0]["args"][1]["rank"] = 1
        self._assert_raises(s, "rank must be 0")
        s = self._struct(); s["procedures"][0]["args"][1]["dims"] = ["3"]
        self._assert_raises(s, "dims is not applicable to a procedure-typed argument")

    def test_unknown_interface_reference_fails_closed(self) -> None:
        s = self._struct(); s["procedures"][0]["args"][1]["spec"]["interface"] = "nope"
        self._assert_raises(s, "does not name an entry of interfaces")

    def test_unreferenced_interface_fails_closed(self) -> None:
        s = self._struct()
        s["interfaces"].append({"kind": "subroutine", "name": "unused_iface", "args": [], "result": None})
        self._assert_raises(s, r"\['unused_iface'\] are referenced by no procedure argument")

    def test_interface_name_colliding_with_a_published_name_fails_closed(self) -> None:
        for collide_with in ("hx__advance", "dp", "HX__ADVANCE"):  # case-insensitive
            with self.subTest(collide_with=collide_with):
                s = self._struct()
                s["interfaces"][0]["name"] = collide_with
                s["procedures"][0]["args"][1]["spec"]["interface"] = collide_with
                self._assert_raises(s, "collides with another published name")
        s = self._struct(); s["types"] = [{"name": "rhs_2d", "components": []}]
        self._assert_raises(s, "collides with another published name")
        s = self._struct(); s["interfaces"].append(dict(s["interfaces"][0]))
        self._assert_raises(s, "collides with another published name")

    def test_procedure_type_in_component_or_result_fails_closed(self) -> None:
        proc_spec = {"type": "procedure", "interface": "rhs_2d"}
        s = self._struct(); s["types"] = [{"name": "hx__t", "components": [{"name": "f", "spec": proc_spec}]}]
        self._assert_raises(s, "allowed only for a procedure's argument")
        s = self._struct()
        s["procedures"][0] = {"kind": "function", "name": "hx__g", "args": [],
                              "result": {"name": "r", "spec": proc_spec}}
        self._assert_raises(s, "allowed only for a procedure's argument")

    def test_nested_procedure_arg_in_interface_fails_closed(self) -> None:
        s = self._struct()
        s["interfaces"][0]["args"].append({"name": "inner", "spec": {"type": "procedure", "interface": "rhs_2d"}})
        self._assert_raises(s, r"interfaces\[0\].args\[2\].spec.type 'procedure' is allowed only")

    def test_inapplicable_fields_on_procedure_spec_fail_closed(self) -> None:
        # The `_inapplicable` matrix extended: kind/len/name/alloc on `procedure`, and
        # `interface` on every other type.
        for field, value in (("kind", "dp"), ("len", "4"), ("name", "t"), ("alloc", True)):
            with self.subTest(field=field):
                s = self._struct(); s["procedures"][0]["args"][1]["spec"][field] = value
                self._assert_raises(s, f"spec.{field} is not applicable to type 'procedure'")
        for other in ("real", "integer", "logical", "string", "derived"):
            with self.subTest(other=other):
                spec = {"type": other, "interface": "rhs_2d"}
                spec.update({"string": {"len": "4"}, "derived": {"name": "t"}}.get(other, {}))
                with self.assertRaisesRegex(SignatureParseError,
                                            f"spec.interface is not applicable to type '{other}'"):
                    render_symbol_to_fortran({"kind": "subroutine", "name": "hx__f",
                                              "args": [{"name": "x", "spec": spec}]})

    def test_missing_interface_name_fails_closed(self) -> None:
        s = self._struct(); del s["procedures"][0]["args"][1]["spec"]["interface"]
        self._assert_raises(s, "spec.interface .*is required")

    def test_every_spec_type_has_an_inapplicable_row(self) -> None:
        # `_validate_spec` indexes a dict by `type`; a member of `_VALID_SPEC_TYPES` with no row
        # would escape as a `KeyError` — a gate crash, not a fail-closed violation. Pinned two
        # ways: set identity of the table's keys, and the crash itself when a row is removed.
        self.assertEqual(set(structured_signatures.INAPPLICABLE_SPEC_FIELDS),
                         set(structured_signatures.VALID_SPEC_TYPES))
        for t in sorted(structured_signatures.VALID_SPEC_TYPES):
            with self.subTest(type=t):
                spec = {"type": t}
                spec.update({"string": {"len": "4"}, "derived": {"name": "t"},
                             "procedure": {"interface": "i"}}.get(t, {}))
                render_symbol_to_fortran({"kind": "subroutine", "name": "hx__f",
                                          "args": [{"name": "x", "spec": spec}]})  # accepted
                table = dict(structured_signatures.INAPPLICABLE_SPEC_FIELDS); del table[t]
                with mock.patch.object(structured_signatures, "INAPPLICABLE_SPEC_FIELDS", table), \
                        self.assertRaises(KeyError):  # the shape the identity assertion forbids
                    render_symbol_to_fortran({"kind": "subroutine", "name": "hx__f",
                                              "args": [{"name": "x", "spec": spec}]})

    def test_unsupported_procedure_type_specs_fail_closed(self) -> None:
        # Fortran admits more than the neutral form models: an implicit interface, an intrinsic
        # type-spec. Each fails closed rather than parsing to a named prototype.
        for decl in ("procedure() :: rhs", "procedure :: rhs", "procedure(real) :: rhs"):
            with self.subTest(decl=decl), \
                    self.assertRaisesRegex(SignatureParseError, "unsupported procedure type-spec"):
                parse_signatures_from_fortran(
                    f"subroutine hx__g(rhs)\n  {decl}\nend subroutine hx__g\n")

    def test_loader_accepts_the_interfaces_key(self) -> None:
        struct, err = load_structured_signatures(
            "interfaces:\n  - kind: subroutine\n    name: rhs\n    args: []\n"
            "procedures:\n  - kind: subroutine\n    name: hx__f\n"
            "    args:\n      - name: r\n        spec: {type: procedure, interface: rhs}\n")
        self.assertIsNone(err)
        self.assertEqual([i["name"] for i in struct["interfaces"]], ["rhs"])
        render_signatures_to_fortran(struct)  # and the loaded struct renders
        _s, err = load_structured_signatures("interfaces:\n")
        self.assertIn("'interfaces' must be a list", err or "")
        # an absent key loads as the empty list (the struct always carries all four keys) ...
        struct, err = load_structured_signatures("procedures: []\n")
        self.assertIsNone(err)
        self.assertEqual(struct["interfaces"], [])
        # ... and the not-a-mapping refusal names the key among the allowed ones
        _s, err = load_structured_signatures("- 1\n")
        self.assertIn("module_parameters / types / interfaces / procedures", err or "")


class NameCollisionTest(unittest.TestCase):
    """A §5.1 struct that reuses a name inside one Fortran scope fails closed at render.

    Issue #278.

    Fortran identifiers are case-insensitive, so ``f`` and ``F`` are one name. Issue #265 PR-5
    drafted a TC4 forcing component publishing a Coriolis ``f`` (in) beside a forcing ``F`` (out);
    ``_validate_struct`` accepted it, and the first thing that would have refused it is the
    compiler, after a billed Compile and Generate. Each row below is one ``claim`` site, and each
    shape was confirmed refused by ``gfortran -fsyntax-only -std=f2008`` (11.4.0) before the rule
    was written, beside a control with distinct names that it accepts."""

    def _real(self) -> dict:
        return {"type": "real", "kind": "dp"}

    def _arg(self, name: str, intent: str = "in") -> dict:
        return {"name": name, "spec": self._real(), "intent": intent}

    def _sub(self, name: str, *args: str) -> dict:
        return {"kind": "subroutine", "name": name, "args": [self._arg(a) for a in args]}

    def _fn(self, name: str, result: str, *args: str) -> dict:
        return {"kind": "function", "name": name, "args": [self._arg(a) for a in args],
                "result": {"name": result, "spec": self._real()}}

    def _refused(self, struct: dict, pattern: str) -> None:
        with self.assertRaisesRegex(SignatureParseError, pattern):
            render_signatures_to_fortran(struct)

    def test_two_dummies_differing_only_in_case_fail_closed(self) -> None:
        for second in ("F", "f"):
            with self.subTest(second=second):
                self._refused({"procedures": [self._sub("hx__p", "f", second)]},
                              rf"args\[1\]\.name '{second}' collides with .*args\[0\]\.name 'f'")

    def test_a_dummy_named_after_its_procedure_fails_closed(self) -> None:
        self._refused({"procedures": [self._sub("hx__p", "HX__P")]},
                      r"args\[0\]\.name 'HX__P' collides with .*procedures\[0\]\.name")

    def test_a_result_named_after_a_dummy_fails_closed(self) -> None:
        self._refused({"procedures": [self._fn("hx__g", "R", "r")]},
                      r"result\.name 'R' collides with .*args\[0\]\.name 'r'")

    def test_a_result_differing_from_its_function_only_in_case_fails_closed(self) -> None:
        # The EXACT spelling is the implicit result and renders without a `result(...)` clause;
        # another case renders `result(G)`, which gfortran refuses ("RESULT variable at (1) must
        # be different than function name").
        self._refused({"procedures": [self._fn("hx__g", "HX__G", "x")]},
                      r"result\.name 'HX__G' collides with .*procedures\[0\]\.name")
        rendered = render_signatures_to_fortran({"procedures": [self._fn("hx__g", "hx__g", "x")]})
        self.assertIn("function hx__g(x)\n", rendered)

    def test_two_components_differing_only_in_case_fail_closed(self) -> None:
        struct = {"types": [{"name": "hx__t", "components": [
            {"name": "a", "spec": self._real()}, {"name": "A", "spec": self._real()}]}]}
        self._refused(struct, r"components\[1\]\.name 'A' collides with .*components\[0\]")

    def test_two_published_names_of_any_kind_collide_case_insensitively(self) -> None:
        rows = {
            "procedure/procedure": {"procedures": [self._sub("hx__p"), self._sub("HX__P")]},
            "type/procedure": {"types": [{"name": "hx__t", "components": []}],
                               "procedures": [self._sub("HX__T")]},
            "parameter/procedure": {"module_parameters": [{"name": "dp", "value": "float64"}],
                                    "procedures": [self._sub("DP")]},
            "parameter/type": {"module_parameters": [{"name": "dp", "value": "float64"}],
                               "types": [{"name": "Dp", "components": []}]},
        }
        for label, struct in rows.items():
            with self.subTest(label):
                self._refused(struct, r"collides with another published name, .* in the module")

    def test_an_interface_prototype_is_its_own_scope_too(self) -> None:
        proto = self._sub("hx__rhs", "q", "Q")
        caller = {"kind": "subroutine", "name": "hx__step", "args": [
            {"name": "rhs", "spec": {"type": "procedure", "interface": "hx__rhs"}}]}
        self._refused({"interfaces": [proto], "procedures": [caller]},
                      r"interfaces\[0\]\.args\[1\]\.name 'Q' collides")

    def test_distinct_names_render(self) -> None:
        struct = {"module_parameters": [{"name": "dp", "value": "float64"}],
                  "types": [{"name": "hx__t", "components": [{"name": "a", "spec": self._real()}]}],
                  "procedures": [self._sub("hx__p", "f", "g"), self._fn("hx__g", "r", "x")]}
        self.assertIn("subroutine hx__p(f, g)", render_signatures_to_fortran(struct))

    def test_every_in_tree_section51_renders(self) -> None:
        # The collision rule reaches a spec at Compile.static, after a billed Compile leaf. This
        # row reaches it in the suite, before any run: every §5.1 in the tree must still parse.
        # A `component` / `infrastructure` spec MUST carry one (its published surface is pinned
        # from it), so a missing or broken fence there is a failure rather than a skip; the other
        # kinds carry none.
        from tools.validate_pipeline_semantics import (
            _parse_canonical_interface_from_controlled_spec,
            _section51_fence_body,
        )

        publishing = ("component", "infrastructure")
        seen = []
        for cs in sorted((REPO_ROOT / "spec").rglob("controlled_spec.md")):
            rel = cs.relative_to(REPO_ROOT)
            body, err = _section51_fence_body(cs)
            if rel.parts[1] not in publishing:
                continue
            self.assertIsNone(err, f"{rel}: {err}")
            self.assertIsNotNone(body, str(rel))
            seen.append(cs)
            *_stanzas, parse_err = _parse_canonical_interface_from_controlled_spec(
                cs, fortran_signatures)
            self.assertIsNone(parse_err, f"{rel}: {parse_err}")
        self.assertIn(HARNESS_SPEC, seen, "the sweep found no §5.1 — its reader has drifted")


class NeutralVocabularyTest(unittest.TestCase):
    """C2: the §5.1 / IR leaf vocabulary is language-neutral — string lengths are
    `deferred`/`assumed` (not the Fortran `:`/`*`) and kind values are `float64`/`float32`
    (not `real64`/`real32`). The old Fortran tokens fail closed; the neutral tokens render to
    their Fortran spelling. Driven by the REAL harness §5.1 to avoid fixture fiction."""

    def test_real_section51_struct_carries_only_neutral_tokens(self) -> None:
        # (#1/#4) The real §5.1 loads to a struct whose string lengths are neutral tokens and
        # whose module-parameter values are neutral — never a raw Fortran `:`/`*`/`real64`.
        struct = _real_section51_struct()
        lens = [
            c["spec"].get("len")
            for t in struct["types"] for c in t["components"]
            if c["spec"].get("type") == "string"
        ]
        lens += [
            e["spec"].get("len")
            for p in struct["procedures"]
            for e in [*p.get("args", []), *([p["result"]] if p.get("result") else [])]
            if e["spec"].get("type") == "string"
        ]
        self.assertIn("deferred", lens)
        self.assertIn("assumed", lens)
        self.assertNotIn(":", lens)
        self.assertNotIn("*", lens)
        values = {mp["name"]: mp["value"] for mp in struct["module_parameters"]}
        self.assertEqual(str(values["dp"]).lower(), "float64")
        self.assertNotIn("real64", [str(v).lower() for v in values.values()])

    def test_old_fortran_len_token_fails_closed_with_neutral_alternative(self) -> None:
        # (#2) `len: ':'` / `len: '*'` in a neutral struct fail closed, naming the neutral token.
        for bad, alt in ((":", "deferred"), ("*", "assumed")):
            with self.assertRaisesRegex(SignatureParseError, alt):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "spec": {"type": "string", "len": bad}}]})

    def test_old_fortran_kind_value_fails_closed_with_neutral_alternative(self) -> None:
        # (#2) `value: real64` / `real32` fail closed, naming `float64` / `float32`.
        for bad, alt in (("real64", "float64"), ("real32", "float32")):
            with self.assertRaisesRegex(SignatureParseError, alt):
                render_signatures_to_fortran(
                    {"module_parameters": [{"name": "dp", "value": bad}],
                     "types": [], "procedures": []})

    def test_module_parameter_render_matches_the_renderer_pin(self) -> None:
        # (#3) The neutral `dp = float64` lowers to the exact Fortran the renderer pin expects.
        line = render_module_parameter_to_fortran({"name": "dp", "value": "float64"})
        self.assertEqual(line, "integer, parameter :: dp = real64")
        self.assertEqual(line, _HARNESS_V3_PARAMETERS[0])
        self.assertEqual(
            render_module_parameter_to_fortran({"name": "float32_kind", "value": "float32"}),
            "integer, parameter :: float32_kind = real32")
        self.assertEqual(
            render_module_parameter_to_fortran({"name": "case_id_len", "value": 64}),
            "integer, parameter :: case_id_len = 64")

    def test_neutral_len_tokens_render_to_fortran(self) -> None:
        # `deferred` -> `character(len=:)`, `assumed` -> `character(len=*)`.
        out = render_symbol_to_fortran(
            {"kind": "subroutine", "name": "hx__f", "args": [
                {"name": "a", "intent": "in", "spec": {"type": "string", "len": "deferred"}},
                {"name": "b", "intent": "in", "spec": {"type": "string", "len": "assumed"}}]})
        self.assertIn("character(len=:), intent(in) :: a", out)
        self.assertIn("character(len=*), intent(in) :: b", out)
        self.assertNotIn("deferred", out)
        self.assertNotIn("assumed", out)

    def test_controlled_spec_type_list_matches_the_backend(self) -> None:
        # docs/CONTROLLED_SPEC.md §5.1 enumerates the neutral `type` vocabulary in prose, and the
        # backend defines it (`_VALID_SPEC_TYPES`). Coupled element by element AT the statement
        # position: the enumeration is read from the parenthesis that follows the anchor text
        # "neutral `type` (" inside the "**5.1 Canonical interface block**" subsection — the
        # anchor precedes the list and is byte-identical in every wording, so a token added to
        # the code or dropped from the document reddens this row (witnessed by removing one
        # token from the document). The count comes from the code, not the prose.
        md = CONTROLLED_SPEC_DOC.read_text(encoding="utf-8")
        section = md.split("**5.1 Canonical interface block**", 1)[1].split("\n6. ", 1)[0]
        m = re.search(r"neutral `type` \(([^)]*)\)", section)
        self.assertIsNotNone(m, "§5.1 no longer states the neutral `type` enumeration")
        tokens = re.findall(r"`([a-z_]+)`", m.group(1))
        self.assertEqual(set(tokens), set(structured_signatures.VALID_SPEC_TYPES))  # names the token
        self.assertEqual(len(tokens), len(structured_signatures.VALID_SPEC_TYPES))  # no repeat

    def test_real_section51_fence_text_has_no_fortran_tokens(self) -> None:
        # (#5) A hand-edit that reintroduces a Fortran token into the real §5.1 fence is caught:
        # the fenced block text must carry no `len: ':'` / `len: '*'` / `value: real64`.
        md = HARNESS_SPEC.read_text(encoding="utf-8")
        fence = md.split("### 5.1", 1)[1]
        m = _FENCED_BLOCK_RE.search(fence)
        assert m, "harness §5.1 fenced block not found"
        block = m.group(1)
        self.assertNotIn("len: ':'", block)
        self.assertNotIn("len: '*'", block)
        self.assertNotIn("value: real64", block)
        self.assertIn("len: deferred", block)
        self.assertIn("len: assumed", block)
        self.assertIn("value: float64", block)


class SharedSplitterMigrationTests(unittest.TestCase):
    """`_split_paren_aware` delegates to `fortran_lines.split_top_level_commas`.

    It used to reach into the validator for a private, quote-BLIND copy. A derived-type
    component with a character initializer then split inside the literal and the §5.1 lowering
    failed closed on legal Fortran — `gfortran -std=f2008` accepts the type below."""

    def test_a_comma_in_a_component_initializer_does_not_break_the_lowering(self) -> None:
        struct = parse_signatures_from_fortran(
            "type :: cfg_t\n"
            "  character(len=8) :: sep = ','\n"
            "  real :: g\n"
            "end type cfg_t\n"
        )
        names = [c["name"] for t in struct["types"] for c in t["components"]]
        self.assertEqual(names, ["sep", "g"])



class NoImportCycleWithTheValidatorTest(unittest.TestCase):
    """No backend module the validator imports may import it back — at module level or lazily.

    Two reasons, and they cover different spellings, so both are stated:

    * a MODULE-LEVEL back-import is a cycle. `validate_pipeline_semantics` imports these modules at
      module level, so importing it back closes one. This half is mechanical.
    * a LAZY back-import is not a cycle — a function-local import is the standard way to break one,
      and it executes fine. What forbids it here is narrower than the boundary rule and narrower
      than "no cycles": these modules had their OWN SUBJECT MATTER sitting in the neutral core and
      the whole point of the move was that they no longer need anything from it. A renewed
      dependency IS the regression, whatever its spelling.

    Stating that precisely matters because the boundary rule does NOT forbid this direction:
    `docs/BACKEND_BOUNDARY.md` says a backend MAY import the neutral core, and a sibling in this
    very package does (`structure_differential.py:66`). The first version of this test asserted the
    boundary rule and would have refused a boundary-legal import — the over-rejection direction.
    The second asserted the cycle for both spellings, which is false for the lazy one.

    What it guards against is the state these modules were in until the §5.1 line and stanza layer
    moved here: `signatures` imported `_fortran_logical_lines`, `_normalize_fortran_line` and
    `_parse_interface_stanzas` out of the validator, so the Fortran backend reached into the
    neutral core for its own subject matter (TODO.md's blocking sub-item). Nothing observed that:
    the import worked and the suite was green.

    FOUR checks, because no subset of them covers the property:

    * the SOURCE check reads every import in every scanned module, at any nesting depth, in every
      spelling the reader knows — `import`, `from ... import` (module AND alias), relative, and a
      literal `importlib.import_module` / `__import__`. The subprocess probe cannot see a lazy one.
    * WHICH modules are scanned is DISCOVERED by walking `tools/backends/`, not enumerated. Four
      review rounds each found a different file an enumerated version had missed — `structure.py`,
      `registry.py`, and both package `__init__` modules — which is what an enumeration standing in
      for a rule looks like. The only hand-maintained part left is the inverse: the modules that
      legitimately depend on the neutral core, which is a list of exceptions a reader can audit.
    * the EXCEPTION check holds that list to its justification, so an entry cannot outlive its
      reason and go on exempting whatever the module grows next.
    * the IMPORT check runs a fresh interpreter, so a module-level import — including one reached
      transitively through a sibling — is an executed fact rather than a reading of the source.
      In-process `sys.modules` says nothing: most of this test run imports the validator anyway.

    A COMPUTED module name (`importlib.import_module(name_from_a_variable)`) is out of reach of the
    source check and invisible to the probe when lazy. It is recorded as a limit here rather than
    implied to be covered, the same way `tools/tests/test_backend_boundary.py` records it.
    """

    #: The modules whose SUBJECT MATTER moved here. A renewed dependency on the neutral core from
    #: these is the regression the move removed, whatever its spelling.
    _SUBJECT_MATTER_ROOTS = (
        "tools/backends/language/fortran/signatures.py",
        "tools/backends/language/fortran/lines.py",
    )

    @classmethod
    def _rel_paths_for(cls, dotted: str, root: Path) -> list[str]:
        """The files importing `dotted` executes: the module (or package) and its parent packages."""
        parts = dotted.split(".")
        out: list[str] = []
        for i in range(1, len(parts) + 1):
            stem = "/".join(parts[:i])
            for rel in (f"{stem}.py", f"{stem}/__init__.py"):
                if (root / rel).is_file():
                    out.append(rel)
        return out

    @staticmethod
    def _absolute_name(name: str, importer_rel: str) -> str:
        """Resolve a written import name against the module importing it.

        A relative import arrives here as its WRITTEN form (`.keywords`, `..registry`), which no
        prefix test on `tools.backends` can match — so the closure never followed the edge, and a
        helper split out of `signatures.py` and imported as `from . import keywords` could reach
        the validator with the whole suite green. That was the sixth spelling to break this check
        and the first one the closure itself introduced.
        """
        level = len(name) - len(name.lstrip("."))
        if not level:
            return name
        package = importer_rel[: -len(".py")].split("/")
        if package[-1] == "__init__":
            package = package[:-1]
        else:
            package = package[:-1]
        base = package[: len(package) - (level - 1)] if level > 1 else package
        tail = name.lstrip(".")
        return ".".join(base + ([tail] if tail else []))

    @classmethod
    def _modules_on_the_cycle(cls, root: Path | None = None,
                              subject_matter_roots: tuple[str, ...] | None = None) -> list[str]:
        """The backend modules that must not import the validator — COMPUTED, not listed.

        Two kinds of root, for the two reasons the class docstring gives:

        * the subject-matter roots;
        * every backend module the validator imports, since importing it back is a cycle.

        Then everything reachable from those roots by imports WITHIN `tools/backends`, because a
        dependency routed through a sibling is the same dependency — and every parent package on
        the way, since importing `a.b.c` executes `a/__init__.py` too. Relative imports are
        resolved against the importing module first.

        What this deliberately does NOT cover is a backend module no root reaches:
        `structure_differential.py` (a developer harness) imports the validator today and is
        allowed to, and so would a future `build_system/make/` module —
        `docs/BACKEND_BOUNDARY.md` permits backend -> neutral core. An earlier version walked every
        file under `tools/backends/` and refused exactly that.

        RECORDED LIMITS, because a check that overstates itself is worse than one that does not:
        a dependency routed through a NEUTRAL module (backend -> `tools/some_shim.py` -> validator)
        is one hop outside the traversal and is not seen; nor is a module name computed at runtime,
        or a literal handed to a locally-defined import helper. Following arbitrary chains through
        the neutral core is not a question a source reader at this level can answer, and stating
        that beats implying coverage.

        `root` and `subject_matter_roots` are parameters so the algorithm can be driven against a
        SYNTHETIC tree with a known shape. Before that, deleting the transitive step below left the
        whole suite green — the redesign that introduced it had no witness at all.
        """
        root = REPO_ROOT if root is None else root
        roots = cls._SUBJECT_MATTER_ROOTS if subject_matter_roots is None else subject_matter_roots
        seen: set[str] = set()
        queue: list[str] = [rel for rel in roots if (root / rel).is_file()]
        validator = root / "tools/validate_pipeline_semantics.py"
        if validator.is_file():
            for _lineno, name in cls._imported_names(validator):
                if name.startswith("tools.backends"):
                    queue += cls._rel_paths_for(name, root)
        while queue:
            rel = queue.pop()
            if rel in seen or not (root / rel).is_file():
                continue
            seen.add(rel)
            for _lineno, written in cls._imported_names(root / rel):
                name = cls._absolute_name(written, rel)
                if name.startswith("tools.backends"):
                    queue += cls._rel_paths_for(name, root)
        return sorted(seen)

    #: The importer callables whose LITERAL first argument the source check reads. Same set, and
    #: the same limit, as `tools/tests/test_backend_boundary.py`.
    _IMPORTER_CALLS = frozenset({"import_module", "__import__"})

    @classmethod
    def _imported_names(cls, path: Path) -> list[tuple[int, str]]:
        """Every dotted name imported anywhere in `path`, at any nesting depth.

        `ImportFrom` contributes `module.alias` and not just `module`: `from tools import
        validate_pipeline_semantics` names the target in the ALIAS, and reading only `node.module`
        missed it — a reviewer reinstated the whole backend-to-neutral-core edge in that spelling
        with this test green. Relative imports contribute their written form too, since `tools` is
        a namespace package and a relative import can cross into it. A literal
        `importlib.import_module("...")` contributes its argument, because it is an import that no
        import statement announces — a census showed it evaded both halves of this test.
        """
        import ast

        out: list[tuple[int, str]] = []
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out += [(node.lineno, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # The written form is KEPT, dots and all: `.strip(".")` here turned
                # `from . import keywords` into the bare name `keywords`, which no prefix test
                # matches and no resolver can place — the relative edge was invisible to the
                # closure because of this one call.
                prefix = "." * node.level + (node.module or "")
                out.append((node.lineno, prefix))
                sep = "." if node.module else ""
                out += [(node.lineno, f"{prefix}{sep}{alias.name}") for alias in node.names]
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", None)
                if name not in cls._IMPORTER_CALLS:
                    continue
                args = list(node.args) + [kw.value for kw in node.keywords
                                          if kw.arg in (None, "name")]
                out += [(node.lineno, a.value) for a in args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        return out

    def test_no_import_of_the_validator_at_any_nesting_depth(self) -> None:
        scanned = self._modules_on_the_cycle()
        # The floor is the roots plus the packages every import of them executes; stated as a
        # membership check rather than a number, because a number is what the previous version
        # used and it sat at 5 against an actual 8.
        for required in self._SUBJECT_MATTER_ROOTS + (
                "tools/backends/__init__.py",
                "tools/backends/language/__init__.py",
                "tools/backends/language/fortran/__init__.py"):
            self.assertIn(required, scanned,
                          "the closure stopped covering a module every import of the backend "
                          "executes")
        for rel in scanned:
            for lineno, name in self._imported_names(REPO_ROOT / rel):
                self.assertNotIn(
                    "validate_pipeline_semantics", name,
                    f"{rel}:{lineno} imports {name}. A module-level import closes a cycle (the "
                    f"validator imports this module at module level); a lazy one reinstates the "
                    f"dependency on the neutral core that moving the §5.1 layer here removed.")

    @staticmethod
    def _synthetic_tree(base: Path, files: dict[str, str]) -> None:
        for rel, body in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    #: A backend package with the shapes that matter: a root, a helper reached by an ABSOLUTE
    #: import, a helper reached by a RELATIVE one, a module no root reaches, and a second axis
    #: standing in for the migration ledger's next area.
    _SYNTHETIC = {
        "tools/validate_pipeline_semantics.py": (
            "from tools.backends.language.fortran import lines as fortran_lines\n"),
        "tools/backends/__init__.py": "",
        "tools/backends/registry.py": "import importlib\n",
        "tools/backends/language/__init__.py": "",
        "tools/backends/language/fortran/__init__.py": "",
        "tools/backends/language/fortran/lines.py": "import re\n",
        "tools/backends/language/fortran/signatures.py": (
            "from tools.backends.language.fortran import lines\n"
            "from . import keywords\n"),
        "tools/backends/language/fortran/keywords.py": "WORDS = ()\n",
        "tools/backends/language/fortran/structure_differential.py": (
            "from tools.validate_pipeline_semantics import anything\n"),
        "tools/backends/build_system/__init__.py": "",
        "tools/backends/build_system/make/__init__.py": "",
        "tools/backends/build_system/make/rules.py": (
            "from tools.validate_pipeline_semantics import anything\n"),
    }

    def _closure_of(self, base: Path) -> set[str]:
        return set(self._modules_on_the_cycle(
            root=base,
            subject_matter_roots=("tools/backends/language/fortran/signatures.py",
                                  "tools/backends/language/fortran/lines.py")))

    def test_the_closure_algorithm_on_a_synthetic_tree(self) -> None:
        # Drives the COMPUTATION, not today's import graph — the distinction this class had to
        # learn twice. Measured before this test existed: deleting the transitive expansion, and
        # reverting wholesale to the previous whole-directory walk plus its exception list, each
        # left every test in this class green. Both are now observed, and so is the relative-import
        # resolution that the closure originally lacked.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            self._synthetic_tree(base, self._SYNTHETIC)
            closure = self._closure_of(base)

            # reached from a root by an absolute import, and by a RELATIVE one
            self.assertIn("tools/backends/language/fortran/lines.py", closure)
            self.assertIn("tools/backends/language/fortran/keywords.py", closure,
                          "a `from . import x` edge was not followed")
            # every parent package an import executes
            for pkg in ("tools/backends/__init__.py", "tools/backends/language/__init__.py",
                        "tools/backends/language/fortran/__init__.py"):
                self.assertIn(pkg, closure)
            # reached by nothing: the developer harness and the next migration area, both of which
            # import the neutral core legally
            self.assertNotIn("tools/backends/language/fortran/structure_differential.py", closure)
            self.assertNotIn("tools/backends/build_system/make/rules.py", closure)

    def test_the_closure_follows_a_dependency_routed_through_a_sibling(self) -> None:
        # The transitive step, witnessed: when a root imports the harness, the harness — and its
        # validator import — come inside the closure.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            files = dict(self._SYNTHETIC)
            files["tools/backends/language/fortran/signatures.py"] += (
                "from tools.backends.language.fortran import structure_differential\n")
            self._synthetic_tree(base, files)
            closure = self._closure_of(base)
            harness = "tools/backends/language/fortran/structure_differential.py"
            self.assertIn(harness, closure,
                          "a dependency routed through a sibling escaped the closure")
            offenders = [
                rel for rel in closure
                if any("validate_pipeline_semantics" in name
                       for _lineno, name in self._imported_names(base / rel))
            ]
            self.assertEqual(offenders, [harness])

    def test_the_live_closure_matches_the_property(self) -> None:
        # The real tree, as the corpus half of the same property.
        scanned = set(self._modules_on_the_cycle())
        self.assertNotIn("tools/backends/language/fortran/structure_differential.py", scanned,
                         "nothing on the cycle imports the developer harness, so it should be "
                         "outside the closure")
        self.assertIn("tools/backends/language/fortran/bundle.py", scanned,
                      "bundle is reached only transitively, through the package __init__ — if it "
                      "dropped out, the transitive step is gone")

    def test_an_unparseable_module_on_the_cycle_raises(self) -> None:
        # The reader must not answer "no imports" for a file it could not read: a module that
        # fails to parse is where an unread import would sit. Making `_imported_names` swallow
        # `SyntaxError` and return `[]` is invisible to every other test.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            broken = Path(td) / "broken.py"
            broken.write_text("def broken(:\n", encoding="utf-8")
            with self.assertRaises(SyntaxError):
                self._imported_names(broken)

    def test_importing_this_backend_does_not_execute_the_validator(self) -> None:
        import subprocess
        import sys

        probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "import tools.backends.language.fortran.signatures\n"
            "assert 'tools.validate_pipeline_semantics' not in sys.modules, "
            "'importing the language backend executed the validator'\n"
            "print('ok')\n" % str(REPO_ROOT)
        )
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr[-2000:])
        self.assertIn("ok", out.stdout)


class Section51StanzaLayerTests(unittest.TestCase):
    """The §5.1 stanza layer: `parse_interface_stanzas`, `declaration_atoms`, `stanza_atoms`.

    Moved here with the functions themselves, which used to be private helpers of
    `validate_pipeline_semantics`. Each of these is a reproducer for a defect that made a
    deterministic gate wrong on source gfortran accepts, so they travel with the code they
    pin rather than with the gates that call it."""

    def test_commented_out_stanza_behind_an_exotic_separator_is_not_parsed(self) -> None:
        # issue #23, defect A, at `_parse_interface_stanzas` — which the generated-signature
        # gate runs over the MODEL SOURCE to compare each pinned §5.1 signature within its own
        # procedure stanza. A form feed inside a comment ended the comment early under
        # `str.splitlines()` and re-admitted its tail AS CODE, conjuring a stanza header out of
        # prose: it never closes, so it is reported unterminated and fails Generate closed on a
        # source gfortran and fortitude both accept.
        for ch, name in (("\x0c", "form feed"), ("\x0b", "vertical tab"), ("\x85", "NEL"),
                         ("\u2028", "LINE SEPARATOR")):
            with self.subTest(separator=name):
                src = ("module hx_model\ncontains\n"
                       f"  ! removed: {ch} subroutine hx__ghost(a)\n"
                       "  subroutine hx__real(a)\n"
                       "    real, intent(in) :: a\n"
                       "  end subroutine\n"
                       "end module\n")
                ops, types, _ifaces, errors = parse_interface_stanzas(src)
                self.assertEqual(errors, [], f"a {name} must not end the comment")
                self.assertEqual(sorted(ops), ["hx__real"])
                self.assertEqual(types, {})

    def test_declaration_atoms_split_combined_declarators(self) -> None:
        # A combined declarator splits into one atom per entity; array-spec commas stay intact.
        self.assertEqual(
            declaration_atoms("integer, intent(in) :: steps, cells_updated"),
            ["integer, intent(in) :: steps", "integer, intent(in) :: cells_updated"])
        self.assertEqual(
            declaration_atoms("real(dp), intent(in) :: a(:), b(2,2)"),
            ["real(dp), intent(in) :: a(:)", "real(dp), intent(in) :: b(2,2)"])
        # A header (no ::) passes through unchanged.
        self.assertEqual(
            declaration_atoms("subroutine hx__foo(a, b)"), ["subroutine hx__foo(a, b)"])

    def test_declaration_atoms_keep_a_comma_inside_a_character_literal(self) -> None:
        # Splitter-level reproducer: this module used to define `_split_top_level_commas` twice
        # ~9,500 lines apart, and the later quote-UNAWARE definition shadowed the quote-aware
        # one, so the initializer's comma read as an entity separator — a truncated first atom
        # plus a phantom `character(len=1), parameter :: '`.
        self.assertEqual(
            declaration_atoms("character(len=1), parameter :: sep = ',', tail = 'z'"),
            ["character(len=1), parameter :: sep = ','",
             "character(len=1), parameter :: tail = 'z'"])

    def test_unbalanced_paren_in_an_initializer_does_not_split_the_forms_apart(self) -> None:
        # The gate-level half, and the one the balanced case above does NOT reach: with a
        # balanced literal both sides of the §5.1 comparison mis-split identically and cancel.
        # An UNBALANCED paren inside the literal suppresses the split on the combined form only,
        # so combined and one-per-line compared UNEQUAL and a legal declaration read as a
        # signature mismatch — fail-closed. (gfortran -std=f2008 accepts the declaration.)
        combined = ["character(len=3), parameter :: msg = 'a(b', tail = 'z'"]
        per_line = ["character(len=3), parameter :: msg = 'a(b'",
                    "character(len=3), parameter :: tail = 'z'"]
        self.assertEqual(stanza_atoms(combined), stanza_atoms(per_line))

    def test_combined_and_split_declarations_compare_equal(self) -> None:
        combined = stanza_atoms(["integer, intent(in) :: a, b"])
        split = stanza_atoms(
            ["integer, intent(in) :: a", "integer, intent(in) :: b"])
        self.assertEqual(combined, split)

    def test_end_line_canonicalizes_trailing_name(self) -> None:
        # bare `end type` compares equal to `end type NAME` (the name is pinned by the header).
        with_name = stanza_atoms(
            ["type :: hx__t", "  integer :: a", "end type hx__t"])
        bare = stanza_atoms(["type :: hx__t", "  integer :: a", "end type"])
        self.assertEqual(with_name, bare)


if __name__ == "__main__":
    unittest.main()
