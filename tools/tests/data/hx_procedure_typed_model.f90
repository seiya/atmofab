! Fixture for issue #266: a `component` model publishing an operation that takes a procedure
! argument. `hx__advance` integrates one explicit step with the tendency `rhs`, whose shape is
! the abstract-interface prototype `hx_rhs_1d`. Read by ProcedureTypedSurfaceGateTests
! (tools/tests/test_validate_pipeline_semantics.py); `gfortran -std=f2008 -fsyntax-only` clean.
module hx_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private
  public :: dp, hx_rhs_1d, hx__advance
  integer, parameter :: dp = real64

  abstract interface
    subroutine hx_rhs_1d(u, dudt)
      import :: dp
      implicit none
      real(dp), intent(in) :: u(:)
      real(dp), intent(out) :: dudt(:)
    end subroutine hx_rhs_1d
  end interface

contains

  subroutine hx__advance(u, rhs, dt, u_next)
    real(dp), intent(in) :: u(:)
    procedure(hx_rhs_1d) :: rhs
    real(dp), intent(in) :: dt
    real(dp), intent(out) :: u_next(:)
    real(dp), allocatable :: k(:)
    allocate(k(size(u)))
    call rhs(u, k)
    u_next = u + dt * k
  end subroutine hx__advance

end module hx_model
