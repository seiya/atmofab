! 2D shallow water equations over bottom topography (problem/shallow_water2d@0.4.7).
! Doubly periodic domain, well-balanced p0 hydrostatic reconstruction (Audusse et al.
! 2004), Rusanov interface flux, periodic ghost copy and an SSPRK2 step composed here
! from two calls of the forward stage update (IR steps 05-22).
! Cell (i, j) of the IR (0-based) is index (i+1, j+1) here; the halo-extended arrays
! run 0..nx+1, 0..ny+1 with one ghost layer (ng = 1).
module shallow_water2d_model
  use, intrinsic :: iso_fortran_env, only: dp => real64, error_unit
  use dynamics_shallow_water_boundary_2d_periodic_copy_model, only: &
    bnd_apply => dynamics_shallow_water_boundary_2d_periodic_copy__apply
  use dynamics_shallow_water_flux_2d_rusanov_p0_model, only: &
    flux_rusanov => dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
  use dynamics_shallow_water_time_update_2d_ssprk2_model, only: &
    ssprk2_advance => dynamics_shallow_water_time_update_2d_ssprk2__advance
  implicit none
  private
  public :: shallow_water2d__step
  public :: shallow_water2d_wave_rate

  integer, parameter :: ncomp_sw = 3
  integer, parameter :: ng_sw = 1

