! L0 SSPRK2 time update of the 2D shallow water state with caller-supplied
! fixed fields L_flux and S_b: both stages use rhs = L_flux + S_b.
module dynamics_shallow_water_time_update_2d_ssprk2_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private

  integer, parameter :: dp = real64

  public :: dp
  public :: dynamics_shallow_water_time_update_2d_ssprk2__advance

contains

  subroutine dynamics_shallow_water_time_update_2d_ssprk2__advance(ncomp, nx, ny, U_n, &
      L_flux, S_b, z_b, dt, dx, dy, U_np1, guard_pass)
    integer, intent(in) :: ncomp
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: U_n(ncomp, nx, ny)
    real(dp), intent(in) :: L_flux(ncomp, nx, ny)
    real(dp), intent(in) :: S_b(ncomp, nx, ny)
    real(dp), intent(in) :: z_b(nx, ny)
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: dx
    real(dp), intent(in) :: dy
    real(dp), intent(out) :: U_np1(ncomp, nx, ny)
    logical, intent(out) :: guard_pass
    real(dp), allocatable :: rhs(:, :, :)
    real(dp), allocatable :: U_1(:, :, :)
    integer :: c, i, j

    ! z_b is accepted and inert at L0.
    associate (unused_z_b => z_b); end associate

    ! step_01: input guard.
    guard_pass = (dt > 0.0_dp) .and. (dx > 0.0_dp) .and. (dy > 0.0_dp)
    if (.not. guard_pass) return

    allocate(rhs(ncomp, nx, ny), U_1(ncomp, nx, ny))

    ! step_02: rhs = L_flux + S_b (the same rhs serves both stages).
    !$omp parallel do collapse(2) schedule(static) private(c)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          rhs(c, i, j) = L_flux(c, i, j) + S_b(c, i, j)
        end do
      end do
    end do
    !$omp end parallel do

    ! step_03: first stage U_1 = U_n + dt*rhs.
    !$omp parallel do collapse(2) schedule(static) private(c)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          U_1(c, i, j) = U_n(c, i, j) + dt * rhs(c, i, j)
        end do
      end do
    end do
    !$omp end parallel do

    ! step_04: second stage with weights 1/2, 1/2.
    !$omp parallel do collapse(2) schedule(static) private(c)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          U_np1(c, i, j) = 0.5_dp * U_n(c, i, j) + &
            0.5_dp * (U_1(c, i, j) + dt * rhs(c, i, j))
        end do
      end do
    end do
    !$omp end parallel do

    deallocate(rhs, U_1)
  end subroutine dynamics_shallow_water_time_update_2d_ssprk2__advance

end module dynamics_shallow_water_time_update_2d_ssprk2_model
