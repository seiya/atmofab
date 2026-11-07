"""The `drive` readers of `tools/runner_ir.py` (issue #589 PR-1a): what the PR-1b renderers will
ask of an IR's `drive` block. No renderer calls them yet. Each reader has its admitted and its
refused rows, and `DriveAgreementTest` holds them equal to `tools/primary_evidence.py`'s reading
of the same block — the two modules sit in different transformation tuples (render, verdict) and
do not import each other, so this row is what keeps the two readings one."""

from __future__ import annotations

import copy
import unittest

import yaml

from tools import primary_evidence as pe
from tools import runner_ir as r
from tools.host_render import RenderError
from tools.tests.test_primary_evidence import (_BENCH_DIR, _DRIVE_DIR, _coriolis, _rk4_renamed,
                                               _rus, _toy, _toyi)


def _swe2d() -> dict:
    return yaml.safe_load((_DRIVE_DIR / "shallow_water2d_0.4.7" / "spec.ir.yaml").read_text())


class BindingTest(unittest.TestCase):
    def test_admitted_forms(self) -> None:
        for value, want in (("inputs.grid.nx", ("inputs", "grid.nx")),
                            (" derived.dt ", ("derived", "dt")), ("state.h", ("state", "h")),
                            ("time", ("time", None)), ("step", ("step", None)),
                            (2, ("literal", 2)), (0.5, ("literal", 0.5)),
                            (True, ("literal", True)),
                            ({"procedure": " f(x) "}, ("procedure", ("f(x)", None, ()))),
                            ({"procedure": "u", "calls": "state.n",
                              "record": {"t": "state.st", "u": ["state.a", "state.b"]}},
                             ("procedure", ("u", "n", (("t", ("st",)), ("u", ("a", "b"))))))):
            with self.subTest(value=value):
                self.assertEqual(r.drive_binding(value), want)

    def test_refused_forms(self) -> None:
        for value in ("inputs", "inputs.", "inputs.a..b", "derived.a.b", "state.", "state.1x",
                      "clock", "final.h", float("nan"), float("inf"), None, [],
                      {"procedure": ""}, {"procedure": "f", "x": 1}, {"expr": "f"}):
            with self.subTest(value=value), self.assertRaisesRegex(RenderError, "drive binding"):
                r.drive_binding(value)


