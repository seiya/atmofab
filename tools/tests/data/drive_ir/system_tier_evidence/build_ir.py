#!/usr/bin/env python3
"""Builds the hand-authored IR of `problem/shallow_water2d_rotating@0.1.0` (system tier PR-S2)
from `tests.md` of that node: the cases (§2-§4), the judged quantities (§5) and the tests (§6),
transcribed by hand into a generator so that the transcription is reviewable as code. It is NOT
an output of `Compile`. The member's hand IR (`../pr3_evidence/ir/shallow_water2d.yaml`) supplies
the `drive` / `public_api` / `io_contract` skeleton, extended with the composite's arguments;
its descriptive text (`algorithm`, the `io_contract.inputs` descriptions) is the member's and is
read by no gate and no step of the bench. What the bench depends on is `drive`, `public_api`, the
cases, the tests and the predicates, all built here, and `bands.yaml` (`calibrate.py --write`).

  PYTHONPATH=. python3 <this file>    (writes ../pr3_evidence/ir/shallow_water2d_rotating.yaml)

The written file is the node's transcription beside every other physics node's: the corpus row
`test_transcription_compile_static_accepts_shallow_water2d_rotating` validates it against the
real `tests.md`, and `bench.py` reads it, refusing to run when it differs from `build()`.
"""
from __future__ import annotations

import argparse
import copy
import math
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
MEMBER_IR = HERE.parent / "pr3_evidence" / "ir" / "shallow_water2d.yaml"
TRANSCRIPTION = HERE.parent / "pr3_evidence" / "ir" / "shallow_water2d_rotating.yaml"
SID = "shallow_water2d_rotating"
VERSION = "0.1.0"
NODE_KEY = f"problem/{SID}@{VERSION}"
SPEC_DIR = f"spec/problem/dynamics/shallow_water/{SID}"
CORIOLIS = "dynamics_shallow_water_source_2d_coriolis"
RK4 = "dynamics_shallow_water_time_update_2d_rk4"
TEST_PROFILE = ("shallow_water2d_rotating_baseline", "0.1.0")

# ---------------------------------------------------------------------------------------------
# closed forms (tests.md §2)

I = "inputs.initial"
GR = "inputs.grid"
ROT = "inputs.rotation"
C = "inputs.constants"


def _s(axis: str, amp: str, phase: str) -> str:
    """amp * sin(2 pi (axis / L - phase))"""
    L = f"{GR}.L_x" if axis == "x" else f"{GR}.L_y"
    return f"{amp} * sin(2 * pi * ({axis} / {L} - {phase}))"


F_EXPR = (f"{ROT}.f0 + {_s('x', f'{ROT}.f_amp_x', f'{ROT}.f_phase_x')}"
          f" + {_s('y', f'{ROT}.f_amp_y', f'{ROT}.f_phase_y')}")

SHEAR = {
    "h": (f"{C}.H_0 + {_s('x', f'{I}.eta_x', f'{I}.eta_phase_x')}"
          f" + {_s('y', f'{I}.eta_y', f'{I}.eta_phase_y')}"),
    "hu": f"{I}.mom_x0 + {_s('y', f'{I}.mom_x1', f'{I}.mom_phase_y')}",
    "hv": f"{I}.mom_y0 + {_s('x', f'{I}.mom_y1', f'{I}.mom_phase_x')}",
    "z_b": "0.0",
}
UNIFORM = {"h": f"{C}.H_0", "hu": f"{I}.mom_x0", "hv": f"{I}.mom_y0", "z_b": "0.0"}
# The inertia-gravity (Poincare) wave in x on an f-plane, linearised about rest at depth H_0:
# eta = eta0 cos(kappa x), hu = (omega / kappa) eta0 cos(kappa x), hv = (f0 / kappa) eta0 sin(kappa x),
# omega^2 = f0^2 + g H_0 kappa^2, kappa = 2 pi / L_x.
KAPPA = f"(2 * pi / {GR}.L_x)"
OMEGA = f"sqrt({ROT}.f0 * {ROT}.f0 + {C}.g * {C}.H_0 * {KAPPA} * {KAPPA})"
POINCARE = {
    "h": f"{C}.H_0 + {I}.eta0 * cos(2 * pi * x / {GR}.L_x)",
    "hu": f"({OMEGA} / {KAPPA}) * {I}.eta0 * cos(2 * pi * x / {GR}.L_x)",
    "hv": f"({ROT}.f0 / {KAPPA}) * {I}.eta0 * sin(2 * pi * x / {GR}.L_x)",
    "z_b": "0.0",
}


