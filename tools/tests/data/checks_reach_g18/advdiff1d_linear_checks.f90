!> advdiff1d_linear checks: case setup / run driving and honest diagnostics.
!> The bound snapshot state is the module variable u (captured by the host).
module advdiff1d_linear_checks
  use, intrinsic :: iso_fortran_env, only: dp => real64
  use advdiff1d_linear_model, only: advdiff1d_linear__step
  implicit none
  private
  public :: u
  public :: case_setup
  public :: case_run
  public :: get_time
  public :: checks_compute
  public :: metric_compute

  !> Bound snapshot state (state_bindings: u), shape [nx], cell centres.
  real(dp), allocatable :: u(:)

  real(dp), parameter :: two_pi = 6.283185307179586_dp
  real(dp), parameter :: four_pi = 12.566370614359172_dp
  real(dp), parameter :: cfl_adv = 0.6_dp
  real(dp), parameter :: cfl_dif = 0.25_dp

  character(len=*), parameter :: id_guard = 'advdiff1d_guard_nx064_shift000_dts120'
  character(len=*), parameter :: id_ref064 = 'advdiff1d_ref_nx064_shift000_dts100'
  character(len=*), parameter :: id_ref128 = 'advdiff1d_ref_nx128_shift000_dts100'
  character(len=*), parameter :: id_ref128_long = &
    'advdiff1d_ref_nx128_shift000_dts100_tend200'
  character(len=*), parameter :: id_ref256 = 'advdiff1d_ref_nx256_shift000_dts100'
  character(len=*), parameter :: id_sym = 'advdiff1d_sym_nx128_shift025_dts100'

  ! Current-case parameters and state (reset by case_setup).
  real(dp) :: t = 0.0_dp
  real(dp), allocatable :: u_init(:)
  integer :: cur_nx = 0
  integer :: cur_n_step = 0
  real(dp) :: cur_len = 1.0_dp
  real(dp) :: cur_a = 1.0_dp
  real(dp) :: cur_nu = 1.0e-2_dp
  real(dp) :: cur_t_start = 0.0_dp
  real(dp) :: cur_t_end = 0.5_dp
  real(dp) :: cur_dt = 0.0_dp
  real(dp) :: cur_shift = 0.0_dp
  logical :: cur_setup_ok = .false.
  logical :: cur_run_ok = .false.

  ! Cross-case accumulators keyed by case_id (sorted run order).
  logical :: have_e064 = .false.
  logical :: have_e128 = .false.
  logical :: have_ref128 = .false.
  real(dp) :: e064 = 0.0_dp
  real(dp) :: e128 = 0.0_dp
  real(dp), allocatable :: u_ref128(:)