class ReadersTest(unittest.TestCase):
    def test_the_block(self) -> None:
        self.assertEqual(set(r.drive_block(_toy())), {"step", "loop", "history", "distribute"})
        with self.assertRaisesRegex(RenderError, "IR has no drive block"):
            r.drive_block({})
        ir = _toy()
        ir["drive"]["extra"] = 1
        with self.assertRaisesRegex(RenderError, r"unknown key\(s\) \['extra'\]"):
            r.drive_block(ir)

    def test_the_mapping_step_and_init(self) -> None:
        steps = r.drive_steps(_toy())
        self.assertEqual(len(steps), 1)
        self.assertEqual((steps[0]["operation"], steps[0]["result"], steps[0]["cases"]),
                         ("toy__step", "ok", None))
        self.assertEqual(steps[0]["bindings"]["h"], ("state", "h"))
        self.assertEqual([d["name"] for d in steps[0]["dummies"]], ["nx", "dt", "t", "h", "ok"])
        self.assertIsNone(r.drive_init(_toy()))
        init = r.drive_init(_toyi())
        self.assertEqual((init["operation"], init["result"]), ("toyi__init", "init_ok"))
        bad = _toyi()
        bad["drive"]["init"] = [bad["drive"]["init"]]
        with self.assertRaisesRegex(RenderError, "drive.init is one mapping entry"):
            r.drive_init(bad)

    def test_the_list_form_carries_its_cases(self) -> None:
        rus = _rus()
        entry = rus["drive"]["step"]
        rus["drive"]["step"] = [{**entry, "cases": ["a"]}, {**entry, "cases": [" b "]}]
        self.assertEqual([s["cases"] for s in r.drive_steps(rus)], [["a"], ["b"]])
        rus["drive"]["step"][1]["cases"] = []
        with self.assertRaisesRegex(RenderError, r"drive.step\[1\].cases must be a non-empty"):
            r.drive_steps(rus)
        rus["drive"]["step"] = []
        with self.assertRaisesRegex(RenderError, "drive.step is an empty list"):
            r.drive_steps(rus)

    def test_a_malformed_entry_is_refused(self) -> None:
        rows = [
            (lambda d: d.pop("step"), "drive.step is required"),
            (lambda d: d["step"].pop("result"), "drive.step.result must name"),
            (lambda d: d["step"].update(operation=""), "drive.step.operation must name"),
            (lambda d: d["step"].update(arguments=[]), "drive.step.arguments must be a mapping"),
            (lambda d: d["step"]["arguments"].pop("t"), r"drive.step.arguments binds"),
            (lambda d: d["step"].update(cases=["a"]), r"drive.step carries unknown key"),
            (lambda d: d["step"].update(operation="toy__nope"), "'toy__nope' has no public_api"),
            (lambda d: d["step"]["arguments"].update(t="clock"), "drive binding 'clock'"),
        ]
        for fn, msg in rows:
            with self.subTest(msg=msg):
                ir = _toy()
                fn(ir["drive"])
                with self.assertRaisesRegex(RenderError, msg):
                    r.drive_steps(ir)

    def test_loop_and_history(self) -> None:
        self.assertEqual(r.drive_loop(_toy()), {
            "n_step": ("derived", "n_step"), "dt": ("derived", "dt"),
            "t_start": ("inputs", "time.t_start"), "t_end": ("inputs", "time.t_end")})
        self.assertEqual(r.drive_loop(_toyi())["n_step"], ("state", "n_plan"))
        self.assertEqual(r.drive_history_every(_toy()), "history.every")
        self.assertIsNone(r.drive_loop(_coriolis()))
        self.assertIsNone(r.drive_history_every(_coriolis()))
        for fn, msg in ((lambda d: d["loop"].pop("t_end"), "drive.loop must carry exactly"),
                        (lambda d: d["loop"].update(dt="time"), "drive.loop.dt must be"),
                        (lambda d: d["loop"].update(dt=0.1), "drive.loop.dt must be")):
            with self.subTest(msg=msg):
                ir = _toy()
                fn(ir["drive"])
                with self.assertRaisesRegex(RenderError, msg):
                    r.drive_loop(ir)
        for every in ("derived.k", {"every": "derived.k"}, {"every": "inputs.a", "x": 1}):
            with self.subTest(every=every):
                ir = _toy()
                ir["drive"]["history"] = every
                with self.assertRaisesRegex(RenderError, "drive.history must be"):
                    r.drive_history_every(ir)


def _binding(b: pe.Binding) -> tuple:
    """`pe.Binding` in `runner_ir.drive_binding`'s shape: a procedure's `ref` holds all three."""
    return (b.kind, (b.ref, b.calls, b.record)) if b.kind == "procedure" else (b.kind, b.ref)


