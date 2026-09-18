#!/usr/bin/env python3
"""Unit tests for the Fortran backend's runner_render capability (R1/M3c-β runner glue).

`render_runner` is a pure function of the IR: these tests pin its rendered shape
(a boundary-copy IR and a metrics-bearing IR), determinism, the render-error
matrix, and the harness signature pin. A `gfortran`-gated smoke compiles+runs the
rendered runner against a v2 harness stub + a fixed-ABI checks stub end-to-end.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.backends.language.fortran import lines as fortran_lines
from tools.backends.language.fortran.runner import (
    CASE_ID_LEN,
    CHECK_STATUS_WIDTH,
    CHECKS_PUBLIC_NAMES,
    EXPECTED_HARNESS_SPEC_ID,
    METRIC_COMPUTE_DEFERRED_LENGTH_DUMMY,
    METRIC_COMPUTE_DUMMIES,
    _HARNESS_V3_PARAMETERS,
    _HARNESS_V3_INTERFACE,
    assert_harness_pin,
    checks_abi_dummy_violation,
    ir_content_violations,
    render_runner,
)
from tools.backends.language.fortran.signatures import parse_signatures_from_fortran
from tools.host_render import RenderError

HARNESS = "harness_fortran_cpu"
BOUNDARY_SID = "dynamics_shallow_water_boundary_2d_periodic_copy"

_HAVE_GFORTRAN = shutil.which("gfortran") is not None


#: The two host conditions under which the declared lint invocation cannot decide a verdict.
#: Both are LITERAL strings because `test_skip_reasons_are_declared.py` reads them statically —
#: a computed reason is one nobody declared, which is exactly the thing that table exists to
#: refuse.
_LINTER_ABSENT_SKIP = "the declared lint invocation's linter is not installed"
_LINTER_UNMEASURED_SKIP = "the installed linter is outside the measured version range"


def _linter_skip_reason() -> str | None:
    """Which of the two declared skip conditions holds, or `None` when neither does.

    Asks the linter backend the two questions it owns — is the executable here, and is this build
    inside the range the declared rule set was measured on — so this test skips for a reason it
    can state rather than silently passing on a machine with no linter."""
    from tools.backends.linter.fortitude import lint as _lint
    if shutil.which(_lint.EXECUTABLE) is None:
        return _LINTER_ABSENT_SKIP
    probe = subprocess.run(list(_lint.version_argv()), capture_output=True, text=True)
    if _lint.unsupported_version_reason(probe.stdout or probe.stderr) is not None:
        return _LINTER_UNMEASURED_SKIP
    return None


def _boundary_ir() -> dict:
    """A boundary_2d_periodic_copy-shaped IR: 3 cases (2 pass + 1 xfail), rank-2 +
    scalar snapshot variables, 3 checks, no metrics, 1 infra dep."""
    return {
        "meta": {"spec_id": BOUNDARY_SID, "spec_kind": "component"},
        "case": {"test_case_set": [
            {"case_id": "l0_periodic_x_wrap_pass"},
            {"case_id": "l0_periodic_y_wrap_pass"},
            {"case_id": "l0_invalid_ny_xfail"},
        ]},
        "impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp"},
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "backend_overrides": {"openmp": {"num_threads": 1}},
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "required": True, "min_samples": 3, "schema": {
                    "variables": [
                        {"name": "field_ghost", "shape_expr": "[4, 4]"},
                        {"name": "field_interior", "shape_expr": "[2, 2]"},
                        {"name": "max_abs_deviation", "shape_expr": "scalar"},
                        {"name": "guard_fired", "shape_expr": "scalar"},
                    ],
                    "time_variable": "t", "time_shape_expr": "scalar",
                }},
                {"artifact": "metrics_basis.json", "required": True, "min_samples": 1},
            ]},
            "test_evidence_requirements": [
                {"test_id": "l0_periodic_x_wrap_pass",
                 "required_raw_variables": ["field_ghost", "field_interior", "max_abs_deviation"]},
                {"test_id": "l0_periodic_y_wrap_pass",
                 "required_raw_variables": ["field_ghost", "field_interior", "max_abs_deviation"]},
                {"test_id": "l0_invalid_ny_xfail",
                 "required_raw_variables": ["guard_fired"]},
            ],
            "diagnostics_contract": {
                "checks": [{"id": "x_wrap"}, {"id": "y_wrap"}, {"id": "input_guard"}],
                "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "l0_periodic_x_wrap_pass", "expected_outcome": "pass",
                 "target_cases": ["l0_periodic_x_wrap_pass"]},
                {"test_id": "l0_periodic_y_wrap_pass", "expected_outcome": "pass",
                 "target_cases": ["l0_periodic_y_wrap_pass"]},
                {"test_id": "l0_invalid_ny_xfail", "expected_outcome": "xfail",
                 "target_cases": ["l0_invalid_ny_xfail"]},
            ],
        },
        "dependency": {
            "node_key": f"component/{BOUNDARY_SID}@0.1.0",
            "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}],
        },
    }


def _metrics_ir() -> dict:
    """A minimal problem-shaped IR with a metric address, to pin the metric_compute
    rendering path."""
    return {
        "meta": {"spec_id": "prob_x", "spec_kind": "problem"},
        "case": {"test_case_set": [{"case_id": "c_pass"}]},
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
            "test_evidence_requirements": [
                {"test_id": "c_pass", "required_raw_variables": ["u"]},
            ],
            "diagnostics_contract": {
                "checks": [{"id": "conv"}],
                "metrics": ["error.l2", "error.linf"],
                "verdict": {"fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "c_pass", "expected_outcome": "pass", "target_cases": ["c_pass"]},
            ],
        },
        "dependency": {"node_key": "problem/prob_x@0.1.0",
                       "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]},
    }


RANK_SID = "prob_rank"


def _rank34_metrics_ir() -> dict:
    """A problem IR exercising the rank-3 + rank-4 emitter/getter paths AND the
    metrics path together — the code families the boundary IR never compiles."""
    return {
        "meta": {"spec_id": RANK_SID, "spec_kind": "problem"},
        "case": {"test_case_set": [{"case_id": "c0"}]},
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
            "test_evidence_requirements": [
                {"test_id": "c0", "required_raw_variables": ["a3", "a4", "s"]},
            ],
            "diagnostics_contract": {
                "checks": [{"id": "c1"}],
                "metrics": ["m.one", "m.two"],
                "verdict": {"fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "c0", "expected_outcome": "pass", "target_cases": ["c0"]},
            ],
        },
        "dependency": {"node_key": f"problem/{RANK_SID}@0.1.0",
                       "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]},
    }


# A checks stub matching _rank34_metrics_ir: rank-3 + rank-4 + scalar BOUND storage with
# real data (the module-level variables the runner reads directly, named as the IR snapshot
# variables), one check, two metrics — enough to compile+link+run against the harness stub.
_RANK_CHECKS_STUB = textwrap.dedent(f"""\
    module {RANK_SID}_checks
      use, intrinsic :: iso_fortran_env, only: real64
      ! allow(C003)
      implicit none
      private
      integer, parameter :: dp = real64
      real(dp), allocatable :: a3(:, :, :)
      real(dp), allocatable :: a4(:, :, :, :)
      real(dp) :: s = 0.0_dp
      public :: case_setup, case_run, get_time
      public :: checks_compute, metric_compute
      public :: a3, a4, s
    contains
      subroutine case_setup(case_id, ok)
        character(len=*), intent(in) :: case_id
        logical, intent(out) :: ok
        if (.not. allocated(a3)) allocate(a3(2, 2, 2))
        if (.not. allocated(a4)) allocate(a4(2, 2, 2, 2))
        a3 = 1.0_dp
        a4 = 2.0_dp
        s = 3.0_dp
        ok = len_trim(case_id) > 0
      end subroutine case_setup
      subroutine case_run(case_id, steps, cells_updated, ok)
        character(len=*), intent(in) :: case_id
        integer, intent(out) :: steps, cells_updated
        logical, intent(out) :: ok
        steps = 1
        cells_updated = 8
        ok = len_trim(case_id) > 0
      end subroutine case_run
      subroutine get_time(t)
        real(dp), intent(out) :: t
        t = 0.0_dp
      end subroutine get_time
      subroutine checks_compute(case_id, check_id, status)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: check_id
        character(len=4), intent(out) :: status
        select case (trim(check_id))
        case default
          status = 'pass'
        end select
        if (len_trim(case_id) < 0) continue
      end subroutine checks_compute
      subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: name
        real(dp), intent(out) :: val
        logical, intent(out) :: is_na
        character(len=:), allocatable, intent(out) :: reason_na
        logical, intent(out) :: found
        is_na = .false.
        reason_na = ''
        found = .true.
        select case (trim(name))
        case ('m.one')
          val = 1.5_dp
        case ('m.two')
          val = 2.5_dp
        case default
          val = 0.0_dp
          found = .false.
        end select
        if (len_trim(case_id) < 0) continue
      end subroutine metric_compute
    end module {RANK_SID}_checks
    """)


def _harness_signatures() -> list[dict]:
    """The certified harness IR public_api.signatures, synthesized from §5.1."""
    struct = parse_signatures_from_fortran(_HARNESS_V3_INTERFACE)
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
      type :: harness_fortran_cpu__h_check
        character(len=:), allocatable :: id
        character(len=4) :: status
      end type harness_fortran_cpu__h_check
      type :: harness_fortran_cpu__h_metric
        character(len=:), allocatable :: name
        real(dp) :: value
        logical :: is_na
        character(len=:), allocatable :: reason_na
      end type harness_fortran_cpu__h_metric
      type :: harness_fortran_cpu__h_case_result
        character(len=:), allocatable :: case_id
        logical :: expected_xfail
        type(harness_fortran_cpu__h_check), allocatable :: checks(:)
        type(harness_fortran_cpu__h_metric), allocatable :: metrics(:)
      end type harness_fortran_cpu__h_case_result
      type :: harness_fortran_cpu__h_mb_entry
        character(len=:), allocatable :: test_id
        character(len=:), allocatable :: case_id
        type(harness_fortran_cpu__h_named), allocatable :: values(:)
      end type harness_fortran_cpu__h_mb_entry
      public :: harness_fortran_cpu__h_named, harness_fortran_cpu__h_check
      public :: harness_fortran_cpu__h_metric, harness_fortran_cpu__h_case_result
      public :: harness_fortran_cpu__h_mb_entry
      public :: harness_fortran_cpu__parse_cases, harness_fortran_cpu__emit_real
      public :: harness_fortran_cpu__emit_int, harness_fortran_cpu__emit_bool
      public :: harness_fortran_cpu__emit_array_r1, harness_fortran_cpu__emit_array_r2
      public :: harness_fortran_cpu__emit_array_r3, harness_fortran_cpu__emit_array_r4
      public :: harness_fortran_cpu__box, harness_fortran_cpu__write_snapshot
      public :: harness_fortran_cpu__write_metrics_basis
      public :: harness_fortran_cpu__write_diagnostics, harness_fortran_cpu__write_perf
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
      subroutine harness_fortran_cpu__write_metrics_basis(entries, n)
        type(harness_fortran_cpu__h_mb_entry), intent(in) :: entries(:)
        integer, intent(in) :: n
        integer :: u, k, j
        open(newunit=u, file='raw/metrics_basis.json', status='replace')
        write(u, '(A)') '{ "per_test": ['
        do k = 1, n
          if (k > 1) write(u, '(A)') '  ,'
          write(u, '(A)') '  { "test_id": "'//trim(entries(k)%test_id)//'"'
          write(u, '(A)') '  , "case_id": "'//trim(entries(k)%case_id)//'"'
          do j = 1, size(entries(k)%values)
            write(u, '(A)') '  , "'//trim(entries(k)%values(j)%name)//'": '//entries(k)%values(j)%json
          end do
          write(u, '(A)') '  }'
        end do
        write(u, '(A)') '] }'
        close(u)
      end subroutine harness_fortran_cpu__write_metrics_basis
      subroutine harness_fortran_cpu__write_diagnostics(results, n)
        type(harness_fortran_cpu__h_case_result), intent(in) :: results(:)
        integer, intent(in) :: n
        integer :: u, k, c
        open(newunit=u, file='diagnostics.json', status='replace')
        write(u, '(A)') '{ "per_case": {'
        do k = 1, n
          write(u, '(A)') '  "'//trim(results(k)%case_id)//'": {'
          do c = 1, size(results(k)%checks)
            write(u, '(A)') '    "'//trim(results(k)%checks(c)%id)//'": "'// &
              trim(results(k)%checks(c)%status)//'"'
          end do
          write(u, '(A)') '  }'
        end do
        write(u, '(A)') '} }'
        close(u)
      end subroutine harness_fortran_cpu__write_diagnostics
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
        open(newunit=u, file='perf.json', status='replace')
        write(u, '(A)') '{ "case_id": "'//trim(case_id)//'", "target": "'//trim(target)// &
          '", "steps": '//harness_fortran_cpu__emit_int(steps)//' }'
        close(u)
      end subroutine harness_fortran_cpu__write_perf
    end module harness_fortran_cpu_model
    """)

