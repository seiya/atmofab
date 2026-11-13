#!/usr/bin/env python3
"""Builds the hand-authored IR of `problem/shallow_water2d_channel_subcycled@0.1.0` (issue #632
P4) from `tests.md` of that node, transcribed into a generator so that the transcription is
reviewable as code. It is NOT an output of `Compile`.

Most of the suite restates judgments of the member `shallow_water2d_channel` on cases equal to a
member case input for input but for the composite's own input `subcycle.substeps` (and, on the
pair cases, `dt_rule.dt_scale`): such a judgment is the member's predicate, as the member's hand
IR (`../pr3_evidence/ir/shallow_water2d_channel.yaml`) transcribes it, with its case ids mapped
onto the composite's (`RESTATED`). Only the stability index `cfl.max` changes form: the composite
advances the member by `initial.dt / substeps` per sub-call, so its index is read with that step
(`_subcycled_cfl`). The judgments the member has no counterpart of — the sub-cycling pairs, the
translation residual of the two momentum components, the refusals of `substeps` — are written
here (`OWN`). The member's IR also supplies the `drive` / `public_api` / `io_contract` skeleton,
renamed and extended with `substeps`; its descriptive text (`algorithm`, the
`io_contract.inputs` descriptions) is the member's and is read by no gate and no step of the
bench.

  PYTHONPATH=. python3 <this file>    (writes ../pr3_evidence/ir/shallow_water2d_channel_subcycled.yaml)

The written file is the node's transcription beside every other physics node's: the corpus row
`test_transcription_compile_static_accepts_shallow_water2d_channel_subcycled` validates it
against the real `tests.md`, and `bench.py` reads it, refusing to run when it differs from
`build()`.
"""
from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
MEMBER_IR = HERE.parent / "pr3_evidence" / "ir" / "shallow_water2d_channel.yaml"
TRANSCRIPTION = HERE.parent / "pr3_evidence" / "ir" / "shallow_water2d_channel_subcycled.yaml"
MEMBER = "shallow_water2d_channel"
MEMBER_VERSION = "0.4.1"
SID = "shallow_water2d_channel_subcycled"
VERSION = "0.1.0"
NODE_KEY = f"problem/{SID}@{VERSION}"
SPEC_DIR = f"spec/problem/dynamics/shallow_water/{SID}"
TEST_PROFILE = ("shallow_water2d_channel_subcycled_baseline", "0.1.0")
SUBSTEPS = "inputs.subcycle.substeps"

# ---------------------------------------------------------------------------------------------
# the cases (tests.md §3, §4): id -> (the member case it equals, substeps, overrides, every)

CASES: dict[str, tuple[str, int, dict[str, Any], int]] = {
    "chs_dry_n032_dts1600_m2": ("chan_dry_n032_dts800", 2, {"dt_rule.dt_scale": 16.0}, 0),
    "chs_guard_n032_dts100_m0": ("chan_tc2_ref_n032_dts100", 0, {}, 0),
    "chs_guard_n032_dts100_mneg1": ("chan_tc2_ref_n032_dts100", -1, {}, 0),
    "chs_tc2_ref_n032_dts050_m1": ("chan_tc2_ref_n032_dts100", 1, {"dt_rule.dt_scale": 0.5}, 441),
    "chs_tc2_ref_n032_dts100_m1": ("chan_tc2_ref_n032_dts100", 1, {}, 221),
    "chs_tc2_ref_n032_dts100_m2": ("chan_tc2_ref_n032_dts100", 2, {}, 221),
    "chs_tc2_ref_n032_dts100_m2_badprofile": ("chan_tc2_ref_n032_dts100_badprofile", 2, {}, 0),
    "chs_tc2_xpert_n064_dts100_m2": ("chan_tc2_xpert_n064_dts100", 2, {}, 443),
    "chs_tc2_xpert_n064_dts100_m2_sx025": ("chan_tc2_xpert_n064_dts100_sx025", 2, {}, 443),
    "chs_tc3_ref_n032_dts100_m1": ("chan_tc3_ref_n032_dts100", 1, {}, 142),
    "chs_tc4_ref_n032_dts025_m1": ("chan_tc4_ref_n032_dts100", 1, {"dt_rule.dt_scale": 0.25}, 1086),
    "chs_tc4_ref_n032_dts050_m1": ("chan_tc4_ref_n032_dts100", 1, {"dt_rule.dt_scale": 0.5}, 543),
    "chs_tc4_ref_n032_dts100_m1": ("chan_tc4_ref_n032_dts100", 1, {}, 272),
    "chs_tc4_ref_n032_dts100_m2": ("chan_tc4_ref_n032_dts100", 2, {}, 272),
    "chs_tc4_ref_n032_dts100_m4": ("chan_tc4_ref_n032_dts100", 4, {}, 272),
    "chs_tc4_ref_n064_dts100_m2": ("chan_tc4_ref_n064_dts100", 2, {}, 544),
    "chs_tc4_ref_n064_dts100_m2_rect": ("chan_tc4_ref_n064_dts100_rect", 2, {}, 576),
    "chs_tc4_ref_n064_dts100_m2_rectaxis": ("chan_tc4_ref_n064_dts100_rectaxis", 2, {}, 576),
    "chs_tc4_ref_n064_dts100_m2_xmir": ("chan_tc4_ref_n064_dts100_xmir", 2, {}, 544),
    "chs_tc4_ref_n064_dts100_m2_ymir": ("chan_tc4_ref_n064_dts100_ymir", 2, {}, 544),
    "chs_tc4_ref_n128_dts100_m2": ("chan_tc4_ref_n128_dts100", 2, {}, 1087),
    "chs_zrepeat_n032_dts100_m2": ("chan_zrepeat_n032_dts100", 2, {}, 221),
}
#: the continuation (tests.md 2-8, §3 the `restart` line): id -> (base, K)
RESTART = {"chs_tc4_ref_n032_dts100_m2_cont": ("chs_tc4_ref_n032_dts100_m2", 272)}


