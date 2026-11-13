! Issue #632 P4: a HAND-WRITTEN composing model of `problem/shallow_water2d_channel_subcycled`
! (Controlled Spec §5), over the hand-written member `../pr3_evidence/models/
! shallow_water2d_channel_model.f90`. It is the bench's subject, not an output of `Generate`.
! The composite's only loop is the sequence of sub-steps, which carries a dependence, so the
! plan a generated model of this node declares is `parallelization.model: none`.
module shallow_water2d_channel_subcycled_model
  use, intrinsic :: iso_fortran_env, only: real64
  use shallow_water2d_channel_model, only: shallow_water2d_channel__init, &
    shallow_water2d_channel__step
  implicit none
  private
  integer, parameter :: dp = real64
  public :: shallow_water2d_channel_subcycled__init, shallow_water2d_channel_subcycled__step
contains
  subroutine shallow_water2d_channel_subcycled__init(nx, ny, nx_g, ny_g, L_x, L_y, ng, i0, j0, &
      t_start, t_end, cfl_target, dt_scale, g, Omega, a, theta0, along_axis, flow_profile_id, &
      zonal_sign, gh0, eta0, shift_x_fraction, u_jet, h, hu, hv, dt, n_step_plan, init_ok)
    integer, intent(in) :: nx, ny, nx_g, ny_g, ng, i0, j0
    real(dp), intent(in) :: L_x, L_y, t_start, t_end, cfl_target, dt_scale, g, Omega, a, theta0
    integer, intent(in) :: along_axis, flow_profile_id, zonal_sign
    real(dp), intent(in) :: gh0, eta0, shift_x_fraction, u_jet
    real(dp), intent(out) :: h(nx, ny), hu(nx, ny), hv(nx, ny), dt
    integer, intent(out) :: n_step_plan
    logical, intent(out) :: init_ok
    call shallow_water2d_channel__init(nx, ny, nx_g, ny_g, L_x, L_y, ng, i0, j0, t_start, &
      t_end, cfl_target, dt_scale, g, Omega, a, theta0, along_axis, flow_profile_id, &
      zonal_sign, gh0, eta0, shift_x_fraction, u_jet, h, hu, hv, dt, n_step_plan, init_ok)
  end subroutine shallow_water2d_channel_subcycled__init

  subroutine shallow_water2d_channel_subcycled__step(nx, ny, dt, t, substeps, h, hu, hv, ok)
    integer, intent(in) :: nx, ny, substeps
    real(dp), intent(in) :: dt, t
    real(dp), intent(inout) :: h(nx, ny), hu(nx, ny), hv(nx, ny)
    logical, intent(out) :: ok
    logical :: member_ok
    integer :: j
    real(dp) :: dts
    ok = .false.
    if (substeps < 1) return
    dts = dt / real(substeps, dp)
    do j = 1, substeps
      call shallow_water2d_channel__step(nx, ny, dts, t + real(j - 1, dp) * dts, h, hu, hv, &
        member_ok)
      ok = member_ok
      if (.not. ok) return
    end do
  end subroutine shallow_water2d_channel_subcycled__step
end module shallow_water2d_channel_subcycled_model
