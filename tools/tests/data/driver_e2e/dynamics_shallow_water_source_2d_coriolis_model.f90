! Coriolis source term of the 2D shallow water equations (pointwise, explicit).
module dynamics_shallow_water_source_2d_coriolis_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private

  integer, parameter :: dp = real64

  public :: dp
  public :: dynamics_shallow_water_source_2d_coriolis__apply

contains

  ! step_01: guard_pass = (ncomp == 3) .and. (nx >= 1) .and. (ny >= 1); on a false
  ! guard the operation returns at once and S is left undefined.
  ! step_02: S(1) = 0, S(2) = f*hv, S(3) = -f*hu at every interior cell.
  subroutine dynamics_shallow_water_source_2d_coriolis__apply(ncomp, nx, ny, f, U, S, &
      guard_pass)
    integer, intent(in) :: ncomp
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: f(nx, ny)
    real(dp), intent(in) :: U(ncomp, nx, ny)
    real(dp), intent(out) :: S(ncomp, nx, ny)
    logical, intent(out) :: guard_pass
    integer :: i, j

    guard_pass = (ncomp == 3) .and. (nx >= 1) .and. (ny >= 1)
    if (.not. guard_pass) return

    !$omp parallel do private(i) schedule(static)
    do j = 1, ny
      do i = 1, nx
        S(1, i, j) = 0.0_dp
        S(2, i, j) = f(i, j) * U(3, i, j)
        S(3, i, j) = -f(i, j) * U(2, i, j)
      end do
    end do
    !$omp end parallel do
  end subroutine dynamics_shallow_water_source_2d_coriolis__apply

end module dynamics_shallow_water_source_2d_coriolis_model
