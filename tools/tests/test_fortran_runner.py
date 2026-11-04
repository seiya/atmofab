#!/usr/bin/env python3
"""Unit tests for the Fortran backend's runner_render capability (issue #589: the host-rendered
driver).

`render_runner` is a pure function of the IR: these tests pin its rendered shape (a boundary-copy
IR and a higher-rank IR), determinism, the render-error matrix, and the harness signature pin. A
`gfortran`-gated smoke compiles and runs the rendered runner over a model holding the operation the
IR's `drive` names, against the reading harness of `data/driver_e2e/`. What the driver promises per
case is `test_driver_render`'s; this file is about the text and the refusals.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

import yaml

from tools import primary_evidence as pe
from tools.backends.language.fortran.runner import (
    CASE_ID_LEN,
    EXPECTED_HARNESS_SPEC_ID,
    _HARNESS_PINS,
    _HARNESS_V3_PARAMETERS,
    _HARNESS_V3_INTERFACE,
    _SINGLE_PROCESS_INTERFACE,
    _used_harness_ops,
    assert_harness_pin,
    ir_content_violations,
    render_runner,
)
from tools.backends.language.fortran.signatures import parse_signatures_from_fortran
from tools.host_render import RenderError
from tools.tests.target_fixtures import FORTRAN_CPU as _TARGET_PROFILE
from tools.tests.test_driver_render import call_ir, init_ir, loop_ir, procedure_ir

HARNESS = "harness_fortran_cpu"
BOUNDARY_SID = "dynamics_shallow_water_boundary_2d_periodic_copy"


def _require_gfortran() -> None:
    """A missing compiler fails with what to install (#498: a test in this suite does not skip)."""
    if shutil.which("gfortran") is None:
        raise AssertionError("gfortran is not on PATH; install gfortran (docs/RUNBOOK.md#0-1)")


def _require_declared_linter() -> None:
    """The declared lint invocation can decide a verdict on this host, or a failure that says
    what to install.

    Asks the linter backend the two questions it owns — is the executable here, and is this build
    inside the range the declared rule set was measured on — so the test never passes silently
    on a machine with no linter, nor on one whose linter the rule set was not measured on."""
    from tools.backends.linter.fortitude import lint as _lint
    if shutil.which(_lint.EXECUTABLE) is None:
        raise AssertionError(
            f"{_lint.EXECUTABLE} is not on PATH; the workflow's lint gate cannot run and neither "
            f"can these checks — install {_lint.SUPPORTED_VERSION_SPEC} (docs/RUNBOOK.md#0-1)")
    probe = subprocess.run(list(_lint.version_argv()), capture_output=True, text=True)
    reason = _lint.unsupported_version_reason(probe.stdout or probe.stderr)
    if reason is not None:
        raise AssertionError(
            f"{_lint.EXECUTABLE} is installed but outside the range the declared rule set was "
            f"measured on: {reason}; install {_lint.SUPPORTED_VERSION_SPEC}")


REAL = {"type": "real", "kind": "dp"}
DRIVER_E2E = Path(__file__).parent / "data" / "driver_e2e"


def _dummy(name: str, intent: str, spec: dict, rank: int = 0) -> dict:
    out = {"name": name, "intent": intent, "spec": spec}
    if rank:
        out["dims"] = [":"] * rank
        out["rank"] = rank
    return out


def _driven(ir: dict, op: str, states: dict[str, tuple[str, int]]) -> dict:
    """`ir` with the one operation it is driven by: an integer `mode` from the case's inputs, a
    real dummy per entry of `states` (name -> intent, rank) bound to the state of that name, and
    the logical result `ok`, which joins the snapshot schema. Assumed-shape dummies, so an array
    takes its extent from the case file."""
    dummies = [_dummy("mode", "in", {"type": "integer"}),
               *(_dummy(n, intent, REAL, rank) for n, (intent, rank) in states.items()),
               _dummy("ok", "out", {"type": "logical"})]
    ir["public_api"] = {
        "published_operations": [{"operation_id": op}],
        "module_parameters": [{"name": "dp", "value": "float64"}],
        "signatures": [{"symbol": op, "signature": {"kind": "subroutine", "name": op,
                                                    "args": dummies}}]}
    ir["drive"] = {"step": {"operation": op, "result": "ok", "arguments": {
        "mode": "inputs.mode", **{n: f"state.{n}" for n in states}, "ok": "state.ok"}}}
    schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
    schema["variables"].append({"name": "ok", "shape_expr": "scalar"})
    return ir


def _rename_state(ir: dict, old: str, new: str) -> None:
    """Rename a state where the IR names it: the schema and the operation's binding."""
    schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
    for var in schema["variables"]:
        if var["name"] == old:
            var["name"] = new
    arguments = ir["drive"]["step"]["arguments"]
    for dummy, binding in arguments.items():
        if binding == f"state.{old}":
            arguments[dummy] = f"state.{new}"


BOUNDARY_OP = f"{BOUNDARY_SID}__fill"


def _boundary_ir() -> dict:
    """A boundary_2d_periodic_copy-shaped IR: 3 cases (2 pass + 1 xfail), rank-2 + scalar snapshot
    variables, 3 tests, 1 infra dep, one call per case. Each case's `mode` is its ordinal; its
    initial state is an all-zero ghost array around a known interior."""
    interior = [[11.0, 12.0], [21.0, 22.0]]

    def case(case_id: str, mode: int) -> dict:
        return {"case_id": case_id, "inputs": {"mode": mode},
                "initial_state": {"field_ghost": 0.0, "field_interior": interior,
                                  "max_abs_deviation": -1.0, "guard_fired": 0.0}}

    return _driven({
        "meta": {"spec_id": BOUNDARY_SID, "spec_kind": "component"},
        "case": {"test_case_set": [
            case("l0_periodic_x_wrap_pass", 1),
            case("l0_periodic_y_wrap_pass", 2),
            case("l0_invalid_ny_xfail", 3),
        ]},
        "impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp"},
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "backend_overrides": {"openmp": {"num_threads": 1}},
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "required": True, "schema": {
                    "variables": [
                        {"name": "field_ghost", "shape_expr": "[4, 4]"},
                        {"name": "field_interior", "shape_expr": "[2, 2]"},
                        {"name": "max_abs_deviation", "shape_expr": "scalar"},
                        {"name": "guard_fired", "shape_expr": "scalar"},
                    ],
                    "time_variable": "t", "time_shape_expr": "scalar",
                }},
            ]},
            "tests": [
                {"test_id": "l0_periodic_x_wrap_pass", "expected_outcome": "pass",
                 "target_cases": ["l0_periodic_x_wrap_pass"], "quantities": ["x_wrap_err"]},
                {"test_id": "l0_periodic_y_wrap_pass", "expected_outcome": "pass",
                 "target_cases": ["l0_periodic_y_wrap_pass"], "quantities": ["y_wrap_err"]},
                {"test_id": "l0_invalid_ny_xfail", "expected_outcome": "xfail",
                 "target_cases": ["l0_invalid_ny_xfail"], "quantities": ["guard"]},
            ],
            "predicates": [],
        },
        "dependency": {
            "node_key": f"component/{BOUNDARY_SID}@0.1.0",
            "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}],
        },
    }, BOUNDARY_OP, {"field_ghost": ("out", 2), "field_interior": ("in", 2),
                     "max_abs_deviation": ("out", 0), "guard_fired": ("out", 0)})


# The operation `_boundary_ir` drives: the interior copied into the centre of the ghost array, the
# case's ordinal in `max_abs_deviation`, and mode 3 the xfail case's refusal.
_BOUNDARY_MODEL = textwrap.dedent(f"""\
    module {BOUNDARY_SID}_model
      use, intrinsic :: iso_fortran_env, only: dp => real64
      implicit none
      private
      public :: {BOUNDARY_OP}
    contains
      subroutine {BOUNDARY_OP}(mode, field_ghost, field_interior, &
          max_abs_deviation, guard_fired, ok)
        integer, intent(in) :: mode
        real(dp), intent(out) :: field_ghost(:, :)
        real(dp), intent(in) :: field_interior(:, :)
        real(dp), intent(out) :: max_abs_deviation
        real(dp), intent(out) :: guard_fired
        logical, intent(out) :: ok
        field_ghost = 0.0_dp
        field_ghost(2:3, 2:3) = field_interior
        max_abs_deviation = real(mode, dp)
        guard_fired = merge(1.0_dp, 0.0_dp, mode == 3)
        ok = mode /= 3
      end subroutine {BOUNDARY_OP}
    end module {BOUNDARY_SID}_model
    """)


def _metrics_ir() -> dict:
    """A minimal problem-shaped IR with one rank-1 snapshot variable."""
    return _driven({
        "meta": {"spec_id": "prob_x", "spec_kind": "problem"},
        "case": {"test_case_set": [{"case_id": "c_pass", "inputs": {"mode": 0},
                                    "initial_state": {"u": [1.0, 2.0, 3.0]}}]},
        "impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp"},
            "backend_overrides": {"openmp": {"num_threads": 4}},
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "schema": {
                    "variables": [{"name": "u", "shape_expr": "[3]"}],
                    "time_variable": "t",
                }},
            ]},
            "tests": [{"test_id": "c_pass", "expected_outcome": "pass",
                       "target_cases": ["c_pass"], "quantities": ["q"]}],
            "predicates": [],
        },
        "dependency": {"node_key": "problem/prob_x@0.1.0",
                       "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]},
    }, "prob_x__apply", {"u": ("inout", 1)})


RANK_SID = "prob_rank"
RANK_OP = f"{RANK_SID}__apply"


def _rank34_metrics_ir() -> dict:
    """A problem IR exercising the rank-3 + rank-4 reader and emitter paths — the code families the
    boundary IR never compiles."""
    a3 = [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]]
    return _driven({
        "meta": {"spec_id": RANK_SID, "spec_kind": "problem"},
        "case": {"test_case_set": [{"case_id": "c0", "inputs": {"mode": 0},
                                    "initial_state": {"a3": a3, "a4": [a3, a3], "s": 0.0}}]},
        "impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp"},
            "backend_overrides": {"openmp": {"num_threads": 2}},
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "schema": {
                    "variables": [
                        {"name": "a3", "shape_expr": "[2, 2, 2]"},
                        {"name": "a4", "shape_expr": "[2, 2, 2, 2]"},
                        {"name": "s", "shape_expr": "scalar"},
                    ],
                    "time_variable": "t",
                }},
            ]},
            "tests": [{"test_id": "c0", "expected_outcome": "pass",
                       "target_cases": ["c0"], "quantities": ["q"]}],
            "predicates": [],
        },
        "dependency": {"node_key": f"problem/{RANK_SID}@0.1.0",
                       "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]},
    }, RANK_OP, {"a3": ("inout", 3), "a4": ("inout", 4), "s": ("out", 0)})


# The operation `_rank34_metrics_ir` drives: both arrays doubled, `s` the sum of the rank-3 one.
_RANK_MODEL = textwrap.dedent(f"""\
    module {RANK_SID}_model
      use, intrinsic :: iso_fortran_env, only: dp => real64
      implicit none
      private
      public :: {RANK_OP}
    contains
      subroutine {RANK_OP}(mode, a3, a4, s, ok)
        integer, intent(in) :: mode
        real(dp), intent(inout) :: a3(:, :, :)
        real(dp), intent(inout) :: a4(:, :, :, :)
        real(dp), intent(out) :: s
        logical, intent(out) :: ok
        a3 = 2.0_dp * a3
        a4 = 2.0_dp * a4
        s = sum(a3)
        ok = mode == 0
      end subroutine {RANK_OP}
    end module {RANK_SID}_model
    """)


def _harness_signatures() -> list[dict]:
    """The certified harness IR public_api.signatures, synthesized from §5.1."""
    struct = parse_signatures_from_fortran(_HARNESS_PINS[EXPECTED_HARNESS_SPEC_ID].interface)
    return [
        {"symbol": signature["name"], "signature": signature}
        for signature in [*struct["procedures"], *struct["types"]]
    ]


