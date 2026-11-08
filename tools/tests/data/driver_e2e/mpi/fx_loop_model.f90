! Fixture model (4) of the distributed driver's end to end (issue #610, PR-B2): a `loop`
! component in rk4's and tc4's shape. The state `u [ncomp, nx, ny]` is advanced by four calls of
! a host-rendered procedure argument; the kernel reads the host arrays `f [nx, ny]`, `x [nx]`,
! `y [ny]` at `(i, j)` (so a swapped or unsliced one tells), a cell `in` state `zb [nx, ny]` and
! a ghosted `in` state `ug [nx + 2*ng]` at its interior position only (its domain ghosts are a
! sentinel, so a block sliced off by `ng` tells). The prototype's recorded `in` dummy is `q`,
! named like no state of the node.
module fx_loop_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private
  integer, parameter :: dp = real64
  public :: fx_loop__advance
  abstract interface
    subroutine fx_rhs_proto(ncomp, nx, ny, t, q, dqdt)
      import :: dp
      integer, intent(in) :: ncomp, nx, ny
      real(dp), intent(in) :: t
      real(dp), intent(in) :: q(ncomp, nx, ny)
      real(dp), intent(out) :: dqdt(ncomp, nx, ny)
    end subroutine fx_rhs_proto
  end interface
contains
  subroutine fx_loop__advance(ncomp, nx, ny, ng, u, ug, zb, f, x, y, rhs, t, dt, u_np1, ok)
    integer, intent(in) :: ncomp, nx, ny, ng
    real(dp), intent(in) :: u(ncomp, nx, ny)
    real(dp), intent(in) :: ug(nx + 2 * ng)
    real(dp), intent(in) :: zb(nx, ny)
    real(dp), intent(in) :: f(nx, ny)
    real(dp), intent(in) :: x(nx)
    real(dp), intent(in) :: y(ny)
    procedure(fx_rhs_proto) :: rhs
    real(dp), intent(in) :: t, dt
    real(dp), intent(out) :: u_np1(ncomp, nx, ny)
    logical, intent(out) :: ok
    real(dp) :: k(ncomp, nx, ny)
    integer :: i, j, c, s
    ok = .true.
    u_np1 = u
    do s = 1, 4
      call rhs(ncomp, nx, ny, t + 0.25_dp * (s - 1) * dt, u_np1, k)
      do j = 1, ny
        do i = 1, nx
          do c = 1, ncomp
            u_np1(c, i, j) = u_np1(c, i, j) + 0.25_dp * dt * (k(c, i, j) &
              + f(i, j) * x(i) * 0.001_dp + y(j) * 0.01_dp + zb(i, j) * 0.5_dp &
              + ug(i + ng) * 0.1_dp * c)
          end do
        end do
      end do
    end do
  end subroutine fx_loop__advance
end module fx_loop_model
