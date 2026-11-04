! Pointwise first-order (p0) Rusanov interface fluxes of the 2D shallow water
! equations: x flux from U_L/U_R, y flux from U_B/U_T, each state [h, hu, hv].
module dynamics_shallow_water_flux_2d_rusanov_p0_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private

  integer, parameter :: dp = real64

  public :: dp
  public :: dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux

contains

  ! step_01: input guard; steps 02-04 only when every depth is positive.
  subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(U_L, U_R, U_B, U_T, &
      g, F_star, G_star, a_x, a_y, guard_ok)
    real(dp), intent(in) :: U_L(3)
    real(dp), intent(in) :: U_R(3)
    real(dp), intent(in) :: U_B(3)
    real(dp), intent(in) :: U_T(3)
    real(dp), intent(in) :: g
    real(dp), intent(out) :: F_star(3)
    real(dp), intent(out) :: G_star(3)
    real(dp), intent(out) :: a_x
    real(dp), intent(out) :: a_y
    logical, intent(out) :: guard_ok
    real(dp) :: q_b(3), q_t(3), g_n(3)

    guard_ok = (U_L(1) > 0.0_dp) .and. (U_R(1) > 0.0_dp) .and. &
      (U_B(1) > 0.0_dp) .and. (U_T(1) > 0.0_dp)
    if (.not. guard_ok) then
      F_star = 0.0_dp
      G_star = 0.0_dp
      a_x = 0.0_dp
      a_y = 0.0_dp
      return
    end if

    ! x interface: normal momentum hu, tangential hv (states used as-is).
    call rusanov_normal(U_L, U_R, g, F_star, a_x)

    ! y interface: states written [h, hv, hu]; result put back as [h, hu, hv].
    q_b = [U_B(1), U_B(3), U_B(2)]
    q_t = [U_T(1), U_T(3), U_T(2)]
    call rusanov_normal(q_b, q_t, g, g_n, a_y)
    G_star = [g_n(1), g_n(3), g_n(2)]
  end subroutine dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux

  ! Steps 02-04 for one interface in normal/tangential form q = [h, m_n, m_t]:
  ! wave speed a = max(|u_a| + c_a, |u_b| + c_b), physical normal fluxes and
  ! Rusanov flux 0.5*(f_a + f_b) - 0.5*a*(q_b - q_a).
  subroutine rusanov_normal(q_a, q_b, g, f_star, a)
    real(dp), intent(in) :: q_a(3)
    real(dp), intent(in) :: q_b(3)
    real(dp), intent(in) :: g
    real(dp), intent(out) :: f_star(3)
    real(dp), intent(out) :: a
    real(dp) :: un_a, un_b, c_a, c_b
    real(dp) :: f_a(3), f_b(3)

    un_a = q_a(2) / q_a(1)
    un_b = q_b(2) / q_b(1)
    c_a = sqrt(g * q_a(1))
    c_b = sqrt(g * q_b(1))
    a = max(abs(un_a) + c_a, abs(un_b) + c_b)

    f_a = [q_a(2), q_a(2) * q_a(2) / q_a(1) + g * q_a(1) * q_a(1) / 2.0_dp, &
      q_a(2) * q_a(3) / q_a(1)]
    f_b = [q_b(2), q_b(2) * q_b(2) / q_b(1) + g * q_b(1) * q_b(1) / 2.0_dp, &
      q_b(2) * q_b(3) / q_b(1)]

    f_star = 0.5_dp * (f_a + f_b) - 0.5_dp * a * (q_b - q_a)
  end subroutine rusanov_normal

end module dynamics_shallow_water_flux_2d_rusanov_p0_model