contains

  subroutine case_setup(case_id, ok)
    character(len=*), intent(in) :: case_id
    logical, intent(out) :: ok
    logical :: known
    real(dp) :: dts
    real(dp) :: dx
    real(dp) :: dt_raw
    real(dp) :: xi
    real(dp) :: arg
    integer :: i

    known = .true.
    cur_len = 1.0_dp
    cur_a = 1.0_dp
    cur_nu = 1.0e-2_dp
    cur_t_start = 0.0_dp
    cur_t_end = 0.5_dp
    cur_shift = 0.0_dp
    dts = 1.0_dp
    cur_nx = 0
    select case (case_id)
    case (id_guard)
      cur_nx = 64
      dts = 1.2_dp
    case (id_ref064)
      cur_nx = 64
    case (id_ref128)
      cur_nx = 128
    case (id_ref128_long)
      cur_nx = 128
      cur_t_end = 2.0_dp
    case (id_ref256)
      cur_nx = 256
    case (id_sym)
      cur_nx = 128
      cur_shift = 0.25_dp
    case default
      known = .false.
    end select

    cur_setup_ok = .false.
    cur_run_ok = .false.
    cur_dt = 0.0_dp
    cur_n_step = 0
    t = cur_t_start
    if (allocated(u)) deallocate(u)
    if (allocated(u_init)) deallocate(u_init)
    allocate(u(max(cur_nx, 1)))
    allocate(u_init(max(cur_nx, 1)))
    u = 0.0_dp
    u_init = 0.0_dp
    ok = known .and. cur_nx >= 2 .and. cur_a > 0.0_dp .and. cur_nu > 0.0_dp &
      .and. cur_len > 0.0_dp .and. cur_t_end > cur_t_start
    if (.not. ok) then
      cur_nx = size(u)
      return
    end if

    dx = cur_len/real(cur_nx, dp)
    dt_raw = dts*min(cfl_adv*dx/cur_a, cfl_dif*dx**2/cur_nu)
    cur_n_step = ceiling((cur_t_end - cur_t_start)/dt_raw)
    cur_dt = (cur_t_end - cur_t_start)/real(cur_n_step, dp)

    do i = 1, cur_nx
      xi = (real(i - 1, dp) + 0.5_dp)*dx
      arg = xi/cur_len - cur_shift
      u(i) = sin(two_pi*arg) + 0.5_dp*sin(four_pi*arg)
    end do
    u_init = u
    cur_setup_ok = .true.
  end subroutine case_setup

  subroutine case_run(case_id, steps, cells_updated, ok)
    character(len=*), intent(in) :: case_id
    integer, intent(out) :: steps
    integer, intent(out) :: cells_updated
    logical, intent(out) :: ok
    real(dp), allocatable :: u_work(:)
    real(dp) :: t_new
    logical :: step_ok
    integer :: n

    steps = 0
    cells_updated = 0
    ok = .false.
    cur_run_ok = .false.
    if (.not. cur_setup_ok) return

    allocate(u_work(cur_nx))
    ok = .true.
    do n = 1, cur_n_step
      call advdiff1d_linear__step(cur_nx, cur_len, cur_a, cur_nu, cur_t_start, &
        cur_t_end, cur_n_step, n, u, u_work, t_new, step_ok)
      if (.not. step_ok) then
        ok = .false.
        exit
      end if
      u = u_work
      t = t_new
      steps = n
    end do
    cells_updated = steps*cur_nx
    cur_run_ok = ok

    if (ok) then
      select case (case_id)
      case (id_ref064)
        e064 = l2_rel_analytic()
        have_e064 = .true.
      case (id_ref128)
        e128 = l2_rel_analytic()
        have_e128 = .true.
        if (allocated(u_ref128)) deallocate(u_ref128)
        allocate(u_ref128(cur_nx))
        u_ref128 = u
        have_ref128 = .true.
      case default
        ! no cross-case accumulation for this case
      end select
    end if
  end subroutine case_run

  subroutine get_time(tval)
    real(dp), intent(out) :: tval
    tval = t
  end subroutine get_time

  subroutine checks_compute(case_id, check_id, status)
    character(len=*), intent(in) :: case_id
    character(len=*), intent(in) :: check_id
    character(len=4), intent(out) :: status
    associate (unused_case_id => case_id); end associate
    status = 'na  '
    select case (check_id)
    case ('cfl')
      if (cur_setup_ok) then
        if (cfl_combined() > 1.0_dp) then
          status = 'fail'
        else
          status = 'pass'
        end if
      end if
    case default
      status = 'na  '
    end select
  end subroutine checks_compute

  subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
    character(len=*), intent(in) :: case_id
    character(len=*), intent(in) :: name
    real(dp), intent(out) :: val
    logical, intent(out) :: is_na
    character(len=:), allocatable, intent(out) :: reason_na
    logical, intent(out) :: found
    logical :: state_ok
    real(dp) :: dx
    real(dp) :: e_f

    val = 0.0_dp
    is_na = .false.
    reason_na = ''
    found = .false.
    state_ok = cur_setup_ok .and. cur_run_ok
    dx = cur_len/real(max(cur_nx, 1), dp)
    select case (name)
    case ('cfl.combined_max')
      found = .true.
      if (cur_setup_ok) then
        val = cfl_combined()
      else
        is_na = .true.
        reason_na = 'case setup rejected the inputs'
      end if
    case ('conserved.mass.initial')
      found = .true.
      if (cur_setup_ok) then
        val = sum(u_init)*dx
      else
        is_na = .true.
        reason_na = 'case setup rejected the inputs'
      end if
    case ('conserved.mass.abs_initial')
      found = .true.
      if (cur_setup_ok) then
        val = sum(abs(u_init))*dx
      else
        is_na = .true.
        reason_na = 'case setup rejected the inputs'
      end if
    case ('conserved.mass.final')
      found = .true.
      if (state_ok) then
        val = sum(u)*dx
      else
        is_na = .true.
        reason_na = 'case run did not complete'
      end if
    case ('metrics.mass_drift_rel')
      found = .true.
      if (state_ok) then
        val = abs(sum(u)*dx - sum(u_init)*dx) &
          /max(sum(abs(u_init))*dx, 1.0e-14_dp)
      else
        is_na = .true.
        reason_na = 'case run did not complete'
      end if
    case ('errors.analytic.l2_rel_tend')
      found = .true.
      if (state_ok) then
        val = l2_rel_analytic()
      else
        is_na = .true.
        reason_na = 'case run did not complete'
      end if
    case ('errors.mode_scheme_fidelity')
      found = .true.
      if (state_ok) then
        val = mode_fidelity()
      else
        is_na = .true.
        reason_na = 'case run did not complete'
      end if
    case ('errors.symmetry_l2_rel')
      if (case_id == id_sym) then
        found = .true.
        if (state_ok .and. have_ref128) then
          val = symmetry_rel()
        else
          is_na = .true.
          reason_na = 'reference or shifted state unavailable'
        end if
      end if
    case ('convergence.nx64_to_nx128.l2_order')
      if (case_id == id_ref128) then
        found = .true.
        e_f = 0.0_dp
        if (state_ok) e_f = l2_rel_analytic()
        if (have_e064 .and. e_f > 0.0_dp .and. e064 > 0.0_dp) then
          val = log(e064/e_f)/log(2.0_dp)
        else
          is_na = .true.
          reason_na = 'coarse or fine error unavailable'
        end if
      end if
    case ('convergence.nx128_to_nx256.l2_order')
      if (case_id == id_ref256) then
        found = .true.
        e_f = 0.0_dp
        if (state_ok) e_f = l2_rel_analytic()
        if (have_e128 .and. e_f > 0.0_dp .and. e128 > 0.0_dp) then
          val = log(e128/e_f)/log(2.0_dp)
        else
          is_na = .true.
          reason_na = 'coarse or fine error unavailable'
        end if
      end if
    case default
      found = .false.
    end select
  end subroutine metric_compute

  !> C + 2D of the case (constant dt, so equal to its maximum over steps).
  function cfl_combined() result(cfl)
    real(dp) :: cfl
    real(dp) :: dx
    dx = cur_len/real(cur_nx, dp)
    cfl = cur_a*cur_dt/dx + 2.0_dp*cur_nu*cur_dt/dx**2
  end function cfl_combined

  !> Relative l2 error of the current state against the analytic solution at t_end.
  function l2_rel_analytic() result(err)
    real(dp) :: err
    real(dp) :: dx
    real(dp) :: xi
    real(dp) :: k1
    real(dp) :: k2
    real(dp) :: tt
    real(dp) :: s
    real(dp) :: ue
    real(dp) :: num
    real(dp) :: den
    integer :: i
    dx = cur_len/real(cur_nx, dp)
    k1 = two_pi/cur_len
    k2 = four_pi/cur_len
    tt = cur_t_end
    s = cur_shift*cur_len
    num = 0.0_dp
    den = 0.0_dp
    do i = 1, cur_nx
      xi = (real(i - 1, dp) + 0.5_dp)*dx
      ue = exp(-cur_nu*k1**2*tt)*sin(k1*(xi - cur_a*tt - s)) &
        + 0.5_dp*exp(-cur_nu*k2**2*tt)*sin(k2*(xi - cur_a*tt - s))
      num = num + (u(i) - ue)**2
      den = den + ue**2
    end do
    err = sqrt(num)/max(sqrt(den), tiny(1.0_dp))
  end function l2_rel_analytic

  !> max over m in {1,2} of |uhat_m(t_end) - G_num(m)**n_step*uhat_m(0)|/|uhat_m(0)|.
  function mode_fidelity() result(fid)
    real(dp) :: fid
    real(dp) :: dx
    real(dp) :: c_num
    real(dp) :: d_num
    real(dp) :: th
    real(dp) :: km
    real(dp) :: gr
    real(dp) :: gi
    real(dp) :: rmod
    real(dp) :: ph
    real(dp) :: pr
    real(dp) :: pim
    real(dp) :: b0r
    real(dp) :: b0i
    real(dp) :: b1r
    real(dp) :: b1i
    real(dp) :: dr
    real(dp) :: di
    real(dp) :: f
    integer :: m
    dx = cur_len/real(cur_nx, dp)
    c_num = cur_a*cur_dt/dx
    d_num = cur_nu*cur_dt/dx**2
    fid = 0.0_dp
    do m = 1, 2
      th = two_pi*real(m, dp)/real(cur_nx, dp)
      km = two_pi*real(m, dp)/cur_len
      gr = 1.0_dp - (c_num + 2.0_dp*d_num)*(1.0_dp - cos(th))
      gi = -c_num*sin(th)
      rmod = (gr**2 + gi**2)**(0.5_dp*real(cur_n_step, dp))
      ph = real(cur_n_step, dp)*atan2(gi, gr)
      pr = rmod*cos(ph)
      pim = rmod*sin(ph)
      call mode_coef(u_init, km, b0r, b0i)
      call mode_coef(u, km, b1r, b1i)
      dr = b1r - (pr*b0r - pim*b0i)
      di = b1i - (pr*b0i + pim*b0r)
      f = sqrt(dr**2 + di**2)/max(sqrt(b0r**2 + b0i**2), tiny(1.0_dp))
      fid = max(fid, f)
    end do
  end function mode_fidelity

  !> Discrete Fourier coefficient sum_i v_i*exp(-i*km*x_i) at the cell centres.
  subroutine mode_coef(v, km, cr, ci)
    real(dp), intent(in) :: v(:)
    real(dp), intent(in) :: km
    real(dp), intent(out) :: cr
    real(dp), intent(out) :: ci
    real(dp) :: dx
    real(dp) :: xi
    integer :: i
    dx = cur_len/real(size(v), dp)
    cr = 0.0_dp
    ci = 0.0_dp
    do i = 1, size(v)
      xi = (real(i - 1, dp) + 0.5_dp)*dx
      cr = cr + v(i)*cos(km*xi)
      ci = ci - v(i)*sin(km*xi)
    end do
  end subroutine mode_coef

  !> ||u_shifted - shift(u_ref, +delta_s)||_2 / ||u_ref||_2, delta_s/dx cells.
  function symmetry_rel() result(rel)
    real(dp) :: rel
    real(dp) :: num
    real(dp) :: den
    integer :: j
    integer :: src
    integer :: nshift
    integer :: nref
    nref = size(u_ref128)
    nshift = nint(cur_shift*real(cur_nx, dp))
    num = 0.0_dp
    den = 0.0_dp
    do j = 1, size(u)
      src = modulo(j - 1 - nshift, nref) + 1
      num = num + (u(j) - u_ref128(src))**2
    end do
    do j = 1, nref
      den = den + u_ref128(j)**2
    end do
    rel = sqrt(num)/max(sqrt(den), tiny(1.0_dp))
  end function symmetry_rel

end module advdiff1d_linear_checks