class DriveAgreementTest(unittest.TestCase):
    """`runner_ir.drive_state_classes` equals `primary_evidence.drive_model`'s classes, and the
    bindings equal its `parse_binding`, on every fixture of the drive shape."""

    def _fixtures(self) -> dict[str, dict]:
        rus_list = _rus()
        entry = rus_list["drive"]["step"]
        rus_list["drive"]["step"] = [{**entry, "cases": ["a"]}]
        return {"coriolis": _coriolis(), "swe2d": _swe2d(), "toy": _toy(), "toyi": _toyi(),
                "rus": _rus(), "rus_list": rus_list}

    def test_the_classes_agree(self) -> None:
        for name, ir in self._fixtures().items():
            with self.subTest(ir=name):
                model = pe.drive_model(ir)
                self.assertEqual(r.drive_state_classes(ir), {
                    "init_written": model.init_written, "written": model.written,
                    "read": model.read, "logical": model.logical,
                    "result_states": model.result_states,
                    "operation_only": model.operation_only})

    def test_the_bindings_and_the_entries_agree(self) -> None:
        for name, ir in self._fixtures().items():
            with self.subTest(ir=name):
                block = pe.drive_block(ir)
                steps = r.drive_steps(ir)
                self.assertEqual([(s["operation"], s["result"], s["cases"]) for s in steps],
                                 [(e.operation, e.result, list(e.cases) if e.cases else None)
                                  for e in block.step])
                for s, e in zip(steps, block.step):
                    self.assertEqual(s["bindings"], {k: _binding(b) for k, b in e.arguments.items()})
                loop = r.drive_loop(ir)
                self.assertEqual(loop, None if block.loop is None
                                 else {k: _binding(b) for k, b in block.loop.items()})
                self.assertEqual(r.drive_history_every(ir), block.history_every)

    def test_the_layouts_agree(self) -> None:
        """Issue #610 (plan §3.2): gate (n)'s derivation (`primary_evidence.distribute_layouts`,
        over its entry parser) and the renderer's (`runner_ir.distribute_layouts`, over the
        render readers) give equal layouts over every bench IR and the two renamed-prototype
        rk4 variants, where a lookup of the recorded dummy's name among the states differs."""
        irs = {p.stem: yaml.safe_load(p.read_text()) for p in sorted(_BENCH_DIR.glob("*.yaml"))}
        irs.update({f"rk4_{new}": _rk4_renamed(new) for new in ("V", "stage_t")})
        irs.update(self._fixtures())
        with_axes = 0
        for name, ir in irs.items():
            with self.subTest(ir=name):
                variables = {v["name"]: v["shape_expr"]
                             for v in pe.snapshot_schema(ir)["variables"]}
                layouts, refusals = r.distribute_layouts(ir, variables)
                self.assertEqual(refusals, [])
                self.assertEqual(layouts, pe.distribute_layouts(ir))
                with_axes += bool(layouts)
        self.assertEqual(with_axes, 10 + 2 + 2)   # 10 bench nodes, 2 variants, coriolis, swe2d
        for new in ("V", "stage_t"):
            variables = {v["name"]: v["shape_expr"]
                         for v in pe.snapshot_schema(irs[f"rk4_{new}"])["variables"]}
            layouts, _ = r.distribute_layouts(irs[f"rk4_{new}"], variables)
            self.assertEqual(layouts["stage_U1"], (None, (0, "cell"), (1, "cell")))

    def test_the_classes_on_the_known_fixtures(self) -> None:
        self.assertEqual(r.drive_state_classes(_toyi())["init_written"],
                         frozenset({"h", "dt", "n_plan", "init_ok"}))
        self.assertEqual(r.drive_state_classes(_coriolis())["operation_only"], frozenset({"S"}))
        mutated = copy.deepcopy(_rus())
        mutated["public_api"]["signatures"][0]["signature"]["args"][2]["intent"] = "inout"
        self.assertEqual(r.drive_state_classes(mutated)["operation_only"], frozenset({"a_x"}))
        self.assertIn("F", r.drive_state_classes(mutated)["read"])