# The boundary checks stub. Its snapshot variables are BOUND module-level storage (named as the
# IR declares them, `public ::`-listed, arrays allocated by `case_setup`) that the rendered
# runner reads directly. Two deliberate properties make it the Z6 fixture (d)
# (`zero_base_architecture.md:253`): `checks_compute` OVERWRITES every bound variable with a
# sentinel (-99) the moment it is first called for a case, so a runner that captured AFTER a
# callback — or re-read storage instead of the serialized copy — would emit -99 somewhere;
# and `case_run` changes `field_ghost` from its `case_setup` value, so the `initial/` and the
# final snapshot of one case are distinguishable.
_CHECKS_STUB = textwrap.dedent("""\
    module dynamics_shallow_water_boundary_2d_periodic_copy_checks
      use, intrinsic :: iso_fortran_env, only: real64
      ! allow(C003)
      implicit none
      private
      integer, parameter :: dp = real64
      real(dp), allocatable :: field_ghost(:, :)
      real(dp), allocatable :: field_interior(:, :)
      real(dp) :: max_abs_deviation = 0.0_dp
      real(dp) :: guard_fired = 0.0_dp
      integer :: ncase_seen = 0
      public :: case_setup, case_run, get_time
      public :: checks_compute, metric_compute
      public :: field_ghost, field_interior, max_abs_deviation, guard_fired
    contains
      subroutine case_setup(case_id, ok)
        character(len=*), intent(in) :: case_id
        logical, intent(out) :: ok
        integer :: i, j
        if (.not. allocated(field_ghost)) allocate(field_ghost(4, 4))
        if (.not. allocated(field_interior)) allocate(field_interior(2, 2))
        do i = 1, 2
          do j = 1, 2
            field_interior(i, j) = real(i * 10 + j, dp)
          end do
        end do
        field_ghost = 0.0_dp
        ! A per-case ordinal, so a metrics-basis row that sourced the WRONG case's
        ! snapshot carries a detectably wrong value.
        ncase_seen = ncase_seen + 1
        max_abs_deviation = real(ncase_seen, dp)
        guard_fired = 0.0_dp
        ok = trim(case_id) /= 'l0_invalid_ny_xfail'
      end subroutine case_setup
      subroutine case_run(case_id, steps, cells_updated, ok)
        character(len=*), intent(in) :: case_id
        integer, intent(out) :: steps, cells_updated
        logical, intent(out) :: ok
        steps = 1
        cells_updated = 4
        field_ghost(2:3, 2:3) = field_interior
        if (trim(case_id) == 'l0_invalid_ny_xfail') then
          guard_fired = 1.0_dp
          ok = .false.
        else
          ok = .true.
        end if
      end subroutine case_run
      subroutine get_time(t)
        real(dp), intent(out) :: t
        t = 0.0_dp
      end subroutine get_time
      subroutine checks_compute(case_id, check_id, status)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: check_id
        character(len=4), intent(out) :: status
        ! Fixture (d): a callback that writes to the bound storage AFTER case_run. The
        ! captured snapshot must not see it.
        field_ghost = -99.0_dp
        field_interior = -99.0_dp
        max_abs_deviation = -99.0_dp
        guard_fired = -99.0_dp
        select case (trim(check_id))
        case ('input_guard')
          status = 'fail'
        case default
          status = 'pass'
        end select
        if (len_trim(case_id) < 0) continue
      end subroutine checks_compute
      subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: name
        real(dp), intent(out) :: val
        logical, intent(out) :: is_na
        character(len=:), allocatable, intent(out) :: reason_na
        logical, intent(out) :: found
        val = 0.0_dp
        is_na = .false.
        reason_na = ''
        found = .false.
        if (len_trim(case_id) < 0 .or. len_trim(name) < 0) continue
      end subroutine metric_compute
    end module dynamics_shallow_water_boundary_2d_periodic_copy_checks
    """)


class RenderShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.txt = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)

    def test_program_and_uses(self) -> None:
        self.assertIn(f"program {BOUNDARY_SID}_runner", self.txt)
        self.assertIn("use harness_fortran_cpu_model, only:", self.txt)
        self.assertIn(f"use {BOUNDARY_SID}_checks, only:", self.txt)
        # only the emitters for the ranks in use are imported (r2 + scalar; not r1/r3/r4)
        self.assertIn("harness_fortran_cpu__emit_array_r2", self.txt)
        self.assertIn("harness_fortran_cpu__emit_real", self.txt)
        self.assertNotIn("emit_array_r1", self.txt)
        self.assertNotIn("emit_array_r3", self.txt)

    def test_calls_checks_abi(self) -> None:
        for name in ("case_setup", "case_run", "get_time"):
            self.assertIn(f"call {name}(", self.txt)

    def test_snapshot_is_read_from_bound_storage_not_a_getter(self) -> None:
        # Z6 fixture (b) (`zero_base_architecture.md:253`): the retired getter path must be
        # UNREACHABLE from the rendered runner — no getter is named in the ABI, imported, or
        # called; the snapshot values come from the checks module's bound storage, imported
        # under the `sb_` alias and passed straight to the certified harness emitter.
        for gone in ("get_scalar", "get_r1", "get_r2", "get_r3", "get_r4",
                     "gfound", "r2buf", "sval"):
            self.assertNotIn(gone, CHECKS_PUBLIC_NAMES)
            self.assertNotIn(gone, self.txt)
        self.assertEqual(
            CHECKS_PUBLIC_NAMES,
            ("case_setup", "case_run", "get_time", "checks_compute", "metric_compute"))
        self.assertIn("    sb_field_ghost => field_ghost, &", self.txt)
        self.assertIn("    sb_max_abs_deviation => max_abs_deviation, &", self.txt)
        self.assertIn(
            "    out(1) = harness_fortran_cpu__box('field_ghost', &\n"
            "      harness_fortran_cpu__emit_array_r2(sb_field_ghost))", self.txt)
        self.assertIn(
            "harness_fortran_cpu__box('max_abs_deviation', &\n"
            "      harness_fortran_cpu__emit_real(sb_max_abs_deviation))", self.txt)
        # an unallocated bound array stops the run (never an unallocated actual to the emitter)
        self.assertIn("    call require_bound(allocated(sb_field_ghost), &\n"
                      "      'field_ghost', cid)", self.txt)
        self.assertNotIn("require_bound(allocated(sb_max_abs_deviation)", self.txt)  # scalar
        # EVERY declared variable is captured for EVERY case — the snapshot is the full state,
        # not the per-case union of `required_raw_variables` (the xfail case requires only
        # `guard_fired`, and still gets all four): one capture body, no per-case select.
        self.assertIn("    allocate(out(4))", self.txt)
        self.assertNotIn("select case (cid)", self.txt)
        self.assertEqual(self.txt.count("harness_fortran_cpu__box('guard_fired', &"), 1)

    def test_a_schema_variable_no_test_requires_is_still_captured(self) -> None:
        # The set the runner reads is the SCHEMA, not the union of `required_raw_variables`:
        # a declared state no test names is imported, its emitter imported, and captured for
        # every case (the round-3 census's only corpus-dependent decision on the renderer).
        ir = copy.deepcopy(_boundary_ir())
        schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
        schema["variables"].append({"name": "orphan_r1", "shape_expr": "[7]"})
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("    sb_orphan_r1 => orphan_r1", txt)
        self.assertIn("    harness_fortran_cpu__emit_array_r1, &", txt)
        self.assertIn("    allocate(out(5))", txt)
        self.assertIn("harness_fortran_cpu__emit_array_r1(sb_orphan_r1))", txt)

    def test_capture_points_bracket_case_run_and_precede_every_callback(self) -> None:
        # Z6 capture contract: initial capture right after case_setup, final capture right
        # after case_run, both BEFORE the first checks_compute / metric_compute of the case;
        # `initial/` snapshots go under the sub-directory, and the metrics basis is picked
        # from the final capture's cache (never re-read from storage).
        body = self.txt[self.txt.index("  do ci = 1, ncases"):
                        self.txt.index("  call system_clock(count=clock1)")]
        order = [
            "call case_setup(trim(case_ids(ci)), setup_ok)",
            "call capture_state(trim(case_ids(ci)), vals)",
            "call get_time(tval)",  # generated code: only AFTER the capture
            "call harness_fortran_cpu__write_snapshot('initial/'//trim(case_ids(ci)), vals, tval)",
            "call case_run(trim(case_ids(ci)), steps_c, cells_c, run_ok)",
            "call capture_state(trim(case_ids(ci)), vals)",
            "call get_time(tval)",
            "call harness_fortran_cpu__write_snapshot(trim(case_ids(ci)), vals, tval)",
            "snap_cache(ci)%values = vals",
            "call checks_compute(trim(case_ids(ci)), &",
        ]
        pos = -1
        for needle in order:
            nxt = body.index(needle, pos + 1)
            self.assertGreater(nxt, pos, needle)
            pos = nxt
        self.assertEqual(body.count("call capture_state("), 2)
        self.assertEqual(body.count("write_snapshot("), 2)
        # no generated procedure runs between case_setup / case_run returning and the capture
        setup_end = body.index("call case_setup(")
        self.assertLess(body.index("call capture_state(", setup_end),
                        body.index("call get_time(", setup_end))
        run_end = body.index("call case_run(")
        self.assertLess(body.index("call capture_state(", run_end),
                        body.index("call get_time(", run_end))

    def test_per_id_checks_abi(self) -> None:
        # Per-id ABI: the runner sizes case_checks to the declared count and calls checks_compute
        # once per IR-declared id, supplying the id as a literal `intent(in)` actual (id on the
        # continuation line). The module authors only the status.
        self.assertIn("    allocate(case_checks(3))", self.txt)
        self.assertIn(
            "    call checks_compute(trim(case_ids(ci)), &\n      'x_wrap', cstatus)", self.txt)
        self.assertIn("    case_checks(1)%id = 'x_wrap'", self.txt)
        self.assertIn("    case_checks(1)%status = cstatus", self.txt)
        self.assertIn("      'input_guard', cstatus)", self.txt)
        self.assertIn("    case_checks(3)%id = 'input_guard'", self.txt)
        self.assertIn(f"character(len={CHECK_STATUS_WIDTH}) :: cstatus", self.txt)
        # the old buffered ABI (module-authored ids) is gone in every form
        for gone in ("ncheck_out", "ncheck_max", "chk_ids", "chk_status"):
            self.assertNotIn(gone, self.txt)

    def test_xfail_flag(self) -> None:
        self.assertIn(
            "results(ci)%expected_xfail = trim(case_ids(ci)) == 'l0_invalid_ny_xfail'",
            self.txt)

    def test_no_metrics_block_when_absent(self) -> None:
        # No import and no call; the ABI comment under `use <spec_id>_checks` names it on
        # every node (issue #261), so the assertion is on the CODE lines, not the whole text.
        code = "\n".join(ln for ln in self.txt.splitlines() if not ln.lstrip().startswith("!"))
        self.assertNotIn("metric_compute", code)
        self.assertIn("metric_compute's", self.txt)
        self.assertIn("allocate(results(ci)%metrics(0))", self.txt)

    def test_terminal_writers(self) -> None:
        self.assertIn("call harness_fortran_cpu__write_metrics_basis(mb_entries, 3)", self.txt)
        self.assertIn("call harness_fortran_cpu__write_diagnostics(results, ncases)", self.txt)
        self.assertIn("harness_fortran_cpu__write_perf(", self.txt)
        # perf parallelism: mpi=1, threads from IR (1), gpu=0
        self.assertIn("steps_total, cells_total, walltime, 1, 1, 0)", self.txt)

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


class MultiTargetMetricsBasisTest(unittest.TestCase):
    """R3-core: the metrics-basis index is the (test_id, target case_id) product."""

    @staticmethod
    def _multi_target_ir() -> dict:
        # The x_wrap test now ranges over BOTH wrap cases (a convergence-sweep shape:
        # one test, several cases). The other two tests stay single-target.
        ir = copy.deepcopy(_boundary_ir())
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [
            "l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass"]
        return ir

    def setUp(self) -> None:
        self.txt = render_runner(self._multi_target_ir(), BOUNDARY_SID, HARNESS)

    def test_entry_count_is_the_product(self) -> None:
        # 2 (multi-target test) + 1 + 1 = 4 rows, NOT 3 (one per test).
        self.assertIn("allocate(mb_entries(4))", self.txt)
        self.assertIn("call harness_fortran_cpu__write_metrics_basis(mb_entries, 4)", self.txt)

    def test_each_row_carries_its_own_case_id(self) -> None:
        rows = re.findall(
            r"mb_entries\((\d+)\)%test_id = '([^']+)'\n"
            r"  mb_entries\(\d+\)%case_id = &\n    '([^']+)'",
            self.txt)
        self.assertEqual(
            [(t, c) for _, t, c in rows],
            [("l0_periodic_x_wrap_pass", "l0_periodic_x_wrap_pass"),
             ("l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass"),
             ("l0_periodic_y_wrap_pass", "l0_periodic_y_wrap_pass"),
             ("l0_invalid_ny_xfail", "l0_invalid_ny_xfail")])
        self.assertEqual([int(i) for i, _, _ in rows], [1, 2, 3, 4])

    def test_each_row_picks_from_its_own_case_snapshot(self) -> None:
        # The two rows of the multi-target test resolve DIFFERENT source cases, so each
        # `pick` reads the snap_cache slot that case's `find_case_index` returned.
        self.assertIn("    'l0_periodic_x_wrap_pass')\n", self.txt)
        self.assertIn("    'l0_periodic_y_wrap_pass')\n", self.txt)
        self.assertEqual(self.txt.count("pick(snap_cache(tci)%values, 'max_abs_deviation')"), 3)

    def test_snapshot_cache_is_keyed_by_case_id(self) -> None:
        self.assertIn("snap_cache(ci)%case_id = trim(case_ids(ci))", self.txt)
        self.assertNotIn("snap_cache(ci)%test_id", self.txt)

    def test_single_target_rows_still_carry_case_id(self) -> None:
        # No special case: a 1:1 test is just a 1-row slice of the same product.
        txt = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        self.assertIn("allocate(mb_entries(3))", txt)
        self.assertEqual(txt.count("%case_id = &"), 3)

    def test_line_width(self) -> None:
        over = [ln for ln in self.txt.splitlines() if len(ln) > 100]
        self.assertEqual(over, [], f"lines over 100 cols: {over}")


class DeterminismTest(unittest.TestCase):
    def test_byte_identical(self) -> None:
        a = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        b = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        self.assertEqual(a, b)


class MetricsRenderTest(unittest.TestCase):
    def test_metric_compute_rendered(self) -> None:
        txt = render_runner(_metrics_ir(), "prob_x", HARNESS)
        self.assertIn("call metric_compute(trim(case_ids(ci)), 'error.l2',", txt)
        self.assertIn("call metric_compute(trim(case_ids(ci)), 'error.linf',", txt)
        self.assertIn("allocate(case_metrics(2))", txt)
        self.assertIn("results(ci)%metrics = case_metrics(1:mcount)", txt)
        # threads flow through to perf (num_threads=4)
        self.assertIn("walltime, 1, 4, 0)", txt)
        # rank-1 snapshot var -> bound `sb_u` + emit_array_r1
        self.assertIn("    sb_u => u", txt)
        self.assertIn("harness_fortran_cpu__emit_array_r1(sb_u))", txt)
        self.assertNotIn("emit_array_r2", txt)