def _case(member_ir: dict[str, Any], cid: str) -> dict[str, Any]:
    src, substeps, overrides, every = CASES[cid]
    c = copy.deepcopy(next(m for m in member_ir["case"]["test_case_set"] if m["case_id"] == src))
    c["case_id"] = cid
    inp = c["inputs"]
    inp.pop("profile_selection", None)   # the member's adopted profile; this node adopts none
    inp["subcycle"] = {"substeps": substeps}
    inp["test_profile_id"], inp["test_profile_version"] = TEST_PROFILE
    inp["history"]["every"] = every
    for path, value in overrides.items():
        d = inp
        *head, last = path.split(".")
        for h in head:
            d = d[h]
        d[last] = value
    return c


def cases(member_ir: dict[str, Any]) -> list[dict[str, Any]]:
    out = [_case(member_ir, cid) for cid in CASES]
    for cid, (base, k) in RESTART.items():
        c = copy.deepcopy(next(x for x in out if x["case_id"] == base))
        c["case_id"] = cid
        c["restart"] = {"from": base, "step": k}
        out.append(c)
    return sorted(out, key=lambda c: c["case_id"])


# ---------------------------------------------------------------------------------------------
# the tests (tests.md §6)

_AT = re.compile(r"at\('([^']+)'\)")
#: the quantities a pair or a repeat restates of the member: no band, every one a per-case bound
#: whose value does not depend on the case
_PER_CASE = ("cfl.max", "metrics.mass_drift_rel", "extrema.h.min", "init.dt", "init.n_step_plan",
             "init_ok", "run.n_step", "cross_target_state_agreement")
_INITIAL = ("errors.initial_h.linf_rel", "errors.initial_h.below_jet_rel",
            "errors.initial_h.above_jet_rel", "errors.initial_u.linf_rel",
            "errors.initial_v.linf_rel", "errors.initial_vel_cross.linf")