# The v3 harness stub source (canonical `type ::` + separate `public ::`), used by
# the pin test and the gfortran smoke. Only enough body to link + emit outputs.
_HARNESS_STUB = textwrap.dedent("""\
    module harness_fortran_cpu_model
      use, intrinsic :: iso_fortran_env, only: real64
      ! allow(C003)
      implicit none
      private
      integer, parameter :: dp = real64
      integer, parameter :: case_id_len = 64
      type :: harness_fortran_cpu__h_named
        character(len=:), allocatable :: name
        character(len=:), allocatable :: json
      end type harness_fortran_cpu__h_named
      public :: harness_fortran_cpu__h_named
      public :: harness_fortran_cpu__parse_cases, harness_fortran_cpu__emit_real
      public :: harness_fortran_cpu__emit_int, harness_fortran_cpu__emit_bool
      public :: harness_fortran_cpu__emit_array_r1, harness_fortran_cpu__emit_array_r2
      public :: harness_fortran_cpu__emit_array_r3, harness_fortran_cpu__emit_array_r4
      public :: harness_fortran_cpu__box, harness_fortran_cpu__write_snapshot
      public :: harness_fortran_cpu__write_perf
      public :: harness_fortran_cpu__read_case, harness_fortran_cpu__get_real
      public :: harness_fortran_cpu__get_int, harness_fortran_cpu__get_bool
      public :: harness_fortran_cpu__get_array_r1, harness_fortran_cpu__get_array_r2
      public :: harness_fortran_cpu__get_array_r3, harness_fortran_cpu__get_array_r4
    contains
      subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)
        character(len=*), intent(in) :: tokens(:)
        integer, intent(in) :: ntokens
        character(len=case_id_len), intent(out) :: case_ids(:)
        integer, intent(out) :: ncases
        logical, intent(out) :: ok
        integer :: i, pos
        ncases = 0
        ok = .false.
        pos = 0
        do i = 1, ntokens
          if (trim(tokens(i)) == '--cases') pos = i
        end do
        if (pos == 0 .or. pos + 1 > ntokens) return
        do i = pos + 2, ntokens
          ncases = ncases + 1
          case_ids(ncases) = tokens(i)
        end do
        ok = ncases > 0
      end subroutine harness_fortran_cpu__parse_cases
      function harness_fortran_cpu__emit_real(x) result(s)
        real(dp), intent(in) :: x
        character(len=:), allocatable :: s
        character(len=32) :: buf
        write(buf, '(ES24.16E3)') x
        s = trim(adjustl(buf))
      end function harness_fortran_cpu__emit_real
      function harness_fortran_cpu__emit_int(i) result(s)
        integer, intent(in) :: i
        character(len=:), allocatable :: s
        character(len=32) :: buf
        write(buf, '(I0)') i
        s = trim(adjustl(buf))
      end function harness_fortran_cpu__emit_int
      function harness_fortran_cpu__emit_bool(b) result(s)
        logical, intent(in) :: b
        character(len=:), allocatable :: s
        if (b) then
          s = 'true'
        else
          s = 'false'
        end if
      end function harness_fortran_cpu__emit_bool
      function harness_fortran_cpu__emit_array_r1(a) result(s)
        real(dp), intent(in) :: a(:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_real(a(i))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r1
      function harness_fortran_cpu__emit_array_r2(a) result(s)
        real(dp), intent(in) :: a(:,:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a, 1)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_array_r1(a(i, :))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r2
      function harness_fortran_cpu__emit_array_r3(a) result(s)
        real(dp), intent(in) :: a(:,:,:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a, 1)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_array_r2(a(i, :, :))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r3
      function harness_fortran_cpu__emit_array_r4(a) result(s)
        real(dp), intent(in) :: a(:,:,:,:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a, 1)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_array_r3(a(i, :, :, :))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r4
      function harness_fortran_cpu__box(name, json) result(nv)
        character(len=*), intent(in) :: name
        character(len=*), intent(in) :: json
        type(harness_fortran_cpu__h_named) :: nv
        nv%name = name
        nv%json = json
      end function harness_fortran_cpu__box
      subroutine harness_fortran_cpu__write_snapshot(case_id, values, time)
        character(len=*), intent(in) :: case_id
        type(harness_fortran_cpu__h_named), intent(in) :: values(:)
        real(dp), intent(in) :: time
        integer :: u, k
        open(newunit=u, file='raw/state_snapshots/'//trim(case_id)//'.json', status='replace')
        write(u, '(A)') '{'
        do k = 1, size(values)
          write(u, '(A)') '  "'//trim(values(k)%name)//'": '//values(k)%json//','
        end do
        write(u, '(A)') '  "t": '//harness_fortran_cpu__emit_real(time)
        write(u, '(A)') '}'
        close(u)
      end subroutine harness_fortran_cpu__write_snapshot
      subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, &
          walltime_sec, mpi_ranks, gpu_devices)
    !$  use omp_lib, only: omp_get_max_threads
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: target
        integer, intent(in) :: steps
        integer, intent(in) :: cells_updated
        real(dp), intent(in) :: walltime_sec
        integer, intent(in) :: mpi_ranks
        integer, intent(in) :: gpu_devices
        integer :: u, threads_per_rank
        threads_per_rank = 1
    !$  threads_per_rank = omp_get_max_threads()
        open(newunit=u, file='perf.json', status='replace')
        write(u, '(A)') '{ "case_id": "'//trim(case_id)//'", "target": "'//trim(target)// &
          '", "steps": '//harness_fortran_cpu__emit_int(steps)//', "parallelism": '// &
          '{ "threads_per_rank": '//harness_fortran_cpu__emit_int(threads_per_rank)//' } }'
        close(u)
      end subroutine harness_fortran_cpu__write_perf
      subroutine harness_fortran_cpu__read_case(case_id, ok)
        character(len=*), intent(in) :: case_id
        logical, intent(out) :: ok
        ok = len_trim(case_id) > 0
      end subroutine harness_fortran_cpu__read_case
      function harness_fortran_cpu__get_real(name) result(x)
        character(len=*), intent(in) :: name
        real(dp) :: x
        x = real(len(name), dp)
      end function harness_fortran_cpu__get_real
      function harness_fortran_cpu__get_int(name) result(i)
        character(len=*), intent(in) :: name
        integer :: i
        i = len(name)
      end function harness_fortran_cpu__get_int
      function harness_fortran_cpu__get_bool(name) result(b)
        character(len=*), intent(in) :: name
        logical :: b
        b = len(name) > 0
      end function harness_fortran_cpu__get_bool
      subroutine harness_fortran_cpu__get_array_r1(name, a)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: a(:)
        allocate(a(len(name)))
      end subroutine harness_fortran_cpu__get_array_r1
      subroutine harness_fortran_cpu__get_array_r2(name, a)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: a(:,:)
        allocate(a(len(name), 1))
      end subroutine harness_fortran_cpu__get_array_r2
      subroutine harness_fortran_cpu__get_array_r3(name, a)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: a(:,:,:)
        allocate(a(len(name), 1, 1))
      end subroutine harness_fortran_cpu__get_array_r3
      subroutine harness_fortran_cpu__get_array_r4(name, a)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: a(:,:,:,:)
        allocate(a(len(name), 1, 1, 1))
      end subroutine harness_fortran_cpu__get_array_r4
    end module harness_fortran_cpu_model
    """)

# The writer of `_HARNESS_STUB` (`harness_fortran_cpu@0.10.0` §3.2: it obtains the thread count
# itself) and the eight-argument one the distributed harness publishes and `harness_fortran_cpu`
# published up to `0.7.0` (its caller passes the count). `_EIGHT_ARG_HARNESS_STUB` is the stub
# with the second in place of the first.
_STUB_WRITE_PERF_HEAD = textwrap.dedent("""\
      subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, &
          walltime_sec, mpi_ranks, gpu_devices)
    !$  use omp_lib, only: omp_get_max_threads
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: target
        integer, intent(in) :: steps
        integer, intent(in) :: cells_updated
        real(dp), intent(in) :: walltime_sec
        integer, intent(in) :: mpi_ranks
        integer, intent(in) :: gpu_devices
        integer :: u, threads_per_rank
        threads_per_rank = 1
    !$  threads_per_rank = omp_get_max_threads()
""")
_STUB_WRITE_PERF_HEAD_8 = textwrap.indent(textwrap.dedent("""\
      subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, &
          walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: target
        integer, intent(in) :: steps
        integer, intent(in) :: cells_updated
        real(dp), intent(in) :: walltime_sec
        integer, intent(in) :: mpi_ranks
        integer, intent(in) :: threads_per_rank
        integer, intent(in) :: gpu_devices
        integer :: u
"""), "  ")
_EIGHT_ARG_HARNESS_STUB = _HARNESS_STUB.replace(_STUB_WRITE_PERF_HEAD, _STUB_WRITE_PERF_HEAD_8)


class RenderShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.txt = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)

    def test_program_and_uses(self) -> None:
        self.assertIn(f"program {BOUNDARY_SID}_runner", self.txt)
        self.assertIn("use harness_fortran_cpu_model, only:", self.txt)
        self.assertIn(f"use {BOUNDARY_SID}_model, only: &\n    {BOUNDARY_OP}\n", self.txt)
        self.assertNotIn("_checks", self.txt)
        # only the emitters for the ranks in use are imported (r2 + scalar; not r1/r3/r4)
        self.assertIn("harness_fortran_cpu__emit_array_r2", self.txt)
        self.assertIn("harness_fortran_cpu__emit_real", self.txt)
        self.assertNotIn("emit_array_r1", self.txt)
        self.assertNotIn("emit_array_r3", self.txt)
        # likewise the readers: what the case file holds for this IR, no other rank's
        self.assertIn("harness_fortran_cpu__get_array_r2", self.txt)
        self.assertIn("harness_fortran_cpu__get_real", self.txt)
        self.assertIn("harness_fortran_cpu__get_int", self.txt)
        for gone in ("get_array_r1", "get_array_r3", "get_array_r4", "get_bool"):
            self.assertNotIn(gone, self.txt)

    def test_the_operation_is_called_with_the_state_itself(self) -> None:
        # Every dummy's actual, in dummy order: the case's input, then each state.
        self.assertIn(
            f"call {BOUNDARY_OP}( &\n"
            "        h1, &\n        st_field_ghost, &\n        st_field_interior, &\n"
            "        st_max_abs_deviation, &\n        st_guard_fired, &\n        lg_ok)\n",
            self.txt)
        self.assertEqual(self.txt.count(f"call {BOUNDARY_OP}("), 1)

    def test_a_captured_value_is_the_drivers_own_variable_read_by_the_emitter(self) -> None:
        # No generated procedure sits between a state and its emitter, so nothing can filter,
        # compute or rewrite a captured value on its way out (Z6 fixture (b),
        # `zero_base_architecture.md:253`, whose retired getter names stay absent).
        for gone in ("get_scalar", "get_r1", "get_r2", "get_r3", "get_r4",
                     "gfound", "r2buf", "sval", "sb_"):
            self.assertNotIn(gone, self.txt)
        self.assertIn(
            "    out(1) = harness_fortran_cpu__box('field_ghost', &\n"
            "      harness_fortran_cpu__emit_array_r2(st_field_ghost))", self.txt)
        self.assertIn(
            "harness_fortran_cpu__box('max_abs_deviation', &\n"
            "      harness_fortran_cpu__emit_real(st_max_abs_deviation))", self.txt)
        # EVERY declared variable, and `step`, is captured for EVERY case — the snapshot is the
        # full state: one capture body, no per-case select.
        self.assertIn("    allocate(out(6))", self.txt)
        self.assertIn("    out(6) = harness_fortran_cpu__box('step', &", self.txt)
        self.assertNotIn("select case (cid)", self.txt)
        self.assertEqual(self.txt.count("harness_fortran_cpu__box('guard_fired', &"), 1)

    def test_a_case_with_several_entries_selects_on_the_case_id(self) -> None:
        # The list form of `drive.step`: the select appears, each case to its entry, and a case no
        # entry names stops the run.
        ir = copy.deepcopy(_boundary_ir())
        step = ir["drive"]["step"]
        ir["drive"]["step"] = [
            {**step, "cases": ["l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass"]},
            {**step, "cases": ["l0_invalid_ny_xfail"]}]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn("    select case (cid)\n    case ('l0_periodic_x_wrap_pass')\n"
                      "      entry_no = 1\n    case ('l0_periodic_y_wrap_pass')\n"
                      "      entry_no = 1\n    case ('l0_invalid_ny_xfail')\n      entry_no = 2\n",
                      txt)
        self.assertIn("'error: no drive entry runs case '//cid", txt)
        self.assertEqual(txt.count(f"call {BOUNDARY_OP}("), 2)

    def test_a_schema_variable_no_test_requires_is_still_captured(self) -> None:
        # The set the runner reads is the SCHEMA, not what the tests' predicates read: a declared
        # state no test names is read from the case file, its emitter imported, and captured for
        # every case (the round-3 census's only corpus-dependent decision on the renderer).
        ir = copy.deepcopy(_boundary_ir())
        schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
        schema["variables"].append({"name": "orphan_r1", "shape_expr": "[7]"})
        txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn("  real(dp), allocatable :: st_orphan_r1(:)", txt)
        self.assertIn("    harness_fortran_cpu__emit_array_r1, &", txt)
        self.assertIn("    allocate(out(7))", txt)
        self.assertIn("harness_fortran_cpu__emit_array_r1(st_orphan_r1))", txt)
        self.assertIn("call harness_fortran_cpu__get_array_r1('orphan_r1', &\n      st_orphan_r1)",
                      txt)

    def test_the_captures_bracket_the_call_and_the_case_file_is_read_before_either(self) -> None:
        # The capture contract: the case file read whole, the initial capture on what it held,
        # the operation, the final capture on what it left; `initial/` snapshots go under the
        # sub-directory. What each capture then holds is `test_driver_render`'s.
        body = self.txt[self.txt.index("  do ci = 1, ncases"):
                        self.txt.index("  if (walltime <= 0.0_dp)")]
        order = [
            "call harness_fortran_cpu__read_case(cid, ok)",
            "call harness_fortran_cpu__get_array_r2('field_ghost', &",
            "call capture_state(vals)",
            "call harness_fortran_cpu__write_snapshot('initial/'//cid, vals, t_c)",
            f"call {BOUNDARY_OP}( &",
            "call capture_state(vals)",
            "call harness_fortran_cpu__write_snapshot(cid, vals, t_c)",
        ]
        pos = -1
        for needle in order:
            nxt = body.index(needle, pos + 1)
            self.assertGreater(nxt, pos, needle)
            pos = nxt
        self.assertEqual(body.count("call capture_state("), 2)
        self.assertEqual(body.count("write_snapshot("), 2)
        # no procedure runs between the operation returning and the final capture
        tail = body[body.index(f"call {BOUNDARY_OP}("):body.rindex("call capture_state(")]
        self.assertEqual(re.findall(r"call (\w+)\(", tail),
                         [BOUNDARY_OP, "system_clock"])

    def test_terminal_writer(self) -> None:
        self.assertEqual(1, self.txt.count("call harness_fortran_cpu__write_perf("))
        # perf parallelism: mpi=1, gpu=0, and no thread count — the harness's writer obtains
        # the count the run is given itself (`harness_fortran_cpu@0.10.0` §3.2, issue #512:
        # Validate runs the binary at several counts, so the profile's is the wrong one for
        # every sweep shape).
        self.assertIn("    steps_total, int(min(cells_total, int(huge(0), int64))), walltime, 1, 0)\n",
                      self.txt)
        # The perf line carries no thread count of the profile's: a render under another
        # count is the same text.
        other = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS, target={
            **_TARGET_PROFILE.doc, "execution": {"threads_per_rank": 7}})
        self.assertNotEqual(_TARGET_PROFILE.threads_per_rank, 7)
        self.assertEqual(self.txt, other)

    def test_no_forbidden_outputs(self) -> None:
        for forbidden in ("verdict.json", "aggregate_verdict", "summary.json", "trial_meta"):
            self.assertNotIn(forbidden, self.txt)

    def test_line_width(self) -> None:
        over = [ln for ln in self.txt.splitlines() if len(ln) > 100]
        self.assertEqual(over, [], f"lines over 100 cols: {over}")

    def test_lint_shape_markers(self) -> None:
        """The runner must be lint-clean under the gate's DECLARED rule set, and the polarity of
        the allow directive inverted when that set was declared (issue #111).

        This file is host-rendered, so a finding in it routes a retry to a leaf with no write
        authority over it — the unwinnable loop issue #110 recorded. The gate now runs with
        `--ignore-allow-comments`, which makes any directive here a `FORT005` finding, and `C003`
        is out of the declared set, so the bare form is the clean one. Asserted as an ABSENCE
        because the old assertion was the presence of exactly the line that would break it."""
        self.assertNotIn("allow(", self.txt)
        self.assertIn("implicit none", self.txt)




class DeterminismTest(unittest.TestCase):
    def test_byte_identical(self) -> None:
        a = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        b = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertEqual(a, b)


class PerfParallelismRenderTest(unittest.TestCase):
    def test_the_perf_record_s_counts_and_the_rank_1_capture(self) -> None:
        txt = render_runner(_metrics_ir(), "prob_x", HARNESS, target=_TARGET_PROFILE.doc)
        # The runner passes no thread count: the harness's writer records its run-time count
        # (issue #512) — neither the profile's nor the IR's `backend_overrides.openmp.num_threads`,
        # which nothing runs with.
        perf_line = "    steps_total, int(min(cells_total, int(huge(0), int64))), walltime, 1, %d)\n"
        self.assertIn(perf_line % 0, txt)
        other = {**_TARGET_PROFILE.doc, "execution": {"threads_per_rank": 7},
                 "hardware": {"class": "gpu", "architecture": "a"}}
        other_txt = render_runner(_metrics_ir(), "prob_x", HARNESS, target=other)
        # The rank and device counts are `host_execution.perf_parallelism`'s (issue #316): a
        # class whose backend states one device per rank records it, where the renderer used
        # to write 0 for every class.
        self.assertIn(perf_line % 1, other_txt)
        self.assertIn("'gpu', &", other_txt)
        # rank-1 snapshot var -> the driver's `st_u` + emit_array_r1
        self.assertIn("  real(dp), allocatable :: st_u(:)", txt)
        self.assertIn("harness_fortran_cpu__emit_array_r1(st_u))", txt)
        self.assertNotIn("emit_array_r2", txt)


class RenderErrorMatrixTest(unittest.TestCase):
    def _expect(self, mutate) -> None:
        ir = copy.deepcopy(_boundary_ir())
        mutate(ir)
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)

    def test_rank_over_4(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][0].__setitem__("shape_expr", "[2,2,2,2,2]"))

    def test_bad_shape_expr(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][2].__setitem__("shape_expr", "banana"))

    def test_reserved_key_collision(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][2].__setitem__("name", "t"))

    def test_two_infra_deps(self) -> None:
        self._expect(lambda ir: ir["dependency"]["direct_deps"].append(
            {"node_key": "infrastructure/other@1.0.0"}))

    def test_over_long_spec_id(self) -> None:
        with self.assertRaises(RenderError):
            render_runner(_boundary_ir(), "z" * 56, HARNESS, target=_TARGET_PROFILE.doc)

    _rename_snapshot_var = staticmethod(_rename_state)

    def test_snapshot_variable_must_be_a_bindable_identifier(self) -> None:
        # A snapshot variable IS a variable of the driver, `st_<name>`, so a name Fortran cannot
        # declare is refused at render / compile.static rather than left to fail the syntax gate
        # on a file no leaf can edit.
        for bad in ("field-ghost", "1field", "field ghost", "field.ghost"):
            with self.subTest(bad=bad):
                self._expect(lambda ir, b=bad: self._rename_snapshot_var(ir, "field_ghost", b))

    def test_snapshot_variable_bounded_by_the_identifier_limit_through_its_prefix(self) -> None:
        # 61 chars is a legal identifier, but `st_` + 61 = 64 > 63: refused by the bound, with its
        # own message (the 100-column backstop would also catch it, one step later and with a
        # message about line width rather than the identifier).
        ir = copy.deepcopy(_boundary_ir())
        self._rename_snapshot_var(ir, "field_ghost", "f" * 61)
        with self.assertRaises(RenderError) as cm:
            render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn("exceeds the 63-char identifier limit", str(cm.exception))
        # A long name that fits the limit and every rendered line renders.
        ir = copy.deepcopy(_boundary_ir())
        self._rename_snapshot_var(ir, "field_ghost", "f" * 50)
        txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn("  real(dp), allocatable :: st_" + "f" * 50 + "(:,:)", txt)

    def test_snapshot_variables_that_fold_to_one_identifier_are_refused(self) -> None:
        # `guard_fired` and `Guard_Fired` are ONE variable to Fortran, declared twice.
        self._expect(lambda ir: self._rename_snapshot_var(ir, "field_ghost", "Guard_Fired"))

    def test_a_state_named_like_a_retired_callback_renders_under_its_prefix(self) -> None:
        # There is no module of the leaf's for a state's name to collide with any more: a state's
        # variable is the driver's, under its prefix, whatever the name spells.
        for name in ("case_setup", "case_run", "get_time"):
            with self.subTest(name=name):
                ir = copy.deepcopy(_boundary_ir())
                self._rename_snapshot_var(ir, "field_ghost", name)
                txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
                self.assertIn(f"  real(dp), allocatable :: st_{name}(:,:)", txt)
                self.assertEqual(ir_content_violations(ir, BOUNDARY_SID, HARNESS), [])

    def test_over_long_case_id_fails_closed(self) -> None:
        # 70 chars: over the harness `case_id_len = 64`, which `__parse_cases` truncates a parsed
        # id to, so the run's snapshot would be written under a name no IR case carries.
        self._expect(_over_long_case_id_mut)

    def test_case_id_at_the_harness_width_still_renders(self) -> None:
        # Exactly `case_id_len` fits: the bound is inclusive, not off-by-one.
        ir = copy.deepcopy(_boundary_ir())
        exact = "c_" + "x" * (CASE_ID_LEN - 2)
        self.assertEqual(len(exact), CASE_ID_LEN)
        ir["case"]["test_case_set"][0]["case_id"] = exact
        txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        # the runner takes the ids from argv: the width it stores them at is the harness's
        self.assertIn(f"  integer, parameter :: case_id_len = {CASE_ID_LEN}", txt)

    def test_duplicate_case_id_fails_closed(self) -> None:
        # Two entries with the same case_id would write one snapshot path twice. Fail closed.
        self._expect(lambda ir: ir["case"]["test_case_set"].append(
            {"case_id": "l0_periodic_x_wrap_pass"}))

    def test_non_t_time_variable(self) -> None:
        # The harness writes the snapshot time under the fixed key 't'; a different
        # time_variable cannot be honored, so fail closed.
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"].__setitem__("time_variable", "tau"))

    def test_control_char_in_name(self) -> None:
        self._expect(lambda ir: ir["case"]["test_case_set"][0]
                     .__setitem__("case_id", "l0\nx"))

    def test_path_traversal_case_id_fails_closed(self) -> None:
        # A case_id is concatenated into `raw/state_snapshots/<case_id>.json`, so `/` or `..`
        # traverses out of the run directory and the cleanly-compiling runner writes an
        # arbitrary file. Reproduced end-to-end before this gate (case_id "../../pwned" wrote
        # node/pwned.json). Reject anything that is not a filesystem-safe token.
        for name in ("../../pwned", "a/b", "..", "foo/../bar", "x\\y"):
            with self.subTest(name=name):
                self._expect(lambda ir, n=name: ir["case"]["test_case_set"][0].__setitem__(
                    "case_id", n))

    def test_leading_dash_case_id_fails_closed(self) -> None:
        # A case id also reaches the runner's argv (`--cases <spec> <case_id>...`), where
        # a leading `-` reads as an option. Rejected at Compile so the run does not get
        # several phases further and fail at the build-runtime argument rule instead.
        for name in ("-c1", "--cases", "-"):
            with self.subTest(name=name):
                self._expect(lambda ir, n=name: ir["case"]["test_case_set"][0].__setitem__(
                    "case_id", n))

    def test_dotted_and_dashed_case_id_still_renders(self) -> None:
        # The safe grammar allows `.` and `-`; a single dot is not `..` and must pass.
        ir = copy.deepcopy(_boundary_ir())
        ok = "l0_v1.2-alpha"  # a dash INSIDE the id stays legal; only a leading one does not
        ir["case"]["test_case_set"][0]["case_id"] = ok
        render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertEqual(ir_content_violations(ir, BOUNDARY_SID, HARNESS), [])

    def test_non_ascii_in_name_fails_closed(self) -> None:
        # Fortran's default character kind counts BYTES; `CASE_ID_LEN` and the 100-column
        # guard count Python code points. A 64-code-point / 68-byte case_id would slip past
        # the bound and be truncated into the harness's `character(len=64)` slot, giving a
        # runner that compiles and then `error stop`s. Reject non-ASCII outright.
        for name in ("l0_café_wrap", "c_" + "x" * 58 + "é" * 4):
            with self.subTest(name=name[:20]):
                self._expect(lambda ir, n=name: ir["case"]["test_case_set"][0].__setitem__(
                    "case_id", n))

    def test_non_ascii_literal_fails_closed(self) -> None:
        # Every name the renderer embeds goes through `_flit`, whichever grammar bounded it first.
        with self.assertRaisesRegex(RenderError, "outside printable ASCII"):
            _flit_for_test("error.ℓ2")

    def test_extreme_name_length_fails_closed(self) -> None:
        # A long name pushes a rendered line past the 100-col lint limit; since the runner is
        # host-rendered (unrepairable by a leaf), fail closed at render time. The name must be a
        # snapshot variable, not a case_id: the runner embeds no case id any more.
        with self.assertRaisesRegex(RenderError, "reaches the linter's 100-column limit"):
            ir = copy.deepcopy(_boundary_ir())
            _long_name_mut(ir)
            render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)


