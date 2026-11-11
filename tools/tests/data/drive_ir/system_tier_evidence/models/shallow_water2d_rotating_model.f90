! A `problem` over a `problem` (system tier PR-S1): a HAND-WRITTEN composing model, not a certified
! source; a copy of the PR-S1 fixture `tools/tests/data/driver_e2e/shallow_water2d_rotating_model.f90`.
! `shallow_water2d_rotating__step` calls the member `shallow_water2d__step` (the bench links the
! member's certified source) and then, when `rotation == 1` and
! `mod(step + 1, rotation_every) == 0`, takes one step of the certified RK4
! (`dynamics_shallow_water_time_update_2d_rk4@0.1.1`) of length `rotation_every * dt` of the
! certified Coriolis source (`dynamics_shallow_water_source_2d_coriolis@0.2.0`) over [h, hu, hv].
! Lie splitting: the member first, the rotation second.
! A false result of the member is this step's false result and nothing further runs. A false
! `guard_pass` of the Coriolis operation in any RK4 stage, or of RK4, makes the result false
! without overwriting a false of the member. The RK4 tendency prototype carries neither `f` nor a
! logical: `__step` sets the private `cor_f` from its own `f` at every call before RK4 and resets
! the private flag `cor_ok` before RK4 and reads it after (the certified channel model's `rhs_ok`
! is the precedent). The tendency receives the Coriolis output in a local and assigns from it,
! the form the `problem` dependency-dataflow gate reads.
module shallow_water2d_rotating_model
  use, intrinsic :: iso_fortran_env, only: real64
  use shallow_water2d_model, only: shallow_water2d__step
  use dynamics_shallow_water_source_2d_coriolis_model, only: &
    dynamics_shallow_water_source_2d_coriolis__apply
  use dynamics_shallow_water_time_update_2d_rk4_model, only: &
    dynamics_shallow_water_time_update_2d_rk4__advance
  implicit none
  private
  integer, parameter :: dp = real64
  public :: shallow_water2d_rotating__step

  integer, parameter :: ncomp_sw = 3

  ! This call's Coriolis parameter and the Coriolis guard over every RK4 stage.
  real(dp), allocatable :: cor_f(:, :)
  logical :: cor_ok = .true.

contains

  subroutine shallow_water2d_rotating__step(nx, ny, dx, dy, g, dt, t, step, rotation, &
      rotation_every, f, z_b, h, hu, hv, ok)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: dx
    real(dp), intent(in) :: dy
    real(dp), intent(in) :: g
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    integer, intent(in) :: step
    integer, intent(in) :: rotation
    integer, intent(in) :: rotation_every
    real(dp), intent(in) :: f(nx, ny)
    real(dp), intent(in) :: z_b(nx, ny)
    real(dp), intent(inout) :: h(nx, ny)
    real(dp), intent(inout) :: hu(nx, ny)
    real(dp), intent(inout) :: hv(nx, ny)
    logical, intent(out) :: ok

    real(dp), allocatable :: u_n(:, :, :)
    real(dp), allocatable :: u_np1(:, :, :)
    logical :: member_ok
    logical :: rk4_ok
    integer :: i, j

    ok = .false.
    if (rotation /= 0 .and. rotation /= 1) return
    if (rotation_every < 1) return

    call shallow_water2d__step(nx, ny, dx, dy, g, dt, t, z_b, h, hu, hv, member_ok)
    ok = member_ok
    if (.not. ok) return
    if (rotation /= 1) return
    if (mod(step + 1, rotation_every) /= 0) return

    if (allocated(cor_f)) deallocate(cor_f)
    allocate(cor_f(nx, ny))
    cor_f = f
    cor_ok = .true.

    allocate(u_n(ncomp_sw, nx, ny), u_np1(ncomp_sw, nx, ny))
    !$omp parallel do private(i) schedule(static)
    do j = 1, ny
      do i = 1, nx
        u_n(1, i, j) = h(i, j)
        u_n(2, i, j) = hu(i, j)
        u_n(3, i, j) = hv(i, j)
      end do
    end do
    !$omp end parallel do

    call dynamics_shallow_water_time_update_2d_rk4__advance(ncomp_sw, nx, ny, u_n, coriolis_tendency, &
      t, real(rotation_every, dp) * dt, u_np1, rk4_ok)
    ok = rk4_ok .and. cor_ok
    if (.not. ok) return

    !$omp parallel do private(i) schedule(static)
    do j = 1, ny
      do i = 1, nx
        h(i, j) = u_np1(1, i, j)
        hu(i, j) = u_np1(2, i, j)
        hv(i, j) = u_np1(3, i, j)
      end do
    end do
    !$omp end parallel do
  end subroutine shallow_water2d_rotating__step

  ! The tendency RK4 evaluates: the Coriolis source of the state it is handed, with this call's
  ! `f` (`cor_f`). A false guard clears `cor_ok`, which `__step` reads after RK4.
  subroutine coriolis_tendency(ncomp, nx, ny, t, U, dUdt)
    integer, intent(in) :: ncomp
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: t
    real(dp), intent(in) :: U(ncomp, nx, ny)
    real(dp), intent(out) :: dUdt(ncomp, nx, ny)

    real(dp), allocatable :: s(:, :, :)
    logical :: stage_ok

    associate (unused_t => t)
    end associate
    allocate(s(ncomp, nx, ny))
    call dynamics_shallow_water_source_2d_coriolis__apply(ncomp, nx, ny, cor_f, U, s, stage_ok)
    if (.not. stage_ok) then
      cor_ok = .false.
      dUdt = 0.0_dp
      return
    end if
    dUdt = s
  end subroutine coriolis_tendency

end module shallow_water2d_rotating_model
