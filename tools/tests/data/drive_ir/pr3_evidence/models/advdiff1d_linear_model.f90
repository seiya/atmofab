! Model of problem/advdiff1d_linear@0.5.1: ONE step of the 1D linear advection-diffusion
! update, composed from the boundary, flux and time-update components (issue #589 D3, a hand
! edit of the certified `advdiff1d_linear@0.4.5` model: its loop body; the dt rule, the loop and
! the initial state are the host's).
module advdiff1d_linear_model
  use, intrinsic :: iso_fortran_env, only: real64
  use dynamics_advection_diffusion_boundary_1d_periodic_copy_model, only: &
    dynamics_advection_diffusion_boundary_1d_periodic_copy__apply
  use dynamics_advdiff_flux_1d_upwind_center2_model, only: &
    dynamics_advdiff_flux_1d_upwind_center2__compute_flux
  use dynamics_advection_diffusion_time_update_1d_euler1_model, only: &
    dynamics_advection_diffusion_time_update_1d_euler1__advance
  implicit none
  private
  integer, parameter :: dp = real64
  public :: advdiff1d_linear__step

  ! Ghost width, a fixed constant of this node (derived_field_rules ng).
  integer, parameter :: ng = 1

contains

  ! Published operation: u (the driver's work copy of u^n) becomes u^{n+1}. ok is false when a
  ! component guard is false; u is then undefined and the driver does not commit it.
  subroutine advdiff1d_linear__step(nx, dx, a, nu, dt, t, u, ok)
    integer, intent(in) :: nx
    real(dp), intent(in) :: dx
    real(dp), intent(in) :: a
    real(dp), intent(in) :: nu
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    real(dp), intent(inout) :: u(nx)
    logical, intent(out) :: ok

    real(dp) :: u_np1(nx), L_tend(nx)
    real(dp) :: u_ext_in(nx + 2 * ng), u_ext(nx + 2 * ng)
    real(dp) :: flux_adv(nx + 1), flux_dif(nx + 1), F_face(nx + 1)
    logical :: guard_pass_bc, guard_pass_flux, guard_pass_time
    integer :: i, nx_ext, nx_faces

    ok = .false.
    associate (unused_t => t)
    end associate

    nx_ext = nx + 2 * ng
    nx_faces = nx + 1

    ! step_01_pack_ghost: interior copy, ghost elements a defined placeholder.
    !$omp parallel do schedule(static)
    do i = 1, nx
      u_ext_in(ng + i) = u(i)
    end do
    !$omp end parallel do
    u_ext_in(1) = 0.0_dp
    u_ext_in(nx_ext) = 0.0_dp

    ! step_02_boundary_apply: periodic ghost images.
    call dynamics_advection_diffusion_boundary_1d_periodic_copy__apply(nx, ng, &
      u_ext_in, u_ext, guard_pass_bc)
    if (.not. guard_pass_bc) return

    ! step_03_flux_compute: face fluxes, a passed as given.
    call dynamics_advdiff_flux_1d_upwind_center2__compute_flux(nx, ng, u_ext, a, nu, &
      dx, dt, flux_adv, flux_dif, guard_pass_flux)
    if (.not. guard_pass_flux) return

    ! step_04_tendency: total face flux and its divergence.
    !$omp parallel do schedule(static)
    do i = 1, nx_faces
      F_face(i) = flux_adv(i) + flux_dif(i)
    end do
    !$omp end parallel do
    !$omp parallel do schedule(static)
    do i = 1, nx
      L_tend(i) = -(F_face(i + 1) - F_face(i)) / dx
    end do
    !$omp end parallel do

    ! step_05_time_integrate: forward Euler on the nx interior values.
    call dynamics_advection_diffusion_time_update_1d_euler1__advance(nx, u, L_tend, &
      dt, u_np1, guard_pass_time)
    if (.not. guard_pass_time) return

    ! step_06_commit.
    !$omp parallel do schedule(static)
    do i = 1, nx
      u(i) = u_np1(i)
    end do
    !$omp end parallel do
    ok = .true.
  end subroutine advdiff1d_linear__step

end module advdiff1d_linear_model
