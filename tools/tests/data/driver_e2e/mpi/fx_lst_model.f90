! Fixture model (5) of the distributed driver's end to end (issue #610, PR-B2 review round 2): a
! list-form `component` with an axis. Case `a` runs `__plain` on a state `q [nx]` read from the
! case file; case `b` runs `__proc`, which hands a host-rendered procedure a constant field whose
! `record` fills the same state `q`, so `q` is file-backed in one entry and driver-written in the
! other: the perf record's global size of `q` must come from the entry that ran the case.
module fx_lst_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private
  integer, parameter :: dp = real64
  public :: fx_lst__plain, fx_lst__proc
  abstract interface
    subroutine fx_lst_proto(nx, q, dqdt)
      import :: dp
      integer, intent(in) :: nx
      real(dp), intent(in) :: q(nx)
      real(dp), intent(out) :: dqdt(nx)
    end subroutine fx_lst_proto
  end interface
contains
  subroutine fx_lst__plain(nx, q, w, ok)
    integer, intent(in) :: nx
    real(dp), intent(in) :: q(nx)
    real(dp), intent(out) :: w(nx)
    logical, intent(out) :: ok
    w = 2.0_dp * q
    ok = .true.
  end subroutine fx_lst__plain
  subroutine fx_lst__proc(nx, rhs, w, ok)
    integer, intent(in) :: nx
    procedure(fx_lst_proto) :: rhs
    real(dp), intent(out) :: w(nx)
    logical, intent(out) :: ok
    real(dp) :: z(nx)
    z = 3.0_dp
    call rhs(nx, z, w)
    ok = .true.
  end subroutine fx_lst__proc
end module fx_lst_model
