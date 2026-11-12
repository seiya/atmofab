! A composite `problem` over a member that publishes `__init` (issue #632, Part 2 P3), in the
! subcycling shape of the plan's sample: a test fixture. `cmp_sub__init` is the member's
! `__init`, called once with the composite's own arguments; `cmp_sub__step` calls the member's
! `__step` `substeps` times with `dt / substeps`, the `j`-th at `t + (j - 1) * dt / substeps`,
! and a false result of the member ends the call with false.
module cmp_sub_model
  use, intrinsic :: iso_fortran_env, only: real64
  use cmp_mem_model, only: cmp_mem__init, cmp_mem__step
  implicit none
  integer, parameter :: dp = real64
  private
  public :: cmp_sub__init, cmp_sub__step

contains

  subroutine cmp_sub__init(nx, mode, u, dt, n_plan, init_ok)
    integer, intent(in) :: nx
    integer, intent(in) :: mode
    real(dp), intent(out) :: u(nx)
    real(dp), intent(out) :: dt
    integer, intent(out) :: n_plan
    logical, intent(out) :: init_ok
    call cmp_mem__init(nx, mode, u, dt, n_plan, init_ok)
  end subroutine cmp_sub__init

  subroutine cmp_sub__step(nx, dt, t, substeps, u, ok)
    integer, intent(in) :: nx
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    integer, intent(in) :: substeps
    real(dp), intent(inout) :: u(nx)
    logical, intent(out) :: ok
    logical :: member_ok
    real(dp) :: dts
    integer :: j
    ok = .false.
    if (substeps < 1) return
    dts = dt / real(substeps, dp)
    do j = 1, substeps
      call cmp_mem__step(nx, dts, t + real(j - 1, dp) * dts, u, member_ok)
      ok = member_ok
      if (.not. ok) return
    end do
  end subroutine cmp_sub__step

end module cmp_sub_model