T2 = {"chan_tc2_ref_n032_dts100"}
#: test_id -> (outcome, target cases, [(member test, {member case: composite case(s)},
#: quantities or None for every one)]). A quantity is taken from the first source that has it.
RESTATED: dict[str, tuple[str, list[str], list[tuple[str, dict[str, Any], tuple | None]]]] = {
    "l1_tc2_substeps1_restates_member": ("pass", ["chs_tc2_ref_n032_dts100_m1"], [
        ("l1_tc2_refinement_steady_geostrophic",
         {"chan_tc2_ref_n032_dts100": "chs_tc2_ref_n032_dts100_m1"}, None),
        ("l0_tc2_initial_state_matches_analytic",
         {"chan_tc2_ref_n064_dts100": "chs_tc2_ref_n032_dts100_m1"}, _INITIAL)]),
    "l1_tc3_substeps1_restates_member": ("pass", ["chs_tc3_ref_n032_dts100_m1"], [
        ("l1_tc3_refinement_compact_jet",
         {"chan_tc3_ref_n032_dts100": "chs_tc3_ref_n032_dts100_m1"}, None),
        ("l0_tc3_initial_jet_matches_analytic",
         {"chan_tc3_ref_n064_dts100": "chs_tc3_ref_n032_dts100_m1"}, _INITIAL)]),
    "l1_tc4_substeps1_restates_member": ("pass", ["chs_tc4_ref_n032_dts100_m1"], [
        ("l1_tc4_refinement_translating_low",
         {"chan_tc4_ref_n032_dts100": "chs_tc4_ref_n032_dts100_m1"}, None),
        ("l0_tc4_initial_state_matches_analytic",
         {"chan_tc4_ref_n064_dts100": "chs_tc4_ref_n032_dts100_m1"}, _INITIAL)]),
    "l3_substeps_pair_tc2": ("pass", ["chs_tc2_ref_n032_dts050_m1", "chs_tc2_ref_n032_dts100_m2"], [
        ("l1_tc2_refinement_steady_geostrophic",
         {"chan_tc2_ref_n032_dts100": ["chs_tc2_ref_n032_dts050_m1",
                                       "chs_tc2_ref_n032_dts100_m2"]}, _PER_CASE)]),
    "l3_substeps_pair_tc4": ("pass", ["chs_tc4_ref_n032_dts050_m1", "chs_tc4_ref_n032_dts100_m2"], [
        ("l1_tc4_refinement_translating_low",
         {"chan_tc4_ref_n032_dts100": ["chs_tc4_ref_n032_dts050_m1",
                                       "chs_tc4_ref_n032_dts100_m2"]}, _PER_CASE)]),
    "l3_substeps_pair_tc4_m4": ("pass", ["chs_tc4_ref_n032_dts025_m1",
                                         "chs_tc4_ref_n032_dts100_m4"], [
        ("l1_tc4_refinement_translating_low",
         {"chan_tc4_ref_n032_dts100": ["chs_tc4_ref_n032_dts025_m1",
                                       "chs_tc4_ref_n032_dts100_m4"]}, _PER_CASE)]),
    "l3_tc4_time_refinement_pair_substeps1": ("pass", ["chs_tc4_ref_n032_dts050_m1",
                                                       "chs_tc4_ref_n032_dts100_m1"], [
        ("l1_tc4_refinement_translating_low",
         {"chan_tc4_ref_n032_dts100": ["chs_tc4_ref_n032_dts050_m1",
                                       "chs_tc4_ref_n032_dts100_m1"]}, _PER_CASE)]),
    "l1_tc4_refinement_substeps2": ("pass", ["chs_tc4_ref_n032_dts100_m2",
                                             "chs_tc4_ref_n064_dts100_m2",
                                             "chs_tc4_ref_n128_dts100_m2"], [
        ("l1_tc4_refinement_translating_low",
         {f"chan_tc4_ref_n{n:03d}_dts100": f"chs_tc4_ref_n{n:03d}_dts100_m2"
          for n in (32, 64, 128)}, None),
        ("l0_tc4_initial_state_matches_analytic",
         {"chan_tc4_ref_n064_dts100": [f"chs_tc4_ref_n{n:03d}_dts100_m2"
                                       for n in (32, 64, 128)]}, _INITIAL)]),
    "l1_tc4_rectangular_grid_substeps2": ("pass", ["chs_tc4_ref_n064_dts100_m2_rect"], [
        ("l1_tc4_rectangular_grid",
         {"chan_tc4_ref_n064_dts100_rect": "chs_tc4_ref_n064_dts100_m2_rect"}, None)]),
    "l3_axis_exchange_pair": ("pass", ["chs_tc4_ref_n064_dts100_m2_rect",
                                       "chs_tc4_ref_n064_dts100_m2_rectaxis"], [
        ("l3_axis_exchange_pairs",
         {"chan_tc4_ref_n064_dts100_rect": "chs_tc4_ref_n064_dts100_m2_rect",
          "chan_tc4_ref_n064_dts100_rectaxis": "chs_tc4_ref_n064_dts100_m2_rectaxis"}, None)]),
    "l3_along_channel_mirror_pair": ("pass", ["chs_tc4_ref_n064_dts100_m2",
                                              "chs_tc4_ref_n064_dts100_m2_xmir"], [
        ("l3_along_channel_mirror_pairs",
         {"chan_tc4_ref_n064_dts100": "chs_tc4_ref_n064_dts100_m2",
          "chan_tc4_ref_n064_dts100_xmir": "chs_tc4_ref_n064_dts100_m2_xmir"}, None)]),
    "l3_cross_channel_mirror_pair": ("pass", ["chs_tc4_ref_n064_dts100_m2",
                                              "chs_tc4_ref_n064_dts100_m2_ymir"], [
        ("l3_cross_channel_mirror_pairs",
         {"chan_tc4_ref_n064_dts100": "chs_tc4_ref_n064_dts100_m2",
          "chan_tc4_ref_n064_dts100_ymir": "chs_tc4_ref_n064_dts100_m2_ymir"}, None)]),
    "l3_x_translation_pair": ("pass", ["chs_tc2_xpert_n064_dts100_m2",
                                       "chs_tc2_xpert_n064_dts100_m2_sx025"], [
        ("l3_x_translation_equivariance_and_wave_decay",
         {"chan_tc2_xpert_n064_dts100": "chs_tc2_xpert_n064_dts100_m2",
          "chan_tc2_xpert_n064_dts100_sx025": "chs_tc2_xpert_n064_dts100_m2_sx025"}, None)]),
    "l3_restart_equivalence": ("pass", ["chs_tc4_ref_n032_dts100_m2",
                                        "chs_tc4_ref_n032_dts100_m2_cont"], [
        ("l3_restart_equivalence",
         {"chan_tc4_ref_n032_dts100": "chs_tc4_ref_n032_dts100_m2",
          "chan_tc4_ref_n032_dts100_cont": "chs_tc4_ref_n032_dts100_m2_cont"}, None)]),
    "l3_case_independence_repeat": ("pass", ["chs_tc2_ref_n032_dts100_m2",
                                             "chs_zrepeat_n032_dts100_m2"], [
        ("l3_case_independence_repeat",
         {"chan_tc2_ref_n032_dts100": "chs_tc2_ref_n032_dts100_m2",
          "chan_zrepeat_n032_dts100": "chs_zrepeat_n032_dts100_m2"}, None)]),
    "l0_init_refusal_xfail": ("xfail", ["chs_tc2_ref_n032_dts100_m2_badprofile"], [
        ("l0_init_refusal_xfail",
         {"chan_tc2_ref_n032_dts100_badprofile": "chs_tc2_ref_n032_dts100_m2_badprofile"},
         None)]),
    "l0_wet_domain_refusal_xfail": ("xfail", ["chs_dry_n032_dts1600_m2"], [
        ("l0_wet_domain_refusal_xfail", {"chan_dry_n032_dts800": "chs_dry_n032_dts1600_m2"},
         None)]),
}
ORDER = ["l1_tc2_substeps1_restates_member", "l1_tc3_substeps1_restates_member",
         "l1_tc4_substeps1_restates_member", "l3_substeps_pair_tc2", "l3_substeps_pair_tc4",
         "l3_substeps_pair_tc4_m4", "l3_tc4_time_refinement_pair_substeps1",
         "l1_tc4_refinement_substeps2", "l1_tc4_rectangular_grid_substeps2",
         "l3_axis_exchange_pair", "l3_along_channel_mirror_pair", "l3_cross_channel_mirror_pair",
         "l3_x_translation_pair", "l3_restart_equivalence", "l3_case_independence_repeat",
         "l0_substeps_refusal_xfail", "l0_init_refusal_xfail", "l0_wet_domain_refusal_xfail"]


