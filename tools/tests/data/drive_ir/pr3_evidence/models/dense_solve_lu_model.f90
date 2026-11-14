! Solution of a dense linear system by the LU driver of the external library `lapack`.
module dense_solve_lu_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private

  integer, parameter :: dp = real64

  public :: dp
  public :: dense_solve_lu__solve

  interface
    subroutine dgesv(n, nrhs, a, lda, ipiv, b, ldb, info)
      import :: dp
      implicit none
      integer, intent(in) :: n
      integer, intent(in) :: nrhs
      integer, intent(in) :: lda
      integer, intent(in) :: ldb
      real(dp), intent(inout) :: a(lda, n)
      integer, intent(out) :: ipiv(n)
      real(dp), intent(inout) :: b(ldb, nrhs)
      integer, intent(out) :: info
    end subroutine dgesv
  end interface

contains

  ! step_01: solved = .false. and return when n < 1, before the library is reached.
  ! step_02: the library factorizes a copy of a with row interchanges and solves for a copy
  ! of b; a and b are not modified.
  ! step_03: solved = (info == 0); x is the solution only then.
  subroutine dense_solve_lu__solve(n, a, b, x, solved)
    integer, intent(in) :: n
    real(dp), intent(in) :: a(n, n)
    real(dp), intent(in) :: b(n)
    real(dp), intent(out) :: x(n)
    logical, intent(out) :: solved
    real(dp), allocatable :: lu(:, :)
    real(dp), allocatable :: rhs(:, :)
    integer, allocatable :: ipiv(:)
    integer :: info

    solved = .false.
    if (n < 1) return

    allocate (lu(n, n), rhs(n, 1), ipiv(n))
    lu = a
    rhs(:, 1) = b
    call dgesv(n, 1, lu, n, ipiv, rhs, n, info)
    solved = (info == 0)
    if (solved) x = rhs(:, 1)
  end subroutine dense_solve_lu__solve

end module dense_solve_lu_model