class RenderErrorMatrixTest(unittest.TestCase):
    def _expect(self, mutate) -> None:
        ir = copy.deepcopy(_boundary_ir())
        mutate(ir)
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS)

    def test_rank_over_4(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][0].__setitem__("shape_expr", "[2,2,2,2,2]"))

    def test_bad_shape_expr(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][2].__setitem__("shape_expr", "banana"))

    def test_reserved_key_collision(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][2].__setitem__("name", "t"))

    def test_verdict_fields_unsupported(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["diagnostics_contract"]["verdict"]
                     .__setitem__("fields", ["overall", "failed_checks", "score"]))

    def test_two_infra_deps(self) -> None:
        self._expect(lambda ir: ir["dependency"]["direct_deps"].append(
            {"node_key": "infrastructure/other@1.0.0"}))

    def test_over_long_spec_id(self) -> None:
        with self.assertRaises(RenderError):
            render_runner(_boundary_ir(), "z" * 56, HARNESS)

    def test_required_raw_not_in_schema(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["test_evidence_requirements"][0]
                     ["required_raw_variables"].append("ghost_field_typo"))

    def _rename_snapshot_var(self, ir: dict, old: str, new: str) -> None:
        schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
        for v in schema["variables"]:
            if v["name"] == old:
                v["name"] = new
        for r in ir["io_contract"]["test_evidence_requirements"]:
            r["required_raw_variables"] = [
                new if v == old else v for v in r["required_raw_variables"]]

    def test_snapshot_variable_must_be_a_bindable_identifier(self) -> None:
        # A snapshot variable IS a module-level variable of the checks module (the binding
        # convention), so a name Fortran cannot declare is refused at render / compile.static
        # rather than left to fail the syntax gate on a file no leaf can edit.
        for bad in ("field-ghost", "1field", "field ghost", "field.ghost"):
            with self.subTest(bad=bad):
                self._expect(lambda ir, b=bad: self._rename_snapshot_var(ir, "field_ghost", b))

    def test_snapshot_variable_alias_bounded_by_identifier_limit(self) -> None:
        # 61 chars is a legal identifier, but `sb_` + 61 = 64 > 63: refused by the alias
        # bound, with its own message (the 100-column backstop would also catch it, one
        # step later and with a message about line width rather than the identifier).
        ir = copy.deepcopy(_boundary_ir())
        self._rename_snapshot_var(ir, "field_ghost", "f" * 61)
        with self.assertRaises(RenderError) as cm:
            render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("exceeds the 63-char identifier limit", str(cm.exception))
        # A long name that fits every rendered line takes the wrapped rename form.
        ir = copy.deepcopy(_boundary_ir())
        self._rename_snapshot_var(ir, "field_ghost", "f" * 50)
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("    sb_" + "f" * 50 + " => &\n      " + "f" * 50 + ", &", txt)

    def test_snapshot_variables_that_fold_to_one_identifier_are_refused(self) -> None:
        # `guard_fired` and `Guard_Fired` are ONE variable to Fortran; two bindings of one
        # storage would be an ambiguous mapping, and a schema that lists both is unbindable.
        self._expect(lambda ir: self._rename_snapshot_var(ir, "field_ghost", "Guard_Fired"))

    def test_snapshot_variable_named_like_an_abi_procedure_is_refused(self) -> None:
        # A module cannot hold a variable and a procedure of one name.
        for name in CHECKS_PUBLIC_NAMES + ("Case_Run",):
            with self.subTest(name=name):
                self._expect(lambda ir, n=name: self._rename_snapshot_var(ir, "field_ghost", n))

    def test_over_long_case_id_fails_closed(self) -> None:
        # 70 chars: under the 100-column render guard (a `case ('<id>')` label only reaches
        # column 100 at ~87 chars), but over the harness `case_id_len = 64`. Without this gate
        # the runner COMPILES and then `error stop 1`s on every run, because `__parse_cases`
        # truncated the stored id to 64 while the emitted literal carries all 70.
        def mut(ir: dict) -> None:
            long_id = "c_" + "x" * 68
            ir["case"]["test_case_set"][0]["case_id"] = long_id
            ir["io_contract"]["test_predicates"][0]["target_cases"] = [long_id]
        self._expect(mut)

    def test_case_id_at_the_harness_width_still_renders(self) -> None:
        # Exactly `case_id_len` fits: the bound is inclusive, not off-by-one.
        ir = copy.deepcopy(_boundary_ir())
        exact = "c_" + "x" * (CASE_ID_LEN - 2)
        self.assertEqual(len(exact), CASE_ID_LEN)
        ir["case"]["test_case_set"][0]["case_id"] = exact
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [exact]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        # the id reaches the metrics-basis lookup literal (no per-case `select case` exists
        # since every case captures the full state)
        self.assertIn(f"    '{exact}')", txt)
        self.assertIn(f"  integer, parameter :: case_id_len = {CASE_ID_LEN}", txt)

    def test_duplicate_case_id_fails_closed(self) -> None:
        # Two entries with the same case_id would emit overlapping `case ('id')` labels in
        # the runner's select-case — a hard gfortran error the leaf cannot repair. Fail closed.
        self._expect(lambda ir: ir["case"]["test_case_set"].append(
            {"case_id": "l0_periodic_x_wrap_pass"}))

    def test_test_with_no_target_case_fails_closed(self) -> None:
        # A test declaring metrics-basis evidence but ranging over no case has no
        # (test_id, case_id) row to record — its evidence source is unresolvable.
        self._expect(lambda ir: ir["io_contract"]["test_predicates"][0].__setitem__(
            "target_cases", []))

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
                self._expect(lambda ir, n=name: (
                    ir["case"]["test_case_set"][0].__setitem__("case_id", n),
                    ir["io_contract"]["test_predicates"][0].__setitem__("target_cases", [n])))

    def test_leading_dash_case_id_fails_closed(self) -> None:
        # A case id also reaches the runner's argv (`--cases <spec> <case_id>...`), where
        # a leading `-` reads as an option. Rejected at Compile so the run does not get
        # several phases further and fail at the MCP argument rule instead.
        for name in ("-c1", "--cases", "-"):
            with self.subTest(name=name):
                self._expect(lambda ir, n=name: (
                    ir["case"]["test_case_set"][0].__setitem__("case_id", n),
                    ir["io_contract"]["test_predicates"][0].__setitem__("target_cases", [n])))

    def test_dotted_and_dashed_case_id_still_renders(self) -> None:
        # The safe grammar allows `.` and `-`; a single dot is not `..` and must pass.
        ir = copy.deepcopy(_boundary_ir())
        ok = "l0_v1.2-alpha"  # a dash INSIDE the id stays legal; only a leading one does not
        ir["case"]["test_case_set"][0]["case_id"] = ok
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [ok]
        self.assertIn(f"    '{ok}')", render_runner(ir, BOUNDARY_SID, HARNESS))

    def test_non_ascii_in_name_fails_closed(self) -> None:
        # Fortran's default character kind counts BYTES; `CASE_ID_LEN` and the 100-column
        # guard count Python code points. A 64-code-point / 68-byte case_id would slip past
        # the bound and be truncated into the harness's `character(len=64)` slot, giving a
        # runner that compiles and then `error stop`s. Reject non-ASCII outright.
        for name in ("l0_café_wrap", "c_" + "x" * 58 + "é" * 4):
            with self.subTest(name=name[:20]):
                self._expect(lambda ir, n=name: (
                    ir["case"]["test_case_set"][0].__setitem__("case_id", n),
                    ir["io_contract"]["test_predicates"][0].__setitem__("target_cases", [n])))

    def test_non_ascii_metric_address_fails_closed(self) -> None:
        # Every IR-sourced name the renderer embeds goes through `_flit`, so one check covers
        # metric addresses and snapshot variables too, not just case ids.
        ir = copy.deepcopy(_metrics_ir())
        ir["io_contract"]["diagnostics_contract"]["metrics"] = ["error.ℓ2"]
        with self.assertRaisesRegex(RenderError, "outside printable ASCII"):
            render_runner(ir, "prob_x", HARNESS)

    def test_extreme_name_length_fails_closed(self) -> None:
        # A pathologically long name pushes a rendered line past the 100-col lint limit; since
        # the runner is host-rendered (unrepairable by a leaf), fail closed at render time.
        # The name must be a test_id, not a case_id: a long case_id now trips the earlier
        # `CASE_ID_LEN` bound instead, so it would never reach the column guard.
        with self.assertRaisesRegex(RenderError, "reaches the 100-column lint limit"):
            ir = copy.deepcopy(_boundary_ir())
            _long_name_mut(ir)
            render_runner(ir, BOUNDARY_SID, HARNESS)


def _over_long_case_id_mut(ir: dict) -> None:
    long_id = "c_" + "x" * 68  # > CASE_ID_LEN, < the 100-column render guard
    ir["case"]["test_case_set"][0]["case_id"] = long_id
    ir["io_contract"]["test_predicates"][0]["target_cases"] = [long_id]


