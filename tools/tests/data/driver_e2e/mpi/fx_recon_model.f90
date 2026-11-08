! Fixture model (1) of the distributed driver's end to end (issue #610, PR-B2): a component in
! muscl's shape. A ghosted `in` (`u_in`, ghost-extended along both axes, its domain ghosts a
! sentinel outside the interior's range) and two face `out` arrays; the stencil is asymmetric
! (left-biased along x, right-biased along y), so a block sliced off by `ng` reads other cells.
module fx_recon_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private
  integer, parameter :: dp = real64
  public :: fx_recon__apply
contains
  subroutine fx_recon__apply(nx, ny, ng, nx_total, ny_total, u_in, u_l, u_r, ok)
    integer, intent(in) :: nx, ny, ng, nx_total, ny_total
    real(dp), intent(in) :: u_in(nx_total, ny_total)
    real(dp), intent(out) :: u_l(nx + 1, ny)
    real(dp), intent(out) :: u_r(nx, ny + 1)
    logical, intent(out) :: ok
    integer :: i, j
    ok = nx >= 1 .and. ny >= 1 .and. ng >= 2 .and. nx_total == nx + 2 * ng &
      .and. ny_total == ny + 2 * ng
    if (.not. ok) return
    ! face i lies between cells i - 1 and i; cell i is at position i + ng
    do j = 1, ny
      do i = 1, nx + 1
        u_l(i, j) = u_in(i + ng - 1, j + ng) &
          + 0.3_dp * (u_in(i + ng - 1, j + ng) - u_in(i + ng - 2, j + ng))
      end do
    end do
    do j = 1, ny + 1
      do i = 1, nx
        u_r(i, j) = u_in(i + ng, j + ng - 1) &
          - 0.2_dp * (u_in(i + ng, j + ng) - u_in(i + ng, j + ng - 1))
      end do
    end do
  end subroutine fx_recon__apply
end module fx_recon_model
