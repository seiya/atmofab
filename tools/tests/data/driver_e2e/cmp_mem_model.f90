! A member `problem` that publishes `__init` (issue #632, Part 2 P3): a test fixture.
! `cmp_mem__init` establishes the private `rate` from the CURRENT case's `mode` (form a of
! `docs/CONTROLLED_SPEC.md`'s private-state rule) and writes the state and the loop plan;
! `cmp_mem__step` reads `rate`. A composite that skips this `__init`, calls it once per process
! or calls it from `__step` changes what the step computes.
module cmp_mem_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  integer, parameter :: dp = real64
  private
  public :: cmp_mem__init, cmp_mem__step

  real(dp), allocatable :: rate(:)

contains

  subroutine cmp_mem__init(nx, mode, u, dt, n_plan, init_ok)
    integer, intent(in) :: nx
    integer, intent(in) :: mode
    real(dp), intent(out) :: u(nx)
    real(dp), intent(out) :: dt
    integer, intent(out) :: n_plan
    logical, intent(out) :: init_ok
    integer :: i
    if (allocated(rate)) deallocate(rate)
    allocate(rate(nx))
    do i = 1, nx
      rate(i) = 0.125_dp * real(mode * i, dp)
      u(i) = 1.0_dp + real(i, dp)
    end do
    dt = 0.25_dp
    n_plan = 6
    init_ok = mode >= 0
  end subroutine cmp_mem__init

  subroutine cmp_mem__step(nx, dt, t, u, ok)
    integer, intent(in) :: nx
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    real(dp), intent(inout) :: u(nx)
    logical, intent(out) :: ok
    integer :: i
    ok = allocated(rate)
    if (.not. ok) return
    if (size(rate) /= nx) then
      ok = .false.
      return
    end if
    do i = 1, nx
      u(i) = u(i) + dt * (0.0625_dp * t - rate(i) * u(i))
    end do
  end subroutine cmp_mem__step

end module cmp_mem_model
