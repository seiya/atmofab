!> advdiff1d_linear model: one explicit integration step of the 1D linear
!> advection-diffusion problem, composed from the certified components
!> (periodic ghost fill, upwind/central face flux, forward-Euler update).
module advdiff1d_linear_model
  use, intrinsic :: iso_fortran_env, only: dp => real64
  use dynamics_advection_diffusion_boundary_1d_periodic_copy_model, only: &
    dynamics_advection_diffusion_boundary_1d_periodic_copy__apply
  use dynamics_advdiff_flux_1d_upwind_center2_model, only: &
    dynamics_advdiff_flux_1d_upwind_center2__compute_flux
  use dynamics_advection_diffusion_time_update_1d_euler1_model, only: &
    dynamics_advection_diffusion_time_update_1d_euler1__advance
  implicit none
  private
  public :: advdiff1d_linear__step

  !> Ghost width fixed by the controlled spec (derived_field_rules: ng).
  integer, parameter :: ng = 1

contains

  !> One iteration n of step_00..step_05: u_next = F(u; a, nu, dx, dt) and
  !> t_next = t_start + n*dt, with dx = L/nx and dt = (t_end - t_start)/n_step.
  !> ok is false (fail closed) when any component guard rejects its inputs.
  subroutine advdiff1d_linear__step(nx, L, a, nu, t_start, t_end, n_step, n, u, &
      u_next, t_next, ok)
    integer, intent(in) :: nx
    real(dp), intent(in) :: L
    real(dp), intent(in) :: a
    real(dp), intent(in) :: nu
    real(dp), intent(in) :: t_start
    real(dp), intent(in) :: t_end
    integer, intent(in) :: n_step
    integer, intent(in) :: n
    real(dp), intent(in) :: u(nx)
    real(dp), intent(out) :: u_next(nx)
    real(dp), intent(out) :: t_next
    logical, intent(out) :: ok
    real(dp) :: u_in(nx + 2*ng)
    real(dp) :: u_out(nx + 2*ng)
    real(dp) :: flux_adv(nx + 1)
    real(dp) :: flux_dif(nx + 1)
    real(dp) :: L_flux(nx)
    real(dp) :: u_np1(nx)
    logical :: guard_pass_bc
    logical :: guard_pass_flux
    logical :: guard_pass_time
    real(dp) :: dx
    real(dp) :: dt
    integer :: nx_total
    integer :: i

    ok = .false.
    u_next = u
    t_next = t_start
    if (nx < 2 .or. n_step < 1 .or. L <= 0.0_dp) return
    dx = L/real(nx, dp)
    dt = (t_end - t_start)/real(n_step, dp)
    nx_total = nx + 2*ng

    ! step_00: ghost-extended pack; the ghost elements are placeholders.
    u_in(1) = 0.0_dp
    u_in(nx_total) = 0.0_dp
!$omp parallel do schedule(static)
    do i = 1, nx
      u_in(ng + i) = u(i)
    end do
!$omp end parallel do

    ! step_01: periodic ghost fill.
    call dynamics_advection_diffusion_boundary_1d_periodic_copy__apply(nx, ng, u_in, &
      u_out, guard_pass_bc)
    ok = guard_pass_bc
    if (.not. ok) return

    ! step_02: advective and diffusive face fluxes F_{-1/2}..F_{nx-1/2}.
    call dynamics_advdiff_flux_1d_upwind_center2__compute_flux(nx, ng, u_out, a, nu, &
      dx, dt, flux_adv, flux_dif, guard_pass_flux)
    ok = guard_pass_bc .and. guard_pass_flux
    if (.not. ok) return

    ! step_03: tendency L_i = -(F_{i+1/2} - F_{i-1/2})/dx, no limiter.
!$omp parallel do schedule(static)
    do i = 1, nx
      L_flux(i) = -((flux_adv(i + 1) + flux_dif(i + 1)) &
        - (flux_adv(i) + flux_dif(i)))/dx
    end do
!$omp end parallel do

    ! step_04: forward Euler on the interior state.
    call dynamics_advection_diffusion_time_update_1d_euler1__advance(nx, u, L_flux, dt, &
      u_np1, guard_pass_time)
    ok = guard_pass_bc .and. guard_pass_flux .and. guard_pass_time
    if (.not. ok) return

    ! step_05: state commit and time from the step index.
    u_next = u_np1
    if (n == n_step) then
      t_next = t_end
    else
      t_next = t_start + real(n, dp)*dt
    end if
  end subroutine advdiff1d_linear__step

end module advdiff1d_linear_model
