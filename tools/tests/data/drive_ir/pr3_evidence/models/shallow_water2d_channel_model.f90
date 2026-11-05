! 2D shallow water channel (problem/shallow_water2d_channel@0.3.1): a hand edit of the certified
! 0.2.5 model (issue #589 D3). The setup is __init; the loop and the run reductions are the
! host-rendered driver's; __step is one committed step of the certified loop body.
! Periodic along the channel, free-slip mirror walls across it, beta-plane Coriolis,
! p1 MUSCL-MC reconstruction, Rusanov flux, classic RK4 (method of lines).
! Setup (IR steps 01-06) is shallow_water2d_channel__init; shallow_water2d_channel__step performs
! one step (IR steps 08, 17), passing channel_rhs (IR steps 09-16) to the time-update component.
module shallow_water2d_channel_model
  use, intrinsic :: iso_fortran_env, only: real64, error_unit
  use dynamics_shallow_water_boundary_2d_channel_mirror_model, only: &
    bnd_apply => dynamics_shallow_water_boundary_2d_channel_mirror__apply
  use dynamics_shallow_water_flux_2d_rusanov_model, only: &
    flux_rusanov => dynamics_shallow_water_flux_2d_rusanov__compute_flux
  use dynamics_shallow_water_reconstruction_2d_muscl_mc_model, only: &
    recon_mc => dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct
  use dynamics_shallow_water_source_2d_coriolis_model, only: &
    cor_apply => dynamics_shallow_water_source_2d_coriolis__apply
  use dynamics_shallow_water_source_2d_tc4_forcing_model, only: &
    tc4_apply => dynamics_shallow_water_source_2d_tc4_forcing__apply
  use dynamics_shallow_water_time_update_2d_rk4_model, only: &
    rk4_advance => dynamics_shallow_water_time_update_2d_rk4__advance
  implicit none
  private
  integer, parameter :: dp = real64
  public :: shallow_water2d_channel__init
  public :: shallow_water2d_channel__step

  real(dp), parameter :: pi = 3.141592653589793238462643383279502884_dp
  integer, parameter :: ncomp_sw = 3
  integer, parameter :: prof_tc2 = 1
  integer, parameter :: prof_tc3 = 2
  integer, parameter :: prof_pert = 3
  integer, parameter :: prof_tc4 = 4

  ! Run-fixed data set once by the setup (IR steps 02-06) and read by channel_rhs.
  logical :: m_rhs_ok = .true.
  logical :: m_is_tc4 = .false.
  logical :: m_odd_hu = .false.
  logical :: m_odd_hv = .true.
  integer :: m_ng = 2
  integer :: m_wall_axis = 2
  integer :: m_along_axis = 1
  real(dp) :: m_g = 0.0_dp
  real(dp) :: m_dx = 0.0_dp
  real(dp) :: m_dy = 0.0_dp
  real(dp) :: m_l_along = 0.0_dp
  real(dp) :: m_psi0 = 0.0_dp
  real(dp) :: m_r_low = 0.0_dp
  real(dp) :: m_c_tr = 0.0_dp
  real(dp) :: m_s_c0 = 0.0_dp
  real(dp) :: m_r_c = 0.0_dp
  real(dp), allocatable :: m_xc(:)
  real(dp), allocatable :: m_yc(:)
  real(dp), allocatable :: m_f(:, :)
  real(dp), allocatable :: m_dfdr(:, :)
  real(dp), allocatable :: m_ubar(:, :)
  real(dp), allocatable :: m_dubar(:, :)
  real(dp), allocatable :: m_hbar(:, :)

contains

  ! IR steps 01-06: validation, grid, Coriolis, initial state, tc4 setup, dt rule.
  subroutine shallow_water2d_channel__init(nx, ny, L_x, L_y, ng, t_start, t_end, &
      cfl_target, dt_scale, g, Omega, a, theta0, along_axis, flow_profile_id, zonal_sign, gh0, &
      eta0, shift_x_fraction, u_jet, h, hu, hv, dt, n_step_plan, init_ok)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: L_x
    real(dp), intent(in) :: L_y
    integer, intent(in) :: ng
    real(dp), intent(in) :: t_start
    real(dp), intent(in) :: t_end
    real(dp), intent(in) :: cfl_target
    real(dp), intent(in) :: dt_scale
    real(dp), intent(in) :: g
    real(dp), intent(in) :: Omega
    real(dp), intent(in) :: a
    real(dp), intent(in) :: theta0
    integer, intent(in) :: along_axis
    integer, intent(in) :: flow_profile_id
    integer, intent(in) :: zonal_sign
    real(dp), intent(in) :: gh0
    real(dp), intent(in) :: eta0
    real(dp), intent(in) :: shift_x_fraction
    real(dp), intent(in) :: u_jet
    real(dp), intent(out) :: h(nx, ny)
    real(dp), intent(out) :: hu(nx, ny)
    real(dp), intent(out) :: hv(nx, ny)
    real(dp), intent(out) :: dt
    integer, intent(out) :: n_step_plan
    logical, intent(out) :: init_ok

    logical :: is_x
    integer :: prof, i, j, k, nr, nq, q
    real(dp) :: sigma, l_s, l_r, f0, beta, h0, u0, uj, kw, dr, hs, rq, acc
    real(dp) :: r_b, r_e, s, r, us, ur, h_tc2, xi, rho2, psi, psi_s, psi_r
    real(dp) :: u_t, v_t, h_t, f_mid, lam0, dt_raw
    real(dp), parameter :: x_e = 0.3_dp
    real(dp), allocatable :: h_line(:), us_line(:)

    dt = 0.0_dp
    n_step_plan = 0
    h = 0.0_dp
    hu = 0.0_dp
    hv = 0.0_dp

    ! --- step 01: setup validation -------------------------------------------------
    init_ok = .true.
    is_x = along_axis == 1
    if (.not. (is_x .or. along_axis == 2)) init_ok = .false.
    prof = flow_profile_id
    select case (prof)
    case (prof_tc2, prof_tc3)
      continue
    case (prof_pert)
      if (.not. (eta0 > 0.0_dp)) init_ok = .false.
      if (.not. (shift_x_fraction >= 0.0_dp .and. shift_x_fraction < 1.0_dp)) &
        init_ok = .false.
    case (prof_tc4)
      if (.not. (u_jet > 0.0_dp)) init_ok = .false.
    case default
      prof = 0
      init_ok = .false.
    end select
    if (abs(zonal_sign) /= 1) init_ok = .false.
    if (.not. (abs(Omega) > 0.0_dp)) init_ok = .false.
    if (.not. (abs(theta0) > 0.0_dp .and. abs(theta0) < 0.5_dp * pi)) init_ok = .false.
    if (ng /= 2) init_ok = .false.
    if (.not. init_ok) then
      write(error_unit, '(a)') 'shallow_water2d_channel: setup validation failed'
      return
    end if

    ! --- step 02: grid ----------------------------------------------------------------
    m_g = g
    m_ng = ng
    m_dx = l_x / real(nx, dp)
    m_dy = l_y / real(ny, dp)
    if (allocated(m_xc)) deallocate(m_xc)
    if (allocated(m_yc)) deallocate(m_yc)
    allocate(m_xc(nx), m_yc(ny))
    do i = 1, nx
      m_xc(i) = (real(i, dp) - 0.5_dp) * m_dx
    end do
    do j = 1, ny
      m_yc(j) = (real(j, dp) - 0.5_dp) * m_dy
    end do
    if (is_x) then
      sigma = 1.0_dp
      l_s = l_x
      l_r = l_y
      m_wall_axis = 2
      m_along_axis = 1
      m_odd_hu = .false.
      m_odd_hv = .true.
      nr = ny
      dr = m_dy
    else
      sigma = -1.0_dp
      l_s = l_y
      l_r = l_x
      m_wall_axis = 1
      m_along_axis = 2
      m_odd_hu = .true.
      m_odd_hv = .false.
      nr = nx
      dr = m_dx
    end if

    ! --- step 03: beta-plane Coriolis parameter -----------------------------------------
    f0 = 2.0_dp * omega * sin(theta0)
    beta = 2.0_dp * omega * cos(theta0) / a
    if (allocated(m_f)) deallocate(m_f)
    allocate(m_f(nx, ny))
    !$omp parallel do private(i, r) schedule(static)
    do j = 1, ny
      do i = 1, nx
        if (is_x) then
          r = m_yc(j)
        else
          r = m_xc(i)
        end if
        m_f(i, j) = f0 + beta * (r - 0.5_dp * l_r)
      end do
    end do
    !$omp end parallel do

    ! --- step 04 / 05: initial state and tc4 setup ---------------------------------------
    h0 = gh0 / g
    u0 = real(zonal_sign, dp) * 2.0_dp * pi * a / (12.0_dp * 86400.0_dp)
    uj = 0.0_dp
    kw = pi / l_r
    m_is_tc4 = prof == prof_tc4
    r_b = l_r / 8.0_dp
    r_e = 7.0_dp * l_r / 8.0_dp

    ! tc3: depth by composite Simpson on a dr/64 sub-grid from 0 to each r_k.
    allocate(h_line(nr), us_line(nr))
    h_line = 0.0_dp
    us_line = 0.0_dp
    if (prof == prof_tc3) then
      hs = dr / 64.0_dp
      !$omp parallel do private(nq, q, rq, acc) schedule(dynamic)
      do k = 1, nr
        nq = 64 * k - 32
        acc = 0.0_dp
        do q = 0, nq
          rq = real(q, dp) * hs
          if (q == 0 .or. q == nq) then
            acc = acc + jet_integrand(rq, f0, beta, l_r, r_b, r_e, u0, x_e)
          else if (mod(q, 2) == 1) then
            acc = acc + 4.0_dp * jet_integrand(rq, f0, beta, l_r, r_b, r_e, u0, x_e)
          else
            acc = acc + 2.0_dp * jet_integrand(rq, f0, beta, l_r, r_b, r_e, u0, x_e)
          end if
        end do
        h_line(k) = h0 - (sigma / g) * (hs / 3.0_dp) * acc
        us_line(k) = jet_speed((real(k, dp) - 0.5_dp) * dr, r_b, r_e, u0, x_e)
      end do
      !$omp end parallel do
    end if

    if (allocated(m_dfdr)) deallocate(m_dfdr)
    if (allocated(m_ubar)) deallocate(m_ubar)
    if (allocated(m_dubar)) deallocate(m_dubar)
    if (allocated(m_hbar)) deallocate(m_hbar)
    allocate(m_dfdr(nx, ny), m_ubar(nx, ny), m_dubar(nx, ny), m_hbar(nx, ny))
    m_dfdr = 0.0_dp
    m_ubar = 0.0_dp
    m_dubar = 0.0_dp
    m_hbar = 0.0_dp
    if (m_is_tc4) then
      uj = real(zonal_sign, dp) * u_jet
      m_psi0 = -0.03_dp * gh0 / f0
      m_r_low = 2.0_dp * a / 12.74244_dp
      m_c_tr = uj * cos(theta0)
      m_s_c0 = 0.5_dp * l_s
      m_r_c = 0.5_dp * l_r
      m_l_along = l_s
    end if
    f_mid = tc4_bigf(0.5_dp * l_r, f0, beta, l_r, kw)

    !$omp parallel do private(i, s, r, us, ur, h_tc2, xi, rho2, psi, psi_s, psi_r, &
    !$omp& u_t, v_t, h_t, k) schedule(static)
    do j = 1, ny
      do i = 1, nx
        if (is_x) then
          s = m_xc(i)
          r = m_yc(j)
          k = j
        else
          s = m_yc(j)
          r = m_xc(i)
          k = i
        end if
        us = u0
        ur = 0.0_dp
        h_tc2 = h0 - sigma * (u0 / g) * (f0 * (r - 0.5_dp * l_r) &
          + 0.5_dp * beta * (r - 0.5_dp * l_r)**2)
        select case (prof)
        case (prof_tc2)
          h(i, j) = h_tc2
        case (prof_tc3)
          h(i, j) = h_line(k)
          us = us_line(k)
        case (prof_pert)
          h(i, j) = h_tc2 + eta0 * sin(2.0_dp * pi * (s / l_s - shift_x_fraction))
        case (prof_tc4)
          m_dfdr(i, j) = beta
          m_ubar(i, j) = uj * sin(kw * r)**2
          m_dubar(i, j) = uj * kw * sin(2.0_dp * kw * r)
          m_hbar(i, j) = h0 - sigma * (uj / g) * (tc4_bigf(r, f0, beta, l_r, kw) - f_mid)
          xi = pi * (s - m_s_c0) / l_s
          rho2 = (l_s / pi)**2 * sin(xi)**2 + (r - m_r_c)**2
          psi = m_psi0 * exp(-rho2 / m_r_low**2)
          psi_s = -(l_s / (pi * m_r_low**2)) * sin(2.0_dp * xi) * psi
          psi_r = -2.0_dp * (r - m_r_c) / m_r_low**2 * psi
          h_t = m_hbar(i, j) + m_f(i, j) * psi / g
          if (is_x) then
            u_t = m_ubar(i, j) - psi_r
            v_t = psi_s
          else
            u_t = -psi_s
            v_t = m_ubar(i, j) + psi_r
          end if
          h(i, j) = h_t
        case default
          h(i, j) = 0.0_dp
        end select
        if (prof == prof_tc4) then
          hu(i, j) = h(i, j) * u_t
          hv(i, j) = h(i, j) * v_t
        else if (is_x) then
          hu(i, j) = h(i, j) * us
          hv(i, j) = h(i, j) * ur
        else
          hu(i, j) = h(i, j) * ur
          hv(i, j) = h(i, j) * us
        end if
      end do
    end do
    !$omp end parallel do

    if (any(h <= 0.0_dp)) then
      init_ok = .false.
      write(error_unit, '(a)') 'shallow_water2d_channel: initial depth not positive'
      return
    end if

    ! --- step 06: dt rule (cfl_ceil) ---------------------------------------------------
    lam0 = stab_rate(nx, ny, h, hu, hv)
    dt_raw = dt_scale * cfl_target / lam0
    n_step_plan = ceiling((t_end - t_start) / dt_raw)
    dt = (t_end - t_start) / real(n_step_plan, dp)
  end subroutine shallow_water2d_channel__init

  ! IR steps 08 and 17: one RK4 step of the state at the time t of its start. A false result leaves
  ! the state as received.
  subroutine shallow_water2d_channel__step(nx, ny, dt, t, h, hu, hv, ok)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: dt
    real(dp), intent(in) :: t
    real(dp), intent(inout) :: h(nx, ny)
    real(dp), intent(inout) :: hu(nx, ny)
    real(dp), intent(inout) :: hv(nx, ny)
    logical, intent(out) :: ok

    real(dp), allocatable :: u_n(:, :, :)
    real(dp), allocatable :: u_np1(:, :, :)
    logical :: guard_pass

    ok = .false.
    allocate(u_n(ncomp_sw, nx, ny), u_np1(ncomp_sw, nx, ny))
    u_n(1, :, :) = h
    u_n(2, :, :) = hu
    u_n(3, :, :) = hv
    m_rhs_ok = .true.
    call rk4_advance(ncomp_sw, nx, ny, u_n, channel_rhs, t, dt, u_np1, guard_pass)
    if (.not. (guard_pass .and. m_rhs_ok)) then
      write(error_unit, '(a, es24.16)') 'shallow_water2d_channel: step failed at t = ', t
      return
    end if
    h = u_np1(1, :, :)
    hu = u_np1(2, :, :)
    hv = u_np1(3, :, :)
    ok = .true.
  end subroutine shallow_water2d_channel__step

  ! IR steps 09-16: the tendency of the stage state U at the stage time t.
  subroutine channel_rhs(ncomp, nx, ny, t, u, dudt)
    integer, intent(in) :: ncomp
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: t
    real(dp), intent(in) :: u(ncomp, nx, ny)
    real(dp), intent(out) :: dudt(ncomp, nx, ny)

    real(dp), allocatable :: w_in(:, :), w_h(:, :), w_hu(:, :), w_hv(:, :)
    real(dp), allocatable :: ul_h(:, :), ur_h(:, :), ub_h(:, :), ut_h(:, :)
    real(dp), allocatable :: ul_hu(:, :), ur_hu(:, :), ub_hu(:, :), ut_hu(:, :)
    real(dp), allocatable :: ul_hv(:, :), ur_hv(:, :), ub_hv(:, :), ut_hv(:, :)
    real(dp), allocatable :: f_star(:, :, :), g_star(:, :, :)
    real(dp), allocatable :: l_flux(:, :, :), s_cor(:, :, :), q_force(:, :, :)
    real(dp) :: st_l(3), st_r(3), fs3(3), gs3(3), a_x, a_y
    logical :: grid_valid, guard_ok, flux_ok, cor_guard, frc_guard
    integer :: i, j, m, c, ng, nxt, nyt

    ! step 09: wet-domain check before anything reaches the flux component
    if (any(u(1, :, :) <= 0.0_dp)) then
      m_rhs_ok = .false.
      dudt = 0.0_dp
      return
    end if

    ! step 10: ghost-extended work arrays, ghosts filled by the boundary component
    ng = m_ng
    nxt = nx + 2 * ng
    nyt = ny + 2 * ng
    allocate(w_in(nxt, nyt), w_h(nxt, nyt), w_hu(nxt, nyt), w_hv(nxt, nyt))
    w_in = 0.0_dp
    w_in(ng + 1:ng + nx, ng + 1:ng + ny) = u(1, :, :)
    call bnd_apply(nx, ny, ng, nxt, nyt, m_wall_axis, .false., w_in, w_h, grid_valid)
    if (.not. grid_valid) m_rhs_ok = .false.
    w_in(ng + 1:ng + nx, ng + 1:ng + ny) = u(2, :, :)
    call bnd_apply(nx, ny, ng, nxt, nyt, m_wall_axis, m_odd_hu, w_in, w_hu, grid_valid)
    if (.not. grid_valid) m_rhs_ok = .false.
    w_in(ng + 1:ng + nx, ng + 1:ng + ny) = u(3, :, :)
    call bnd_apply(nx, ny, ng, nxt, nyt, m_wall_axis, m_odd_hv, w_in, w_hv, grid_valid)
    if (.not. grid_valid) m_rhs_ok = .false.

    ! step 11: MUSCL-MC reconstruction of each conserved component
    allocate(ul_h(nx + 1, ny), ur_h(nx + 1, ny), ub_h(nx, ny + 1), ut_h(nx, ny + 1))
    allocate(ul_hu(nx + 1, ny), ur_hu(nx + 1, ny), ub_hu(nx, ny + 1), ut_hu(nx, ny + 1))
    allocate(ul_hv(nx + 1, ny), ur_hv(nx + 1, ny), ub_hv(nx, ny + 1), ut_hv(nx, ny + 1))
    call recon_mc(nx, ny, ng, nxt, nyt, w_h, ul_h, ur_h, ub_h, ut_h, grid_valid)
    if (.not. grid_valid) m_rhs_ok = .false.
    call recon_mc(nx, ny, ng, nxt, nyt, w_hu, ul_hu, ur_hu, ub_hu, ut_hu, grid_valid)
    if (.not. grid_valid) m_rhs_ok = .false.
    call recon_mc(nx, ny, ng, nxt, nyt, w_hv, ul_hv, ur_hv, ub_hv, ut_hv, grid_valid)
    if (.not. grid_valid) m_rhs_ok = .false.

    ! step 12: Rusanov flux at every x interface (m, j) and y interface (i, m)
    allocate(f_star(ncomp_sw, nx + 1, ny), g_star(ncomp_sw, nx, ny + 1))
    flux_ok = .true.
    !$omp parallel do private(m, st_l, st_r, fs3, gs3, a_x, a_y, guard_ok) &
    !$omp& reduction(.and.:flux_ok) schedule(static)
    do j = 1, ny
      do m = 1, nx + 1
        st_l = [ul_h(m, j), ul_hu(m, j), ul_hv(m, j)]
        st_r = [ur_h(m, j), ur_hu(m, j), ur_hv(m, j)]
        call flux_rusanov(st_l, st_r, st_l, st_r, m_g, fs3, gs3, a_x, a_y, guard_ok)
        f_star(:, m, j) = fs3
        flux_ok = flux_ok .and. guard_ok
      end do
    end do
    !$omp end parallel do
    !$omp parallel do private(i, st_l, st_r, fs3, gs3, a_x, a_y, guard_ok) &
    !$omp& reduction(.and.:flux_ok) schedule(static)
    do m = 1, ny + 1
      do i = 1, nx
        st_l = [ub_h(i, m), ub_hu(i, m), ub_hv(i, m)]
        st_r = [ut_h(i, m), ut_hu(i, m), ut_hv(i, m)]
        call flux_rusanov(st_l, st_r, st_l, st_r, m_g, fs3, gs3, a_x, a_y, guard_ok)
        g_star(:, i, m) = gs3
        flux_ok = flux_ok .and. guard_ok
      end do
    end do
    !$omp end parallel do
    if (.not. flux_ok) m_rhs_ok = .false.

    ! step 13: flux difference on every interior cell
    allocate(l_flux(ncomp, nx, ny), s_cor(ncomp, nx, ny), q_force(ncomp, nx, ny))
    !$omp parallel do private(i, c) schedule(static)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          l_flux(c, i, j) = -(f_star(c, i + 1, j) - f_star(c, i, j)) / m_dx &
            - (g_star(c, i, j + 1) - g_star(c, i, j)) / m_dy
        end do
      end do
    end do
    !$omp end parallel do

    ! step 14: Coriolis source at the stage state
    call cor_apply(ncomp, nx, ny, m_f, u, s_cor, cor_guard)
    if (.not. cor_guard) m_rhs_ok = .false.

    ! step 15: tc4 forcing at the stage time (tc4_translating_low only)
    if (m_is_tc4) then
      call tc4_apply(ncomp, nx, ny, m_along_axis, m_xc, m_yc, t, m_f, m_dfdr, m_ubar, &
        m_dubar, m_hbar, m_g, m_l_along, m_psi0, m_r_low, m_c_tr, m_s_c0, m_r_c, &
        q_force, frc_guard)
      if (.not. frc_guard) m_rhs_ok = .false.
    else
      q_force = 0.0_dp
    end if

    ! step 16: tendency assembly
    !$omp parallel do private(i, c) schedule(static)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          dudt(c, i, j) = l_flux(c, i, j) + s_cor(c, i, j) + q_force(c, i, j)
        end do
      end do
    end do
    !$omp end parallel do
  end subroutine channel_rhs

  ! max_ij((|u| + c)/dx + (|v| + c)/dy); times dt it is the stability index (cfl_index).
  function stab_rate(nx, ny, h, hu, hv) result(lam)
    integer, intent(in) :: nx
    integer, intent(in) :: ny
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
        cw = sqrt(m_g * h(i, j))
        lam = max(lam, (abs(hu(i, j) / h(i, j)) + cw) / m_dx &
          + (abs(hv(i, j) / h(i, j)) + cw) / m_dy)
      end do
    end do
    !$omp end parallel do
  end function stab_rate

  ! tc3 jet speed u_s(r) with the branch-free b(z) = exp(-1/max(z, 1e-300)).
  pure function jet_speed(r, r_b, r_e, u0, x_e) result(us)
    real(dp), intent(in) :: r
    real(dp), intent(in) :: r_b
    real(dp), intent(in) :: r_e
    real(dp), intent(in) :: u0
    real(dp), intent(in) :: x_e
    real(dp) :: us
    real(dp) :: zeta

    zeta = x_e * (r - r_b) / (r_e - r_b)
    us = u0 * exp(-1.0_dp / max(zeta, 1.0e-300_dp)) &
      * exp(-1.0_dp / max(x_e - zeta, 1.0e-300_dp)) * exp(4.0_dp / x_e)
  end function jet_speed

  ! tc3 integrand f(r) u_s(r) of the depth quadrature.
  pure function jet_integrand(r, f0, beta, l_r, r_b, r_e, u0, x_e) result(val)
    real(dp), intent(in) :: r
    real(dp), intent(in) :: f0
    real(dp), intent(in) :: beta
    real(dp), intent(in) :: l_r
    real(dp), intent(in) :: r_b
    real(dp), intent(in) :: r_e
    real(dp), intent(in) :: u0
    real(dp), intent(in) :: x_e
    real(dp) :: val

    val = (f0 + beta * (r - 0.5_dp * l_r)) * jet_speed(r, r_b, r_e, u0, x_e)
  end function jet_integrand

  ! tc4 background primitive F(r) of the hbar rule.
  pure function tc4_bigf(r, f0, beta, l_r, kw) result(val)
    real(dp), intent(in) :: r
    real(dp), intent(in) :: f0
    real(dp), intent(in) :: beta
    real(dp), intent(in) :: l_r
    real(dp), intent(in) :: kw
    real(dp) :: val

    val = f0 * (0.5_dp * r - sin(2.0_dp * kw * r) / (4.0_dp * kw)) &
      + beta * (0.25_dp * r**2 - 0.25_dp * l_r * r &
      - (r - 0.5_dp * l_r) * sin(2.0_dp * kw * r) / (4.0_dp * kw) &
      - (cos(2.0_dp * kw * r) - 1.0_dp) / (8.0_dp * kw**2))
  end function tc4_bigf

end module shallow_water2d_channel_model
