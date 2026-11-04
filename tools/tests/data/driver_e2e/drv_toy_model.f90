! The operations the driver's tests call (issue #589 PR-1b): each `mode` is one behaviour the
! driver must answer for. A test fixture.
module drv_toy_model
  use, intrinsic :: iso_fortran_env, only: dp => real64
  use, intrinsic :: ieee_arithmetic, only: ieee_value, ieee_quiet_nan
  implicit none
  private
  public :: drv_toy__step, drv_toy__init, drv_toy__apply, drv_toy__advance, drv_rhs_proto

  abstract interface
    subroutine drv_rhs_proto(nx, t, u, dudt)
      import :: dp
      integer, intent(in) :: nx
      real(dp), intent(in) :: t
      real(dp), intent(in) :: u(nx)
      real(dp), intent(out) :: dudt(nx)
    end subroutine drv_rhs_proto
  end interface
contains

  subroutine drv_toy__init(nx, mode, n_plan, dt_plan, u, init_ok)
    integer, intent(in) :: nx
    integer, intent(in) :: mode
    integer, intent(out) :: n_plan
    real(dp), intent(out) :: dt_plan
    real(dp), intent(out) :: u(nx)
    logical, intent(out) :: init_ok
    n_plan = 3
    dt_plan = 0.25_dp
    u = 1.0_dp
    init_ok = .true.
    if (mode == 10) init_ok = .false.
    if (mode == 11) n_plan = 0
    if (mode == 12) dt_plan = 0.0_dp
  end subroutine drv_toy__init

  ! One committed step: u += dt, w = u. mode 1 refuses at step 3 after writing its work copy,
  ! 2 leaves w(1) unassigned, 3 never assigns ok, 4 puts a NaN into u at step 2, 5 records dt in u.
  subroutine drv_toy__step(nx, mode, dt, t, stepno, u, w, ok)
    integer, intent(in) :: nx
    integer, intent(in) :: mode
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    integer, intent(in) :: stepno
    real(dp), intent(inout) :: u(nx)
    real(dp), intent(out) :: w(nx)
    logical, intent(out) :: ok
    u = u + dt
    if (mode == 5) u = dt
    if (mode == 2) then
      w(2:nx) = u(2:nx)
    else
      w = u + t - t
    end if
    if (mode == 3) return
    ok = .true.
    if (mode == 1 .and. stepno == 2) then
      u = -99.0_dp
      w = -99.0_dp
      ok = .false.
    end if
    if (mode == 4 .and. stepno == 1) u(2) = ieee_value(1.0_dp, ieee_quiet_nan)
  end subroutine drv_toy__step

  ! One call on the states themselves. mode 1 writes then refuses, 2 assigns nothing, 3 assigns
  ! no result. `a` and `b` are two states; `s` a kernel scalar.
  subroutine drv_toy__apply(nx, mode, a, b, c, s, ok)
    integer, intent(in) :: nx
    integer, intent(in) :: mode
    real(dp), intent(in) :: a(nx)
    real(dp), intent(out) :: b(nx)
    real(dp), intent(inout) :: c(nx)
    real(dp), intent(out) :: s
    logical, intent(out) :: ok
    if (mode == 2) then
      ok = .true.
      return
    end if
    b = 2.0_dp * a
    c = c + 1.0_dp
    s = sum(a)
    if (mode == 3) return
    ok = mode /= 1
  end subroutine drv_toy__apply

  ! Heun's method over a procedure argument: two calls of `rhs` per call; a non-positive dt refuses
  ! with no call.
  subroutine drv_toy__advance(nx, t, dt, u_n, rhs, u_np1, ok)
    integer, intent(in) :: nx
    real(dp), intent(in) :: t
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: u_n(nx)
    procedure(drv_rhs_proto) :: rhs
    real(dp), intent(out) :: u_np1(nx)
    logical, intent(out) :: ok
    real(dp) :: k1(nx), k2(nx)
    ok = dt > 0.0_dp
    if (.not. ok) then
      u_np1 = u_n
      return
    end if
    call rhs(nx, t, u_n, k1)
    call rhs(nx, t + dt, u_n + dt * k1, k2)
    u_np1 = u_n + 0.5_dp * dt * (k1 + k2)
  end subroutine drv_toy__advance

end module drv_toy_model