def member_case(member_ir: dict[str, Any], case_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(c for c in member_ir["case"]["test_case_set"]
                              if c["case_id"] == case_id))


DERIVED = {
    "dx": f"{GR}.L_x / {GR}.nx",
    "dy": f"{GR}.L_y / {GR}.ny",
    "lam0": ("max((abs(initial.hu / initial.h) + sqrt(inputs.constants.g * initial.h)) / derived.dx"
             " + (abs(initial.hv / initial.h) + sqrt(inputs.constants.g * initial.h)) / derived.dy)"),
    "dt_raw": "inputs.dt_rule.dt_scale * inputs.dt_rule.cfl_target / derived.lam0",
    "n_step": "ceil((inputs.time.t_end - inputs.time.t_start) / derived.dt_raw)",
    "dt": "(inputs.time.t_end - inputs.time.t_start) / derived.n_step",
}


def case(case_id: str, *, nx: int, ny: int, L_x: float = 1.0, L_y: float = 1.0,
         t_end: float = 0.2, every: int, dt_scale: float = 1.0, initial: dict[str, Any],
         state: dict[str, str], rotation: dict[str, Any],
         topography: dict[str, Any] | None = None) -> dict[str, Any]:
    inputs = {
        "grid": {"nx": nx, "ny": ny, "L_x": L_x, "L_y": L_y},
        "time": {"t_start": 0.0, "t_end": t_end},
        "history": {"every": every},
        "dt_rule": {"kind": "cfl_ceil_n_step", "dt_scale": dt_scale, "cfl_target": 0.45},
        "initial": initial,
        "topography": topography or {"topography_profile": "flat"},
        "boundary": {"kind": "periodic"},
        "constants": {"g": 9.81, "H_0": 1.0},
        "rotation": rotation,
        "test_profile_id": TEST_PROFILE[0],
        "test_profile_version": TEST_PROFILE[1],
    }
    return {"case_id": case_id, "inputs": inputs,
            "initial_state": {**state, "f": F_EXPR}, "derived": dict(DERIVED)}


def rot(on: int, every: int, f0: float, ax: float = 0.0, px: float = 0.0, ay: float = 0.0,
        py: float = 0.0) -> dict[str, Any]:
    return {"on": on, "every": every, "f0": f0, "f_amp_x": ax, "f_phase_x": px,
            "f_amp_y": ay, "f_phase_y": py}


# The shear-flow base of judgments 4, 6, 7, 8 (tests.md 2-3).
SHEAR_BASE = {"initial_profile": "shear_flow", "eta_x": 0.01, "eta_phase_x": 0.1,
              "eta_y": 0.006, "eta_phase_y": 0.3, "mom_x0": 0.05, "mom_x1": 0.04,
              "mom_phase_y": 0.4, "mom_y0": -0.03, "mom_y1": 0.05, "mom_phase_x": 0.15}
SHEAR_ROT = rot(1, 2, 20.0, 10.0, 0.2, 8.0, 0.05)


def _neg(p: float) -> float:
    """-p modulo 1, in [0, 1)"""
    return round((-p) % 1.0, 12)


def x_mirror(ini: dict[str, Any], r: dict[str, Any]) -> tuple[dict, dict]:
    """x -> L_x - x, hu -> -hu, f -> -f (tests.md 2-11)."""
    i = dict(ini)
    i.update(eta_x=-ini["eta_x"], eta_phase_x=_neg(ini["eta_phase_x"]),
             mom_x0=-ini["mom_x0"], mom_x1=-ini["mom_x1"],
             mom_y1=-ini["mom_y1"], mom_phase_x=_neg(ini["mom_phase_x"]))
    q = dict(r)
    q.update(f0=-r["f0"], f_amp_x=r["f_amp_x"], f_phase_x=_neg(r["f_phase_x"]),
             f_amp_y=-r["f_amp_y"])
    return i, q


def y_mirror(ini: dict[str, Any], r: dict[str, Any]) -> tuple[dict, dict]:
    """y -> L_y - y, hv -> -hv, f -> -f."""
    i = dict(ini)
    i.update(eta_y=-ini["eta_y"], eta_phase_y=_neg(ini["eta_phase_y"]),
             mom_y0=-ini["mom_y0"], mom_y1=-ini["mom_y1"],
             mom_x1=-ini["mom_x1"], mom_phase_y=_neg(ini["mom_phase_y"]))
    q = dict(r)
    q.update(f0=-r["f0"], f_amp_x=-r["f_amp_x"], f_amp_y=r["f_amp_y"],
             f_phase_y=_neg(r["f_phase_y"]))
    return i, q