#: Plan §8 (p): the `pass` cases `distribute_plan` distributes per grid, over the bench.
_GRIDS = ((2, 2), (4, 1), (1, 4), (3, 1), (1, 3), (2, 1), (1, 2))
_CENSUS = {
    "dynamics_advdiff_flux_1d_upwind_center2": (8, 1, 0, 1, 0, 8, 0),
    "dynamics_advection_diffusion_time_update_1d_euler1": (3, 3, 0, 3, 0, 3, 0),
    "dynamics_shallow_water_reconstruction_2d_muscl_mc": (9,) * 7,
    "dynamics_shallow_water_source_2d_coriolis": (6, 5, 1, 6, 6, 6, 6),
    "dynamics_shallow_water_source_2d_tc4_forcing": (18, 14, 14, 18, 18, 18, 18),
    "dynamics_shallow_water_time_update_2d_rk4": (7, 6, 1, 7, 7, 7, 7),
    "dynamics_shallow_water_time_update_2d_ssprk2": (8, 7, 1, 8, 8, 8, 8),
    "advdiff1d_linear": (7, 7, 0, 7, 0, 7, 0),
    "shallow_water2d": (24,) * 7,
    "shallow_water2d_channel": (34,) * 7,
}
_C = lambda k: (k, "cell")   # noqa: E731
_F = lambda k: (k, "face")   # noqa: E731
_G = lambda k: (k, "ghost")  # noqa: E731
#: Plan §4: the derived layout of every array of every node with axes.
_LAYOUTS = {
    "dynamics_advdiff_flux_1d_upwind_center2": {
        "u": (_G(0),), "flux_adv": (_F(0),), "flux_dif": (_F(0),)},
    "dynamics_advection_diffusion_time_update_1d_euler1": {
        "u_n": (_C(0),), "L_flux": (_C(0),), "u_np1": (_C(0),)},
    "dynamics_shallow_water_reconstruction_2d_muscl_mc": {
        "U_in": (_G(0), _G(1)), "U_L": (_F(0), _C(1)), "U_R": (_F(0), _C(1)),
        "U_B": (_C(0), _F(1)), "U_T": (_C(0), _F(1))},
    "dynamics_shallow_water_source_2d_coriolis": {
        "U": (None, _C(0), _C(1)), "S": (None, _C(0), _C(1)), "derived.f": (_C(0), _C(1))},
    "dynamics_shallow_water_source_2d_tc4_forcing": {
        "S": (None, _C(0), _C(1)), "derived.x": (_C(0),), "derived.y": (_C(1),),
        **{f"derived.{n}": (_C(0), _C(1)) for n in ("f", "dfdr", "ubar", "dubar", "hbar")}},
    "dynamics_shallow_water_time_update_2d_rk4": {
        "U": (None, _C(0), _C(1)), "stage_t": (None,),
        **{f"stage_U{i}": (None, _C(0), _C(1)) for i in range(1, 5)}},
    "dynamics_shallow_water_time_update_2d_ssprk2": {
        **{n: (None, _C(0), _C(1)) for n in ("U_n", "L_flux", "S_b", "U_np1")},
        "z_b": (_C(0), _C(1))},
    "advdiff1d_linear": {"u": (_C(0),)},
    "shallow_water2d": {n: (_C(0), _C(1)) for n in ("h", "hu", "hv", "z_b")},
    "shallow_water2d_channel": {n: (_C(0), _C(1)) for n in ("h", "hu", "hv")},
}


def _bench_ir(spec_id: str) -> dict:
    return yaml.safe_load((_BENCH_DIR / f"{spec_id}.yaml").read_text())


def _variables(ir: dict) -> dict:
    return {v["name"]: v["shape_expr"] for v in pe.snapshot_schema(ir)["variables"]}


def _roles(table: dict) -> object:
    return lambda name: table.get(name)