def _long_name_mut(ir: dict) -> None:
    # A long TEST_ID (the case_id stays short, so the CASE_ID_LEN bound does not pre-empt this):
    # `  mb_entries(1)%test_id = '<95 chars>'` renders past the 100-column lint limit.
    long_id = "t_" + "x" * 95
    ir["io_contract"]["test_evidence_requirements"][0]["test_id"] = long_id
    ir["io_contract"]["test_predicates"][0]["test_id"] = long_id


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
    ("verdict_fields_unsupported", lambda ir: ir["io_contract"]["diagnostics_contract"]
     ["verdict"].__setitem__("fields", ["overall", "failed_checks", "score"]),
     BOUNDARY_SID, False),
    ("required_raw_not_in_schema", lambda ir: ir["io_contract"]["test_evidence_requirements"]
     [0]["required_raw_variables"].append("ghost_field_typo"), BOUNDARY_SID, False),
    ("duplicate_case_id", lambda ir: ir["case"]["test_case_set"].append(
        {"case_id": "l0_periodic_x_wrap_pass"}), BOUNDARY_SID, False),
    ("test_with_no_target_case", lambda ir: ir["io_contract"]["test_predicates"][0].__setitem__(
        "target_cases", []), BOUNDARY_SID, False),
    ("over_long_case_id", _over_long_case_id_mut, BOUNDARY_SID, False),
    ("path_traversal_case_id", lambda ir: (
        ir["case"]["test_case_set"][0].__setitem__("case_id", "../../evil"),
        ir["io_contract"]["test_predicates"][0].__setitem__("target_cases", ["../../evil"])),
     BOUNDARY_SID, False),
    ("non_ascii_case_id", lambda ir: (
        ir["case"]["test_case_set"][0].__setitem__("case_id", "l0_café"),
        ir["io_contract"]["test_predicates"][0].__setitem__("target_cases", ["l0_café"])),
     BOUNDARY_SID, False),
    ("non_t_time_variable", lambda ir: ir["io_contract"]["raw_requirements"]
     ["required_evidence"][0]["schema"].__setitem__("time_variable", "tau"), BOUNDARY_SID, False),
    ("no_checks", lambda ir: ir["io_contract"]["diagnostics_contract"].__setitem__(
        "checks", []), BOUNDARY_SID, False),
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
    cannot silently reopen (e.g. a plausible ~50-char metric address or a control char in an IR
    name — R1-F1 — now surfaces at compile instead of killing the workflow at render)."""

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
                    render_runner(ir, spec_id, HARNESS)
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
        # A truthy non-iterable where a list is expected (e.g. `verdict.fields: 5`) makes
        # render_runner raise a bare TypeError, not a RenderError. Running inside the compile
        # validator, that must NOT escape (it would abort the gate and discard sibling
        # violations); it is converted to a renderable-failure violation instead.
        for field, value in (
            ("fields", 5),  # under diagnostics_contract.verdict
        ):
            ir = copy.deepcopy(_boundary_ir())
            ir["io_contract"]["diagnostics_contract"]["verdict"] = {
                "required": False, field: value}
            v = ir_content_violations(ir, BOUNDARY_SID, HARNESS)
            self.assertTrue(v and any("not renderable" in x for x in v), v)
        # a few more non-iterable field shapes, each must be caught (not raised)
        for path in (
            lambda ir: ir["io_contract"]["diagnostics_contract"].__setitem__("checks", 5),
            lambda ir: ir["io_contract"].__setitem__("test_predicates", 5),
            lambda ir: ir["case"].__setitem__("test_case_set", 5),
        ):
            ir = copy.deepcopy(_boundary_ir())
            path(ir)
            v = ir_content_violations(ir, BOUNDARY_SID, HARNESS)
            self.assertTrue(v, v)  # non-empty, and no exception escaped


class SpecIdBoundAgreementTest(unittest.TestCase):
    """The neutral spec-input bound and this backend's render-time bound are one number.

    `tools/spec_input_gates.MAX_SPEC_ID_LEN` cannot ask a backend for it — the gate runs on a
    `spec_ref` before any IR exists, so no language has been resolved — so the number is spelled
    on both sides. That is the pattern the ledger already accepts for a pre-IR bound, and it is
    only safe with this comparison: without it, raising the identifier limit in the backend would
    silently leave the spec-input gate rejecting spec_ids the renderer would now accept, and
    lowering it would let an unrenderable spec_id through to a render-time workflow-kill."""

    def test_the_backend_bound_derives_from_the_language_identifier_limit(self) -> None:
        from tools.backends.language.fortran import bundle
        from tools.backends.language.fortran.runner import MAX_SPEC_ID_LEN as BACKEND_BOUND
        # The longest generated name appends a 7-character role suffix; the extra character is
        # the declared margin. Restating `55` here would be a third copy of the same number.
        self.assertEqual(bundle.IDENTIFIER_MAX - len("_runner") - 1, BACKEND_BOUND)
        for suffix in ("_runner", "_checks", "_model"):
            self.assertLessEqual(BACKEND_BOUND + len(suffix), bundle.IDENTIFIER_MAX)

    def test_the_bound_tracks_the_language_identifier_limit(self) -> None:
        """The DERIVATION, observed by moving the thing it derives from.

        Replacing the derivation with today's literal is behaviourally identical at the current
        limit, so every value comparison in this class survives it. What the derivation buys is
        the future change: raise the language's identifier limit and the bound must move, or a
        spec_id one character too long renders symbols that breach it.

        The first two instruments for this read the SOURCE and pinned a spelling. Both
        over-rejected — the first refused `from ...bundle import IDENTIFIER_MAX`, the second
        refused the annotated assignment and then, once widened, accepted a locally restated
        `IDENTIFIER_MAX = 63` (the extra copy of that number the ledger forbids). A rule that has
        to be rewritten twice and still rejects legitimate work is the wrong FORM, not the wrong
        wording. So: move the limit and re-import. Every spelling that derives passes, every
        spelling that restates fails, and no spelling is named.
        """
        import subprocess
        import sys

        from tools.backends.language.fortran import bundle

        moved = bundle.IDENTIFIER_MAX - 23
        # In a SUBPROCESS, not with an in-process reload: reloading the emitter rebinds the
        # module every other test in this file holds a reference to, and getting the restore
        # even slightly wrong leaves the rest of the suite measuring a bound that is not the
        # real one. Measured — the first attempt did exactly that and failed 12 tests.
        # The emitter is already in `sys.modules` by the time the limit can be patched — the
        # package `__init__` re-exports it, and reaching `bundle` imports the package. Dropping
        # it first is what makes the re-import read the patched value; without that the probe
        # reports the unpatched bound and the test passes for no reason.
        probe = (
            "import importlib, sys\n"
            "from unittest import mock\n"
            "from tools.backends.language.fortran import bundle\n"
            "del sys.modules['tools.backends.language.fortran.runner']\n"
            f"with mock.patch.object(bundle, 'IDENTIFIER_MAX', {moved}):\n"
            "    runner = importlib.import_module("
            "'tools.backends.language.fortran.runner')\n"
            "    print(runner.MAX_SPEC_ID_LEN)\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(
            moved - len("_runner") - 1, int(out.stdout.strip()),
            "the backend's spec_id bound did not move with the language identifier limit: it is "
            "restated rather than derived, and a literal does not move")

    def test_the_spec_input_gate_carries_the_same_bound(self) -> None:
        from tools.backends.language.fortran.runner import MAX_SPEC_ID_LEN as BACKEND_BOUND
        from tools.spec_input_gates import MAX_SPEC_ID_LEN as NEUTRAL_BOUND
        self.assertEqual(BACKEND_BOUND, NEUTRAL_BOUND)


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
        long_harness = "h" * (bundle.IDENTIFIER_MAX - len("__write_metrics_basis") + 1)
        with self.assertRaises(RenderError) as ctx:
            render_runner(ir, BOUNDARY_SID, long_harness)
        # `identity=True`: a harness id is node identity, so the compile.static mirror excludes
        # it rather than routing it to a re-author that cannot change it.
        self.assertTrue(ctx.exception.identity)
        self.assertIn(str(bundle.IDENTIFIER_MAX), str(ctx.exception))


class LineWidthTest(unittest.TestCase):
    """R1/M3c-β (review round 3): every rendered line must stay within the 100-col lint limit,
    including for long IR-sourced names (metric addresses, case_ids) — the hot lines are wrapped."""

    def _maxw(self, txt: str) -> int:
        return max(len(ln) for ln in txt.splitlines())

    def test_long_metric_address_wraps(self) -> None:
        ir = _metrics_ir()
        ir["io_contract"]["diagnostics_contract"]["metrics"] = ["convergence.observed_order.l2"]
        txt = render_runner(ir, "prob_x", HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)
        # the metric_compute call is wrapped (address on the header, out-args on the next line)
        self.assertIn("'convergence.observed_order.l2', &", txt)

    def test_two_xfail_cases_multiline_expr_not_false_failed(self) -> None:
        # The `_xfail_expr` for >=2 xfail cases is a multi-line (`&`-continued) entry; the
        # column guard must measure per physical line, not the joined entry, else it wedges a
        # valid two-guard-case node into an unrepairable fail_closed.
        ir = _boundary_ir()
        ir["io_contract"]["test_predicates"][1]["expected_outcome"] = "xfail"
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)
        self.assertIn(".or. &", txt)  # the two-term expression rendered as a continuation
        self.assertIn("== 'l0_periodic_y_wrap_pass'", txt)

    def test_long_case_id_target_wraps(self) -> None:
        ir = _boundary_ir()
        long_id = "l0_periodic_x_wrap_with_a_fairly_long_descriptive_name_pass"  # 58 chars
        ir["case"]["test_case_set"][0]["case_id"] = long_id
        ir["io_contract"]["test_evidence_requirements"][0]["test_id"] = long_id
        ir["io_contract"]["test_predicates"][0]["test_id"] = long_id
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [long_id]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)

    def test_max_length_check_id_wraps_within_limit(self) -> None:
        # A 64-char check id (the `_checks()` fail-closed ceiling) still renders within 100 cols:
        # the id rides the continuation line, so `      '<64>', cstatus)` is 82 columns.
        ir = _boundary_ir()
        long_check = "c" * CASE_ID_LEN  # 64
        ir["io_contract"]["diagnostics_contract"]["checks"] = [{"id": long_check}]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)
        self.assertIn(f"      '{long_check}', cstatus)", txt)

    def test_overlong_check_id_is_render_error(self) -> None:
        # With the old buffered ABI's de-facto 32-char id width gone, `_checks()` fail-closes on an
        # id longer than CASE_ID_LEN rather than emit a per-id call that breaches the column guard.
        ir = _boundary_ir()
        ir["io_contract"]["diagnostics_contract"]["checks"] = [{"id": "c" * (CASE_ID_LEN + 1)}]
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS)

    def test_apostrophe_check_id_escaping_to_the_lint_bound_is_render_error(self) -> None:
        # A raw-<=64 id whose Fortran apostrophe-doubling expands the `case_checks(k)%id = '<lit>'`
        # assignment to EXACTLY 100 cols passes render_runner's `>100` backstop but fails the S001
        # lint (=100) on a host-authored line the leaf cannot repair. `_checks()` must fail-close on
        # the ESCAPED width, not just the raw length. (54 'a' + 10 "'" == 64 raw -> 74 escaped ->
        # 25 + 1 + 74 == 100.)
        ir = _boundary_ir()
        ir["io_contract"]["diagnostics_contract"]["checks"] = [{"id": "a" * 54 + "'" * 10}]
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS)
        # One fewer apostrophe (98 cols) renders cleanly and stays within the limit.
        ir["io_contract"]["diagnostics_contract"]["checks"] = [{"id": "a" * 54 + "'" * 9}]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertLessEqual(self._maxw(txt), 99)

    def test_a_snapshot_name_rendering_an_exactly_100_column_line_is_refused(self) -> None:
        # The variable-name path had no `_checks`-style strict bound: a scalar name whose
        # `out(k) = harness_fortran_cpu__box('<name>', &` line is EXACTLY 100 columns slipped a
        # `> 100` backstop and failed S001 on a host-authored line (a round-1 reviewer measured
        # it on fortitude 0.8.0). The backstop is `>=` now, so no rendered line reaches 100.
        ir = copy.deepcopy(_boundary_ir())
        schema = ir["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]
        name = "m" * (100 - len("    out(3) = harness_fortran_cpu__box('', &"))
        for v in schema["variables"]:
            if v["name"] == "max_abs_deviation":
                v["name"] = name
        for r in ir["io_contract"]["test_evidence_requirements"]:
            r["required_raw_variables"] = [
                name if x == "max_abs_deviation" else x for x in r["required_raw_variables"]]
        with self.assertRaises(RenderError) as cm:
            render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("reaches the 100-column lint limit (100 columns", str(cm.exception))
        # one char shorter renders, and every line is at most 99 wide
        ir2 = copy.deepcopy(ir)
        for v in ir2["io_contract"]["raw_requirements"]["required_evidence"][0]["schema"]["variables"]:
            if v["name"] == name:
                v["name"] = name[:-1]
        for r in ir2["io_contract"]["test_evidence_requirements"]:
            r["required_raw_variables"] = [
                name[:-1] if x == name else x for x in r["required_raw_variables"]]
        self.assertLessEqual(self._maxw(render_runner(ir2, BOUNDARY_SID, HARNESS)), 99)


class FortranLiteralEscapingTest(unittest.TestCase):
    """R1/M3c-β (Codex review): IR-sourced names are only required non-empty, so a name with a
    single quote must be doubled (`''`) in the generated Fortran literal, not break it."""

    def test_apostrophe_in_names_is_escaped(self) -> None:
        # A case_id may not contain `'` (it is filesystem-gated to [A-Za-z0-9._-]), but a
        # test_id flows into a Fortran literal (`mb_entries(k)%test_id = '...'`) without being
        # a path, so an apostrophe there must be doubled, not break the literal.
        ir = _boundary_ir()
        ir["io_contract"]["test_evidence_requirements"][0]["test_id"] = "l0_x'wrap"
        ir["io_contract"]["test_predicates"][0]["test_id"] = "l0_x'wrap"
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("%test_id = 'l0_x''wrap'", txt)
        self.assertNotIn("'l0_x'wrap'", txt)  # no broken (unescaped) literal

    @unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
    def test_escaped_runner_compiles(self) -> None:
        # An apostrophe in a test_id must produce a valid (doubled-quote) Fortran literal that
        # compiles + links against the harness/checks stubs, not a broken one. (case_ids are
        # token-gated, so the apostrophe lives on a non-path name that still flows through `_flit`.)
        ir = _boundary_ir()
        for r in ir["io_contract"]["test_evidence_requirements"]:
            r["test_id"] = r["test_id"].replace("l0_", "l0'")
        for p in ir["io_contract"]["test_predicates"]:
            p["test_id"] = p["test_id"].replace("l0_", "l0'")
        runner = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("''", runner)  # the apostrophe was doubled
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(_CHECKS_STUB)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)
            for srcs in (["harness_fortran_cpu_model.f90"], [f"{BOUNDARY_SID}_checks.f90"],
                         [f"{BOUNDARY_SID}_runner.f90"]):
                r = subprocess.run(["gfortran", "-std=f2008", "-c", *srcs],
                                   cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(_CHECKS_STUB)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)
            for srcs in (["harness_fortran_cpu_model.f90"], [f"{BOUNDARY_SID}_checks.f90"],
                         [f"{BOUNDARY_SID}_runner.f90"]):
                r = subprocess.run(["gfortran", "-std=f2008", "-c", *srcs],
                                   cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


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
        self.assertIn("is not the pinned", str(cm.exception))
        self.assertIn("the renderer only targets that harness", str(cm.exception))

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
        txt = render_runner(self.ir, BOUNDARY_SID, HARNESS)
        self.assertIn(f"  integer, parameter :: case_id_len = {CASE_ID_LEN}", txt)
        self.assertIn(f"integer, parameter :: case_id_len = {CASE_ID_LEN}",
                      _HARNESS_V3_PARAMETERS)
        over = "c" * (CASE_ID_LEN + 1)
        ir = copy.deepcopy(self.ir)
        ir["case"]["test_case_set"][0]["case_id"] = over
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [over]
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS)

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
        # reordering two components of h_case_result must be caught as drift.
        bad = copy.deepcopy(self.sigs)
        for e in bad:
            if e["symbol"].endswith("__h_case_result"):
                components = e["signature"]["components"]
                cid = next(i for i, ent in enumerate(components) if ent["name"] == "case_id")
                xf = next(
                    i for i, ent in enumerate(components) if ent["name"] == "expected_xfail")
                components[cid], components[xf] = components[xf], components[cid]
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
            "    character(len=:), allocatable :: case_id\n"
            "    logical :: expected_xfail\n",
            "    logical :: expected_xfail\n"
            "    character(len=:), allocatable :: case_id\n")
        self.assertNotEqual(reordered, self.src, "the reorder did not apply to the stub")
        with self.assertRaises(RenderError) as cm:
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, reordered)
        self.assertIn("model source signature", str(cm.exception))


class DeclaredLintRuleHoldTest(unittest.TestCase):
    """Hold the RENDERED runner to the rule set the `Generate.gate` lint check imposes.

    A CLASS OF ITS OWN, because these rows render and lint and never compile: inside
    `GfortranSmokeTest` they inherited its `skipUnless(_HAVE_GFORTRAN)` and could not run
    on a host with a linter and no compiler — the two skip reasons they declare for
    themselves could never fire, because the class guard won first. A review round found
    it, and the consequence was in the commit message: "a renderer edit that introduces a
    finding fails in an unbilled unit test rather than at a node's gate" held only where
    gfortran happened to be installed.
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
        reason = _linter_skip_reason()
        if reason == _LINTER_ABSENT_SKIP:
            self.skipTest("the declared lint invocation's linter is not installed")
        if reason == _LINTER_UNMEASURED_SKIP:
            self.skipTest("the installed linter is outside the measured version range")
        from tools.backends.linter.fortitude import lint as _lint
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / f"{sid}_runner.f90").write_text(render_runner(ir, sid, HARNESS))
            r = subprocess.run(list(_lint.check_argv(".")), cwd=d,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_rendered_runner_clean_under_the_declared_lint_rules(self) -> None:
        self._assert_runner_clean_under_the_declared_lint_rules(
            _boundary_ir(), BOUNDARY_SID)

    def test_rendered_metrics_runner_clean_under_the_declared_lint_rules(self) -> None:
        self._assert_runner_clean_under_the_declared_lint_rules(
            _rank34_metrics_ir(), RANK_SID)


@unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
class GfortranSmokeTest(unittest.TestCase):
    def test_rendered_runner_compiles_and_runs(self) -> None:
        runner = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(_CHECKS_STUB)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)

            def fc(*srcs: str) -> None:
                r = subprocess.run(
                    ["gfortran", "-std=f2008", "-c", *srcs],
                    cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            fc("harness_fortran_cpu_model.f90")
            fc(f"{BOUNDARY_SID}_checks.f90")
            fc(f"{BOUNDARY_SID}_runner.f90")
            link = subprocess.run(
                ["gfortran", "harness_fortran_cpu_model.o",
                 f"{BOUNDARY_SID}_checks.o", f"{BOUNDARY_SID}_runner.o", "-o", "runner"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(link.returncode, 0, link.stderr)

            (d / "raw" / "state_snapshots" / "initial").mkdir(parents=True)
            run = subprocess.run(
                ["./runner", "--cases", "spec.ir.yaml",
                 "l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass", "l0_invalid_ny_xfail"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            # every case emitted its own snapshot; diagnostics + perf + metrics_basis exist
            for cid in ("l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass",
                        "l0_invalid_ny_xfail"):
                self.assertTrue((d / "raw" / "state_snapshots" / f"{cid}.json").is_file())
                self.assertTrue(
                    (d / "raw" / "state_snapshots" / "initial" / f"{cid}.json").is_file())
            self.assertTrue((d / "diagnostics.json").is_file())
            self.assertTrue((d / "perf.json").is_file())
            self.assertTrue((d / "raw" / "metrics_basis.json").is_file())
            diag = (d / "diagnostics.json").read_text()
            self.assertIn("input_guard", diag)
            # Z6 fixture (d): the checks stub overwrites every bound variable with -99 inside
            # `checks_compute`, which runs AFTER the final capture. Neither snapshot nor the
            # metrics basis may carry the sentinel — the captured value is the serialized copy
            # taken before any callback, not a later re-read of the storage.
            initial_path = d / "raw" / "state_snapshots" / "initial" / "l0_periodic_x_wrap_pass.json"
            final_path = d / "raw" / "state_snapshots" / "l0_periodic_x_wrap_pass.json"
            mb_path = d / "raw" / "metrics_basis.json"
            # On the RAW text: the harness writes `-9.9000000000000000E+001`, which a parsed
            # document re-serializes as `-99.0` (a round-3 reviewer found the assertion on
            # `json.dumps(doc)` vacuous for that reason and the metrics basis unpinned).
            for path in (initial_path, final_path, mb_path):
                self.assertNotIn("-9.9", path.read_text(), path)
            initial = json.loads(initial_path.read_text())
            final = json.loads(final_path.read_text())
            mb = json.loads(mb_path.read_text())
            for row in mb["per_test"]:
                for value in row.values():
                    self.assertNotEqual(value, -99.0, row)
            # initial = the case_setup state (ghost all zero), final = after case_run (the
            # interior copied into the ghost's centre): the two capture points differ.
            self.assertEqual(initial["field_ghost"][1][1], 0.0)
            self.assertEqual(final["field_ghost"][1][1], 11.0)
            self.assertEqual(final["field_interior"], [[11.0, 12.0], [21.0, 22.0]])
            self.assertEqual(final["max_abs_deviation"], 1.0)
            self.assertEqual(initial["max_abs_deviation"], 1.0)

    def test_unallocated_bound_array_stops_the_run(self) -> None:
        # A checks module whose `case_setup` never allocates a bound array is a binding that
        # was never established: the runner must `error stop` with the variable named, not
        # hand an unallocated actual to the emitter.
        runner = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        broken = _CHECKS_STUB
        for line in ("    if (.not. allocated(field_ghost)) allocate(field_ghost(4, 4))\n",
                     "    field_ghost = 0.0_dp\n",
                     "    field_ghost(2:3, 2:3) = field_interior\n",
                     "    field_ghost = -99.0_dp\n"):
            self.assertEqual(broken.count(line), 1, line)
            broken = broken.replace(line, "")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(broken)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)
            for srcs in (["harness_fortran_cpu_model.f90"], [f"{BOUNDARY_SID}_checks.f90"],
                         [f"{BOUNDARY_SID}_runner.f90"]):
                r = subprocess.run(["gfortran", "-std=f2008", "-c", *srcs],
                                   cwd=d, capture_output=True, text=True, check=False)
                self.assertEqual(r.returncode, 0, r.stderr)
            link = subprocess.run(
                ["gfortran", "harness_fortran_cpu_model.o",
                 f"{BOUNDARY_SID}_checks.o", f"{BOUNDARY_SID}_runner.o", "-o", "runner"],
                cwd=d, capture_output=True, text=True, check=False)
            self.assertEqual(link.returncode, 0, link.stderr)
            (d / "raw" / "state_snapshots" / "initial").mkdir(parents=True)
            run = subprocess.run(
                ["./runner", "--cases", "spec.ir.yaml", "l0_periodic_x_wrap_pass"],
                cwd=d, capture_output=True, text=True, check=False)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("bound state field_ghost is not allocated at capture", run.stderr)
            self.assertFalse(
                (d / "raw" / "state_snapshots" / "initial" / "l0_periodic_x_wrap_pass.json")
                .is_file())

    def test_multi_target_runner_compiles_and_emits_one_row_per_case(self) -> None:
        # The multi-target mb_rows path is otherwise only text-asserted. Compile, link and RUN
        # it, then parse the emitted metrics_basis.json: the multi-target test must contribute
        # one entry per target case, each keyed by its own case_id and holding THAT case's
        # evidence (a wrong `tci` would silently copy one case's values into both rows).
        ir = copy.deepcopy(_boundary_ir())
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [
            "l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass"]
        runner = render_runner(ir, BOUNDARY_SID, HARNESS)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(_CHECKS_STUB)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)

            def fc(*srcs: str) -> None:
                r = subprocess.run(
                    ["gfortran", "-std=f2008", "-c", *srcs],
                    cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            fc("harness_fortran_cpu_model.f90")
            fc(f"{BOUNDARY_SID}_checks.f90")
            fc(f"{BOUNDARY_SID}_runner.f90")
            link = subprocess.run(
                ["gfortran", "harness_fortran_cpu_model.o",
                 f"{BOUNDARY_SID}_checks.o", f"{BOUNDARY_SID}_runner.o", "-o", "runner"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(link.returncode, 0, link.stderr)

            (d / "raw" / "state_snapshots" / "initial").mkdir(parents=True)
            run = subprocess.run(
                ["./runner", "--cases", "spec.ir.yaml",
                 "l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass", "l0_invalid_ny_xfail"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

            mb = json.loads((d / "raw" / "metrics_basis.json").read_text())
            rows = [(e["test_id"], e["case_id"]) for e in mb["per_test"]]
            self.assertEqual(rows, [
                ("l0_periodic_x_wrap_pass", "l0_periodic_x_wrap_pass"),
                ("l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass"),
                ("l0_periodic_y_wrap_pass", "l0_periodic_y_wrap_pass"),
                ("l0_invalid_ny_xfail", "l0_invalid_ny_xfail"),
            ])
            # Each row carries ITS OWN case's evidence, not the first case's copied twice.
            # The checks stub sets `max_abs_deviation` to the case ordinal, so the two rows of
            # the multi-target test must differ — a wrong `tci` would make them equal.
            by_row = {r: e for r, e in zip(rows, mb["per_test"])}
            self.assertEqual(
                by_row[("l0_periodic_x_wrap_pass", "l0_periodic_x_wrap_pass")]["max_abs_deviation"],
                1.0)
            self.assertEqual(
                by_row[("l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass")]["max_abs_deviation"],
                2.0)

    def test_metrics_and_high_rank_runner_compiles_and_runs(self) -> None:
        # The boundary smoke only covers the no-metrics, scalar+rank-2 family. This one
        # compiles+links+runs the metrics path AND the rank-3/rank-4 emitter/getter path,
        # so a future edit that makes either produce invalid Fortran cannot ship green.
        runner = render_runner(_rank34_metrics_ir(), RANK_SID, HARNESS)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{RANK_SID}_checks.f90").write_text(_RANK_CHECKS_STUB)
            (d / f"{RANK_SID}_runner.f90").write_text(runner)

            def fc(*srcs: str) -> None:
                r = subprocess.run(
                    ["gfortran", "-std=f2008", "-c", *srcs],
                    cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            fc("harness_fortran_cpu_model.f90")
            fc(f"{RANK_SID}_checks.f90")
            fc(f"{RANK_SID}_runner.f90")
            link = subprocess.run(
                ["gfortran", "harness_fortran_cpu_model.o",
                 f"{RANK_SID}_checks.o", f"{RANK_SID}_runner.o", "-o", "runner"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(link.returncode, 0, link.stderr)

            (d / "raw" / "state_snapshots" / "initial").mkdir(parents=True)
            run = subprocess.run(
                ["./runner", "--cases", "spec.ir.yaml", "c0"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue((d / "raw" / "state_snapshots" / "c0.json").is_file())
            self.assertTrue((d / "raw" / "state_snapshots" / "initial" / "c0.json").is_file())
            self.assertTrue((d / "raw" / "metrics_basis.json").is_file())
            self.assertTrue((d / "diagnostics.json").is_file())
            self.assertTrue((d / "perf.json").is_file())
            # the rank-3/rank-4 snapshot values were emitted as nested JSON arrays
            snap = (d / "raw" / "state_snapshots" / "c0.json").read_text()
            self.assertIn("\"a3\"", snap)
            self.assertIn("\"a4\"", snap)

    def _assert_runner_clean_under_promoted_warnings(
            self, ir: dict, sid: str, checks_stub: str) -> None:
        """The `Generate.gate` syntax check promotes unused-dummy-argument / unused-variable /
        ampersand to errors over the whole staged set. The runner is host-rendered, so such a
        warning is unfixable by the leaf and would spin a futile warm-repair loop — the
        rendered artifact must be clean under all three. The stubs are compiled WITHOUT the
        flags: only the runner, the artifact the renderer owns, is held to them."""
        runner = render_runner(ir, sid, HARNESS)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            mods = d / "mods"
            mods.mkdir()
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{sid}_checks.f90").write_text(checks_stub)
            (d / f"{sid}_runner.f90").write_text(runner)
            pre = subprocess.run(
                ["gfortran", "-fsyntax-only", "-std=f2008", "-J", str(mods),
                 "harness_fortran_cpu_model.f90", f"{sid}_checks.f90"],
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
            _boundary_ir(), BOUNDARY_SID, _CHECKS_STUB)

    def test_rendered_metrics_runner_clean_under_promoted_unused_warnings(self) -> None:
        self._assert_runner_clean_under_promoted_warnings(
            _rank34_metrics_ir(), RANK_SID, _RANK_CHECKS_STUB)


class ChecksAbiDummyDeclarationTest(unittest.TestCase):
    """`checks_abi_dummy_violation` (issue #261): the one `metric_compute` dummy-argument fact
    the compiler cannot check against the rendered call. What is PINNED: the position the
    constant names is the position at which the rendered runner passes its unallocated
    deferred-length actual (read off the render, not restated); the attribute is required on
    the dummy at that position however it is spelled; and the judgment is positive-evidence
    only. What is SAMPLED: the spellings in the two matrices — each row is one spelling the
    language allows, and the set is a regression guard, not the definition of the class."""

    _MODULE = textwrap.dedent("""\
        module bx_checks
          implicit none
          private
          public :: metric_compute
        contains
          subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
            character(len=*), intent(in) :: case_id, name
            real(8), intent(out) :: val
            logical, intent(out) :: is_na, found
            {DECL}
            associate (u => case_id); end associate
            val = 0.0d0; is_na = .false.; found = .false.; reason_na = ''
          end subroutine metric_compute
        end module bx_checks
        """)

    def _v(self, decl: str, **over) -> str | None:
        text = self._MODULE.replace("{DECL}", decl)
        for old, new in over.items():
            self.assertIn(old, text)
            text = text.replace(old, new)
        return checks_abi_dummy_violation(text, "bx")

    def test_the_position_is_the_one_the_rendered_runner_passes_its_unallocated_actual_at(
            self) -> None:
        runner = render_runner(_metrics_ir(), "prob_x", HARNESS)
        stmts = [s.strip().lower() for ln in fortran_lines.fortran_logical_line_texts(runner)
                 for s in fortran_lines.split_fortran_statements(ln)]
        calls = [s for s in stmts if s.startswith("call metric_compute(")]
        self.assertTrue(calls)
        position = METRIC_COMPUTE_DUMMIES.index(METRIC_COMPUTE_DEFERRED_LENGTH_DUMMY)
        actuals = {tuple(a.strip() for a in fortran_lines.split_top_level_commas(
            c[len("call metric_compute("):-1])) for c in calls}
        self.assertEqual(1, len({a[position] for a in actuals}))
        actual = next(iter(actuals))[position]
        self.assertEqual(len(METRIC_COMPUTE_DUMMIES), len(next(iter(actuals))))
        # that actual is declared deferred-length allocatable in the runner ...
        decls = [s for s in stmts if re.match(
            rf"^character\s*\(\s*len\s*=\s*:\s*\)\s*,\s*allocatable\s*::\s*{actual}$", s)]
        self.assertEqual(1, len(decls), actual)
        # ... and nothing in the runner allocates or assigns it before the call: the callee is
        # the first writer, so the callee's declaration decides whether the write is legal.
        first_call = next(i for i, s in enumerate(stmts) if s in calls)
        before = stmts[:first_call]
        self.assertFalse([s for s in before if re.match(rf"^allocate\s*\(.*\b{actual}\b", s)
                          or re.match(rf"^{actual}\s*=", s)], actual)

    def test_the_pinned_declaration_and_its_spellings_are_accepted(self) -> None:
        for decl in (
                "character(len=:), allocatable, intent(out) :: reason_na",
                "CHARACTER(LEN=:), ALLOCATABLE, INTENT(OUT) :: Reason_NA",
                "character(len=:), allocatable, &\n      intent(out) :: reason_na",
                "character(:), allocatable, intent(out) :: reason_na",
                "character(len=:), intent(out) :: reason_na\n    allocatable :: reason_na",
                "character(len=:), intent(out) :: reason_na\n    allocatable reason_na",
                "character(len=:), intent(out) :: reason_na; allocatable :: reason_na",
                "character(len=:), allocatable, intent(out) :: reason_na, extra",
                "character(len=:), allocatable, intent(out) :: extra, reason_na",
                # round 2: a `type(...)` declaration is not a derived-type definition
                ("type :: tt\n      integer :: n\n    end type tt\n    type(tt) :: t1\n"
                 "    character(len=:), allocatable, intent(out) :: reason_na"),
                # a lookalike entity is not the dummy
                ("character(len=64) :: xreason_na\n"
                 "    character(len=:), allocatable, intent(out) :: reason_na"),
                # a literal carrying the dummy's name and a `::` is not a declaration of it
                ("character(len=20) :: note = 'reason_na :: x'\n"
                 "    character(len=:), allocatable, intent(out) :: reason_na"),
        ):
            with self.subTest(decl=decl):
                self.assertIsNone(self._v(decl))

    def test_a_non_allocatable_dummy_is_refused_however_spelled(self) -> None:
        for decl in (
                "character(len=64), intent(out) :: reason_na",  # the billed run's form
                "character(len=*), intent(out) :: reason_na",
                "character(len=64) reason_na",
                "character*64 reason_na",
                "character*(64) reason_na",
                "character(len=64), intent(out) :: reason_na, extra",
                ("character(len=8) :: junk = 'a::b'\n"
                 "    character(len=64), intent(out) :: reason_na"),
        ):
            with self.subTest(decl=decl):
                r = self._v(decl)
                self.assertIsNotNone(r, decl)
                self.assertIn("without the `allocatable` attribute", r)
                self.assertIn("'reason_na' (position 5)", r)
                self.assertIn("character(len=:), allocatable, intent(out) :: reason_na", r)

    def test_the_dummy_is_found_by_position_not_name(self) -> None:
        r = self._v("character(len=64), intent(out) :: rs",
                    **{"reason_na, found)": "rs, found)", "reason_na = ''": "rs = ''"})
        self.assertIsNotNone(r)
        self.assertIn("'rs' (position 5)", r)

    def test_a_missing_declaration_and_a_short_dummy_list_fail_closed(self) -> None:
        r = self._v("")
        self.assertIsNotNone(r)
        self.assertIn("has no type declaration statement", r)
        for dummies, n in (("(a, b)", 2), ("(a, b, c, d)", 4)):  # 4 is the boundary (`<=`)
            r = self._v("", **{"(case_id, name, val, is_na, reason_na, found)": dummies})
            self.assertIsNotNone(r)
            self.assertIn(f"declares {n} dummy argument(s)", r)
            self.assertIn("metric_compute(case_id, name, val, is_na, reason_na, found)", r)

    def test_the_specification_part_is_read_past_nested_blocks(self) -> None:
        """Round-1 review, both directions. A derived type, an `interface` block or an `enum`
        inside `metric_compute` neither ends the reading (its `end type` / `end interface` /
        `end enum` is not the procedure's end — the pinned declaration AFTER it was reported
        missing) nor supplies the declaration (a component / prototype dummy / enumerator named
        like the dummy is not the dummy)."""
        pinned = "character(len=:), allocatable, intent(out) :: reason_na"
        bad = "character(len=64), intent(out) :: reason_na"
        blocks = (
            "type :: t\n      character(len=:), allocatable :: reason_na\n    end type t",
            ("interface\n      subroutine f(reason_na)\n"
             "        character(len=:), allocatable :: reason_na\n      end subroutine f\n"
             "    end interface"),
            # (an enumerator cannot share the dummy's name; the row pins the skip only)
            "enum, bind(c)\n      enumerator :: reason_code = 1\n    end enum",
        )
        for block in blocks:
            with self.subTest(block=block.split("\n")[0]):
                # block BEFORE the real declaration: not an over-refusal
                self.assertIsNone(self._v(block + "\n    " + pinned))
                # block AFTER: same
                self.assertIsNone(self._v(pinned + "\n    " + block))
                # the block's own declaration does not vouch for a fixed-length dummy
                r = self._v(block + "\n    " + bad)
                self.assertIsNotNone(r)
                self.assertIn("without the `allocatable` attribute", r)

    def test_header_and_end_spellings_the_walk_must_read(self) -> None:
        """Survivors of a round-1 mechanism sweep, one row each: a prefixed header, a bare
        `end` closing the previous procedure, `endsubroutine` as one word, a module-level
        `abstract interface` block before `contains`, a statement label on the target's own
        header, and two attribute look-alikes (`allocatable :: other`, an identifier
        containing `allocatable` inside the len spec)."""
        v = checks_abi_dummy_violation
        bad = "character(len=64), intent(out) :: reason_na"
        module = self._MODULE.replace("{DECL}", bad)
        rows = {
            "pure prefix": module.replace("  subroutine metric_compute(",
                                          "  pure subroutine metric_compute("),
            "bare end before": module.replace(
                "contains\n", "contains\n  subroutine get_time(t)\n"
                "    real(8), intent(out) :: t\n    t = 0d0\n  end\n", 1),
            "endsubroutine before": module.replace(
                "contains\n", "contains\n  subroutine get_time(t)\n"
                "    real(8), intent(out) :: t\n    t = 0d0\n  endsubroutine get_time\n", 1),
            "abstract interface before contains": module.replace(
                "contains\n", "  abstract interface\n    subroutine cb(x)\n"
                "      real(8), intent(in) :: x\n    end subroutine cb\n"
                "  end interface\ncontains\n", 1),
            "labelled header": module.replace("  subroutine metric_compute(",
                                              "10 subroutine metric_compute("),
            # round 2: `interface` is not reserved — an assignment to a variable of that name
            # in an earlier procedure must not open an interface block that never closes
            "interface as an identifier": module.replace(
                "contains\n", "contains\n  subroutine get_time(t)\n"
                "    real(8), intent(out) :: t\n    integer :: interface\n"
                "    interface = 1\n    t = real(interface, 8)\n  end subroutine get_time\n", 1),
            # round 2: a generic interface before `contains` (its `module procedure` line is
            # not a `module` unit header)
            "generic interface before contains": module.replace(
                "contains\n", "  interface gt\n    module procedure get_time\n"
                "  end interface gt\n  public :: gt\ncontains\n  subroutine get_time(t)\n"
                "    real(8), intent(out) :: t\n    t = 0d0\n  end subroutine get_time\n", 1),
            # round 2: a `block` local named like the dummy does not vouch for it
            "block-local shadow": module.replace(
                "    associate (u => case_id); end associate\n",
                "    associate (u => case_id); end associate\n    block\n"
                "      character(len=:), allocatable :: reason_na\n      reason_na = 'x'\n"
                "      if (len(reason_na) < 0) val = 1.0d0\n    end block\n"),
            "named block-local shadow, attribute statement": module.replace(
                "    associate (u => case_id); end associate\n",
                "    associate (u => case_id); end associate\n    b: block\n"
                "      character(len=:) :: reason_na\n      allocatable :: reason_na\n"
                "      reason_na = 'x'\n      if (len(reason_na) < 0) val = 1.0d0\n"
                "    end block b\n"),
            "allocatable :: other": module.replace(
                bad, "real(8), dimension(:) :: other\n    allocatable :: other\n    " + bad),
            "allocatable inside len spec": module.replace(
                bad, "character(len=8), parameter :: allocatable_x = 'abcdefgh'\n"
                "    character(len=len(allocatable_x)), intent(out) :: reason_na"),
        }
        for label, text in rows.items():
            with self.subTest(row=label):
                r = v(text, "bx")
                self.assertIsNotNone(r, label)
                self.assertIn("without the `allocatable` attribute", r)

    def test_positive_evidence_only(self) -> None:
        v = checks_abi_dummy_violation
        bad = "character(len=64), intent(out) :: reason_na"
        # published but not defined here: not judged (the syntax gate resolves the `use`)
        self.assertIsNone(v("module bx_checks\n private\n public :: metric_compute\n"
                            "end module bx_checks\n", "bx"))
        # defined in another module, or after `end module`: not the runner's callee
        self.assertIsNone(self._v(bad, bx_checks="other"))
        self.assertIsNone(v(self._MODULE.replace("{DECL}", bad).replace(
            "contains\n", "end module bx_checks\nmodule tail\ncontains\n").replace(
            "end module bx_checks\n", "end module tail\n", 1), "bx"))
        # a prototype in an interface block is not a definition
        self.assertIsNone(v("module bx_checks\n interface\n  subroutine metric_compute("
                            "a, b, c, d, e, f)\n   character(len=64) :: e\n  end subroutine\n"
                            " end interface\nend module bx_checks\n", "bx"))
        # an internal procedure of that name is not the module-level definition; the
        # module-level one that follows is judged
        nested = self._MODULE.replace("{DECL}", "character(len=:), allocatable, intent(out) :: reason_na").replace(
            "contains\n",
            "contains\n  subroutine case_setup(case_id, ok)\n"
            "    character(len=*), intent(in) :: case_id\n    logical, intent(out) :: ok\n"
            "    ok = .true.\n  contains\n"
            "    subroutine metric_compute(a, b, c, d, e, f)\n      character(len=64) :: e\n"
            "    end subroutine metric_compute\n  end subroutine case_setup\n", 1)
        self.assertIsNone(v(nested, "bx"))
        self.assertIsNotNone(v(nested.replace(
            "character(len=:), allocatable, intent(out) :: reason_na", bad), "bx"))
        # a bare `end` never closes the module. Since labels are stripped (round 1) this
        # row's labelled header IS read, so its bare `end` arrives at depth 1 and the depth-0
        # branch is defensive — no legal module-level header the pattern misses is known
        # (round-2 review); the row stays as the regression guard for the labelled shape
        self.assertIsNotNone(v(self._MODULE.replace("{DECL}", bad).replace(
            "contains\n", "contains\n10 subroutine get_time(t)\n    real(8), intent(out) :: t\n"
            "    t = 0d0\n  end\n", 1), "bx"))
        # a string literal spelling a header is not a header: unmasked, the literal would be
        # read as a six-dummy `metric_compute` with no declarations and refused; the real one
        # after it is the pinned form and passes
        self.assertIsNone(v(self._MODULE.replace(
            "{DECL}", "character(len=:), allocatable, intent(out) :: reason_na").replace(
            "contains\n", "  character(len=*), parameter :: note = &\n"
            "    'see subroutine metric_compute(a, b, c, d, e, f)'\n  public :: note\ncontains\n",
            1), "bx"))

    @unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
    def test_the_premise_the_compiler_does_not_see_it(self) -> None:
        """The gate exists because `-fsyntax-only` accepts the refused form against the
        rendered runner (measured, gfortran 11.4). If this row starts failing, the compiler has
        started diagnosing it and the gate is a pre-emption of the syntax check rather than the
        only deterministic reader — update `METRIC_COMPUTE_DUMMIES`' comment, not the gate."""
        v = checks_abi_dummy_violation
        ir = _rank34_metrics_ir()
        runner = render_runner(ir, RANK_SID, HARNESS)
        pinned = "character(len=:), allocatable, intent(out) :: reason_na"
        bad = "character(len=64), intent(out) :: reason_na"
        self.assertIn(pinned, _RANK_CHECKS_STUB)
        for decl, refused in ((pinned, False), (bad, True)):
            checks = _RANK_CHECKS_STUB.replace(pinned, decl)
            self.assertEqual(refused, v(checks, RANK_SID) is not None, decl)
            with tempfile.TemporaryDirectory() as td:
                d = Path(td)
                (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
                (d / f"{RANK_SID}_checks.f90").write_text(checks)
                (d / f"{RANK_SID}_runner.f90").write_text(runner)
                r = subprocess.run(
                    ["gfortran", "-fsyntax-only", "-std=f2008", "-J", td,
                     "harness_fortran_cpu_model.f90", f"{RANK_SID}_checks.f90",
                     f"{RANK_SID}_runner.f90"],
                    cwd=d, capture_output=True, text=True, check=False)
                self.assertEqual(0, r.returncode, (decl, r.stderr))


if __name__ == "__main__":
    unittest.main()