contains

  ! Published operation: one committed step of the certified loop body (issue #589 D3, a hand edit
  ! of the certified `shallow_water2d@0.4.7` model: steps 06-21, the loop and the run reductions
  ! are the host-rendered driver's). `h`, `hu`, `hv` are the driver's work copies.
  subroutine shallow_water2d__step(nx, ny, dx, dy, g, dt, t, z_b, h, hu, hv, ok)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: dx
    real(dp), intent(in) :: dy
    real(dp), intent(in) :: g
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    real(dp), intent(in) :: z_b(nx, ny)
    real(dp), intent(inout) :: h(nx, ny)
    real(dp), intent(inout) :: hu(nx, ny)
    real(dp), intent(inout) :: hv(nx, ny)
    logical, intent(out) :: ok

    real(dp), allocatable :: z_b_ext(:, :)
    real(dp), allocatable :: u_n(:, :, :)
    real(dp), allocatable :: u_1(:, :, :)
    real(dp), allocatable :: u_2(:, :, :)
    real(dp), allocatable :: l_flux(:, :, :)
    real(dp), allocatable :: s_b(:, :, :)
    logical :: rhs_ok
    logical :: guard_pass
    integer :: i, j

    ok = .false.

    ! z_b on the halo-extended grid, ghost layer the periodic image
    allocate(z_b_ext(0:nx + 1, 0:ny + 1))
    z_b_ext = 0.0_dp
    z_b_ext(1:nx, 1:ny) = z_b
    z_b_ext(0, 1:ny) = z_b(nx, :)
    z_b_ext(nx + 1, 1:ny) = z_b(1, :)
    z_b_ext(1:nx, 0) = z_b(:, ny)
    z_b_ext(1:nx, ny + 1) = z_b(:, 1)

    allocate(u_n(ncomp_sw, nx, ny), u_1(ncomp_sw, nx, ny), u_2(ncomp_sw, nx, ny))
    allocate(l_flux(ncomp_sw, nx, ny), s_b(ncomp_sw, nx, ny))

    !$omp parallel do private(i) schedule(static)
    do j = 1, ny
      do i = 1, nx
        u_n(1, i, j) = h(i, j)
        u_n(2, i, j) = hu(i, j)
        u_n(3, i, j) = hv(i, j)
      end do
    end do
    !$omp end parallel do

    call eval_rhs(nx, ny, g, dx, dy, z_b_ext, u_n, l_flux, s_b, rhs_ok)
    if (.not. rhs_ok) return
    call ssprk2_advance(ncomp_sw, nx, ny, u_n, l_flux, s_b, z_b, dt, dx, dy, u_1, guard_pass)
    if (.not. guard_pass) return
    call eval_rhs(nx, ny, g, dx, dy, z_b_ext, u_1, l_flux, s_b, rhs_ok)
    if (.not. rhs_ok) return
    call ssprk2_advance(ncomp_sw, nx, ny, u_1, l_flux, s_b, z_b, dt, dx, dy, u_2, guard_pass)
    if (.not. guard_pass) return

    !$omp parallel do private(i) schedule(static)
    do j = 1, ny
      do i = 1, nx
        h(i, j) = 0.5_dp * u_n(1, i, j) + 0.5_dp * u_2(1, i, j)
        hu(i, j) = 0.5_dp * u_n(2, i, j) + 0.5_dp * u_2(2, i, j)
        hv(i, j) = 0.5_dp * u_n(3, i, j) + 0.5_dp * u_2(3, i, j)
      end do
    end do
    !$omp end parallel do
    ok = .true.
  end subroutine shallow_water2d__step

  ! Right-hand side at the stage state u: periodic ghost copy (dependency), hydrostatic
  ! reconstruction, Rusanov flux (dependency), bed source S_b and flux difference L_flux.
  subroutine eval_rhs(nx, ny, g, dx, dy, z_b_ext, u, l_flux, s_b, rhs_ok)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: g
    real(dp), intent(in) :: dx
    real(dp), intent(in) :: dy
    real(dp), intent(in) :: z_b_ext(0:nx + 1, 0:ny + 1)
    real(dp), intent(in) :: u(ncomp_sw, nx, ny)
    real(dp), intent(out) :: l_flux(ncomp_sw, nx, ny)
    real(dp), intent(out) :: s_b(ncomp_sw, nx, ny)
    logical, intent(out) :: rhs_ok

    real(dp), allocatable :: w_in(:, :)
    real(dp), allocatable :: w_h(:, :)
    real(dp), allocatable :: w_hu(:, :)
    real(dp), allocatable :: w_hv(:, :)
    real(dp), allocatable :: hs_e_l(:, :)
    real(dp), allocatable :: hs_e_r(:, :)
    real(dp), allocatable :: hs_n_b(:, :)
    real(dp), allocatable :: hs_n_t(:, :)
    real(dp), allocatable :: f_e(:, :, :)
    real(dp), allocatable :: g_n(:, :, :)
    real(dp) :: st_l(3), st_r(3), st_b(3), st_t(3), fs(3), gs(3)
    real(dp) :: a_x, a_y, z_face, d_l, d_r, d_b, d_t, cx, cy
    logical :: grid_valid, guard_ok, flux_ok, wet_ok
    integer :: i, j, im, jm, nxt, nyt

    rhs_ok = .true.
    nxt = nx + 2 * ng_sw
    nyt = ny + 2 * ng_sw
    allocate(w_in(0:nx + 1, 0:ny + 1), w_h(0:nx + 1, 0:ny + 1))
    allocate(w_hu(0:nx + 1, 0:ny + 1), w_hv(0:nx + 1, 0:ny + 1))

    ! steps 07-08 / 14-15: interior copy and the periodic ghost copy, per component
    w_in = 0.0_dp
    w_in(1:nx, 1:ny) = u(1, :, :)
    call bnd_apply(nx, ny, ng_sw, nxt, nyt, w_in, w_h, grid_valid)
    if (.not. grid_valid) rhs_ok = .false.
    w_in(1:nx, 1:ny) = u(2, :, :)
    call bnd_apply(nx, ny, ng_sw, nxt, nyt, w_in, w_hu, grid_valid)
    if (.not. grid_valid) rhs_ok = .false.
    w_in(1:nx, 1:ny) = u(3, :, :)
    call bnd_apply(nx, ny, ng_sw, nxt, nyt, w_in, w_hv, grid_valid)
    if (.not. grid_valid) rhs_ok = .false.

    ! steps 09-10 / 16-17: reconstruction at the east and north face of every cell,
    ! wet-domain check, then the interface flux of both faces
    allocate(hs_e_l(nx, ny), hs_e_r(nx, ny), hs_n_b(nx, ny), hs_n_t(nx, ny))
    allocate(f_e(ncomp_sw, nx, ny), g_n(ncomp_sw, nx, ny))
    flux_ok = .true.
    wet_ok = .true.
    !$omp parallel do private(i, z_face, d_l, d_r, d_b, d_t, st_l, st_r, st_b, st_t, &
    !$omp& fs, gs, a_x, a_y, guard_ok) reduction(.and.:flux_ok, wet_ok) schedule(static)
    do j = 1, ny
      do i = 1, nx
        z_face = max(z_b_ext(i, j), z_b_ext(i + 1, j))
        d_l = max(0.0_dp, w_h(i, j) + z_b_ext(i, j) - z_face)
        d_r = max(0.0_dp, w_h(i + 1, j) + z_b_ext(i + 1, j) - z_face)
        z_face = max(z_b_ext(i, j), z_b_ext(i, j + 1))
        d_b = max(0.0_dp, w_h(i, j) + z_b_ext(i, j) - z_face)
        d_t = max(0.0_dp, w_h(i, j + 1) + z_b_ext(i, j + 1) - z_face)
        hs_e_l(i, j) = d_l
        hs_e_r(i, j) = d_r
        hs_n_b(i, j) = d_b
        hs_n_t(i, j) = d_t
        if (d_l <= 0.0_dp .or. d_r <= 0.0_dp .or. d_b <= 0.0_dp .or. d_t <= 0.0_dp) then
          wet_ok = .false.
          f_e(:, i, j) = 0.0_dp
          g_n(:, i, j) = 0.0_dp
        else
          st_l(1) = d_l
          st_l(2) = d_l * (w_hu(i, j) / w_h(i, j))
          st_l(3) = d_l * (w_hv(i, j) / w_h(i, j))
          st_r(1) = d_r
          st_r(2) = d_r * (w_hu(i + 1, j) / w_h(i + 1, j))
          st_r(3) = d_r * (w_hv(i + 1, j) / w_h(i + 1, j))
          st_b(1) = d_b
          st_b(2) = d_b * (w_hu(i, j) / w_h(i, j))
          st_b(3) = d_b * (w_hv(i, j) / w_h(i, j))
          st_t(1) = d_t
          st_t(2) = d_t * (w_hu(i, j + 1) / w_h(i, j + 1))
          st_t(3) = d_t * (w_hv(i, j + 1) / w_h(i, j + 1))
          call flux_rusanov(st_l, st_r, st_b, st_t, g, fs, gs, a_x, a_y, guard_ok)
          f_e(:, i, j) = fs
          g_n(:, i, j) = gs
          flux_ok = flux_ok .and. guard_ok
        end if
      end do
    end do
    !$omp end parallel do
    if (.not. wet_ok) rhs_ok = .false.
    if (.not. flux_ok) rhs_ok = .false.

    ! steps 11-12 / 18-19: bed source and flux difference, periodic-index wrap
    cx = g / (2.0_dp * dx)
    cy = g / (2.0_dp * dy)
    !$omp parallel do private(i, im, jm) schedule(static)
    do j = 1, ny
      jm = j - 1
      if (jm < 1) jm = ny
      do i = 1, nx
        im = i - 1
        if (im < 1) im = nx
        s_b(1, i, j) = 0.0_dp
        s_b(2, i, j) = cx * (hs_e_l(i, j)**2 - hs_e_r(im, j)**2)
        s_b(3, i, j) = cy * (hs_n_b(i, j)**2 - hs_n_t(i, jm)**2)
        l_flux(:, i, j) = -(f_e(:, i, j) - f_e(:, im, j)) / dx &
          - (g_n(:, i, j) - g_n(:, i, jm)) / dy
      end do
    end do
    !$omp end parallel do
  end subroutine eval_rhs

  ! max_ij((|u| + c)/dx + (|v| + c)/dy), c = sqrt(g h); lam0 of rule dt_rule, and
  ! times dt the cfl index of rule cfl_index.
  function shallow_water2d_wave_rate(nx, ny, g, dx, dy, h, hu, hv) result(lam)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: g
    real(dp), intent(in) :: dx
    real(dp), intent(in) :: dy
    real(dp), intent(in) :: h(nx, ny)
    real(dp), intent(in) :: hu(nx, ny)
    real(dp), intent(in) :: hv(nx, ny)
    real(dp) :: lam
    real(dp) :: cw
    integer :: i, j

    lam = 0.0_dp
    !$omp parallel do private(i, cw) reduction(max:lam) schedule(static)
    do j = 1, ny
      do i = 1, nx
        cw = sqrt(g * h(i, j))
        lam = max(lam, (abs(hu(i, j) / h(i, j)) + cw) / dx &
          + (abs(hv(i, j) / h(i, j)) + cw) / dy)
      end do
    end do
    !$omp end parallel do
  end function shallow_water2d_wave_rate

end module shallow_water2d_model
