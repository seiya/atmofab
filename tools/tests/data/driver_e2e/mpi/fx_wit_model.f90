! Fixture model (3) of the distributed driver's end to end (issue #610, PR-B2): a witness. Each
! cell of `w` holds the cell count the call was handed through `nx`, each of `wb` the one handed
! through `nx_b` (a second dummy bound to the same input), each of `wg` the count `nx_g`, bound to
! a `derived` value: the first two are the block's on a distributed case, the third the case's.
module fx_wit_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private
  integer, parameter :: dp = real64
  public :: fx_wit__apply
contains
  subroutine fx_wit__apply(nx, nx_b, nx_g, w, wb, wg, ok)
    integer, intent(in) :: nx, nx_b, nx_g
    real(dp), intent(out) :: w(nx)
    real(dp), intent(out) :: wb(nx)
    real(dp), intent(out) :: wg(nx)
    logical, intent(out) :: ok
    w = real(nx, dp)
    wb = real(nx_b, dp)
    wg = real(nx_g, dp)
    ok = .true.
  end subroutine fx_wit__apply
end module fx_wit_model
