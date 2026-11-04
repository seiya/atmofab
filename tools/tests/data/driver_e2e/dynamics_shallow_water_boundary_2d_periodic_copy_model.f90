! 2D periodic ghost-cell mapping: U_out = periodic padding of the interior of U_in,
! x wrap first (along the first array index), then y wrap (along the second).
module dynamics_shallow_water_boundary_2d_periodic_copy_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private

  integer, parameter :: dp = real64

  public :: dp
  public :: dynamics_shallow_water_boundary_2d_periodic_copy__apply

contains

  ! step_01 grid guard; steps 02-04 run, in order, only on valid input.
  subroutine dynamics_shallow_water_boundary_2d_periodic_copy__apply(nx, ny, ng, &
      nx_total, ny_total, U_in, U_out, grid_valid)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    integer, intent(in) :: ng
    integer, intent(in) :: nx_total
    integer, intent(in) :: ny_total
    real(dp), intent(in) :: U_in(nx_total, ny_total)
    real(dp), intent(out) :: U_out(nx_total, ny_total)
    logical, intent(out) :: grid_valid
    integer :: i, j, k

    ! step_01_grid_guard
    grid_valid = (nx >= 2) .and. (ny >= 2) .and. (ng >= 1)
    if (.not. grid_valid) return

    ! step_02_interior_copy
    !$omp parallel do collapse(2) schedule(static) private(i, j)
    do j = ng + 1, ng + ny
      do i = ng + 1, ng + nx
        U_out(i, j) = U_in(i, j)
      end do
    end do
    !$omp end parallel do

    ! step_03_periodic_wrap_x: interior rows only, along the first index.
    !$omp parallel do schedule(static) private(j, k)
    do j = ng + 1, ng + ny
      do k = 1, ng
        U_out(k, j) = U_in(k + nx, j)
        U_out(ng + nx + k, j) = U_in(ng + k, j)
      end do
    end do
    !$omp end parallel do

    ! step_04_periodic_wrap_y: every column, reading rows completed above.
    !$omp parallel do schedule(static) private(i, k)
    do i = 1, nx_total
      do k = 1, ng
        U_out(i, k) = U_out(i, k + ny)
        U_out(i, ng + ny + k) = U_out(i, ng + k)
      end do
    end do
    !$omp end parallel do
  end subroutine dynamics_shallow_water_boundary_2d_periodic_copy__apply

end module dynamics_shallow_water_boundary_2d_periodic_copy_model
