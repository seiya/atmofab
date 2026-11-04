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
from tools.tests.test_primary_evidence import _DRIVE_DIR, _coriolis, _rus, _toy, _toyi


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
        self.assertEqual(set(r.drive_block(_toy())), {"step", "loop", "history"})
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

    def test_the_classes_on_the_known_fixtures(self) -> None:
        self.assertEqual(r.drive_state_classes(_toyi())["init_written"],
                         frozenset({"h", "dt", "n_plan", "init_ok"}))
        self.assertEqual(r.drive_state_classes(_coriolis())["operation_only"], frozenset({"S"}))
        mutated = copy.deepcopy(_rus())
        mutated["public_api"]["signatures"][0]["signature"]["args"][2]["intent"] = "inout"
        self.assertEqual(r.drive_state_classes(mutated)["operation_only"], frozenset({"a_x"}))
        self.assertIn("F", r.drive_state_classes(mutated)["read"])


if __name__ == "__main__":
    unittest.main()
