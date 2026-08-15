#!/usr/bin/env python3
"""Unit tests for tools/backends/language/fortran/signatures (Objective B language backend).

The correctness contract is a round-trip driven by the REAL published interfaces (not a synthetic
fixture — a hand-built struct could pass while the real §5.1 shape breaks; see the fixture-fiction
lesson): loading the real harness structured §5.1 block and rendering/reparsing it through Fortran
must preserve the struct and the exact NORMALIZED stanza lines the current gates compare. The same must hold for
`runner_renderer._HARNESS_V3_INTERFACE`, the third hardcoded copy of the harness signatures the
renderer pin uses. Drift tests confirm the structured form keeps the gate's discriminating power
(a changed intent / rank / type / name changes the normalized index).
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.backends.language.fortran.lines import normalize_fortran_line
from tools.backends.language.fortran.signatures import (
    SignatureParseError,
    canonicalize_end_line,
    declaration_atoms,
    load_structured_signatures,
    normalized_stanza_index,
    parse_interface_stanzas,
    parse_signatures_from_fortran,
    render_module_parameter_to_fortran,
    render_signatures_to_fortran,
    render_symbol_to_fortran,
    stanza_atoms,
)
from tools.runner_renderer import _HARNESS_V3_INTERFACE, _HARNESS_V3_PARAMETERS
from tools.validate_pipeline_semantics import _FENCED_BLOCK_RE

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_SPEC = (
    REPO_ROOT
    / "spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md"
)


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

    def test_round_trip_runner_renderer_hardcoded_copy(self) -> None:
        # The third copy of the signatures (runner_renderer._HARNESS_V3_INTERFACE) must lower and
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
        ops, _types, errors = parse_interface_stanzas(block)
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

    def test_the_two_procedure_header_patterns_agree(self) -> None:
        # `_PROC_HEADER_RE` (which lowers a header) is a strict extension of `_IFACE_PROC_START`
        # (which finds one). They cannot be collapsed — the group numbering differs — so the
        # containment is pinned instead: a header the lowering pattern accepts must be one the
        # splitter finds, or a stanza would be split out and then fail to lower. The type pair had
        # the same shape and WAS collapsed; this is the half that could not be.
        #
        # The prefix ALTERNATION is compared as a set identity, not sampled. The first version of
        # this test only ran the real §5.1 headers plus three hand-written probes, so adding an
        # alternative (`impure`) to one pattern and not the other survived it — a witness that
        # could only see the prefixes someone had already thought of.
        import re as _re

        from tools.backends.language.fortran.signatures import (
            _IFACE_PROC_START, _PROC_HEADER_RE)

        def prefixes(pattern: str) -> set[str]:
            m = _re.search(r"\(\?:((?:[a-z]+\\s\+\|?)+)\)\*", pattern)
            assert m, f"prefix alternation not found in {pattern!r}"
            return {alt for alt in m.group(1).split("|") if alt}

        self.assertEqual(prefixes(_PROC_HEADER_RE.pattern), prefixes(_IFACE_PROC_START.pattern),
                         "the two procedure header patterns accept different prefix sets")

        # ... and the containment holds behaviourally, over every prefix either pattern declares
        # and over the real §5.1 headers.
        for prefix in sorted(prefixes(_PROC_HEADER_RE.pattern)):
            word = prefix.replace("\\s+", " ")
            for header in (f"{word}subroutine hx__p(a)",
                           f"{word.upper()}FUNCTION hx__f(a) RESULT(s)"):
                self.assertIsNotNone(_PROC_HEADER_RE.match(header), header)
                self.assertIsNotNone(_IFACE_PROC_START.match(header), header)

        headers = [ln for ln in _real_section51_block().splitlines()
                   if _PROC_HEADER_RE.match(ln)]
        self.assertTrue(headers, "no procedure headers in the real §5.1 block")
        for header in headers:
            self.assertIsNotNone(
                _IFACE_PROC_START.match(header),
                f"the stanza splitter does not find a header the parser lowers: {header!r}")

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
        ops, types, errors = parse_interface_stanzas(block)
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
        ops_lone, _types_lone, errors_lone = parse_interface_stanzas(lone)
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

    def test_a_component_declaration_is_not_a_type_header(self) -> None:
        # `_TYPE_HEADER_RE` is now the ONE owner of "what a type header is" — the stanza splitter
        # and `_parse_type` share it — so what it accepts is worth an explicit test: a component
        # declaration must not open a stanza and swallow the rest of the enclosing type.
        #
        # MEASURED, and deliberately NOT pinning the pattern's `[^:()]` class: what keeps a
        # component out is the missing comma before `::`, not the paren exclusion. The only input
        # the paren exclusion decides is `type, extends(base_t) :: t` — a legal extensible-type
        # header that the pattern REFUSES (silently: `parse_interface_stanzas` returns no stanza
        # and no error). Asserting the class here would freeze that over-rejection, so the ledger
        # records it instead.
        block = ("type :: hx__outer\n"
                 "  type(hx__inner), allocatable :: parts(:)\n"
                 "  integer :: n\n"
                 "end type hx__outer\n")
        _ops, types, errors = parse_interface_stanzas(block)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(types), ["hx__outer"])
        self.assertEqual(len(types["hx__outer"]), 4)

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
    _ops, types, _errs = parse_interface_stanzas(block)
    name = next(n for n in types if n.endswith(suffix))
    return [normalize_fortran_line(ln) for ln in types[name] if normalize_fortran_line(ln)]


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

    The reason is mechanical, and stating it precisely matters because the boundary rule does NOT
    forbid this direction: `docs/BACKEND_BOUNDARY.md` says a backend MAY import the neutral core,
    and a sibling in this very package does (`structure_differential.py:66`). What forbids it HERE
    is that `validate_pipeline_semantics` now imports this module at module level, so any
    back-import closes a cycle. An earlier version of this test asserted the boundary rule instead
    and would have refused a boundary-legal import — the over-rejection direction.

    What it is guarding against is the state this module was in for its whole life until the §5.1
    line and stanza layer moved here: it imported `_fortran_logical_lines`,
    `_normalize_fortran_line` and `_parse_interface_stanzas` out of the validator, so the Fortran
    backend reached into the neutral core for its own subject matter (TODO.md's blocking
    sub-item). Nothing observed that: the import worked and the suite was green.

    Two checks, because neither alone covers the property:

    * the SOURCE check reads every `import` / `from ... import` in this module and in `lines`, at
      any nesting depth, so a function-local import — the exact spelling `runner_renderer` used —
      is caught. The subprocess probe below cannot see one.
    * the IMPORT check runs a fresh interpreter, so it catches a module-level import (and one
      reached transitively through `lines`) as an actually-executed fact rather than a reading of
      the source. In-process `sys.modules` says nothing here: most of this test run imports the
      validator anyway.
    """

    #: The modules the validator imports AT MODULE LEVEL, which is what makes a back-import a
    #: cycle. Not hand-picked: `test_the_scanned_modules_are_the_ones_the_validator_imports`
    #: compares this against the validator's own imports, so adding a third module-level backend
    #: import without listing it here fails rather than going unscanned.
    _BACKEND_MODULES = (
        "tools/backends/language/fortran/signatures.py",
        "tools/backends/language/fortran/lines.py",
        # The validator imports the tree-sitter front end at module level too, so it is on the
        # same cycle. It was NOT in the hand-written pair: the derivation below found it the first
        # time it ran, which is the reason the bound is derived rather than written.
        "tools/backends/language/fortran/structure.py",
    )

    @staticmethod
    def _imported_names(path: Path) -> list[tuple[int, str]]:
        """Every dotted name imported anywhere in `path`, at any nesting depth.

        `ImportFrom` contributes `module.alias` and not just `module`: `from tools import
        validate_pipeline_semantics` names the target in the ALIAS, and reading only `node.module`
        missed it — a reviewer reinstated the whole backend-to-neutral-core edge in that spelling
        with this test green. Relative imports contribute their written form too, since `tools` is
        a namespace package and a relative import can cross into it.
        """
        import ast

        out: list[tuple[int, str]] = []
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out += [(node.lineno, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                out.append((node.lineno, "." * node.level + base))
                out += [(node.lineno, f"{'.' * node.level}{base}.{alias.name}".strip("."))
                        for alias in node.names]
        return out

    def test_no_import_of_the_validator_at_any_nesting_depth(self) -> None:
        for rel in self._BACKEND_MODULES:
            for lineno, name in self._imported_names(REPO_ROOT / rel):
                self.assertNotIn(
                    "validate_pipeline_semantics", name,
                    f"{rel}:{lineno} imports {name} — the validator imports this package at "
                    f"module level, so this closes an import cycle")

    def test_the_scanned_modules_are_the_ones_the_validator_imports(self) -> None:
        # The bound of the check above is which files it reads. Derive it from the validator
        # instead of trusting a hand-written pair: every backend module the validator imports at
        # MODULE level is a module a back-import would cycle through, so it must be scanned.
        import ast

        validator = REPO_ROOT / "tools/validate_pipeline_semantics.py"
        tree = ast.parse(validator.read_text(encoding="utf-8"), filename=str(validator))
        at_module_level: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for lineno, name in self._imported_names_of_node(node):
                del lineno
                if not name.startswith("tools.backends.language.fortran"):
                    continue
                rel = name.replace(".", "/") + ".py"
                if (REPO_ROOT / rel).is_file():
                    at_module_level.add(rel)
        self.assertTrue(at_module_level, "the validator imports no backend module at all")
        self.assertLessEqual(
            at_module_level, set(self._BACKEND_MODULES),
            "the validator imports a backend module that the cycle check does not scan; add it "
            "to _BACKEND_MODULES")

    @staticmethod
    def _imported_names_of_node(node) -> list[tuple[int, str]]:
        import ast

        if isinstance(node, ast.Import):
            return [(node.lineno, alias.name) for alias in node.names]
        base = node.module or ""
        out = [(node.lineno, base)]
        out += [(node.lineno, f"{base}.{alias.name}") for alias in node.names]
        return out

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
                ops, types, errors = parse_interface_stanzas(src)
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