def axis_exchange(ini: dict[str, Any], r: dict[str, Any]) -> tuple[dict, dict]:
    """(h, hu, hv)(x, y) -> (h, hv, hu)(y, x), grid and lengths exchanged, f -> -f."""
    i = dict(ini)
    i.update(eta_x=ini["eta_y"], eta_phase_x=ini["eta_phase_y"],
             eta_y=ini["eta_x"], eta_phase_y=ini["eta_phase_x"],
             mom_x0=ini["mom_y0"], mom_x1=ini["mom_y1"], mom_phase_y=ini["mom_phase_x"],
             mom_y0=ini["mom_x0"], mom_y1=ini["mom_x1"], mom_phase_x=ini["mom_phase_y"])
    q = dict(r)
    q.update(f0=-r["f0"], f_amp_x=-r["f_amp_y"], f_phase_x=r["f_phase_y"],
             f_amp_y=-r["f_amp_x"], f_phase_y=r["f_phase_x"])
    return i, q


def translate(ini: dict[str, Any], r: dict[str, Any], sx: float, sy: float) -> tuple[dict, dict]:
    """Every field moved by (sx L_x, sy L_y): each x phase + sx, each y phase + sy."""
    i = dict(ini)
    i.update(eta_phase_x=round((ini["eta_phase_x"] + sx) % 1, 12),
             mom_phase_x=round((ini["mom_phase_x"] + sx) % 1, 12),
             eta_phase_y=round((ini["eta_phase_y"] + sy) % 1, 12),
             mom_phase_y=round((ini["mom_phase_y"] + sy) % 1, 12))
    q = dict(r)
    q.update(f_phase_x=round((r["f_phase_x"] + sx) % 1, 12),
             f_phase_y=round((r["f_phase_y"] + sy) % 1, 12))
    return i, q