def _subcycled_cfl(p: dict[str, Any]) -> dict[str, Any]:
    """The stability index of a sub-call: the member's `initial.dt * ...` read with the step the
    member is handed, `initial.dt / substeps`."""
    if p["quantity"] != "cfl.max":
        return p
    if p["expr"].count("initial.dt * ") != 1:
        raise SystemExit(f"cfl.max of {p['test_id']}: unexpected form {p['expr']!r}")
    p["expr"] = p["expr"].replace("initial.dt * ", f"(initial.dt / {SUBSTEPS}) * ")
    return p


def _rename(text: str, cmap: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in cmap:
            raise KeyError(m.group(1))
        return f"at('{cmap[m.group(1)]}')"
    return _AT.sub(sub, text)


def restate(member_ir: dict[str, Any], test_id: str, targets: list[str],
            sources: list[tuple[str, dict[str, Any], tuple | None]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for src_test, cmap, wanted in sources:
        member_preds = [p for p in member_ir["io_contract"]["predicates"]
                        if p["test_id"] == src_test]
        if not member_preds:
            raise SystemExit(f"member test {src_test} has no predicate")
        here: set[str] = set()
        single = {k: v for k, v in cmap.items() if isinstance(v, str)}
        for mp in member_preds:
            q = mp["quantity"]
            if q in taken or (wanted is not None and q not in wanted):
                continue
            p = copy.deepcopy(mp)
            p["test_id"] = test_id
            if p.get("case"):
                refs = {p["case"], *_AT.findall(repr(p))}
                if not refs <= set(single):
                    continue
                p["case"] = single[p["case"]]
                p["expr"] = _rename(p["expr"], single)
                if "bind" in p:
                    p["bind"] = {k: _rename(v, single) for k, v in p["bind"].items()}
                p["target_cases"] = list(targets)
            else:
                mapped: list[str] = []
                for c in mp["target_cases"]:
                    v = cmap.get(c)
                    mapped += [v] if isinstance(v, str) else list(v or [])
                if not mapped:
                    continue
                if _AT.search(repr(p)):
                    raise SystemExit(f"{src_test}/{q}: a per-case predicate reads another case")
                p["target_cases"] = mapped
                if isinstance(p["value"], dict):
                    per = p["value"]["per_case"]
                    p["value"] = {"per_case": {}}
                    for c, v in cmap.items():
                        for t in ([v] if isinstance(v, str) else v):
                            if c in per:
                                p["value"]["per_case"][t] = per[c]
            out.append(_subcycled_cfl(p))
            here.add(q)
        taken |= here
    return out


def own(test_id: str) -> list[dict[str, Any]]:
    """The predicates of this suite's own judgments (tests.md 5-4)."""
    out: list[dict[str, Any]] = []
    pairs = {"l3_substeps_pair_tc2": ("chs_tc2_ref_n032_dts050_m1", "chs_tc2_ref_n032_dts100_m2",
                                      2, None),
             "l3_substeps_pair_tc4": ("chs_tc4_ref_n032_dts050_m1", "chs_tc4_ref_n032_dts100_m2",
                                      2, 1.0e-12),
             "l3_substeps_pair_tc4_m4": ("chs_tc4_ref_n032_dts025_m1",
                                         "chs_tc4_ref_n032_dts100_m4", 4, 1.0e-12)}
    if test_id in pairs:
        b, a, m, rel = pairs[test_id]
        targets = [b, a]
        out.append({"test_id": test_id, "quantity": "pair.n_step_premise", "target_cases": targets,
                    "case": a,
                    "expr": f"abs(at('{b}').initial.n_step_plan - {m} * initial.n_step_plan)",
                    "op": "le", "value": 0.5})
        for v in ("h", "hu", "hv"):
            diff = f"maxabs(final.{v} - at('{b}').final.{v})"
            out.append({"test_id": test_id, "quantity": f"pair.{v}", "target_cases": targets,
                        "case": a,
                        "expr": diff if rel is None else f"{diff} / maxabs(at('{b}').final.{v})",
                        "op": "le", "value": 0.0 if rel is None else rel})
    if test_id == "l3_tc4_time_refinement_pair_substeps1":
        # the member's time-refinement pair (its tests.md 2-8, 5-5), carried by the case that
        # sorts last, `_dts100_m1`, and normalised by that case's (the coarser run's) depth
        half, base = "chs_tc4_ref_n032_dts050_m1", "chs_tc4_ref_n032_dts100_m1"
        expr = f"norm2(at('{half}').final.h - final.h) / norm2(final.h)"
        for q, op, v in (("lower", "ge", 2.482e-8), ("upper", "le", 2.488e-8)):
            out.append({"test_id": test_id, "quantity": f"errors.time_pair.h_l2_rel.{q}",
                        "target_cases": [half, base], "case": base, "expr": expr,
                        "op": op, "value": v})
    if test_id == "l3_x_translation_pair":
        base, shifted = "chs_tc2_xpert_n064_dts100_m2", "chs_tc2_xpert_n064_dts100_m2_sx025"
        norm = f"max(norm2(at('{base}').final.hu), norm2(at('{base}').final.hv))"
        for v in ("hu", "hv"):
            out.append({"test_id": test_id, "quantity": f"errors.symmetry_{v}.l2_rel",
                        "target_cases": [base, shifted], "case": shifted,
                        "expr": (f"norm2(final.{v} - roll(at('{base}').final.{v}, "
                                 f"inputs.initial.shift_x_fraction * inputs.grid.nx, 0)) / {norm}"),
                        "op": "le", "value": 2.0e-11})
    if test_id == "l0_substeps_refusal_xfail":
        targets = ["chs_guard_n032_dts100_m0", "chs_guard_n032_dts100_mneg1"]
        out.append({"test_id": test_id, "quantity": "ok", "target_cases": targets,
                    "per_case": True, "expr": "final.ok", "op": "eq", "value": 0})
        out.append({"test_id": test_id, "quantity": "run.first_call_refused",
                    "target_cases": targets, "per_case": True,
                    "expr": ("final.step + max(maxabs(final.h - initial.h), "
                             "maxabs(final.hu - initial.hu), maxabs(final.hv - initial.hv))"),
                    "op": "eq", "value": 0})
    return out


def tests_and_predicates(member_ir: dict[str, Any]) -> tuple[list, list]:
    tests: list[dict[str, Any]] = []
    preds: list[dict[str, Any]] = []
    for tid in ORDER:
        if tid in RESTATED:
            outcome, targets, sources = RESTATED[tid]
            ps = restate(member_ir, tid, targets, sources) + own(tid)
        else:
            outcome, targets = "xfail", ["chs_guard_n032_dts100_m0", "chs_guard_n032_dts100_mneg1"]
            ps = own(tid)
        quantities = sorted({p["quantity"] for p in ps} - {"cross_target_state_agreement"})
        tests.append({"test_id": tid, "expected_outcome": outcome, "target_cases": targets,
                      "quantities": quantities})
        preds += ps
    return tests, preds


# ---------------------------------------------------------------------------------------------
# the skeleton

def skeleton(member_ir: dict[str, Any]) -> dict[str, Any]:
    ir = copy.deepcopy(member_ir)
    ir["meta"].update({"node_key": NODE_KEY, "spec_id": SID, "spec_version": VERSION,
                       "source_refs": {"controlled_spec": f"{SPEC_DIR}/controlled_spec.md",
                                       "tests": f"{SPEC_DIR}/tests.md",
                                       "deps": f"{SPEC_DIR}/deps.yaml"}})
    ir["dependency"] = {"node_key": NODE_KEY, "direct_deps": [
        {"kind": "problem", "node_key": f"problem/{MEMBER}@{MEMBER_VERSION}",
         "operations": [f"{MEMBER}__init", f"{MEMBER}__step"]}]}
    drive = ir["drive"]
    drive["init"]["operation"] = f"{SID}__init"
    step = drive["step"]
    step["operation"] = f"{SID}__step"
    args = {}
    for k, v in step["arguments"].items():
        args[k] = v
        if k == "t":
            args["substeps"] = SUBSTEPS
    step["arguments"] = args
    for entry in ir["public_api"]["signatures"]:
        op = entry["symbol"].replace(MEMBER, SID, 1)
        entry["symbol"] = entry["signature"]["name"] = op
        if op.endswith("__step"):
            new = []
            for arg in entry["signature"]["args"]:
                new.append(arg)
                if arg["name"] == "t":
                    new.append({"name": "substeps", "rank": 0, "intent": "in",
                                "spec": {"type": "integer"}})
            entry["signature"]["args"] = new
    for e in ir["public_api"]["published_operations"]:
        e["operation_id"] = e["operation_id"].replace(MEMBER, SID, 1)
        e["exercised_by"] = list(ORDER) if e["operation_id"].endswith("__step") else [
            t for t in ORDER if t != "l0_substeps_refusal_xfail"]
    ir["io_contract"]["inputs"] = [*ir["io_contract"]["inputs"], {
        "name": "substeps", "shape_expr": "scalar", "evidence_ref": "spec.ir.yaml",
        "description": ("case inputs.subcycle.substeps: the number of member steps of length "
                        "dt / substeps one call of the step makes; below 1 is refused")}]
    ir["case"]["sweeps"] = {}
    ir["case"]["refinements"] = {}
    ir["io_contract"]["tests"] = []
    ir["io_contract"]["predicates"] = []
    return ir


def build() -> dict[str, Any]:
    member_ir = yaml.safe_load(MEMBER_IR.read_text())
    ir = skeleton(member_ir)
    ir["case"]["test_case_set"] = cases(member_ir)
    tests, preds = tests_and_predicates(member_ir)
    ir["io_contract"]["tests"] = tests
    ir["io_contract"]["predicates"] = preds
    return ir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(TRANSCRIPTION))
    a = ap.parse_args()
    ir = build()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(yaml.safe_dump(ir, sort_keys=False, width=100))
    print(a.out, len(ir["case"]["test_case_set"]), "cases", len(ir["io_contract"]["tests"]),
          "tests", len(ir["io_contract"]["predicates"]), "predicates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