def _over_long_case_id_mut(ir: dict) -> None:
    ir["case"]["test_case_set"][0]["case_id"] = "c_" + "x" * 68  # > CASE_ID_LEN


#: The widest line a rank-2 state's name reaches: its reader. A name of this many characters puts
#: it at exactly 100 columns, the linter's limit on the builds where the rule fires at the limit.
_R2_READER_LINE = "    call harness_fortran_cpu__get_array_r2('', &"
_NAME_AT_100 = "m" * (100 - len(_R2_READER_LINE))


def _long_name_mut(ir: dict) -> None:
    # A SNAPSHOT VARIABLE whose `st_` local fits the identifier limit and whose reader line reaches
    # the 100-column lint limit.
    _rename_state(ir, "field_ghost", _NAME_AT_100)


# (label, mutate, spec_id, is_identity): each mutation of _boundary_ir makes render_runner raise.
# `is_identity` = the RenderError is a node-identity defect (`identity=True`) a re-author cannot
# repair, which `ir_content_violations` excludes; otherwise it is Compile-authored content that
# must surface at compile.static. This table is the classification contract.
_RENDER_FAILCLOSE_CASES = [
    ("rank_over_4", lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
     ["schema"]["variables"][0].__setitem__("shape_expr", "[2,2,2,2,2]"), BOUNDARY_SID, False),
    ("bad_shape_expr", lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
     ["schema"]["variables"][2].__setitem__("shape_expr", "banana"), BOUNDARY_SID, False),
    ("reserved_key_collision", lambda ir: ir["io_contract"]["raw_requirements"]
     ["required_evidence"][0]["schema"]["variables"][2].__setitem__("name", "t"),
     BOUNDARY_SID, False),
    ("duplicate_case_id", lambda ir: ir["case"]["test_case_set"].append(
        {"case_id": "l0_periodic_x_wrap_pass"}), BOUNDARY_SID, False),
    ("over_long_case_id", _over_long_case_id_mut, BOUNDARY_SID, False),
    ("path_traversal_case_id", lambda ir: ir["case"]["test_case_set"][0].__setitem__(
        "case_id", "../../evil"), BOUNDARY_SID, False),
    ("non_ascii_case_id", lambda ir: ir["case"]["test_case_set"][0].__setitem__(
        "case_id", "l0_café"), BOUNDARY_SID, False),
    ("non_t_time_variable", lambda ir: ir["io_contract"]["raw_requirements"]
     ["required_evidence"][0]["schema"].__setitem__("time_variable", "tau"), BOUNDARY_SID, False),
    # These two USED to be render-only backstops; the render-based mirror now hoists them
    # (they are Compile-authored names, not node identity) — the R1-F1 gap fix.
    ("control_char_in_name", lambda ir: ir["case"]["test_case_set"][0].__setitem__(
        "case_id", "l0\nx"), BOUNDARY_SID, False),
    ("over_100_col_line", _long_name_mut, BOUNDARY_SID, False),
    # Node-identity defects: excluded from the hoist (unrepairable by a re-author).
    ("two_infra_deps", lambda ir: ir["dependency"]["direct_deps"].append(
        {"node_key": "infrastructure/other@1.0.0"}), BOUNDARY_SID, True),
    ("over_long_spec_id", None, "z" * 56, True),
]


class IrContentViolationsTest(unittest.TestCase):
    """``ir_content_violations`` is the compile.static mirror of ``render_runner``: it INVOKES
    the renderer with the same ``(ir, spec_id, harness_spec_id)`` the conductor uses and reports
    its ``RenderError`` unless the error is ``identity=True``. So the mirror is exact by
    construction — no hand-maintained precondition list to drift. These tests pin the
    content-vs-identity classification (the routing contract) so the E2E #3 workflow-kill class
    cannot silently reopen (e.g. a control char in an IR name — R1-F1 — now surfaces at compile instead of killing the workflow at render)."""

    def test_clean_ir_has_no_violations(self) -> None:
        self.assertEqual(ir_content_violations(_boundary_ir(), BOUNDARY_SID, HARNESS), [])
        self.assertEqual(ir_content_violations(_metrics_ir(), "prob_x", HARNESS), [])
        self.assertEqual(ir_content_violations(_rank34_metrics_ir(), RANK_SID, HARNESS), [])

    def test_content_vs_identity_classification(self) -> None:
        for label, mutate, spec_id, is_identity in _RENDER_FAILCLOSE_CASES:
            with self.subTest(label):
                ir = copy.deepcopy(_boundary_ir())
                if mutate is not None:
                    mutate(ir)
                # Sanity: every table row is a genuine render fail-close.
                with self.assertRaises(RenderError):
                    render_runner(ir, spec_id, HARNESS, target=_TARGET_PROFILE.doc)
                v = ir_content_violations(ir, spec_id, HARNESS)
                if is_identity:
                    self.assertEqual(v, [], f"{label}: identity defect must be excluded")
                else:
                    self.assertTrue(v, f"{label}: content defect must be hoisted")

    def test_time_variable_message_is_actionable(self) -> None:
        ir = copy.deepcopy(_boundary_ir())
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"][
            "time_variable"] = "tau"
        v = ir_content_violations(ir, BOUNDARY_SID, HARNESS)
        self.assertTrue(any("time_variable is 'tau'" in x and "'t'" in x for x in v), v)

    def test_malformed_non_iterable_field_does_not_crash(self) -> None:
        # A truthy non-iterable where a list is expected (e.g. `schema.variables: 5`) makes
        # render_runner raise a bare TypeError, not a RenderError. Running inside the compile
        # validator, that must NOT escape (it would abort the gate and discard sibling
        # violations); it is converted to a renderable-failure violation instead.
        ir = copy.deepcopy(_boundary_ir())
        ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"] = 5
        v = ir_content_violations(ir, BOUNDARY_SID, HARNESS)
        self.assertTrue(v and any("not renderable" in x for x in v), v)
        ir = copy.deepcopy(_boundary_ir())
        ir["case"]["test_case_set"] = 5
        v = ir_content_violations(ir, BOUNDARY_SID, HARNESS)
        self.assertTrue(v and any("not renderable" in x for x in v), v)