def cases(member_ir: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # judgments 1, 2: the rotation alone (uniform state, uniform f)
    uni = {"initial_profile": "uniform_flow", "mom_x0": 0.1, "mom_y0": 0.05}
    # n_step = 46 at t_end = 0.2 (46 mod 4 = 2) and 47 at t_end = 0.205 (47 mod 4 = 3)
    for cid, k, t_end in (("swr_rot_n016_k1", 1, 0.2), ("swr_rot_n016_k4", 4, 0.2),
                          ("swr_rot_n016_k4_rem3", 4, 0.205)):
        out.append(case(cid, nx=16, ny=16, every=3, initial=uni, state=UNIFORM,
                        rotation=rot(1, k, 50.0), t_end=t_end))
    # judgment 3: the inertia-gravity wave on an f-plane
    out.append(case("swr_pw_n128", nx=128, ny=4, L_y=1.0 / 32, every=10,
                    initial={"initial_profile": "poincare_wave_x", "eta0": 1.0e-3},
                    state=POINCARE, rotation=rot(1, 1, 20.0)))
    # judgments 4, 6, 7, 8: the shear flow under the non-uniform periodic f
    for n in (32, 64, 128, 256):
        out.append(case(f"swr_shear_n{n:03d}", nx=n, ny=n, every=20, initial=SHEAR_BASE,
                        state=SHEAR, rotation=SHEAR_ROT))
    for suffix, fn in (("axis", axis_exchange), ("xmir", x_mirror), ("ymir", y_mirror)):
        i, q = fn(SHEAR_BASE, SHEAR_ROT)
        out.append(case(f"swr_shear_n064_{suffix}", nx=64, ny=64, every=20, initial=i,
                        state=SHEAR, rotation=q))
    i, q = translate(SHEAR_BASE, SHEAR_ROT, 0.25, 0.125)
    out.append(case("swr_shear_n064_shift", nx=64, ny=64, every=20, initial=i, state=SHEAR,
                    rotation=q))
    out.append(case("swr_shear_n064_rect", nx=64, ny=48, L_y=0.5, every=20, initial=SHEAR_BASE,
                    state=SHEAR, rotation=SHEAR_ROT))
    # judgment 5: the process off, equal to the member's cases input for input
    for cid, mid, r in (
            ("swr_off_cone_n064", "swe2d_ref_n064_sx000_sy000_dts100",
             rot(0, 3, 20.0, 10.0, 0.2, 8.0, 0.05)),
            ("swr_off_cone_n064_twin", "swe2d_ref_n064_sx000_sy000_dts100",
             rot(0, 1, -35.0, 4.0, 0.7, -6.0, 0.6)),
            ("swr_off_flat_n064", "swe2d_flat_ref_n064_sx000_sy000_dts100",
             rot(0, 2, 20.0, 10.0, 0.2, 8.0, 0.05))):
        m = member_case(member_ir, mid)
        m["case_id"] = cid
        inp = m["inputs"]
        inp.pop("profile_selection", None)
        inp["rotation"] = r
        inp["test_profile_id"], inp["test_profile_version"] = TEST_PROFILE
        m["initial_state"]["f"] = F_EXPR
        out.append(m)
    # judgment 9: guards
    for cid, r in (("swr_guard_every0_n032", rot(1, 0, 20.0)),
                   ("swr_guard_everyneg_n032", rot(1, -1, 20.0)),
                   ("swr_guard_sel2_n032", rot(2, 1, 20.0))):
        out.append(case(cid, nx=32, ny=32, every=0, initial=SHEAR_BASE, state=SHEAR,
                        rotation=r))
    m = member_case(member_ir, "swe2d_dry_n032_sx000_sy000_dts500")
    m["case_id"] = "swr_dry_n032"
    m["inputs"].pop("profile_selection", None)
    m["inputs"]["rotation"] = rot(1, 1, 20.0, 10.0, 0.2, 8.0, 0.05)
    m["inputs"]["test_profile_id"], m["inputs"]["test_profile_version"] = TEST_PROFILE
    m["initial_state"]["f"] = F_EXPR
    out.append(m)
    m = member_case(member_ir, "swe2d_flat_ref_n032_sx000_sy000_dts100_dtneg")
    m["case_id"] = "swr_dtneg_n032"
    m["inputs"].pop("profile_selection", None)
    m["inputs"]["rotation"] = rot(1, 1, 20.0, 10.0, 0.2, 8.0, 0.05)
    m["inputs"]["test_profile_id"], m["inputs"]["test_profile_version"] = TEST_PROFILE
    m["initial_state"]["f"] = F_EXPR
    out.append(m)
    return out


# ---------------------------------------------------------------------------------------------
# tests (tests.md §6) and predicates (§5)

BANDS_FILE = HERE / "bands.yaml"
ROT_CASES = ("swr_rot_n016_k1", "swr_rot_n016_k4", "swr_rot_n016_k4_rem3")
PW_CASES = ("swr_pw_n128",)
REFINE = ("swr_shear_n032", "swr_shear_n064", "swr_shear_n128", "swr_shear_n256")
ORDER_CASES = ("swr_shear_n064", "swr_shear_n064_rect")
PAIRS = {"axis_exchange": "swr_shear_n064_axis", "mirror_x": "swr_shear_n064_xmir",
         "mirror_y": "swr_shear_n064_ymir", "translation": "swr_shear_n064_shift"}
SHEAR_ALL = (*REFINE, "swr_shear_n064_axis", "swr_shear_n064_rect", "swr_shear_n064_shift",
             "swr_shear_n064_xmir", "swr_shear_n064_ymir")
OFF_CONE = ("swr_off_cone_n064", "swr_off_cone_n064_twin")
OFF_FLAT = ("swr_off_flat_n064",)
GUARDS = {"l0_rotation_every_refusal_xfail": ("swr_guard_every0_n032", "swr_guard_everyneg_n032"),
          "l0_selector_refusal_xfail": ("swr_guard_sel2_n032",),
          "l0_member_refusal_xfail": ("swr_dry_n032",),
          "l0_member_dt_refusal_xfail": ("swr_dtneg_n032",)}
VARS = ("h", "hu", "hv")
NORMS = {"l1": "sum(abs({d})) / {n1}", "l2": "norm2({d}) / {n2}", "linf": "maxabs({d}) / {ni}"}


def load_bands() -> dict[str, Any]:
    return yaml.safe_load(BANDS_FILE.read_text()) if BANDS_FILE.exists() else {}


class _T:
    """One test under construction: its predicates and its `quantities` line."""

    def __init__(self, tests: list, preds: list, test_id: str, outcome: str, cases) -> None:
        self.test = {"test_id": test_id, "expected_outcome": outcome,
                     "target_cases": sorted(cases), "quantities": []}
        tests.append(self.test)
        self.preds = preds

    def add(self, quantity: str, expr: str, op: str, value: Any, *, case: str | None = None,
            bind: dict[str, str] | None = None) -> None:
        p: dict[str, Any] = {"test_id": self.test["test_id"], "quantity": quantity,
                             "target_cases": list(self.test["target_cases"])}
        if case:
            p["case"] = case
        else:
            p["per_case"] = True
        if bind:
            p["bind"] = dict(bind)
        p.update(expr=expr, op=op, value=value)
        self.preds.append(p)
        if quantity not in self.test["quantities"] and quantity != "cross_target_state_agreement":
            self.test["quantities"].append(quantity)

    def band(self, quantity: str, expr: str, bands: dict[str, Any], *, case: str | None = None,
             bind: dict[str, str] | None = None) -> None:
        """two distinct judged quantities, `<quantity>.lower` and `.upper`."""
        b = bands.get(quantity)
        lo, hi = (b["lower"], b["upper"]) if b else (-1.0e300, 1.0e300)
        if isinstance(lo, dict):
            lo, hi = {"per_case": lo}, {"per_case": hi}
        self.add(f"{quantity}.lower", expr, "ge", lo, case=case, bind=bind)
        self.add(f"{quantity}.upper", expr, "le", hi, case=case, bind=bind)

    def common(self, *, history: bool = True) -> None:
        cfl = ("derived.dt * max((abs({s}.hu / {s}.h) + sqrt(inputs.constants.g * {s}.h)) / derived.dx"
               " + (abs({s}.hv / {s}.h) + sqrt(inputs.constants.g * {s}.h)) / derived.dy)")
        self.add("cfl.max", cfl.format(s="final"), "le", 1.0)
        if history:
            self.add("cfl.max", "derived.dt * max(smax((abs(history.hu / history.h) + sqrt(inputs."
                     "constants.g * history.h)) / derived.dx + (abs(history.hv / history.h) + "
                     "sqrt(inputs.constants.g * history.h)) / derived.dy))", "le", 1.0)
        self.add("run.n_step", "final.step - derived.n_step", "eq", 0)
        for v in VARS:
            self.add("cross_target_state_agreement",
                     f"maxabs(final.{v} - comparand.final.{v}) / max(maxabs(final.{v}), "
                     f"maxabs(comparand.final.{v}), 1.0)", "le", 1.0e-10)

    def mass(self, bound: float = 1.0e-12) -> None:
        self.add("mass_drift_rel", "abs(sum(final.h) - sum(initial.h)) / sum(initial.h)", "le",
                 bound)


def tests_and_predicates(case_set: list[dict[str, Any]]) -> tuple[list, list]:
    bands = load_bands()
    tests: list[dict[str, Any]] = []
    preds: list[dict[str, Any]] = []

    # 6-1 the rotation alone against the closed form of the RK4 polynomial (judgments 1, 2)
    t = _T(tests, preds, "l1_rotation_closed_form", "pass", ROT_CASES)
    cf = {"th": "inputs.rotation.f0 * inputs.rotation.every * derived.dt",
          "a": "1 - th * th / 2 + th ** 4 / 24", "b": "-th + th ** 3 / 6",
          "rho": "sqrt(a * a + b * b)", "psi": "atan2(b, a)",
          "u0": "inputs.initial.mom_x0", "v0": "inputs.initial.mom_y0",
          "w0": "sqrt(u0 * u0 + v0 * v0)"}
    fin = {**cf, "m": "floor(derived.n_step / inputs.rotation.every)",
           "eu": "rho ** m * (u0 * cos(m * psi) - v0 * sin(m * psi))",
           "ev": "rho ** m * (u0 * sin(m * psi) + v0 * cos(m * psi))"}
    t.add("rotation.closed_form.final_rel",
          "max(maxabs(final.hu - eu), maxabs(final.hv - ev)) / w0", "le", 1.0e-12, bind=fin)
    his = {**cf, "m": "floor(history.step / inputs.rotation.every)",
           "eu": "rho ** m * (u0 * cos(m * psi) - v0 * sin(m * psi))",
           "ev": "rho ** m * (u0 * sin(m * psi) + v0 * cos(m * psi))"}
    t.add("rotation.closed_form.history_rel",
          "max(max(abs(smax(history.hu) - eu)), max(abs(smin(history.hu) - eu)), "
          "max(abs(smax(history.hv) - ev)), max(abs(smin(history.hv) - ev))) / w0",
          "le", 1.0e-12, bind=his)
    t.common()

    # 6-2 the inertia-gravity wave on an f-plane (judgment 3)
    t = _T(tests, preds, "l2_inertia_gravity_wave_phase", "pass", PW_CASES)
    # The phase of the mode cos(kappa x - phi) of h - H_0 from two first moments in x: with
    # D = nx dx / (2 sin(kappa dx / 2)), sum(x (h - H_0)) = -A D sin(phi) and, the field moved by a
    # quarter wavelength (nx / 4 cells, `roll`), sum(x (h - H_0)) = -A D cos(phi), exactly on the
    # cell centres (tests.md 5-4).
    q = "inputs.grid.nx / 4"
    pw = {"om": "sqrt(inputs.rotation.f0 * inputs.rotation.f0 + inputs.constants.g * "
                "inputs.constants.H_0 * (2 * pi / inputs.grid.L_x) ** 2)"}
    b = bands.get("wave.phase_error", {}).get("upper", 1.0e300)
    t.add("wave.phase_error_max", "max(abs(atan2(sin(d), cos(d))))", "le", b,
          bind={**pw, "s1": "ssum((history.h - inputs.constants.H_0) * x)",
                "s2": f"ssum((roll(history.h, 0, {q}, 0) - inputs.constants.H_0) * x)",
                "d": "atan2(-s1, -s2) - om * history.t"})
    t.add("wave.phase_error_final", "abs(atan2(sin(d), cos(d)))", "le", b,
          bind={**pw, "s1": "sum((final.h - inputs.constants.H_0) * x)",
                "s2": f"sum((roll(final.h, {q}, 0) - inputs.constants.H_0) * x)",
                "d": "atan2(-s1, -s2) - om * final.t"})
    t.mass()
    t.common()

    # 6-3 mass over every sample of the shear-flow cases (judgment 4)
    t = _T(tests, preds, "l2_mass_conservation", "pass", SHEAR_ALL)
    t.add("mass_drift_rel.history", "max(abs(ssum(history.h) - sum(initial.h))) / sum(initial.h)",
          "le", 1.0e-12)
    t.mass()
    t.add("extrema.h.min", "min(min(initial.h), min(final.h), min(smin(history.h)))", "ge", 0.5)
    t.common()

    # 6-4 the process off, the member's cone case (judgment 5 (i))
    t = _T(tests, preds, "l2_process_off_member_cone", "pass", OFF_CONE)
    t.band("response.transverse_momentum.l2_rel_tend", "norm2(final.hv) / norm2(initial.hu)",
           {"response.transverse_momentum.l2_rel_tend": {"lower": 0.016, "upper": 0.021}})
    t.add("off.rotation_inputs_inert",
          "max(maxabs(final.h - bh), maxabs(final.hu - bu), maxabs(final.hv - bv)) / "
          "max(maxabs(bh), maxabs(bu), maxabs(bv))", "le", 0.0, case="swr_off_cone_n064_twin",
          bind={f"b{v[-1] if v != 'h' else 'h'}": f"at('swr_off_cone_n064').final.{v}"
                for v in VARS})
    t.mass(1.0e-10)  # the member's bound on its cone case, restated (tests.md 5-5)
    t.add("extrema.h.min", "min(min(initial.h), min(final.h))", "ge", 0.05)
    t.common()

    # 6-5 the process off, the member's flat linear-wave case (judgment 5 (ii))
    t = _T(tests, preds, "l1_process_off_member_flat", "pass", OFF_FLAT)
    lw = {"c0": "sqrt(inputs.constants.g * inputs.constants.H_0)",
          "ph": "2 * pi * (x / inputs.grid.L_x - inputs.initial.wave_sign * c0 * "
                "inputs.time.t_end / inputs.grid.L_x - inputs.initial.shift_x_fraction)",
          "h_ref": "inputs.constants.H_0 + inputs.initial.eta0 * sin(ph)",
          "hu_ref": "h_ref * (inputs.initial.wave_sign * c0 / inputs.constants.H_0 * "
                    "(h_ref - inputs.constants.H_0))",
          "hv_ref": "0 * h_ref"}
    member_bands = {  # the member's 5-5 at nx = 64, restated
        ("h", "l1"): (1.0e-4, 1.3e-4), ("h", "l2"): (1.1e-4, 1.4e-4),
        ("h", "linf"): (1.5e-4, 2.0e-4), ("hu", "l1"): (0.15, 0.20),
        ("hu", "l2"): (0.15, 0.20), ("hu", "linf"): (0.15, 0.20)}
    for (v, nrm), (lo, hi) in member_bands.items():
        d = f"final.{v} - {v}_ref"
        expr = NORMS[nrm].format(d=d, n1=f"sum(abs({v}_ref))", n2=f"norm2({v}_ref)",
                                 ni=f"maxabs({v}_ref)")
        t.band(f"errors.analytic_{v}.{nrm}_rel_tend", expr,
               {f"errors.analytic_{v}.{nrm}_rel_tend": {"lower": lo, "upper": hi}}, bind=lw)
    for nrm in NORMS:
        expr = NORMS[nrm].format(d="final.hv - hv_ref",
                                 n1="max(sum(abs(hu_ref)), sum(abs(hv_ref)))",
                                 n2="max(norm2(hu_ref), norm2(hv_ref))",
                                 ni="max(maxabs(hu_ref), maxabs(hv_ref))")
        t.add(f"errors.analytic_hv.{nrm}_rel_tend", expr, "le", 1.0e-10, bind=lw)
    t.mass()
    t.common()

    # 6-6 the splitting order and the non-uniform f (judgment 6)
    t = _T(tests, preds, "l3_splitting_order_nonuniform_f", "pass", ORDER_CASES)
    for v in ("hu", "hv"):
        t.band(f"order.{v}_rms", f"sqrt(mean(final.{v} * final.{v}))", bands)
        t.band(f"order.{v}_mean", f"mean(final.{v})", bands)
    t.mass()
    t.common()

    # 6-7 .. 6-10 the symmetry pairs (judgment 7)
    tf = {"axis_exchange": {"h": "transpose(bh)", "hu": "transpose(bv)", "hv": "transpose(bu)"},
          "mirror_x": {"h": "flip(bh, 0)", "hu": "-flip(bu, 0)", "hv": "flip(bv, 0)"},
          "mirror_y": {"h": "flip(bh, 1)", "hu": "flip(bu, 1)", "hv": "-flip(bv, 1)"},
          "translation": {"h": "roll(bh, 16, 8)", "hu": "roll(bu, 16, 8)",
                          "hv": "roll(bv, 16, 8)"}}
    for pair, cid in PAIRS.items():
        t = _T(tests, preds, f"l3_{pair}_pair", "pass", ("swr_shear_n064", cid))
        bind = {"bh": "at('swr_shear_n064').final.h", "bu": "at('swr_shear_n064').final.hu",
                "bv": "at('swr_shear_n064').final.hv"}
        for v in VARS:
            norm = "norm2(bh)" if v == "h" else "max(norm2(bu), norm2(bv))"
            t.add(f"errors.{pair}.{v}_l2_rel", f"norm2(final.{v} - {tf[pair][v]}) / {norm}",
                  "le", 2.0e-11, case=cid, bind=bind)
        t.mass()
        t.common()

    # 6-11 the refinement judgment against the restricted reference (judgment 8)
    t = _T(tests, preds, "l1_refinement_restricted", "pass", REFINE)
    fine = REFINE[-1]
    levels = {"n032": (REFINE[0], 8), "n064": (REFINE[1], 4), "n128": (REFINE[2], 2)}

    def err(level: str, v: str, nrm: str) -> tuple[str, dict[str, str]]:
        cid, fac = levels[level]
        bind = {"qc": f"at('{cid}').final.{v}", "qr": f"coarsen(final.{v}, {fac})"}
        # each variable by the same norm of its own restricted reference: neither momentum
        # component is zero or a residue of the flow here (docs/TESTS.md §Refinement judgment)
        n = {"l1": "sum(abs(qr))", "l2": "norm2(qr)", "linf": "maxabs(qr)"}[nrm]
        d = {"l1": "sum(abs(qc - qr))", "l2": "norm2(qc - qr)", "linf": "maxabs(qc - qr)"}[nrm]
        return f"{d} / {n}", bind

    for level in levels:
        for v in VARS:
            for nrm in NORMS:
                expr, bind = err(level, v, nrm)
                t.band(f"errors.restricted.{level}.{v}_{nrm}_rel", expr, bands, case=fine,
                       bind=bind)
    for a_, b_ in (("n032", "n064"), ("n064", "n128")):
        for v in VARS:
            for nrm in NORMS:
                ea, ba = err(a_, v, nrm)
                eb, bb = err(b_, v, nrm)
                bind = {f"{k}a": val for k, val in ba.items()}
                bind.update({f"{k}b": val for k, val in bb.items()})
                for k in ba:
                    ea = ea.replace(k, f"{k}a")
                for k in bb:
                    eb = eb.replace(k, f"{k}b")
                bind["ea"], bind["eb"] = ea, eb
                q = f"convergence.restricted.{a_}_to_{b_}.{v}_{nrm}_order"
                t.add(q, "log2(ea / eb)", "ge", bands.get(q, {}).get("lower", -1.0e300),
                      case=fine, bind=bind)
    t.mass()
    t.common()

    # 6-12 .. 6-15 the guards (judgment 9)
    for tid, cids in GUARDS.items():
        t = _T(tests, preds, tid, "xfail", cids)
        t.add("ok", "final.ok", "eq", 0)
        if tid == "l0_member_refusal_xfail":
            t.add("run.state_moved", "maxabs(final.h - initial.h)", "gt", 0.0)
    return tests, preds


# ---------------------------------------------------------------------------------------------
# the IR skeleton


def skeleton(member_ir: dict[str, Any]) -> dict[str, Any]:
    ir = copy.deepcopy(member_ir)
    op = f"{SID}__step"
    ir["meta"].update({"node_key": NODE_KEY, "spec_id": SID, "spec_version": VERSION,
                       "source_refs": {"controlled_spec": f"{SPEC_DIR}/controlled_spec.md",
                                       "tests": f"{SPEC_DIR}/tests.md",
                                       "deps": f"{SPEC_DIR}/deps.yaml"}})
    ir["dependency"] = {
        "node_key": NODE_KEY,
        "direct_deps": [
            {"kind": "problem", "node_key": "problem/shallow_water2d@0.5.1",
             "operations": ["shallow_water2d__step"]},
            {"kind": "component", "node_key": f"component/{CORIOLIS}@0.2.0",
             "operations": [f"{CORIOLIS}__apply"]},
            {"kind": "component", "node_key": f"component/{RK4}@0.1.1",
             "operations": [f"{RK4}__advance"]}]}
    step = ir["drive"]["step"]
    step["operation"] = op
    step["arguments"] = {**{k: v for k, v in step["arguments"].items() if k != "z_b"
                            and k not in ("h", "hu", "hv", "ok")},
                         "step": "step", "rotation": "inputs.rotation.on",
                         "rotation_every": "inputs.rotation.every", "f": "state.f",
                         "z_b": "state.z_b", "h": "state.h", "hu": "state.hu", "hv": "state.hv",
                         "ok": "state.ok"}
    entry = ir["public_api"]["signatures"][0]
    args: list[dict[str, Any]] = []
    real = {"type": "real", "kind": "dp"}
    for arg in entry["signature"]["args"]:
        if arg["name"] == "z_b":
            args += [{"name": n, "rank": 0, "intent": "in", "spec": {"type": "integer"}}
                     for n in ("step", "rotation", "rotation_every")]
            args.append({"name": "f", "rank": 2, "intent": "in", "spec": dict(real),
                         "dims": ["nx", "ny"]})
        args.append(arg)
    entry["signature"]["args"] = args
    entry["signature"]["name"] = entry["symbol"] = op
    ir["public_api"]["published_operations"][0]["operation_id"] = op
    variables = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"]
    variables.insert([v["name"] for v in variables].index("z_b") + 1,
                     {"name": "f", "shape_expr": "[nx, ny]"})
    for ev in ir["io_contract"]["raw_requirements"]["required_evidence"]:
        if ev.get("artifact") == "step_series":
            ev["names"] = [*ev["names"], "f_min", "f_max"]
    ir["case"]["sweeps"] = {}
    ir["case"]["refinements"] = {}
    ir["io_contract"]["tests"] = []
    ir["io_contract"]["predicates"] = []
    return ir


def build() -> dict[str, Any]:
    member_ir = yaml.safe_load(MEMBER_IR.read_text())
    ir = skeleton(member_ir)
    ir["case"]["test_case_set"] = cases(member_ir)
    tests, preds = tests_and_predicates(ir["case"]["test_case_set"])
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
    print(a.out, len(ir["case"]["test_case_set"]), "cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