class DistributeBlockTests(unittest.TestCase):
    """`drive.distribute`, `block.<axis>.first`, `array_layouts` and `distribute_plan` (issue
    #610, plan §3.1-§3.2)."""

    def test_an_absent_distribute_reads_as_no_axes_for_a_render(self) -> None:
        ir = _toy()
        del ir["drive"]["distribute"]
        self.assertEqual(r.drive_distribute_axes(ir), ())
        self.assertEqual(r.distribute_layouts(ir, _variables(ir)), ({}, []))
        self.assertFalse(any(r.distribute_plan(ir, c, (2, 2))
                             for c in ir["case"]["test_case_set"]))
        plan = r.driver_plan(ir, _variables(ir))
        self.assertEqual(plan, r.driver_plan(_toy(), _variables(ir)))
        ir["drive"]["distribute"] = {"axes": "x"}
        with self.assertRaisesRegex(RenderError, r"drive.distribute must be \{axes"):
            r.drive_distribute_axes(ir)

    def test_layouts_derive_from_dims_and_shape_expr_tokens(self) -> None:
        for spec_id, want in _LAYOUTS.items():
            with self.subTest(spec_id=spec_id):
                ir = _bench_ir(spec_id)
                self.assertEqual(r.distribute_layouts(ir, _variables(ir)), (want, []))
        for path in sorted(_BENCH_DIR.glob("*.yaml")):
            if path.stem not in _LAYOUTS:
                ir = yaml.safe_load(path.read_text())
                self.assertEqual(r.drive_distribute_axes(ir), (), path.stem)

    def test_a_dims_token_naming_an_axis_input_in_another_form_is_refused(self) -> None:
        roles = _roles({"nx": ("cells", 0), "ny": ("cells", 1), "nxt": ("extended", 0),
                        "ng": "ghost", "dx": "derived"})
        for token in ("nx*ny", "nx + 2", "nx - 1", "2*nx", "nx + ng", "nx + 3*ng", "nxt + 1",
                      "nx + nxt", "nx + dx", "dx", "nx + ncomp", "nx/2", "nx**2", "nxt - 2*ng",
                      "nx +",
                      # twice a name that is no declared ghost width
                      "nx + 2*ncomp",
                      # the ghost-extended count plus twice the ghost: the margin counted twice
                      "nxt + 2*ng"):
            with self.subTest(token=token):
                layouts, refusals = r.array_layouts({"a": ["ncomp", token]}, roles, 2)
                self.assertEqual(layouts, {})
                self.assertEqual(len(refusals), 1, refusals)
                self.assertTrue(refusals[0].startswith(f"a: dims token {token!r}"), refusals)
        for tokens, want in ((["nx"], (_C(0),)), (["nx + 1"], (_F(0),)), (["1 + nx"], (_F(0),)),
                             (["nxt"], (_G(0),)), (["nx + 2*ng"], (_G(0),)),
                             (["2*ng + nx"], (_G(0),)), (["ny", "nx"], (_C(1), _C(0)))):
            with self.subTest(tokens=tokens):
                self.assertEqual(r.array_layouts({"a": tokens}, roles, 2), ({"a": want}, []))
        # one spatial axis on two array dimensions
        layouts, refusals = r.array_layouts({"a": ["nx", "nx + 1"]}, roles, 2)
        self.assertEqual((layouts, len(refusals)), ({}, 1))
        self.assertIn("one spatial axis on two array dimensions", refusals[0])
        # a cells name past the declared count is no axis
        self.assertEqual(r.array_layouts({"a": ["ny"]}, roles, 1), ({"a": (None,)}, []))

    def test_a_token_naming_ghost_alone_is_not_spatial(self) -> None:
        roles = _roles({"nx": ("cells", 0), "ng": "ghost", "ncomp": None})
        for token in ("ng", "2*ng + 1", "ncomp", "4", "ncomp + 1", "ka"):
            with self.subTest(token=token):
                self.assertEqual(r.array_layouts({"a": [token]}, roles, 1), ({"a": (None,)}, []))

    def test_one_ghost_input_may_serve_two_axes(self) -> None:
        roles = _roles({"nx": ("cells", 0), "ny": ("cells", 1), "ng": "ghost"})
        self.assertEqual(r.array_layouts({"U": ["nx + 2*ng", "ny + 2*ng"]}, roles, 2),
                         ({"U": (_G(0), _G(1))}, []))
        ir = _bench_ir("dynamics_shallow_water_reconstruction_2d_muscl_mc")
        axes = r.drive_distribute_axes(ir)
        self.assertEqual({a.ghost for a in axes}, {"grid.ng"})
        args = ir["public_api"]["signatures"][0]["signature"]["args"]
        next(a for a in args if a["name"] == "U_in")["dims"] = ["nx + 2*ng", "ny + 2*ng"]
        self.assertEqual(r.distribute_layouts(ir, _variables(ir))[0]["U_in"], (_G(0), _G(1)))
        self.assertEqual(pe.distribute_layouts(ir)["U_in"], (_G(0), _G(1)))

    def test_block_first_binds_an_integer_in_dummy_only(self) -> None:
        self.assertEqual(r.drive_binding("block.x.first"), ("block", "x"))
        self.assertEqual(pe.parse_binding("block.x.first"), pe.Binding("block", "x"))
        self.assertEqual(pe.parse_binding("block.x.first").text(), "block.x.first")
        for raw in ("block.x", "block.x.last", "block.1x.first", "block..first", "block"):
            with self.subTest(raw=raw):
                with self.assertRaises(RenderError):
                    r.drive_binding(raw)
                with self.assertRaises(pe.PrimaryEvidenceError):
                    pe.parse_binding(raw)

        def with_i0(dtype: str, axis: str = "x", intent: str = "in") -> dict:
            ir = _toy()
            ir["drive"]["distribute"] = {"axes": [{"name": "x", "cells": "inputs.grid.nx"}]}
            args = ir["public_api"]["signatures"][0]["signature"]["args"]
            args.insert(1, {"name": "i0", "rank": 0, "intent": intent, "spec": {"type": dtype}})
            ir["drive"]["step"]["arguments"]["i0"] = f"block.{axis}.first"
            return ir

        ir = with_i0("integer")
        self.assertEqual(pe.drive_violations(ir, kind="problem"), [])
        step = r.driver_plan(ir, _variables(ir)).entries[0].step
        self.assertEqual(step.actuals[1], r.Actual("block_first", "x"))
        for bad, gate in ((with_i0("real"), "(b)"), (with_i0("integer", intent="out"), "(b)"),
                          (with_i0("integer", axis="y"), "(n)")):
            with self.subTest(gate=gate):
                with self.assertRaises(RenderError):
                    r.driver_plan(bad, _variables(bad))
                got = pe.drive_violations(bad, kind="problem")
                self.assertTrue(any("block." in m for m in got), got)
        self.assertIn("(n) drive.step.arguments.i0: block.y.first names no axis of "
                      "drive.distribute.axes (['x'])",
                      pe.drive_violations(with_i0("integer", axis="y"), kind="problem"))

    def test_the_render_readers_refuse_what_gate_n_refuses(self) -> None:
        """The renderer's own refusals over `drive.distribute` (PR-B2's backstop): gate (n) runs
        first on every admitted IR, so no stage of PR-B1 reaches these; each is pinned here on
        the render reader directly."""
        x = {"name": "x", "cells": "inputs.grid.nx"}
        for raw, want in (({"axes": [{**x, "halo": "inputs.grid.ng"}]}, "must be {name, cells"),
                          ({"axes": [{"name": "x"}]}, "cells is required"),
                          ({"axes": [{**x, "extended": "inputs.grid.nxt"}]},
                           "extended requires ghost"),
                          ({"axes": [{**x, "min_block": 0}]}, "min_block is a positive"),
                          ({"axes": [{**x, "cells": "derived.nx"}]}, "must be an inputs.<path>"),
                          ({"axes": [x, x]}, "each named once"),
                          ({"axes": [{**x, "name": n} for n in "xyz"]}, "at most two axes")):
            with self.subTest(raw=raw):
                ir = _toy()
                ir["drive"]["distribute"] = raw
                with self.assertRaises(RenderError) as ctx:
                    r.drive_distribute_axes(ir)
                self.assertIn(want, str(ctx.exception))
        # an array dummy without dims, and a recorded prototype dummy without dims
        ir = _bench_ir("dynamics_advdiff_flux_1d_upwind_center2")
        args = ir["public_api"]["signatures"][0]["signature"]["args"]
        del next(a for a in args if a["name"] == "u")["dims"]
        self.assertEqual(r.distribute_layouts(ir, _variables(ir))[1], [
            "u: the array dummy 'u' of dynamics_advdiff_flux_1d_upwind_center2__compute_flux "
            "has no dims on a node with axes"])
        ir = _bench_ir("dynamics_shallow_water_time_update_2d_rk4")
        proto = ir["public_api"]["interfaces"][0]["signature"]["args"]
        del next(a for a in proto if a["name"] == "U")["dims"]
        layouts, refusals = r.distribute_layouts(ir, _variables(ir))
        self.assertEqual(refusals, [f"stage_U{i}: the recorded dummy 'U' of the prototype has "
                                    "no dims on a node with axes" for i in range(1, 5)])
        self.assertFalse({f"stage_U{i}" for i in range(1, 5)} & set(layouts))
        # a rank-0 dummy name bound two ways reads as `derived`: no layout derives through it
        ir = _toyi()
        ir["drive"]["distribute"] = {"axes": [x]}
        self.assertEqual(r.distribute_layouts(ir, _variables(ir)), ({"h": (_C(0),)}, []))
        ir["drive"]["init"]["arguments"]["nx"] = 4
        self.assertEqual(r.distribute_layouts(ir, _variables(ir)), ({}, [
            "h: dims token 'nx' names ['nx'], bound to a derived value: an extent on a node "
            "with axes names a case input"]))
        # a ghost width that is not an integer replicates the case (the flux: ghost, no extended)
        flux = _bench_ir("dynamics_advdiff_flux_1d_upwind_center2")
        self.assertEqual([r.distribute_plan(flux, {"inputs": {"nx": 8, "ng": ng}}, (2, 1))
                          for ng in (1, 1.5, None)], [True, False, False])

    def test_distribute_plan_matches_the_census_table(self) -> None:
        never: dict[str, list[str]] = {}
        for path in sorted(_BENCH_DIR.glob("*.yaml")):
            ir = yaml.safe_load(path.read_text())
            passing = {c for t in ir["io_contract"]["tests"]
                       if str(t["expected_outcome"]).strip().lower() == "pass"
                       for c in t["target_cases"]}
            cases = [c for c in ir["case"]["test_case_set"] if c["case_id"] in passing]
            got = tuple(sum(r.distribute_plan(ir, c, g) for c in cases) for g in _GRIDS)
            with self.subTest(spec_id=path.stem):
                self.assertEqual(got, _CENSUS.get(path.stem, (0,) * len(_GRIDS)))
            never.update({f"{path.stem}:{c['case_id']}": [] for c in cases
                          if path.stem in _CENSUS
                          and not any(r.distribute_plan(ir, c, g) for g in _GRIDS)})
        # the six `pass` cases no grid distributes (plan B6)
        self.assertEqual(sorted(never), sorted([
            "dynamics_advdiff_flux_1d_upwind_center2:l0_min_extent",
            "dynamics_shallow_water_reconstruction_2d_muscl_mc:l0_min_extent",
            "dynamics_shallow_water_source_2d_coriolis:l0_min_extent",
            "dynamics_shallow_water_time_update_2d_rk4:l0_min_extent",
            "dynamics_shallow_water_source_2d_tc4_forcing:l0_min_extent",
            "dynamics_shallow_water_source_2d_tc4_forcing:l0_min_extent_axis"]))
        # the rule itself, on one hand-made 2-D case
        ir = _bench_ir("dynamics_shallow_water_reconstruction_2d_muscl_mc")
        case = {"inputs": {"grid": {"nx": 3, "ny": 2, "ng": 2, "nx_total": 7, "ny_total": 6}}}
        self.assertEqual([r.distribute_plan(ir, case, g) for g in _GRIDS],
                         [True, False, False, True, False, True, True])
        # every declared axis counts, split or not: (2, 1) splits x alone, and y's 0 cells
        # are under 1 * min_block
        empty_y = {"inputs": {"grid": {**case["inputs"]["grid"], "ny": 0, "ny_total": 4}}}
        self.assertFalse(r.distribute_plan(ir, empty_y, (2, 1)))
        case["inputs"]["grid"]["nx_total"] = 8   # inconsistent: replicated everywhere
        self.assertFalse(any(r.distribute_plan(ir, case, g) for g in _GRIDS))
        self.assertFalse(r.distribute_plan(ir, case, (1, 1)))
        flux = _bench_ir("dynamics_advdiff_flux_1d_upwind_center2")   # min_block 2, 1-D
        self.assertEqual([r.distribute_plan(flux, {"inputs": {"nx": n, "ng": 1}}, (2, 2))
                          for n in (3, 4)], [False, True])
        self.assertFalse(r.distribute_plan(flux, {"inputs": {"nx": 8, "ng": 1}}, (1, 4)))


if __name__ == "__main__":
    unittest.main()