class SpecIdBoundAgreementTest(unittest.TestCase):
    """This backend's spec_id bound: derived from its identifier limit, read by its renderer.

    The number lives in `bundle.SPEC_ID_MAX`, the `bundle_facts` member the neutral spec-input
    gate takes the minimum over (`tools/spec_input_gates.max_spec_id_len`, pinned in
    `test_spec_input_gates`). The renderer's render-time check is the backstop and must read the
    same value: a copy would let the gate admit a spec_id the renderer then kills, or refuse one
    it would accept."""

    def test_the_backend_bound_derives_from_the_language_identifier_limit(self) -> None:
        from tools.backends.language.fortran import bundle
        # The longest generated name appends a 7-character role suffix; the extra character is
        # the declared margin. Pinned to 55 as the value the documents state for this backend.
        self.assertEqual(55, bundle.SPEC_ID_MAX)
        self.assertEqual(bundle.IDENTIFIER_MAX - len("_runner") - 1, bundle.SPEC_ID_MAX)
        for suffix in ("_runner", "_model", "_drv"):
            self.assertLessEqual(bundle.SPEC_ID_MAX + len(suffix), bundle.IDENTIFIER_MAX)

    def test_the_bound_tracks_the_language_identifier_limit(self) -> None:
        """The DERIVATION, observed by moving the thing it derives from.

        Replacing the derivation with today's literal is behaviourally identical at the current
        limit, so every value comparison in this class survives it. Earlier forms of this row
        read the source for a spelling and over-rejected legitimate work, so the row moves the
        limit instead: the module is executed with its `IDENTIFIER_MAX` assignment's VALUE
        replaced, and `SPEC_ID_MAX` must follow. Every spelling that derives passes, a restated
        literal fails, and no spelling of the derivation is named."""
        import ast

        from tools.backends.language.fortran import bundle

        moved = bundle.IDENTIFIER_MAX - 23
        tree = ast.parse(Path(bundle.__file__).read_text(encoding="utf-8"))
        hits = 0
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign) else [])
            if any(isinstance(t, ast.Name) and t.id == "IDENTIFIER_MAX" for t in targets):
                node.value = ast.Constant(moved)
                hits += 1
        self.assertEqual(1, hits, "IDENTIFIER_MAX is not assigned exactly once in bundle.py")
        namespace: dict[str, object] = {"__name__": "_moved_bundle"}
        exec(compile(ast.fix_missing_locations(tree), bundle.__file__, "exec"), namespace)
        self.assertEqual(
            moved - len("_runner") - 1, namespace["SPEC_ID_MAX"],
            "the backend's spec_id bound did not move with the language identifier limit: it is "
            "restated rather than derived, and a literal does not move")

    def test_the_renderer_reads_the_backend_bound(self) -> None:
        """The render-time backstop reads `bundle.SPEC_ID_MAX`, observed by moving it.

        In a SUBPROCESS, not with an in-process reload: reloading the emitter rebinds the module
        every other test in this file holds a reference to (measured: an earlier form failed 12
        tests). The emitter is already in `sys.modules` once `bundle` is reachable — the package
        `__init__` re-exports it — so it is dropped first, or the probe reports the unpatched
        bound and passes for no reason."""
        import subprocess
        import sys

        from tools.backends.language.fortran import bundle

        moved = bundle.SPEC_ID_MAX - 17
        probe = (
            "import importlib, sys\n"
            "from unittest import mock\n"
            "from tools.backends.language.fortran import bundle\n"
            "del sys.modules['tools.backends.language.fortran.runner']\n"
            f"with mock.patch.object(bundle, 'SPEC_ID_MAX', {moved}):\n"
            "    runner = importlib.import_module("
            "'tools.backends.language.fortran.runner')\n"
            "    print(runner.MAX_SPEC_ID_LEN)\n"
            "    try:\n"
            f"        runner._check_identifier_lengths('s' * {moved + 1}, 'h')\n"
            "    except runner.RenderError:\n"
            "        print('refused')\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual([str(moved), "refused"], out.stdout.split())


class DerivedNameLengthTest(unittest.TestCase):
    """The per-name loop in `_check_identifier_lengths`, which the spec_id bound hides.

    The bound leaves a one-character margin, so no spec_id that passes it can produce a derived
    name over the limit — the loop is unreachable from the bound alone. It is reachable from the
    HARNESS id, which the bound does not constrain, and that is what drives it here. Measured:
    without this, replacing `bundle.IDENTIFIER_MAX` in that loop with a much larger number left
    the whole suite green."""

    def test_an_over_long_harness_symbol_is_an_identity_error(self) -> None:
        from tools.backends.language.fortran import bundle

        ir = _boundary_ir()
        long_harness = "h" * (bundle.IDENTIFIER_MAX - len("__write_snapshot") + 1)
        with self.assertRaises(RenderError) as ctx:
            render_runner(ir, BOUNDARY_SID, long_harness, target=_TARGET_PROFILE.doc)
        # `identity=True`: a harness id is node identity, so the compile.static mirror excludes
        # it rather than routing it to a re-author that cannot change it.
        self.assertTrue(ctx.exception.identity)
        self.assertIn(str(bundle.IDENTIFIER_MAX), str(ctx.exception))


def _flit_for_test(text: str) -> str:
    from tools.backends.language.fortran import runner as fortran_runner
    return fortran_runner._flit(text)


class LineWidthTest(unittest.TestCase):
    """R1/M3c-β (review round 3): every rendered line must stay within the 100-col lint limit,
    including for long IR-sourced names — the hot lines are wrapped."""

    def _maxw(self, txt: str) -> int:
        return max(len(ln) for ln in txt.splitlines())

    def test_long_case_id_target_wraps(self) -> None:
        ir = _boundary_ir()
        long_id = "l0_periodic_x_wrap_with_a_fairly_long_descriptive_name_pass"  # 58 chars
        ir["case"]["test_case_set"][0]["case_id"] = long_id
        txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertLessEqual(self._maxw(txt), 100)

    def test_the_column_limit_is_the_linter_s_declaration(self) -> None:
        """The renderer's column limit is the fortran linter's `LINE_LENGTH_LIMIT`, read through
        the registry (issue #424), not a literal of its own: lowering the declaration makes a
        render whose widest line fits under 100 refuse, and the message names the new limit.
        Restoring a literal 100 in the renderer turns this row red."""
        from tools.backends import registry
        from tools.backends.language.fortran import runner as fortran_runner

        lint = registry.capability_module(
            "linter", registry.linter_for_language("fortran"), "lint")
        self.assertEqual(fortran_runner.max_rendered_line(), lint.LINE_LENGTH_LIMIT)
        ir = _boundary_ir()
        txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        widest = self._maxw(txt)
        self.assertLess(widest, 100)
        lowered = widest - 5
        with mock.patch.object(lint, "LINE_LENGTH_LIMIT", lowered):
            self.assertEqual(fortran_runner.max_rendered_line(), lowered)
            with self.assertRaises(RenderError) as cm:
                render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn(f"{lowered}-column limit", str(cm.exception))

    def test_every_limit_read_follows_the_linter_s_declaration_upward(self) -> None:
        """The row above lowers the limit; this one RAISES it, so the refusal must follow the
        declaration both ways: a 100-column line, which the declared limit refuses, renders under
        a raised one. A literal 100 restored at the backstop turns this row red."""
        from tools.backends import registry

        lint = registry.capability_module(
            "linter", registry.linter_for_language("fortran"), "lint")
        raised = 110
        ir = copy.deepcopy(_boundary_ir())
        _long_name_mut(ir)
        with mock.patch.object(lint, "LINE_LENGTH_LIMIT", raised):
            txt = render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertEqual(self._maxw(txt), 100)
        self.assertIn(f"get_array_r2('{_NAME_AT_100}', &", txt)

    def test_a_snapshot_name_rendering_an_exactly_100_column_line_is_refused(self) -> None:
        # A name whose reader line is EXACTLY 100 columns would slip a `> 100` backstop and fail
        # S001 on a host-authored line (a round-1 reviewer measured the shape on fortitude 0.8.0).
        # The backstop is `>=`, so no rendered line reaches 100.
        ir = copy.deepcopy(_boundary_ir())
        _long_name_mut(ir)
        with self.assertRaises(RenderError) as cm:
            render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn("reaches the linter's 100-column limit (100 columns", str(cm.exception))
        self.assertFalse(cm.exception.identity)
        # one char shorter renders, and every line is at most 99 wide
        ir2 = copy.deepcopy(_boundary_ir())
        _rename_state(ir2, "field_ghost", _NAME_AT_100[:-1])
        txt = render_runner(ir2, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertEqual(self._maxw(txt), 99)

    def test_a_loop_drive_ir_is_held_to_the_same_limit(self) -> None:
        # The loop's own lines, over the state its step writes through a work copy: that name's
        # widest line is the copy's reallocation, which spells it twice, and is 39 + 2 * width.
        for width, refused in ((31, True), (30, False)):
            with self.subTest(width=width):
                ir = loop_ir(0)
                schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
                name = "w" * width
                schema["variables"][1]["name"] = name
                ir["drive"]["step"]["arguments"]["w"] = f"state.{name}"
                if refused:
                    with self.assertRaises(RenderError) as cm:
                        render_runner(ir, "drv_toy", HARNESS, target=_TARGET_PROFILE.doc)
                    self.assertIn("100-column limit", str(cm.exception))
                    self.assertFalse(cm.exception.identity)
                    self.assertEqual(len(ir_content_violations(ir, "drv_toy", HARNESS)), 1)
                else:
                    txt = render_runner(ir, "drv_toy", HARNESS, target=_TARGET_PROFILE.doc)
                    self.assertLess(self._maxw(txt), 100)
                    self.assertEqual(ir_content_violations(ir, "drv_toy", HARNESS), [])


class FortranLiteralEscapingTest(unittest.TestCase):
    """R1/M3c-β (Codex review): a value the renderer embeds in a literal may carry a single quote,
    which must be doubled (`''`) in the generated Fortran literal, not break it. Observed on the
    escaper itself: what the runner embeds today (snapshot variables, the target's class) has a
    grammar of its own that refuses the quote first."""

    def test_apostrophe_in_names_is_escaped(self) -> None:
        self.assertEqual(_flit_for_test("l0_x'wrap"), "l0_x''wrap")


class HarnessPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = _boundary_ir()
        self.sigs = _harness_signatures()
        self.src = _HARNESS_STUB

    def test_clean_pin(self) -> None:
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src)

    def test_wrong_harness_id(self) -> None:
        # Assert the MESSAGE, not just the raise. Neutering the `harness_spec_id` guard leaves this
        # test passing on the exception the symbol lookup raises a few lines later ("renderer
        # depends on harness symbol ... not present"), so the guard itself had no observer and the
        # distinct, actionable refusal could have been lost while the test stayed green. A reviewer
        # measured that: replacing the condition with `False` left the whole suite passing.
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, "harness_other", self.sigs, self.src)
        self.assertIn("is not one this renderer targets", str(cm.exception))
        self.assertIn("harness_fortran_cpu, harness_fortran_cpu_mpi", str(cm.exception))

    def test_embedded_interface_carrying_a_prototype_is_a_renderer_bug(self) -> None:
        # The pinned harness surface is 13 operations and 5 types and no interface PROTOTYPE
        # (`docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`); the stanza splitter reads prototypes into
        # a dict of their own since issue #266, and the pin refuses a non-empty one as a
        # renderer bug rather than letting a prototype ride along unpinned.
        from tools.backends.language.fortran import runner as fortran_runner
        with_proto = _HARNESS_V3_INTERFACE + (
            "\nabstract interface\n  subroutine ghost(x)\n    real, intent(in) :: x\n"
            "  end subroutine ghost\nend interface\n")
        pins = {**fortran_runner._HARNESS_PINS,
                HARNESS: fortran_runner._HarnessPin(interface=with_proto, distributed=False)}
        with mock.patch.object(fortran_runner, "_HARNESS_PINS", pins), \
                self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src)
        self.assertIn("carries no interface prototype", str(cm.exception))
        self.assertIn("ghost", str(cm.exception))

    def test_a_certified_harness_whose_writer_takes_the_thread_count_fails_the_pin(self) -> None:
        """Issue #512: the single-process runner calls the seven-argument `__write_perf` of
        `harness_fortran_cpu@0.10.0` §3.2, whose writer obtains the thread count itself, so a
        certified harness whose writer still takes `threads_per_rank` — a `0.7.0` one — is
        refused before Build, in its IR signatures and in its model source alike, naming the
        writer. The pinned interface is the distributed harness's block but for that argument."""
        sym = "harness_fortran_cpu__write_perf"
        self.assertNotIn("threads_per_rank", _SINGLE_PROCESS_INTERFACE)
        self.assertEqual(
            _SINGLE_PROCESS_INTERFACE,
            _HARNESS_V3_INTERFACE.replace(", threads_per_rank, gpu_devices)", ", gpu_devices)")
            .replace("  integer, intent(in) :: threads_per_rank\n", ""))
        old_sigs = copy.deepcopy(self.sigs)
        perf = next(e for e in old_sigs if e["symbol"] == sym)
        args = perf["signature"]["args"]
        args.insert(len(args) - 1, {**copy.deepcopy(args[-1]), "name": "threads_per_rank"})
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, old_sigs, self.src)
        self.assertIn(sym, str(cm.exception))
        old_src = _EIGHT_ARG_HARNESS_STUB
        self.assertNotEqual(old_src, self.src)
        self.assertIn("mpi_ranks, threads_per_rank, gpu_devices)", old_src)
        self.assertIn("intent(in) :: threads_per_rank", old_src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, old_src)
        self.assertIn(sym, str(cm.exception))

    def test_ir_signature_drift(self) -> None:
        bad = copy.deepcopy(self.sigs)
        for e in bad:
            if e["symbol"].endswith("__write_snapshot"):
                time_arg = next(a for a in e["signature"]["args"] if a["name"] == "time")
                time_arg["name"] = "tstamp"
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, bad, self.src)

    def test_old_vocab_ir_signatures_fail_closed_not_crash(self) -> None:
        # C2: a stale IR that still carries the OLD Fortran length token (`len: ':'`) in a string
        # spec is unrenderable through the neutral backend; assert_harness_pin must fail closed with
        # a re-certify RenderError (the entry is skipped as unusable), NEVER an uncaught
        # SignatureParseError that crashes the conductor.
        stale = copy.deepcopy(self.sigs)
        for e in stale:
            for ent in [*e["signature"].get("args", []),
                        *([e["signature"]["result"]] if e["signature"].get("result") else []),
                        *e["signature"].get("components", [])]:
                if ent.get("spec", {}).get("type") == "string":
                    ent["spec"]["len"] = ":"  # revert to the removed Fortran token
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, stale, self.src)

    def test_attribute_form_export_in_the_certified_harness_passes_the_pin(self) -> None:
        # Issue #363: harness §3 leaves the export of `dp` / `case_id_len` free, and the
        # attribute form is one of Fortran's two export spellings. The pin reads the same
        # `source_atoms` view as the Generate gate, so a harness the gate certifies in that
        # form must not stop every dependent node's render here.
        src = self.src
        for name in ("dp = real64", "case_id_len = 64"):
            self.assertIn(f"integer, parameter :: {name}", src)
            src = src.replace(f"integer, parameter :: {name}",
                              f"integer, parameter, public :: {name}", 1)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, src)

    def test_a_public_type_header_passes_the_pin_and_a_private_one_does_not(self) -> None:
        # Issue #343: the pin compares a type layout the way the Generate gate does
        # (`signatures.type_layout_drift`), so a harness the gate certifies with `type, public :: t` must
        # not stop every dependent's render here; `private` still differs.
        header = "type :: harness_fortran_cpu__h_named"
        self.assertIn(header, self.src)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs,
                           self.src.replace(header, "type, public :: harness_fortran_cpu__h_named"))
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src.replace(
                header, "type, private :: harness_fortran_cpu__h_named"))
        self.assertIn("harness_fortran_cpu__h_named", str(cm.exception))

    def test_an_abi_identical_type_spelling_passes_the_pin_and_a_second_definition_does_not(
            self) -> None:
        # Issue #430 PR-2: the pin reads a type the way the Generate gate does — its one
        # module-level definition in the harness model module, components compared as declared —
        # so a `::`-less header and an unnamed `end type` must not stop a dependent's render, while
        # a second definition of a pinned type name, or a drifted component, must. (`h_named` is
        # the one type the runner builds since issue #582.)
        component = "character(len=:), allocatable :: json\n"
        header = "type :: harness_fortran_cpu__h_named\n"
        end = "end type harness_fortran_cpu__h_named\n"
        for needle in (component, header, end):
            self.assertIn(needle, self.src)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src.replace(
            header, "type harness_fortran_cpu__h_named\n", 1).replace(
            end, "end type\n", 1))
        helper = ("  subroutine harness_fortran_cpu__decoy()\n"
                  "    type :: harness_fortran_cpu__h_named\n"
                  "      integer :: k\n"
                  "    end type harness_fortran_cpu__h_named\n"
                  "  end subroutine harness_fortran_cpu__decoy\n")
        tail = "end module harness_fortran_cpu_model"
        self.assertIn(tail, self.src)
        for label, cue, source in (
                ("second definition", "differs from the pinned interface", self.src.replace(tail, helper + tail, 1)),
                ("drifted component", "differs from the pinned interface", self.src.replace(
                    component, "character(len=:), pointer :: json\n", 1)),
                ("not defined at all", "omits 'harness_fortran_cpu__h_named'", self.src.replace(
                    "harness_fortran_cpu__h_named\n", "harness_fortran_cpu__h_named_x\n")),
                # Defined once, but only inside a procedure: count 1, no module-level definition.
                ("defined only outside module level", "omits 'harness_fortran_cpu__h_named'", self.src.replace(
                    "type :: harness_fortran_cpu__h_named\n", "type :: hx_unused_rename\n", 1
                ).replace("end type harness_fortran_cpu__h_named\n",
                          "end type hx_unused_rename\n", 1).replace(
                    tail, helper.replace("integer :: k", "character(len=:), allocatable :: name\n"
                                         "      character(len=:), allocatable :: json") + tail, 1))):
            with self.subTest(label), self.assertRaises(RenderError) as cm:
                assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, source)
            self.assertIn(cue, str(cm.exception))

    def test_an_abi_identical_procedure_spelling_passes_the_pin(self) -> None:
        # Issue #430: the pin reads a procedure the way the Generate gate does — by what its
        # definition declares — so the O1 / O2 spellings the gate now certifies (a `dimension(:)`
        # attribute, a `kind=` selector) must not stop every dependent node's render here.
        o1 = "real(dp), intent(in) :: a(:)\n"
        o2 = "real(dp), intent(in) :: x\n"
        self.assertIn(o1, self.src)
        self.assertIn(o2, self.src)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src.replace(
            o1, "real(dp), dimension(:), intent(in) :: a\n", 1).replace(
            o2, "real(kind=dp), intent(in) :: x\n", 1))

    def test_an_attribute_statement_on_a_pinned_dummy_fails_the_pin(self) -> None:
        # The membership comparison this replaced accepted `optional :: x` beside the pinned
        # declaration; the renderer then emits a call whose actual the harness may ignore.
        decl = "    real(dp), intent(in) :: x\n"
        self.assertIn(decl, self.src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src.replace(
                decl, decl + "    optional :: x\n", 1))
        self.assertIn("harness_fortran_cpu__emit_real", str(cm.exception))

    def test_a_local_type_shadowing_a_pinned_type_fails_the_pin(self) -> None:
        # Issue #430 round 1: a published procedure defining its own type under the name a dummy
        # is pinned to makes that dummy's type the local one. The pin passes the definition's
        # local types to `surface_drift` as the gate does.
        header = "subroutine harness_fortran_cpu__write_snapshot("
        self.assertIn(header, self.src)
        start = self.src.index(header)
        body = self.src.index("\n", start) + 1
        shadowed = (self.src[:body] + "    type :: HARNESS_FORTRAN_CPU__H_NAMED\n"
                    "      integer :: k\n    end type HARNESS_FORTRAN_CPU__H_NAMED\n"
                    + self.src[body:])
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, shadowed)
        self.assertIn("harness_fortran_cpu__write_snapshot", str(cm.exception))

    def test_an_unresolvable_certified_source_is_a_render_error(self) -> None:
        wedged = self.src.replace(
            "contains\n",
            "contains\n  subroutine wedge()\n    real :: endsubroutine\n"
            "    endsubroutine = 1.0\n  end subroutine wedge\n", 1)
        self.assertNotEqual(wedged, self.src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, wedged)
        self.assertIn("no longer resolves through the Fortran structure front end",
                      str(cm.exception))
        # Where, not only how many: the first error's statement and snippet.
        self.assertRegex(str(cm.exception), r"the first at statement \d+ of its joined view: '= 1\.0'")
        self.assertIn("do not edit the renderer pin", str(cm.exception))

    def test_attribute_form_with_a_drifted_value_still_fails_the_pin(self) -> None:
        src = self.src.replace("integer, parameter :: case_id_len = 64",
                               "integer, parameter, public :: case_id_len = 32", 1)
        self.assertNotEqual(src, self.src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, src)
        self.assertIn("does not declare the pinned module parameter", str(cm.exception))

    def test_case_id_len_value_drift_is_caught(self) -> None:
        # The interface stanzas name the SYMBOL `case_id_len`, never its value, so a harness
        # recert lowering the width leaves the signature pin green — while this renderer keeps
        # emitting a 64-wide `case_ids(:)` actual for a 32-wide `intent(out)` dummy. That runner
        # compiles and then breaks at runtime. Pin the parameter VALUE.
        bad_src = self.src.replace("integer, parameter :: case_id_len = 64",
                                   "integer, parameter :: case_id_len = 32")
        self.assertNotEqual(bad_src, self.src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, bad_src)
        self.assertIn("case_id_len", str(cm.exception))

    def test_dp_kind_value_drift_is_caught(self) -> None:
        # `dp` fixes the kind of every `real(dp)` actual the glue passes.
        bad_src = self.src.replace("integer, parameter :: dp = real64",
                                   "integer, parameter :: dp = real32")
        self.assertNotEqual(bad_src, self.src)
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, bad_src)

    def test_combined_parameter_declaration_still_pins(self) -> None:
        # `integer, parameter :: dp = real64, case_id_len = 64` is the same ABI; per-entity
        # atom matching must accept it rather than false-fail a legal harness.
        combined = self.src.replace(
            "  integer, parameter :: dp = real64\n"
            "  integer, parameter :: case_id_len = 64\n",
            "  integer, parameter :: dp = real64, case_id_len = 64\n")
        self.assertNotEqual(combined, self.src)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, combined)

    def test_one_constant_drives_bound_declaration_and_pin(self) -> None:
        # Three things must agree, or the case_id bound stops describing the real truncation
        # width: the `_case_ids` bound, the width the rendered runner declares for its own
        # `case_ids(:)` buffer, and the parameter value pinned against the certified harness.
        # All three are `CASE_ID_LEN`; this test is the invariant, stated once.
        txt = render_runner(self.ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)
        self.assertIn(f"  integer, parameter :: case_id_len = {CASE_ID_LEN}", txt)
        self.assertIn(f"integer, parameter :: case_id_len = {CASE_ID_LEN}",
                      _HARNESS_V3_PARAMETERS)
        over = "c" * (CASE_ID_LEN + 1)
        ir = copy.deepcopy(self.ir)
        ir["case"]["test_case_set"][0]["case_id"] = over
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS, target=_TARGET_PROFILE.doc)

    def test_source_signature_drift(self) -> None:
        bad_src = self.src.replace(
            "subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)",
            "subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, nc, ok)")
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, bad_src)

    def test_missing_symbol_in_ir(self) -> None:
        pruned = [e for e in self.sigs if not e["symbol"].endswith("__box")]
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, pruned, self.src)

    def test_empty_signatures_fail_closed(self) -> None:
        # An uncertified / absent harness IR (empty, None, or non-list public_api.signatures)
        # must NOT false-pass the pin — the whole safety net rests on this. The failure is the
        # distinct "no usable public_api.signatures" (missing artifact), NOT the per-symbol
        # "omits ... recert drift" message (which is reserved for a real single-symbol drift).
        # Blank symbol/signature fields are malformed under the IR validator's non-empty rule,
        # so they must NOT seed a bogus "" key that later reads as per-symbol drift.
        for sigs in ([], None, "not-a-list", 42, [{"symbol": "x"}],
                     [{"symbol": " ", "signature": {}}],
                     [{"symbol": "harness_fortran_cpu__box", "signature": {}}]):
            with self.assertRaises(RenderError) as cm:
                assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, sigs, self.src)
            self.assertIn("no usable public_api.signatures", str(cm.exception))
            self.assertNotIn("omits", str(cm.exception))

    def test_empty_source_fail_closed(self) -> None:
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, "")

    def test_signature_entry_without_signature_fail_closed(self) -> None:
        junk = [{"symbol": e["symbol"]} for e in self.sigs]
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, junk, self.src)

    def test_type_component_reorder_drift(self) -> None:
        # Derived-type components are compared as an ORDERED list (component layout is ABI);
        # reordering the two components of h_named must be caught as drift.
        bad = copy.deepcopy(self.sigs)
        for e in bad:
            if e["symbol"].endswith("__h_named"):
                e["signature"]["components"].reverse()
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, bad, self.src)


    def test_type_component_reorder_drift_in_the_harness_SOURCE(self) -> None:
        # The sibling above perturbs the certified IR signatures, which exercises check (1). The
        # SOURCE-side ordered comparison — check (2), `stanza_line_list(src) == stanza_line_list
        # (exp)` — had no observer at all: relaxing it to a set left the whole suite green, so a
        # recert that reordered a derived type's components in the generated model source while
        # leaving its IR signatures correct would have passed the pin. Component ORDER is the §5
        # compatibility contract (it is the record layout the rendered runner is compiled
        # against), so the two sides need one observer each.
        reordered = self.src.replace(
            "    character(len=:), allocatable :: name\n"
            "    character(len=:), allocatable :: json\n",
            "    character(len=:), allocatable :: json\n"
            "    character(len=:), allocatable :: name\n")
        self.assertNotEqual(reordered, self.src, "the reorder did not apply to the stub")
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, reordered)
        self.assertIn("model source signature", str(cm.exception))


    # Amendment 4 of issue #589: the pin is enforced for what `_used_harness_ops` lists, so a
    # surface the IR's driver does not call may be absent from the certified harness, and one it
    # does call may not. Each row below drives both polarities through the real pin.

    @staticmethod
    def _without(source: str, *symbols: str) -> str:
        """`source` with the definition of each harness procedure removed."""
        for symbol in symbols:
            out, n = re.subn(rf"[ ]*(subroutine|function) {symbol}\(.*?end \1 {symbol}\n", "",
                             source, count=1, flags=re.S)
            if n != 1:
                raise AssertionError(f"{symbol} is not defined in the stub")
            source = out
        return source

    def test_a_certified_source_missing_read_case_is_refused(self) -> None:
        symbol = "harness_fortran_cpu__read_case"
        self.assertIn("read_case", _used_harness_ops(self.ir))
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs,
                               self._without(self.src, symbol))
        self.assertIn(f"certified harness model source omits {symbol!r}", str(cm.exception))
        # ...and in the certified IR's signatures, the pin's other half.
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS,
                               [e for e in self.sigs if e["symbol"] != symbol], self.src)
        self.assertIn(f"public_api.signatures omits {symbol!r}", str(cm.exception))

    def test_a_drifted_read_case_is_refused(self) -> None:
        header = "subroutine harness_fortran_cpu__read_case(case_id, ok)"
        self.assertIn(header, self.src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src.replace(
                header, "subroutine harness_fortran_cpu__read_case(id, ok)").replace(
                "character(len=*), intent(in) :: case_id\n        logical, intent(out) :: ok\n"
                "        ok = len_trim(case_id) > 0",
                "character(len=*), intent(in) :: id\n        logical, intent(out) :: ok\n"
                "        ok = len_trim(id) > 0"))
        self.assertIn("harness_fortran_cpu__read_case", str(cm.exception))

    def test_the_getters_are_pinned_exactly_when_the_ir_uses_them(self) -> None:
        used = _used_harness_ops(self.ir)
        for getter in ("get_array_r2", "get_real", "get_int"):
            self.assertIn(getter, used)
            with self.subTest(getter=getter), self.assertRaises(RenderError) as cm:
                assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs,
                                   self._without(self.src, f"harness_fortran_cpu__{getter}"))
            self.assertIn(f"omits 'harness_fortran_cpu__{getter}'", str(cm.exception))
        unused = ("get_array_r1", "get_array_r3", "get_array_r4", "get_bool")
        for getter in unused:
            self.assertNotIn(getter, used)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self._without(
            self.src, *(f"harness_fortran_cpu__{g}" for g in unused)))

    def test_the_rank_1_emitter_is_pinned_on_a_loop_for_its_series(self) -> None:
        # No rank-1 state in `_boundary_ir`: the rank-1 emitter is the series' on a loop node, and
        # nothing's on a one-call node.
        symbol = "harness_fortran_cpu__emit_array_r1"
        self.assertNotIn("emit_array_r1", _used_harness_ops(self.ir))
        cut = self._without(self.src, symbol)
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, cut)
        looped = copy.deepcopy(self.ir)
        looped["drive"]["loop"] = {"n_step": "inputs.n_step", "dt": "inputs.dt",
                                   "t_start": "inputs.t_start", "t_end": "inputs.t_end"}
        looped["drive"]["history"] = {"every": "inputs.every"}
        self.assertIn("emit_array_r1", _used_harness_ops(looped))
        assert_harness_pin(looped, BOUNDARY_SID, HARNESS, self.sigs, self.src)
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(looped, BOUNDARY_SID, HARNESS, self.sigs, cut)
        self.assertIn(f"omits {symbol!r}", str(cm.exception))

    def test_the_rendered_runner_calls_nothing_of_the_harness_the_pin_does_not_ask(self) -> None:
        """The reason `_used_harness_ops`' list can be trusted: a harness symbol the text names and
        the list omits would be an unenforced pin. Over each shape of driver."""
        for label, ir, sid in (("one call", _boundary_ir(), BOUNDARY_SID),
                               ("high rank", _rank34_metrics_ir(), RANK_SID),
                               ("loop", loop_ir(0), "drv_toy"), ("init", init_ir(0), "drv_toy"),
                               ("call", call_ir(0), "drv_toy"),
                               ("procedure", procedure_ir(), "drv_toy")):
            with self.subTest(label):
                txt = render_runner(ir, sid, HARNESS, target=_TARGET_PROFILE.doc)
                named = set(re.findall(r"harness_fortran_cpu__(\w+)", txt)) - {"h_named"}
                self.assertEqual(named, set(_used_harness_ops(ir)))


class DeclaredLintRuleHoldTest(unittest.TestCase):
    """Hold the RENDERED runner to the rule set the `Generate.gate` lint check imposes.

    A CLASS OF ITS OWN, because these rows render and lint and never compile: inside
    `GfortranSmokeTest` they inherited its compiler guard (then a class-level skip) and could
    not run on a host with a linter and no compiler — their own linter conditions could never
    fire, because the class guard won first. A review round found it, and the consequence
    was in the commit message: "a renderer edit that introduces a finding fails in an
    unbilled unit test rather than at a node's gate" held only where gfortran happened to be
    installed. Every guard here now FAILS rather than skips (#498), but they stay separate:
    a missing compiler must not be reported as a lint finding, nor the reverse.
    """

    def _assert_runner_clean_under_the_declared_lint_rules(
            self, ir: dict, sid: str) -> None:
        """The rendered runner passes the rule set the `Generate.gate` lint check imposes.

        Issue #112's "worth deciding at the same time": hold the host-authored artifact to the
        gate's rules AT THE POINT IT IS RENDERED, so a renderer edit that introduces a finding
        fails here — in an unbilled unit test — instead of at a node's gate, where the finding
        terminalizes the run.

        THIS IS A SAMPLE, NOT A PIN. It renders the IR fixtures this module happens to carry, so
        it cannot claim that every IR renders a clean runner; a shape none of these fixtures
        reaches could still produce one. The gate's `host_rendered_lint_findings` arm remains the
        backstop, and it is what makes the residual visible rather than silent.

        The invocation is the backend's own `check_argv`, never a hand-spelled command line: a
        copy here would be a second declaration of the rule set, and the whole point of that
        declaration is that there is one.
        """
        _require_declared_linter()
        from tools.backends.linter.fortitude import lint as _lint
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / f"{sid}_runner.f90").write_text(render_runner(ir, sid, HARNESS, target=_TARGET_PROFILE.doc))
            r = subprocess.run(list(_lint.check_argv(".")), cwd=d,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_rendered_runner_clean_under_the_declared_lint_rules(self) -> None:
        self._assert_runner_clean_under_the_declared_lint_rules(
            _boundary_ir(), BOUNDARY_SID)

    def test_rendered_high_rank_runner_clean_under_the_declared_lint_rules(self) -> None:
        self._assert_runner_clean_under_the_declared_lint_rules(
            _rank34_metrics_ir(), RANK_SID)

    def test_each_shape_of_driver_is_clean_under_the_declared_lint_rules(self) -> None:
        # The loop, `init`, one-call and procedure-argument shapes, each with lines the two rows
        # above never render.
        for label, ir in (("loop", loop_ir(0)), ("init", init_ir(0)), ("call", call_ir(0)),
                          ("procedure", procedure_ir())):
            with self.subTest(label):
                self._assert_runner_clean_under_the_declared_lint_rules(ir, "drv_toy")


def _build_and_run(d: Path, ir: dict, sid: str, model: str, flags: list[str] | None = None,
                   env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Render `ir`'s runner, build it with `model` against the reading harness of
    `data/driver_e2e/`, and run every case of `ir` in `d`."""
    from tools.backends.language.fortran import control_file

    flags = flags if flags is not None else control_file.flags("f2008", "none").split(" -J")[0].split()
    (d / "ir").mkdir(exist_ok=True)
    (d / "ir" / "spec.ir.yaml").write_text(yaml.safe_dump(ir))
    pe.write_case_inputs(ir, d / "ir")
    (d / "harness_fortran_cpu_model.f90").write_text(
        (DRIVER_E2E / "harness_fortran_cpu_model.f90").read_text())
    (d / f"{sid}_model.f90").write_text(model)
    (d / f"{sid}_runner.f90").write_text(render_runner(ir, sid, HARNESS, target=_TARGET_PROFILE.doc))
    objects = []
    for stem in ("harness_fortran_cpu_model", f"{sid}_model", f"{sid}_runner"):
        built = subprocess.run(["gfortran", *flags, "-c", f"{stem}.f90", "-o", f"{stem}.o"],
                               cwd=d, capture_output=True, text=True)
        if built.returncode != 0:
            raise AssertionError(f"{stem}: {built.stderr[:3000]}")
        objects.append(f"{stem}.o")
    subprocess.run(["gfortran", *flags, *objects, "-o", "runner"], cwd=d, check=True,
                   capture_output=True)
    for sub in ("initial", "history", "series"):
        (d / "raw" / "state_snapshots" / sub).mkdir(parents=True, exist_ok=True)
    return _rerun(d, ir, env)


def _rerun(d: Path, ir: dict, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["./runner", "--cases", "ir/spec.ir.yaml",
         *(c["case_id"] for c in ir["case"]["test_case_set"])],
        cwd=d, capture_output=True, text=True, env={**os.environ, **(env or {})})


class GfortranSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _require_gfortran()

    def test_the_perf_record_carries_the_thread_count_the_run_was_launched_with(self) -> None:
        """Issue #512: one binary runs at several thread counts in Validate, and the perf record
        of each run states that run's count — the one the harness's `__write_perf` obtains
        itself, the rendered runner passing none — not the profile's. Built with the OpenMP backend's flag and
        launched in its environment at two counts other than the profile's; built without the
        flag, the query compiles away and the record states 1."""
        from tools.backends.language.fortran import control_file
        from tools.backends.parallel.openmp import execution as omp_execution
        ir = _boundary_ir()
        omp_flags = control_file.flags("f2008", "openmp").split(" -J")[0].split()
        self.assertIn("-fopenmp", omp_flags)
        for flags, counts in ((omp_flags, {2: 2, 3: 3}),
                              (control_file.flags("f2008", "none").split(" -J")[0].split(),
                               {3: 1})):
            with tempfile.TemporaryDirectory() as td:
                d = Path(td)
                built = _build_and_run(d, ir, BOUNDARY_SID, _BOUNDARY_MODEL, flags)
                self.assertEqual(built.returncode, 0, built.stderr)
                for launched, recorded in counts.items():
                    self.assertNotEqual(launched, _TARGET_PROFILE.threads_per_rank)
                    run = _rerun(d, ir, omp_execution.environment(launched))
                    self.assertEqual(run.returncode, 0, run.stderr)
                    perf = json.loads((d / "perf.json").read_text())
                    with self.subTest(flags=flags, launched=launched):
                        self.assertEqual(perf["parallelism"]["threads_per_rank"], recorded)

    def test_rendered_runner_compiles_and_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            run = _build_and_run(d, _boundary_ir(), BOUNDARY_SID, _BOUNDARY_MODEL)
            self.assertEqual(run.returncode, 0, run.stderr)
            # every case emitted its own snapshot, perf exists, and nothing else the retired
            # judging half wrote does (issue #582)
            cases = ("l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass", "l0_invalid_ny_xfail")
            for cid in cases:
                self.assertTrue((d / "raw" / "state_snapshots" / f"{cid}.json").is_file())
                self.assertTrue(
                    (d / "raw" / "state_snapshots" / "initial" / f"{cid}.json").is_file())
            self.assertTrue((d / "perf.json").is_file())
            self.assertFalse((d / "diagnostics.json").exists())
            self.assertFalse((d / "raw" / "metrics_basis.json").exists())
            initial = json.loads(
                (d / "raw" / "state_snapshots" / "initial" / f"{cases[0]}.json").read_text())
            final = json.loads((d / "raw" / "state_snapshots" / f"{cases[0]}.json").read_text())
            # initial = the case file's state (ghost all zero), final = after the call (the
            # interior copied into the ghost's centre): the two capture points differ.
            self.assertEqual(initial["field_ghost"][1][1], 0.0)
            self.assertEqual(final["field_ghost"][1][1], 11.0)
            self.assertEqual(final["field_interior"], [[11.0, 12.0], [21.0, 22.0]])
            self.assertEqual(initial["max_abs_deviation"], -1.0)
            self.assertEqual(final["max_abs_deviation"], 1.0)
            # Each case's snapshot carries ITS OWN state: the operation's value is the case's
            # ordinal; the third refused, and its capture still holds what it wrote.
            for ordinal, cid in enumerate(cases, start=1):
                snap = json.loads((d / "raw" / "state_snapshots" / f"{cid}.json").read_text())
                self.assertEqual(snap["max_abs_deviation"], float(ordinal), cid)
                self.assertEqual(snap["ok"], 0.0 if ordinal == 3 else 1.0, cid)
                self.assertEqual(snap["step"], 0.0 if ordinal == 3 else 1.0, cid)
                self.assertEqual(snap["guard_fired"], 1.0 if ordinal == 3 else 0.0, cid)

    def test_high_rank_runner_compiles_and_runs(self) -> None:
        # The boundary smoke only covers the scalar+rank-2 family. This one compiles+links+runs
        # the rank-3/rank-4 reader and emitter path, so a future edit that makes it produce
        # invalid Fortran, or reorders an index, cannot ship green.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            ir = _rank34_metrics_ir()
            run = _build_and_run(d, ir, RANK_SID, _RANK_MODEL)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue((d / "perf.json").is_file())
            given = ir["case"]["test_case_set"][0]["initial_state"]
            initial = json.loads((d / "raw" / "state_snapshots" / "initial" / "c0.json").read_text())
            final = json.loads((d / "raw" / "state_snapshots" / "c0.json").read_text())
            # the nested arrays come back as the case gave them, the leading index outermost
            self.assertEqual(initial["a3"], given["a3"])
            self.assertEqual(initial["a4"], given["a4"])
            self.assertEqual(final["a3"][1][1], [14.0, 16.0])
            self.assertEqual(final["a4"][1][0][1], [6.0, 8.0])
            self.assertEqual(final["s"], 72.0)

    def _assert_runner_clean_under_promoted_warnings(self, ir: dict, sid: str, model: str) -> None:
        """The `Generate.gate` syntax check promotes unused-dummy-argument / unused-variable /
        ampersand to errors over the whole staged set. The runner is host-rendered, so such a
        warning is unfixable by the leaf and would spin a futile warm-repair loop — the
        rendered artifact must be clean under all three. The harness and the model are compiled
        WITHOUT the flags: only the runner, the artifact the renderer owns, is held to them."""
        runner = render_runner(ir, sid, HARNESS, target=_TARGET_PROFILE.doc)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            mods = d / "mods"
            mods.mkdir()
            (d / "harness_fortran_cpu_model.f90").write_text(
                (DRIVER_E2E / "harness_fortran_cpu_model.f90").read_text())
            (d / f"{sid}_model.f90").write_text(model)
            (d / f"{sid}_runner.f90").write_text(runner)
            pre = subprocess.run(
                ["gfortran", "-fsyntax-only", "-std=f2008", "-J", str(mods),
                 "harness_fortran_cpu_model.f90", f"{sid}_model.f90"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(pre.returncode, 0, pre.stderr)
            r = subprocess.run(
                ["gfortran", "-fsyntax-only", "-std=f2008",
                 "-Werror=unused-dummy-argument", "-Werror=unused-variable",
                 "-Werror=ampersand",
                 "-J", str(mods), "-I", str(mods), f"{sid}_runner.f90"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_rendered_runner_clean_under_promoted_unused_warnings(self) -> None:
        self._assert_runner_clean_under_promoted_warnings(
            _boundary_ir(), BOUNDARY_SID, _BOUNDARY_MODEL)

    def test_rendered_high_rank_runner_clean_under_promoted_unused_warnings(self) -> None:
        self._assert_runner_clean_under_promoted_warnings(_rank34_metrics_ir(), RANK_SID, _RANK_MODEL)

    def test_each_shape_of_driver_is_clean_under_promoted_unused_warnings(self) -> None:
        model = (DRIVER_E2E / "drv_toy_model.f90").read_text()
        for label, ir in (("loop", loop_ir(0)), ("init", init_ir(0)), ("call", call_ir(0)),
                          ("procedure", procedure_ir())):
            with self.subTest(label):
                self._assert_runner_clean_under_promoted_warnings(ir, "drv_toy", model)


# --- the distributed harness (issue #316, R4-c PR-4) --------------------------------------------
#
# The driver does not render for it yet (`test_driver_render.RenderRefusalTest`), so what stays
# here is what a physics source and the pin still hold it to.

DIST_HARNESS = "harness_fortran_cpu_mpi"
DIST_SID = "prob_dist"


def _distributed_ir() -> dict:
    """A problem IR whose driver would read and emit the array ranks the distributed harness's
    gather covers: what `assert_harness_pin` asks of that harness."""
    return _driven({
        "meta": {"spec_id": DIST_SID, "spec_kind": "problem"},
        "case": {"test_case_set": [{"case_id": "c0", "inputs": {"mode": 0}}]},
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "schema": {
                    "variables": [
                        {"name": "u", "shape_expr": "[10]"},
                        {"name": "w", "shape_expr": "[3, 10]"},
                        {"name": "s", "shape_expr": "scalar"},
                    ],
                    "time_variable": "t",
                }},
            ]},
            "tests": [{"test_id": "c0", "expected_outcome": "pass",
                       "target_cases": ["c0"], "quantities": ["q"]}],
            "predicates": [],
        },
        "dependency": {"node_key": f"problem/{DIST_SID}@0.1.0", "direct_deps": []},
    }, f"{DIST_SID}__apply", {"u": ("inout", 1), "w": ("inout", 2), "s": ("out", 0)})


class DistributedHarnessBoundTest(unittest.TestCase):
    def test_every_distributed_operation_name_is_bounded(self) -> None:
        """`_check_identifier_lengths` refuses a harness id whose longest name would not fit, the
        distributed operations included: one of them is longer than any plumbing name, and
        while `write_metrics_basis` was published that bound came by accident."""
        from tools.backends.language.fortran import runner as fortran_runner
        longest = "exchange_halo_r1"
        self.assertEqual(max((*fortran_runner._HARNESS_CORE_OPS,
                              *fortran_runner._DISTRIBUTED_RUNNER_OPS,
                              *fortran_runner.PHYSICS_CALLABLE_DISTRIBUTED_OPS), key=len), longest)
        limit = fortran_runner.bundle.IDENTIFIER_MAX
        fits = "h" * (limit - len(longest) - 2)
        fortran_runner._check_identifier_lengths("bx", fits)
        with self.assertRaisesRegex(fortran_runner.RenderError, "exchange_halo_r1"):
            fortran_runner._check_identifier_lengths("bx", fits + "h")

    def test_a_refused_distributed_render_is_excluded_from_the_compile_mirror(self) -> None:
        # A re-author cannot repair it, so it must not reach `compile.generate` as a content
        # defect; its refusal is the target's preflight and the render's backstop.
        self.assertEqual(ir_content_violations(_distributed_ir(), DIST_SID, DIST_HARNESS), [])


class HarnessPinTableTest(unittest.TestCase):
    """The renderer's harness table, against the manifest and the harness spec."""

    def test_the_distributed_column_is_the_manifests(self) -> None:
        from tools.backends.language.fortran import runner as fortran_runner
        from tools.codegen_bundle import HARNESS_CAPABILITY_MANIFESTS, capability_name
        seen = set()
        for node_key, provides in HARNESS_CAPABILITY_MANIFESTS.items():
            sid = node_key.split("/", 1)[1].split("@", 1)[0]
            if sid not in fortran_runner._HARNESS_PINS:
                continue
            seen.add(sid)
            self.assertEqual(
                fortran_runner._HARNESS_PINS[sid].distributed,
                any(capability_name(t) == "distributed_state" for t in provides), node_key)
        self.assertEqual(seen, set(fortran_runner._HARNESS_PINS))

    def test_each_pinned_interface_is_its_harness_specs_section_5_1(self) -> None:
        import yaml as _yaml
        from tools.backends.language.fortran import runner as fortran_runner
        from tools.backends.language.fortran.signatures import render_signatures_to_fortran
        root = Path(__file__).resolve().parents[2]
        for sid, pin in fortran_runner._HARNESS_PINS.items():
            with self.subTest(harness=sid):
                spec = (root / "spec/infrastructure/infra/harness" / sid
                        / "controlled_spec.md").read_text(encoding="utf-8")
                block = re.search(r"### 5\.1.*?```yaml\n(.*?)```", spec, re.S).group(1)
                rendered = render_signatures_to_fortran(_yaml.safe_load(block))
                # The module parameters are pinned separately (`_HARNESS_V3_PARAMETERS`).
                body = "\n".join(line for line in rendered.splitlines()
                                  if not line.startswith("integer, parameter ::"))
                self.assertEqual(body.strip(), pin.interface.strip())

    def test_the_distributed_pin_holds_against_its_own_interface_and_names_what_it_used(
            self) -> None:
        from tools.backends.language.fortran import runner as fortran_runner
        iface = fortran_runner._HARNESS_PINS[DIST_HARNESS].interface
        struct = parse_signatures_from_fortran(iface)
        sigs = [{"symbol": s["name"], "signature": s}
                for s in [*struct["procedures"], *struct["types"]]]
        # A module, not the bare interface text: since issue #430 the pin reads each procedure
        # from the harness model module's own definition, as the Generate gate does.
        cut = re.search(r"^(?:subroutine|function) ", iface, re.MULTILINE).start()
        source = (f"module {DIST_HARNESS}_model\n"
                  "  use, intrinsic :: iso_fortran_env, only: real64\n"
                  "  integer, parameter :: dp = real64\n"
                  f"  integer, parameter :: case_id_len = {CASE_ID_LEN}\n" + iface[:cut]
                  + "contains\n" + iface[cut:] + f"end module {DIST_HARNESS}_model\n")
        assert_harness_pin(_distributed_ir(), DIST_SID, DIST_HARNESS, sigs, source)
        for dropped in ("__gather_r2", "__init", "__partition", "__reduce_max"):
            with self.subTest(dropped=dropped):
                bad = [e for e in sigs if not e["symbol"].endswith(dropped)]
                with self.assertRaises(RenderError) as cm:
                    assert_harness_pin(_distributed_ir(), DIST_SID, DIST_HARNESS, bad, source)
                self.assertIn(f"omits '{DIST_HARNESS}{dropped}'", str(cm.exception))
        # The single-process pin asks none of them.
        single = [e for e in sigs if "__init" not in e["symbol"]]
        self.assertIsNone(fortran_runner.physics_harness_uses("harness_fortran_cpu").get(
            "harness_fortran_cpu_model"))
        self.assertTrue(single)

    def test_the_seam_answers(self) -> None:
        from tools.backends.language.fortran import runner as fortran_runner
        uses = fortran_runner.physics_harness_uses(f" {DIST_HARNESS} ")
        self.assertEqual(set(uses), {f"{DIST_HARNESS}_model"})
        self.assertEqual(uses[f"{DIST_HARNESS}_model"], frozenset(
            f"{DIST_HARNESS}__{op}" for op in fortran_runner.PHYSICS_CALLABLE_DISTRIBUTED_OPS))
        self.assertEqual(fortran_runner.physics_harness_uses("harness_fortran_cpu"), {})


class HostVerdictRunnerSurfaceTest(unittest.TestCase):
    """Issue #582: a test is decided by host predicates over the captures, so the rendered runner
    judges nothing and imports only what it calls. Over two rank families and the driver's other
    shapes; the CUDA renderer's counterpart is in `test_language_cuda_cpp_runner`."""

    #: The judging half of the retired runner, as text: the two callbacks, the two writers, and
    #: the records and helpers only they used.
    RETIRED = ("checks_compute", "metric_compute", "write_diagnostics", "write_metrics_basis",
               "mb_entries", "results", "find_case_index", "pick(", "expected_xfail",
               "h_check", "h_metric", "h_case_result", "h_mb_entry", "cstatus")
    #: The leaf-authored checks module the driver replaced (issue #589), as text.
    RETIRED_CHECKS = ("_checks", "case_setup", "case_run", "get_time", "sb_", "require_bound")

    @staticmethod
    def _variants() -> list[tuple[str, dict, str, str]]:
        return [("boundary", _boundary_ir(), BOUNDARY_SID, HARNESS),
                ("high_rank", _rank34_metrics_ir(), RANK_SID, HARNESS),
                ("loop", loop_ir(0), "drv_toy", HARNESS),
                ("init", init_ir(0), "drv_toy", HARNESS),
                ("procedure", procedure_ir(), "drv_toy", HARNESS)]

    @staticmethod
    def _use_block(text: str, module: str) -> tuple[list[str], str]:
        """The names a `use <module>, only:` block imports, and the text after the block."""
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith(f"  use {module}, only: &"))
        names, i = [], start + 1
        while True:
            names.append(lines[i].strip().rstrip("&").strip().rstrip(","))
            i += 1
            if not lines[i - 1].rstrip().endswith("&"):
                break
        return names, "\n".join(lines[i:])

    def _each(self):
        for label, ir, sid, harness in self._variants():
            with self.subTest(label):
                yield ir, sid, harness, render_runner(ir, sid, harness,
                                                      target=_TARGET_PROFILE.doc)

    def test_no_retired_callback_writer_or_record_is_named(self) -> None:
        for _ir, _sid, _harness, text in self._each():
            for gone in (*self.RETIRED, *self.RETIRED_CHECKS):
                self.assertNotIn(gone, text)

    def test_every_imported_harness_and_model_name_is_used(self) -> None:
        for _ir, sid, harness, text in self._each():
            imported, after = self._use_block(text, f"{harness}_model")
            self.assertIn(f"{harness}__h_named", imported)
            for name in imported:
                self.assertRegex(after, rf"\b{name}\b", name)
            # the types the runner no longer builds are not imported, though the pinned harness
            # still publishes them until the harness PR
            self.assertEqual([n for n in imported if "__h_" in n], [f"{harness}__h_named"])
            operations, after = self._use_block(text, f"{sid}_model")
            self.assertTrue(operations)
            for name in operations:
                self.assertRegex(after, rf"call {name}\(", name)

    def test_every_schema_variable_is_captured_at_both_points(self) -> None:
        for ir, _sid, harness, text in self._each():
            variables = [v["name"] for v in ir["io_contract"]["raw_requirements"][
                "required_evidence"][0]["schema"]["variables"]]
            capture = text[text.index("  subroutine capture_state("):
                           text.index("  end subroutine capture_state")]
            self.assertIn(f"allocate(out({len(variables) + 1}))", capture)
            for name in variables:
                self.assertRegex(capture, rf"__box\(\s*&?\s*'{name}'", name)
                self.assertIn(f"st_{name}", capture)
            self.assertIn(f"call {harness}__write_snapshot('initial/'//cid, vals, t_c)", text)
            self.assertIn(f"call {harness}__write_snapshot(cid, vals, t_c)", text)

    def test_write_perf_is_called_once_after_the_case_loop(self) -> None:
        for _ir, _sid, harness, text in self._each():
            self.assertEqual(1, text.count(f"call {harness}__write_perf("))
            self.assertLess(text.rindex("    steps_total = steps_total + step_c"),
                            text.index(f"call {harness}__write_perf("))
            self.assertLess(text.index(f"call {harness}__write_perf("), text.index("contains\n\n  !"))

    def test_the_runner_reads_neither_the_tests_nor_the_predicates(self) -> None:
        """What a test judges changes nothing in the runner: it captures the schema."""
        for ir, sid, harness, text in self._each():
            changed = copy.deepcopy(ir)
            changed["io_contract"].pop("tests", None)
            changed["io_contract"].pop("predicates", None)
            self.assertEqual(text, render_runner(changed, sid, harness,
                                                 target=_TARGET_PROFILE.doc))

    def test_the_pin_asks_for_no_retired_symbol(self) -> None:
        """The surface the pin holds a harness to names none of the retired writers and record
        types, and a harness publishing that surface passes it."""
        symbols = [s["symbol"] for s in _harness_signatures()]
        self.assertEqual(
            [s for s in symbols if s.endswith((
                "__write_diagnostics", "__write_metrics_basis", "__h_check", "__h_metric",
                "__h_case_result", "__h_mb_entry"))], [])
        assert_harness_pin(_boundary_ir(), BOUNDARY_SID, HARNESS, _harness_signatures(),
                           _HARNESS_STUB)


if __name__ == "__main__":
    unittest.main()
