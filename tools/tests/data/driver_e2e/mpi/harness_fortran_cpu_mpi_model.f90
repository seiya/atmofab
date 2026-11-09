! Runner plumbing and distributed-state operations of harness_fortran_cpu_mpi.
! This module is the only code of the node that uses the message-passing library.
module harness_fortran_cpu_mpi_model
  use, intrinsic :: iso_fortran_env, only: real64, error_unit
  use, intrinsic :: ieee_arithmetic, only: ieee_is_nan, ieee_is_finite
  use, intrinsic :: iso_c_binding, only: c_int, c_char, c_null_char, c_ptr, c_loc
  use mpi_f08, only: MPI_Init, MPI_Finalize, MPI_Comm_size, MPI_Comm_rank, MPI_Sendrecv, &
    MPI_Allreduce, MPI_Allgather, MPI_Gatherv, MPI_COMM_WORLD, MPI_DOUBLE_PRECISION, &
    MPI_INTEGER, MPI_SUM, MPI_MAX, MPI_MIN, MPI_PROC_NULL, MPI_STATUS_IGNORE
  implicit none
  private

  public :: harness_fortran_cpu_mpi__get_array_r1
  public :: harness_fortran_cpu_mpi__get_array_r2
  public :: harness_fortran_cpu_mpi__get_array_r3
  public :: harness_fortran_cpu_mpi__get_array_r4
  public :: harness_fortran_cpu_mpi__get_bool
  public :: harness_fortran_cpu_mpi__get_int
  public :: harness_fortran_cpu_mpi__get_real
  public :: harness_fortran_cpu_mpi__read_case
  public :: dp, case_id_len
  public :: harness_fortran_cpu_mpi__h_named, harness_fortran_cpu_mpi__h_check
  public :: harness_fortran_cpu_mpi__h_metric, harness_fortran_cpu_mpi__h_case_result
  public :: harness_fortran_cpu_mpi__h_mb_entry
  public :: harness_fortran_cpu_mpi__parse_cases
  public :: harness_fortran_cpu_mpi__emit_real, harness_fortran_cpu_mpi__emit_int
  public :: harness_fortran_cpu_mpi__emit_bool
  public :: harness_fortran_cpu_mpi__emit_array_r1, harness_fortran_cpu_mpi__emit_array_r2
  public :: harness_fortran_cpu_mpi__emit_array_r3, harness_fortran_cpu_mpi__emit_array_r4
  public :: harness_fortran_cpu_mpi__box
  public :: harness_fortran_cpu_mpi__write_snapshot, harness_fortran_cpu_mpi__write_metrics_basis
  public :: harness_fortran_cpu_mpi__write_diagnostics, harness_fortran_cpu_mpi__write_perf
  public :: harness_fortran_cpu_mpi__init, harness_fortran_cpu_mpi__finalize
  public :: harness_fortran_cpu_mpi__comm_rank, harness_fortran_cpu_mpi__comm_size
  public :: harness_fortran_cpu_mpi__grid_dims, harness_fortran_cpu_mpi__grid_coord
  public :: harness_fortran_cpu_mpi__partition
  public :: harness_fortran_cpu_mpi__exchange_halo_r1, harness_fortran_cpu_mpi__exchange_halo_r2
  public :: harness_fortran_cpu_mpi__gather_r1, harness_fortran_cpu_mpi__gather_r2
  public :: harness_fortran_cpu_mpi__gather_r3, harness_fortran_cpu_mpi__gather_r4
  public :: harness_fortran_cpu_mpi__reduce_sum, harness_fortran_cpu_mpi__reduce_max
  public :: harness_fortran_cpu_mpi__reduce_min, harness_fortran_cpu_mpi__reduce_sum_int

  integer, parameter :: dp = real64
  integer, parameter :: case_id_len = 64

  type :: harness_fortran_cpu_mpi__h_named
    character(len=:), allocatable :: name
    character(len=:), allocatable :: json
  end type harness_fortran_cpu_mpi__h_named

  type :: harness_fortran_cpu_mpi__h_check
    character(len=:), allocatable :: id
    character(len=4) :: status
  end type harness_fortran_cpu_mpi__h_check

  type :: harness_fortran_cpu_mpi__h_metric
    character(len=:), allocatable :: name
    real(dp) :: value
    logical :: is_na
    character(len=:), allocatable :: reason_na
  end type harness_fortran_cpu_mpi__h_metric

  type :: harness_fortran_cpu_mpi__h_case_result
    character(len=:), allocatable :: case_id
    logical :: expected_xfail
    type(harness_fortran_cpu_mpi__h_check), allocatable :: checks(:)
    type(harness_fortran_cpu_mpi__h_metric), allocatable :: metrics(:)
  end type harness_fortran_cpu_mpi__h_case_result

  type :: harness_fortran_cpu_mpi__h_mb_entry
    character(len=:), allocatable :: test_id
    character(len=:), allocatable :: case_id
    type(harness_fortran_cpu_mpi__h_named), allocatable :: values(:)
  end type harness_fortran_cpu_mpi__h_mb_entry

  ! Process grid, fixed for the run by harness_fortran_cpu_mpi__init.
  integer :: grid_size, grid_rank, grid_p1, grid_p2, grid_c1, grid_c2

  ! Number of integers each rank contributes to a gather's metadata exchange.
  integer, parameter :: nmeta = 11

  interface
    function c_mkdir(path, mode) result(rc) bind(c, name='mkdir')
      import :: c_int, c_ptr
      implicit none
      type(c_ptr), intent(in), value :: path
      integer(c_int), intent(in), value :: mode
      integer(c_int) :: rc
    end function c_mkdir
  end interface


  ! Spec path kept by the last successful parse_cases (allocated only then).
  character(len=:), allocatable :: g_spec_path

  ! The loaded case: allocated only while a case is loaded.
  ! g_text holds the whole case file; each entry is a key span in g_text,
  ! an array depth (0 for a scalar), extents, the index of its first value
  ! in g_val and, for a scalar, the span of its number token.
  character(len=:), allocatable :: g_text
  integer, allocatable :: g_klo(:)
  integer, allocatable :: g_khi(:)
  integer, allocatable :: g_depth(:)
  integer, allocatable :: g_ext(:, :)
  integer, allocatable :: g_vfirst(:)
  integer, allocatable :: g_tlo(:)
  integer, allocatable :: g_thi(:)
  real(dp), allocatable :: g_val(:)
  integer :: g_nent
  integer :: g_nval
contains

  ! ---------------------------------------------------------------- argv
  subroutine harness_fortran_cpu_mpi__parse_cases(tokens, ntokens, case_ids, ncases, ok)
    character(len=*), intent(in) :: tokens(:)
    integer, intent(in) :: ntokens
    character(len=case_id_len), intent(out) :: case_ids(:)
    integer, intent(out) :: ncases
    logical, intent(out) :: ok
    integer :: i, k

    case_ids = ' '
    ncases = 0
    ok = .false.
    k = 0
    find_flag: do i = 1, ntokens
      if (tokens(i) == '--cases') then
        k = i
        exit find_flag
      end if
    end do find_flag
    if (k == 0) return
    if (k + 2 > ntokens) return
    do i = k + 2, ntokens
      ncases = ncases + 1
      case_ids(ncases) = tokens(i)
    end do
    g_spec_path = trim(tokens(k + 1))
    ok = .true.
  end subroutine harness_fortran_cpu_mpi__parse_cases

  ! ---------------------------------------------------------------- emitters
  function harness_fortran_cpu_mpi__emit_real(x) result(s)
    real(dp), intent(in) :: x
    character(len=:), allocatable :: s
    character(len=32) :: buf

    if (ieee_is_nan(x)) then
      s = 'NaN'
    else if (.not. ieee_is_finite(x)) then
      if (x > 0.0_dp) then
        s = 'Infinity'
      else
        s = '-Infinity'
      end if
    else
      write(buf, '(ES24.16E3)') x
      s = trim(adjustl(buf))
    end if
  end function harness_fortran_cpu_mpi__emit_real

  function harness_fortran_cpu_mpi__emit_int(i) result(s)
    integer, intent(in) :: i
    character(len=:), allocatable :: s
    character(len=32) :: buf

    write(buf, '(I0)') i
    s = trim(buf)
  end function harness_fortran_cpu_mpi__emit_int

  function harness_fortran_cpu_mpi__emit_bool(b) result(s)
    logical, intent(in) :: b
    character(len=:), allocatable :: s

    if (b) then
      s = 'true'
    else
      s = 'false'
    end if
  end function harness_fortran_cpu_mpi__emit_bool

  function harness_fortran_cpu_mpi__emit_array_r1(a) result(s)
    real(dp), intent(in) :: a(:)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a)
      if (i > 1) s = s // ', '
      s = s // harness_fortran_cpu_mpi__emit_real(a(i))
    end do
    s = s // ']'
  end function harness_fortran_cpu_mpi__emit_array_r1

  function harness_fortran_cpu_mpi__emit_array_r2(a) result(s)
    real(dp), intent(in) :: a(:, :)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ', '
      s = s // harness_fortran_cpu_mpi__emit_array_r1(a(i, :))
    end do
    s = s // ']'
  end function harness_fortran_cpu_mpi__emit_array_r2

  function harness_fortran_cpu_mpi__emit_array_r3(a) result(s)
    real(dp), intent(in) :: a(:, :, :)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ', '
      s = s // harness_fortran_cpu_mpi__emit_array_r2(a(i, :, :))
    end do
    s = s // ']'
  end function harness_fortran_cpu_mpi__emit_array_r3

  function harness_fortran_cpu_mpi__emit_array_r4(a) result(s)
    real(dp), intent(in) :: a(:, :, :, :)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ', '
      s = s // harness_fortran_cpu_mpi__emit_array_r3(a(i, :, :, :))
    end do
    s = s // ']'
  end function harness_fortran_cpu_mpi__emit_array_r4

  function harness_fortran_cpu_mpi__box(name, json) result(nv)
    character(len=*), intent(in) :: name
    character(len=*), intent(in) :: json
    type(harness_fortran_cpu_mpi__h_named) :: nv

    nv%name = name
    nv%json = json
  end function harness_fortran_cpu_mpi__box

  ! ---------------------------------------------------------------- writers
  subroutine harness_fortran_cpu_mpi__write_snapshot(case_id, values, time)
    character(len=*), intent(in) :: case_id
    type(harness_fortran_cpu_mpi__h_named), intent(in) :: values(:)
    real(dp), intent(in) :: time
    integer :: u, i

    call make_dir('raw')
    call make_dir('raw/state_snapshots')
    open(newunit=u, file='raw/state_snapshots/' // trim(case_id) // '.json', &
      status='replace', action='write', form='formatted')
    write(u, '(a)') '{'
    do i = 1, size(values)
      write(u, '(a)') '  "' // values(i)%name // '": ' // values(i)%json // ','
    end do
    write(u, '(a)') '  "t": ' // harness_fortran_cpu_mpi__emit_real(time)
    write(u, '(a)') '}'
    close(u)
  end subroutine harness_fortran_cpu_mpi__write_snapshot

  subroutine harness_fortran_cpu_mpi__write_metrics_basis(entries, n)
    type(harness_fortran_cpu_mpi__h_mb_entry), intent(in) :: entries(:)
    integer, intent(in) :: n
    character(len=:), allocatable :: line
    integer :: u, e, i

    open(newunit=u, file='raw/metrics_basis.json', status='replace', action='write', &
      form='formatted')
    write(u, '(a)') '{'
    write(u, '(a)') '  "per_test": ['
    do e = 1, n
      line = '    {"test_id": "' // entries(e)%test_id // '", "case_id": "' // &
        entries(e)%case_id // '"'
      do i = 1, size(entries(e)%values)
        line = line // ', "' // entries(e)%values(i)%name // '": ' // &
          entries(e)%values(i)%json
      end do
      line = line // '}'
      if (e < n) line = line // ','
      write(u, '(a)') line
    end do
    write(u, '(a)') '  ]'
    write(u, '(a)') '}'
    close(u)
  end subroutine harness_fortran_cpu_mpi__write_metrics_basis

  subroutine harness_fortran_cpu_mpi__write_diagnostics(results, n)
    type(harness_fortran_cpu_mpi__h_case_result), intent(in) :: results(:)
    integer, intent(in) :: n
    character(len=:), allocatable :: top, top_failed, line
    logical :: overall_fail
    integer :: u, c, k

    ! Top-level checks: one entry per distinct id, first-seen order.
    top = ''
    top_failed = ''
    do c = 1, n
      do k = 1, size(results(c)%checks)
        if (.not. seen_before(results, c, k)) then
          if (len(top) > 0) top = top // ', '
          if (id_fails(results, n, results(c)%checks(k)%id)) then
            top = top // '"' // results(c)%checks(k)%id // '": {"status": "fail"}'
            if (len(top_failed) > 0) top_failed = top_failed // ', '
            top_failed = top_failed // '"' // results(c)%checks(k)%id // '"'
          else
            top = top // '"' // results(c)%checks(k)%id // '": {"status": "pass"}'
          end if
        end if
      end do
    end do

    overall_fail = .false.
    do c = 1, n
      if (.not. results(c)%expected_xfail) then
        if (case_fails(results(c))) overall_fail = .true.
      end if
    end do

    open(newunit=u, file='diagnostics.json', status='replace', action='write', &
      form='formatted')
    write(u, '(a)') '{'
    write(u, '(a)') '  "checks": {' // top // '},'
    write(u, '(a)') '  "verdict": {"overall": "' // fold_word(overall_fail) // &
      '", "failed_checks": [' // top_failed // ']},'
    write(u, '(a)') '  "per_case": {'
    do c = 1, n
      line = '    "' // results(c)%case_id // '": ' // case_json(results(c))
      if (c < n) line = line // ','
      write(u, '(a)') line
    end do
    write(u, '(a)') '  }'
    write(u, '(a)') '}'
    close(u)
  end subroutine harness_fortran_cpu_mpi__write_diagnostics

  subroutine harness_fortran_cpu_mpi__write_perf(case_id, target, steps, cells_updated, &
      walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
    character(len=*), intent(in) :: case_id
    character(len=*), intent(in) :: target
    integer, intent(in) :: steps
    integer, intent(in) :: cells_updated
    real(dp), intent(in) :: walltime_sec
    integer, intent(in) :: mpi_ranks
    integer, intent(in) :: threads_per_rank
    integer, intent(in) :: gpu_devices
    real(dp) :: throughput
    integer :: u, degree

    throughput = real(cells_updated, dp) / walltime_sec
    degree = mpi_ranks * threads_per_rank * max(gpu_devices, 1)
    open(newunit=u, file='perf.json', status='replace', action='write', form='formatted')
    write(u, '(a)') '{'
    write(u, '(a)') '  "case_id": "' // case_id // '",'
    write(u, '(a)') '  "target": "' // target // '",'
    write(u, '(a)') '  "walltime_sec": ' // &
      harness_fortran_cpu_mpi__emit_real(walltime_sec) // ','
    write(u, '(a)') '  "steps": ' // harness_fortran_cpu_mpi__emit_int(steps) // ','
    write(u, '(a)') '  "cells_updated": ' // &
      harness_fortran_cpu_mpi__emit_int(cells_updated) // ','
    write(u, '(a)') '  "throughput_cells_per_sec": ' // &
      harness_fortran_cpu_mpi__emit_real(throughput) // ','
    write(u, '(a)') '  "parallelism": {"mpi_ranks": ' // &
      harness_fortran_cpu_mpi__emit_int(mpi_ranks) // ', "threads_per_rank": ' // &
      harness_fortran_cpu_mpi__emit_int(threads_per_rank) // ', "gpu_devices": ' // &
      harness_fortran_cpu_mpi__emit_int(gpu_devices) // ', "parallel_degree_total": ' // &
      harness_fortran_cpu_mpi__emit_int(degree) // ', "process_grid": [' // &
      harness_fortran_cpu_mpi__emit_int(grid_p1) // ', ' // &
      harness_fortran_cpu_mpi__emit_int(grid_p2) // ']},'
    write(u, '(a)') '  "timestamp_utc": "' // utc_stamp() // '"'
    write(u, '(a)') '}'
    close(u)
  end subroutine harness_fortran_cpu_mpi__write_perf

  ! ---------------------------------------------------------------- process grid
  subroutine harness_fortran_cpu_mpi__init()
    character(len=:), allocatable :: env
    integer :: elen, est

    call MPI_Init()
    call MPI_Comm_size(MPI_COMM_WORLD, grid_size)
    call MPI_Comm_rank(MPI_COMM_WORLD, grid_rank)
    call get_environment_variable('SPEC2SCIENCE_PROCESS_GRID', length=elen, status=est)
    if (est == 1 .or. (est == 0 .and. elen == 0)) then
      grid_p1 = grid_size
      grid_p2 = 1
    else if (est /= 0) then
      error stop 'harness_fortran_cpu_mpi__init: cannot read SPEC2SCIENCE_PROCESS_GRID'
    else
      allocate(character(len=elen) :: env)
      call get_environment_variable('SPEC2SCIENCE_PROCESS_GRID', value=env)
      call parse_grid(env, grid_p1, grid_p2)
      if (grid_p1 * grid_p2 /= grid_size) then
        error stop 'harness_fortran_cpu_mpi__init: SPEC2SCIENCE_PROCESS_GRID p1*p2 /= ranks'
      end if
    end if
    grid_c1 = grid_rank / grid_p2
    grid_c2 = mod(grid_rank, grid_p2)
  end subroutine harness_fortran_cpu_mpi__init

  subroutine harness_fortran_cpu_mpi__finalize()
    call MPI_Finalize()
  end subroutine harness_fortran_cpu_mpi__finalize

  function harness_fortran_cpu_mpi__comm_rank() result(r)
    integer :: r

    r = grid_rank
  end function harness_fortran_cpu_mpi__comm_rank

  function harness_fortran_cpu_mpi__comm_size() result(n)
    integer :: n

    n = grid_size
  end function harness_fortran_cpu_mpi__comm_size

  subroutine harness_fortran_cpu_mpi__grid_dims(p1, p2)
    integer, intent(out) :: p1
    integer, intent(out) :: p2

    p1 = grid_p1
    p2 = grid_p2
  end subroutine harness_fortran_cpu_mpi__grid_dims

  subroutine harness_fortran_cpu_mpi__grid_coord(c1, c2)
    integer, intent(out) :: c1
    integer, intent(out) :: c2

    c1 = grid_c1
    c2 = grid_c2
  end subroutine harness_fortran_cpu_mpi__grid_coord

  ! ---------------------------------------------------------------- partition
  subroutine harness_fortran_cpu_mpi__partition(n_global, dim, glo, ghi)
    integer, intent(in) :: n_global
    integer, intent(in) :: dim
    integer, intent(out) :: glo
    integer, intent(out) :: ghi
    integer :: p, c, b, m, k

    select case (dim)
    case (1)
      p = grid_p1
      c = grid_c1
    case (2)
      p = grid_p2
      c = grid_c2
    case default
      error stop 'harness_fortran_cpu_mpi__partition: dim must be 1 or 2'
    end select
    if (n_global < 0) error stop 'harness_fortran_cpu_mpi__partition: n_global < 0'
    b = n_global / p
    m = mod(n_global, p)
    if (c < m) then
      k = b + 1
    else
      k = b
    end if
    glo = c * b + min(c, m) + 1
    ghi = glo + k - 1
  end subroutine harness_fortran_cpu_mpi__partition

  ! ---------------------------------------------------------------- halo exchange
  subroutine harness_fortran_cpu_mpi__exchange_halo_r1(a, dim, ng, periodic)
    real(dp), intent(inout) :: a(:)
    integer, intent(in) :: dim
    integer, intent(in) :: ng
    logical, intent(in) :: periodic
    real(dp), allocatable :: sbuf(:), rbuf(:)
    integer :: nloc, left, right

    if (dim /= 1 .and. dim /= 2) error stop 'harness_fortran_cpu_mpi__exchange_halo_r1: bad dim'
    if (ng < 0) error stop 'harness_fortran_cpu_mpi__exchange_halo_r1: ng < 0'
    nloc = size(a) - 2 * ng
    if (ng > nloc) error stop 'harness_fortran_cpu_mpi__exchange_halo_r1: ng > owned count'
    if (ng == 0) return
    call line_neighbours(dim, periodic, left, right)
    allocate(sbuf(ng), rbuf(ng))
    ! leading ghosts <- last ng owned cells of the lower neighbour
    sbuf = a(nloc + 1:nloc + ng)
    call MPI_Sendrecv(sbuf, ng, MPI_DOUBLE_PRECISION, right, 101, rbuf, ng, &
      MPI_DOUBLE_PRECISION, left, 101, MPI_COMM_WORLD, MPI_STATUS_IGNORE)
    if (left /= MPI_PROC_NULL) a(1:ng) = rbuf
    ! trailing ghosts <- first ng owned cells of the upper neighbour
    sbuf = a(ng + 1:2 * ng)
    call MPI_Sendrecv(sbuf, ng, MPI_DOUBLE_PRECISION, left, 102, rbuf, ng, &
      MPI_DOUBLE_PRECISION, right, 102, MPI_COMM_WORLD, MPI_STATUS_IGNORE)
    if (right /= MPI_PROC_NULL) a(ng + nloc + 1:nloc + 2 * ng) = rbuf
  end subroutine harness_fortran_cpu_mpi__exchange_halo_r1

  subroutine harness_fortran_cpu_mpi__exchange_halo_r2(a, axis, dim, ng, periodic)
    real(dp), intent(inout) :: a(:, :)
    integer, intent(in) :: axis
    integer, intent(in) :: dim
    integer, intent(in) :: ng
    logical, intent(in) :: periodic
    real(dp), allocatable :: sbuf(:), rbuf(:)
    integer :: nloc, m, cnt, left, right

    if (axis /= 1 .and. axis /= 2) error stop 'harness_fortran_cpu_mpi__exchange_halo_r2: bad axis'
    if (dim /= 1 .and. dim /= 2) error stop 'harness_fortran_cpu_mpi__exchange_halo_r2: bad dim'
    if (ng < 0) error stop 'harness_fortran_cpu_mpi__exchange_halo_r2: ng < 0'
    nloc = size(a, axis) - 2 * ng
    if (ng > nloc) error stop 'harness_fortran_cpu_mpi__exchange_halo_r2: ng > owned count'
    if (ng == 0) return
    call line_neighbours(dim, periodic, left, right)
    m = size(a, 3 - axis)
    cnt = ng * m
    allocate(sbuf(cnt), rbuf(cnt))
    ! leading ghosts <- last ng owned cells of the lower neighbour, every other-axis position
    if (axis == 1) then
      sbuf = reshape(a(nloc + 1:nloc + ng, :), [cnt])
    else
      sbuf = reshape(a(:, nloc + 1:nloc + ng), [cnt])
    end if
    call MPI_Sendrecv(sbuf, cnt, MPI_DOUBLE_PRECISION, right, 201, rbuf, cnt, &
      MPI_DOUBLE_PRECISION, left, 201, MPI_COMM_WORLD, MPI_STATUS_IGNORE)
    if (left /= MPI_PROC_NULL) then
      if (axis == 1) then
        a(1:ng, :) = reshape(rbuf, [ng, m])
      else
        a(:, 1:ng) = reshape(rbuf, [m, ng])
      end if
    end if
    ! trailing ghosts <- first ng owned cells of the upper neighbour
    if (axis == 1) then
      sbuf = reshape(a(ng + 1:2 * ng, :), [cnt])
    else
      sbuf = reshape(a(:, ng + 1:2 * ng), [cnt])
    end if
    call MPI_Sendrecv(sbuf, cnt, MPI_DOUBLE_PRECISION, left, 202, rbuf, cnt, &
      MPI_DOUBLE_PRECISION, right, 202, MPI_COMM_WORLD, MPI_STATUS_IGNORE)
    if (right /= MPI_PROC_NULL) then
      if (axis == 1) then
        a(ng + nloc + 1:nloc + 2 * ng, :) = reshape(rbuf, [ng, m])
      else
        a(:, ng + nloc + 1:nloc + 2 * ng) = reshape(rbuf, [m, ng])
      end if
    end if
  end subroutine harness_fortran_cpu_mpi__exchange_halo_r2

  ! ---------------------------------------------------------------- gather
  subroutine harness_fortran_cpu_mpi__gather_r1(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, &
      glo2, g)
    real(dp), intent(in) :: a(:)
    integer, intent(in) :: axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2
    real(dp), allocatable, intent(out) :: g(:)
    real(dp), allocatable :: g4(:, :, :, :)

    call gather_core(reshape(a, [size(a), 1, 1, 1]), 1, axis1, lo1, hi1, glo1, &
      axis2, lo2, hi2, glo2, g4)
    if (allocated(g4)) g = reshape(g4, [size(g4, 1)])
  end subroutine harness_fortran_cpu_mpi__gather_r1

  subroutine harness_fortran_cpu_mpi__gather_r2(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, &
      glo2, g)
    real(dp), intent(in) :: a(:, :)
    integer, intent(in) :: axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2
    real(dp), allocatable, intent(out) :: g(:, :)
    real(dp), allocatable :: g4(:, :, :, :)

    call gather_core(reshape(a, [size(a, 1), size(a, 2), 1, 1]), 2, axis1, lo1, hi1, glo1, &
      axis2, lo2, hi2, glo2, g4)
    if (allocated(g4)) g = reshape(g4, [size(g4, 1), size(g4, 2)])
  end subroutine harness_fortran_cpu_mpi__gather_r2

  subroutine harness_fortran_cpu_mpi__gather_r3(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, &
      glo2, g)
    real(dp), intent(in) :: a(:, :, :)
    integer, intent(in) :: axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2
    real(dp), allocatable, intent(out) :: g(:, :, :)
    real(dp), allocatable :: g4(:, :, :, :)

    call gather_core(reshape(a, [size(a, 1), size(a, 2), size(a, 3), 1]), 3, axis1, lo1, &
      hi1, glo1, axis2, lo2, hi2, glo2, g4)
    if (allocated(g4)) g = reshape(g4, [size(g4, 1), size(g4, 2), size(g4, 3)])
  end subroutine harness_fortran_cpu_mpi__gather_r3

  subroutine harness_fortran_cpu_mpi__gather_r4(a, axis1, lo1, hi1, glo1, axis2, lo2, hi2, &
      glo2, g)
    real(dp), intent(in) :: a(:, :, :, :)
    integer, intent(in) :: axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2
    real(dp), allocatable, intent(out) :: g(:, :, :, :)

    call gather_core(a, 4, axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2, g)
  end subroutine harness_fortran_cpu_mpi__gather_r4

  ! ---------------------------------------------------------------- reductions
  function harness_fortran_cpu_mpi__reduce_sum(x) result(s)
    real(dp), intent(in) :: x
    real(dp) :: s

    call MPI_Allreduce(x, s, 1, MPI_DOUBLE_PRECISION, MPI_SUM, MPI_COMM_WORLD)
  end function harness_fortran_cpu_mpi__reduce_sum

  function harness_fortran_cpu_mpi__reduce_max(x) result(s)
    real(dp), intent(in) :: x
    real(dp) :: s

    call MPI_Allreduce(x, s, 1, MPI_DOUBLE_PRECISION, MPI_MAX, MPI_COMM_WORLD)
  end function harness_fortran_cpu_mpi__reduce_max

  function harness_fortran_cpu_mpi__reduce_min(x) result(s)
    real(dp), intent(in) :: x
    real(dp) :: s

    call MPI_Allreduce(x, s, 1, MPI_DOUBLE_PRECISION, MPI_MIN, MPI_COMM_WORLD)
  end function harness_fortran_cpu_mpi__reduce_min

  function harness_fortran_cpu_mpi__reduce_sum_int(i) result(s)
    integer, intent(in) :: i
    integer :: s

    call MPI_Allreduce(i, s, 1, MPI_INTEGER, MPI_SUM, MPI_COMM_WORLD)
  end function harness_fortran_cpu_mpi__reduce_sum_int

  ! ================================================================ private helpers
  subroutine make_dir(path)
    character(len=*), intent(in) :: path
    character(kind=c_char), allocatable, target :: buf(:)
    integer :: i, n

    n = len(path)
    allocate(buf(n + 1))
    do i = 1, n
      buf(i) = path(i:i)
    end do
    buf(n + 1) = c_null_char
    ! A directory already present makes mkdir fail; the open that follows reports any
    ! real failure, so a non-zero result needs nothing more here.
    if (c_mkdir(c_loc(buf), 511_c_int) /= 0_c_int) then
      return
    end if
  end subroutine make_dir

  function fold_word(failed) result(w)
    logical, intent(in) :: failed
    character(len=4) :: w

    if (failed) then
      w = 'fail'
    else
      w = 'pass'
    end if
  end function fold_word

  function seen_before(results, c, k) result(seen)
    type(harness_fortran_cpu_mpi__h_case_result), intent(in) :: results(:)
    integer, intent(in) :: c
    integer, intent(in) :: k
    logical :: seen
    integer :: c2, k2, kmax

    seen = .false.
    do c2 = 1, c
      if (c2 < c) then
        kmax = size(results(c2)%checks)
      else
        kmax = k - 1
      end if
      do k2 = 1, kmax
        if (results(c2)%checks(k2)%id == results(c)%checks(k)%id) seen = .true.
      end do
    end do
  end function seen_before

  function id_fails(results, n, id) result(fails)
    type(harness_fortran_cpu_mpi__h_case_result), intent(in) :: results(:)
    integer, intent(in) :: n
    character(len=*), intent(in) :: id
    logical :: fails
    integer :: c, k

    fails = .false.
    do c = 1, n
      if (.not. results(c)%expected_xfail) then
        do k = 1, size(results(c)%checks)
          if (results(c)%checks(k)%id == id .and. results(c)%checks(k)%status == 'fail') then
            fails = .true.
          end if
        end do
      end if
    end do
  end function id_fails

  function case_fails(r) result(failed)
    type(harness_fortran_cpu_mpi__h_case_result), intent(in) :: r
    logical :: failed
    integer :: k

    failed = .false.
    do k = 1, size(r%checks)
      if (r%checks(k)%status == 'fail') failed = .true.
    end do
  end function case_fails

  function case_json(r) result(s)
    type(harness_fortran_cpu_mpi__h_case_result), intent(in) :: r
    character(len=:), allocatable :: s
    character(len=:), allocatable :: chk, failed_ids, met
    integer :: k

    chk = ''
    failed_ids = ''
    do k = 1, size(r%checks)
      if (k > 1) chk = chk // ', '
      chk = chk // '"' // r%checks(k)%id // '": {"status": "' // trim(r%checks(k)%status) // '"}'
      if (r%checks(k)%status == 'fail') then
        if (len(failed_ids) > 0) failed_ids = failed_ids // ', '
        failed_ids = failed_ids // '"' // r%checks(k)%id // '"'
      end if
    end do
    met = ''
    do k = 1, size(r%metrics)
      if (k > 1) met = met // ', '
      if (r%metrics(k)%is_na) then
        met = met // '"' // r%metrics(k)%name // '": null, "' // r%metrics(k)%name // &
          '_reason_na": "' // r%metrics(k)%reason_na // '"'
      else
        met = met // '"' // r%metrics(k)%name // '": ' // &
          harness_fortran_cpu_mpi__emit_real(r%metrics(k)%value)
      end if
    end do
    s = '{"checks": {' // chk // '}, "verdict": {"overall": "' // &
      fold_word(case_fails(r)) // '", "failed_checks": [' // failed_ids // &
      ']}, "metrics": {' // met // '}}'
  end function case_json

  function utc_stamp() result(s)
    character(len=20) :: s
    integer :: v(8), days, mins, rem, y, m, d

    call date_and_time(values=v)
    mins = days_from_civil(v(1), v(2), v(3)) * 1440 + v(5) * 60 + v(6) - v(4)
    rem = modulo(mins, 1440)
    days = (mins - rem) / 1440
    call civil_from_days(days, y, m, d)
    write(s, '(I4.4, "-", I2.2, "-", I2.2, "T", I2.2, ":", I2.2, ":", I2.2, "Z")') &
      y, m, d, rem / 60, mod(rem, 60), v(7)
  end function utc_stamp

  function days_from_civil(y, m, d) result(days)
    integer, intent(in) :: y
    integer, intent(in) :: m
    integer, intent(in) :: d
    integer :: days
    integer :: yy, era, yoe, doy, doe, mp

    yy = y
    if (m <= 2) yy = y - 1
    era = yy / 400
    yoe = yy - era * 400
    if (m > 2) then
      mp = m - 3
    else
      mp = m + 9
    end if
    doy = (153 * mp + 2) / 5 + d - 1
    doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
    days = era * 146097 + doe - 719468
  end function days_from_civil

  subroutine civil_from_days(z, y, m, d)
    integer, intent(in) :: z
    integer, intent(out) :: y
    integer, intent(out) :: m
    integer, intent(out) :: d
    integer :: zz, era, doe, yoe, doy, mp

    zz = z + 719468
    era = zz / 146097
    doe = zz - era * 146097
    yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
    mp = (5 * doy + 2) / 153
    d = doy - (153 * mp + 2) / 5 + 1
    if (mp < 10) then
      m = mp + 3
    else
      m = mp - 9
    end if
    if (m <= 2) y = y + 1
  end subroutine civil_from_days

  subroutine parse_grid(s, p1, p2)
    character(len=*), intent(in) :: s
    integer, intent(out) :: p1
    integer, intent(out) :: p2
    integer :: ic, i, ncomma

    ncomma = 0
    do i = 1, len(s)
      if (s(i:i) == ',') ncomma = ncomma + 1
    end do
    if (ncomma /= 1) error stop 'harness_fortran_cpu_mpi__init: malformed SPEC2SCIENCE_PROCESS_GRID'
    ic = index(s, ',')
    call parse_positive(s(1:ic - 1), p1)
    call parse_positive(s(ic + 1:), p2)
  end subroutine parse_grid

  subroutine parse_positive(t, v)
    character(len=*), intent(in) :: t
    integer, intent(out) :: v
    integer :: i, ios

    if (len(t) == 0) error stop 'harness_fortran_cpu_mpi__init: malformed SPEC2SCIENCE_PROCESS_GRID'
    do i = 1, len(t)
      if (t(i:i) < '0' .or. t(i:i) > '9') then
        error stop 'harness_fortran_cpu_mpi__init: malformed SPEC2SCIENCE_PROCESS_GRID'
      end if
    end do
    read(t, *, iostat=ios) v
    if (ios /= 0) error stop 'harness_fortran_cpu_mpi__init: malformed SPEC2SCIENCE_PROCESS_GRID'
    if (v <= 0) error stop 'harness_fortran_cpu_mpi__init: malformed SPEC2SCIENCE_PROCESS_GRID'
  end subroutine parse_positive

  function line_rank(dim, c) result(r)
    integer, intent(in) :: dim
    integer, intent(in) :: c
    integer :: r

    if (dim == 1) then
      r = c * grid_p2 + grid_c2
    else
      r = grid_c1 * grid_p2 + c
    end if
  end function line_rank

  subroutine line_neighbours(dim, periodic, left, right)
    integer, intent(in) :: dim
    logical, intent(in) :: periodic
    integer, intent(out) :: left
    integer, intent(out) :: right
    integer :: c, p

    if (dim == 1) then
      c = grid_c1
      p = grid_p1
    else
      c = grid_c2
      p = grid_p2
    end if
    left = MPI_PROC_NULL
    right = MPI_PROC_NULL
    if (c > 0) then
      left = line_rank(dim, c - 1)
    else if (periodic) then
      left = line_rank(dim, p - 1)
    end if
    if (c < p - 1) then
      right = line_rank(dim, c + 1)
    else if (periodic) then
      right = line_rank(dim, 0)
    end if
  end subroutine line_neighbours

  function block_cells(meta) result(nc)
    integer, intent(in) :: meta(:)
    integer :: nc
    integer :: ext(4)

    nc = 0
    if (meta(1) == 1) then
      ext = meta(8:11)
      if (meta(2) /= 0) ext(meta(2)) = meta(5)
      if (meta(3) /= 0) ext(meta(3)) = meta(7)
      nc = product(ext)
    end if
  end function block_cells

  function intervals_meet(g1, n1, g2, n2) result(meet)
    integer, intent(in) :: g1
    integer, intent(in) :: n1
    integer, intent(in) :: g2
    integer, intent(in) :: n2
    logical :: meet

    meet = g1 <= g2 + n2 - 1 .and. g2 <= g1 + n1 - 1
  end function intervals_meet

  subroutine tile_check(allm, axis1, axis2, own_ext, gext)
    integer, intent(in) :: allm(:, :)
    integer, intent(in) :: axis1
    integer, intent(in) :: axis2
    integer, intent(in) :: own_ext(4)
    integer, intent(out) :: gext(4)
    integer :: r, s, ax, first, total
    logical :: overlap

    first = 0
    do r = size(allm, 2), 1, -1
      if (allm(2, r) /= axis1 .or. allm(3, r) /= axis2) then
        error stop 'harness_fortran_cpu_mpi__gather: ranks pass different axis1/axis2'
      end if
      if (allm(1, r) == 1) first = r
    end do
    gext = own_ext
    if (first > 0) gext = allm(8:11, first)
    if (axis1 /= 0) gext(axis1) = 0
    if (axis2 /= 0) gext(axis2) = 0
    total = 0
    do r = 1, size(allm, 2)
      if (allm(1, r) == 1) then
        do ax = 1, 4
          if (ax /= axis1 .and. ax /= axis2) then
            if (allm(7 + ax, r) /= gext(ax)) then
              error stop 'harness_fortran_cpu_mpi__gather: unequal non-partitioned extents'
            end if
          end if
        end do
        if (axis1 /= 0) then
          if (allm(4, r) < 1) error stop 'harness_fortran_cpu_mpi__gather: block outside'
          gext(axis1) = max(gext(axis1), allm(4, r) + allm(5, r) - 1)
        end if
        if (axis2 /= 0) then
          if (allm(6, r) < 1) error stop 'harness_fortran_cpu_mpi__gather: block outside'
          gext(axis2) = max(gext(axis2), allm(6, r) + allm(7, r) - 1)
        end if
        total = total + block_cells(allm(:, r))
      end if
    end do
    if (total /= product(gext)) then
      error stop 'harness_fortran_cpu_mpi__gather: blocks do not tile the global array'
    end if
    do r = 1, size(allm, 2)
      do s = r + 1, size(allm, 2)
        if (allm(1, r) == 1 .and. allm(1, s) == 1) then
          overlap = .true.
          if (axis1 /= 0) then
            overlap = overlap .and. intervals_meet(allm(4, r), allm(5, r), allm(4, s), allm(5, s))
          end if
          if (axis2 /= 0) then
            overlap = overlap .and. intervals_meet(allm(6, r), allm(7, r), allm(6, s), allm(7, s))
          end if
          if (overlap) error stop 'harness_fortran_cpu_mpi__gather: overlapping blocks'
        end if
      end do
    end do
  end subroutine tile_check

  subroutine gather_core(a4, k, axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2, g4)
    real(dp), intent(in) :: a4(:, :, :, :)
    integer, intent(in) :: k
    integer, intent(in) :: axis1, lo1, hi1, glo1, axis2, lo2, hi2, glo2
    real(dp), allocatable, intent(out) :: g4(:, :, :, :)
    integer :: ext(4), blo(4), bhi(4), meta(nmeta), gext(4), bs(4), gs(4)
    integer :: cnt1, cnt2, nb, r, idx, i1, i2, i3, i4
    integer, allocatable :: allm(:, :), counts(:), displs(:)
    real(dp), allocatable :: sbuf(:), rbuf(:)
    logical :: contrib

    if (axis1 < 0 .or. axis1 > k .or. axis2 < 0 .or. axis2 > k) then
      error stop 'harness_fortran_cpu_mpi__gather: axis out of range'
    end if
    if (axis1 == 0 .and. axis2 == 0) error stop 'harness_fortran_cpu_mpi__gather: both axes 0'
    if (axis1 == axis2) error stop 'harness_fortran_cpu_mpi__gather: axis1 == axis2'

    ext = shape(a4)
    blo = 1
    bhi = ext
    cnt1 = 0
    cnt2 = 0
    contrib = .true.
    if (axis1 == 0) then
      if (grid_c1 /= 0) contrib = .false.
    else
      cnt1 = max(hi1 - lo1 + 1, 0)
      if (cnt1 == 0) contrib = .false.
      blo(axis1) = lo1
      bhi(axis1) = hi1
    end if
    if (axis2 == 0) then
      if (grid_c2 /= 0) contrib = .false.
    else
      cnt2 = max(hi2 - lo2 + 1, 0)
      if (cnt2 == 0) contrib = .false.
      blo(axis2) = lo2
      bhi(axis2) = hi2
    end if
    if (contrib) then
      if (axis1 /= 0) then
        if (lo1 < 1 .or. hi1 > ext(axis1)) then
          error stop 'harness_fortran_cpu_mpi__gather: lo1..hi1 outside the array'
        end if
      end if
      if (axis2 /= 0) then
        if (lo2 < 1 .or. hi2 > ext(axis2)) then
          error stop 'harness_fortran_cpu_mpi__gather: lo2..hi2 outside the array'
        end if
      end if
    end if

    meta = [merge(1, 0, contrib), axis1, axis2, glo1, cnt1, glo2, cnt2, ext(1), ext(2), &
      ext(3), ext(4)]
    allocate(allm(nmeta, grid_size))
    call MPI_Allgather(meta, nmeta, MPI_INTEGER, allm, nmeta, MPI_INTEGER, MPI_COMM_WORLD)
    call tile_check(allm, axis1, axis2, ext, gext)

    nb = 0
    if (contrib) nb = product(bhi - blo + 1)
    allocate(sbuf(nb))
    if (contrib) then
      idx = 0
      do i4 = blo(4), bhi(4)
        do i3 = blo(3), bhi(3)
          do i2 = blo(2), bhi(2)
            do i1 = blo(1), bhi(1)
              idx = idx + 1
              sbuf(idx) = a4(i1, i2, i3, i4)
            end do
          end do
        end do
      end do
    end if

    allocate(counts(grid_size), displs(grid_size))
    do r = 1, grid_size
      counts(r) = block_cells(allm(:, r))
    end do
    displs(1) = 0
    do r = 2, grid_size
      displs(r) = displs(r - 1) + counts(r - 1)
    end do
    if (grid_rank == 0) then
      allocate(rbuf(sum(counts)))
    else
      allocate(rbuf(0))
    end if
    call MPI_Gatherv(sbuf, nb, MPI_DOUBLE_PRECISION, rbuf, counts, displs, &
      MPI_DOUBLE_PRECISION, 0, MPI_COMM_WORLD)

    if (grid_rank == 0) then
      allocate(g4(gext(1), gext(2), gext(3), gext(4)))
      g4 = 0.0_dp
      do r = 1, grid_size
        if (allm(1, r) == 1) then
          bs = allm(8:11, r)
          gs = 1
          if (axis1 /= 0) then
            bs(axis1) = allm(5, r)
            gs(axis1) = allm(4, r)
          end if
          if (axis2 /= 0) then
            bs(axis2) = allm(7, r)
            gs(axis2) = allm(6, r)
          end if
          idx = displs(r)
          do i4 = 0, bs(4) - 1
            do i3 = 0, bs(3) - 1
              do i2 = 0, bs(2) - 1
                do i1 = 0, bs(1) - 1
                  idx = idx + 1
                  g4(gs(1) + i1, gs(2) + i2, gs(3) + i3, gs(4) + i4) = rbuf(idx)
                end do
              end do
            end do
          end do
        end if
      end do
    end if
  end subroutine gather_core

  subroutine harness_fortran_cpu_mpi__read_case(case_id, ok)
    character(len=*), intent(in) :: case_id
    logical, intent(out) :: ok
    character(len=:), allocatable :: path
    integer :: p, u, ios, n

    ok = .false.
    call clear_case()
    if (.not. allocated(g_spec_path)) return
    p = index(g_spec_path, '/', back=.true.)
    if (p == 0) then
      path = './case_inputs/' // trim(case_id) // '.json'
    else
      path = g_spec_path(1:p) // 'case_inputs/' // trim(case_id) // '.json'
    end if
    open (newunit=u, file=path, access='stream', form='unformatted', status='old', &
          action='read', iostat=ios)
    if (ios /= 0) return
    inquire (unit=u, size=n)
    if (n <= 0) then
      close (u)
      return
    end if
    allocate (character(len=n) :: g_text)
    read (u, iostat=ios) g_text
    close (u)
    if (ios /= 0) then
      call clear_case()
      return
    end if
    call parse_text(ok)
    if (.not. ok) call clear_case()
  end subroutine harness_fortran_cpu_mpi__read_case

  subroutine clear_case()
    if (allocated(g_text)) deallocate (g_text)
    if (allocated(g_klo)) deallocate (g_klo)
    if (allocated(g_khi)) deallocate (g_khi)
    if (allocated(g_depth)) deallocate (g_depth)
    if (allocated(g_ext)) deallocate (g_ext)
    if (allocated(g_vfirst)) deallocate (g_vfirst)
    if (allocated(g_tlo)) deallocate (g_tlo)
    if (allocated(g_thi)) deallocate (g_thi)
    if (allocated(g_val)) deallocate (g_val)
  end subroutine clear_case

  ! Character of g_text at pos, or NUL past its end.
  function ch_at(pos) result(c)
    integer, intent(in) :: pos
    character(len=1) :: c

    if (pos >= 1 .and. pos <= len(g_text)) then
      c = g_text(pos:pos)
    else
      c = achar(0)
    end if
  end function ch_at

  ! Skip JSON whitespace inside the one-line object (newline only when allow_lf).
  subroutine skip_ws(pos, allow_lf)
    integer, intent(inout) :: pos
    logical, intent(in) :: allow_lf
    character(len=1) :: c

    scan_ws: do
      c = ch_at(pos)
      if (c == ' ' .or. c == achar(9) .or. c == achar(13)) then
        pos = pos + 1
      else if (allow_lf .and. c == achar(10)) then
        pos = pos + 1
      else
        exit scan_ws
      end if
    end do scan_ws
  end subroutine skip_ws

  ! Advance pos over a run of decimal digits; nd is their count.
  subroutine skip_digits(pos, nd)
    integer, intent(inout) :: pos
    integer, intent(out) :: nd

    nd = 0
    scan_digits: do
      if (lge(ch_at(pos), '0') .and. lle(ch_at(pos), '9')) then
        pos = pos + 1
        nd = nd + 1
      else
        exit scan_digits
      end if
    end do scan_digits
  end subroutine skip_digits

  ! Parse an RFC 8259 number token at pos into a finite binary64 value.
  subroutine parse_number(pos, ok, x)
    integer, intent(inout) :: pos
    logical, intent(out) :: ok
    real(dp), intent(out) :: x
    integer :: start, nd, ios

    ok = .false.
    x = 0.0_dp
    start = pos
    if (ch_at(pos) == '-') pos = pos + 1
    if (ch_at(pos) == '0') then
      pos = pos + 1
    else
      call skip_digits(pos, nd)
      if (nd == 0) return
    end if
    if (ch_at(pos) == '.') then
      pos = pos + 1
      call skip_digits(pos, nd)
      if (nd == 0) return
    end if
    if (ch_at(pos) == 'e' .or. ch_at(pos) == 'E') then
      pos = pos + 1
      if (ch_at(pos) == '+' .or. ch_at(pos) == '-') pos = pos + 1
      call skip_digits(pos, nd)
      if (nd == 0) return
    end if
    read (g_text(start:pos - 1), *, iostat=ios) x
    if (ios /= 0) return
    if (.not. ieee_is_finite(x)) return
    ok = .true.
  end subroutine parse_number

  ! Parse a rectangular nested numeric array (depth 1..4) at pos for entry ient.
  subroutine parse_array(pos, ok, ient)
    integer, intent(inout) :: pos
    logical, intent(out) :: ok
    integer, intent(in) :: ient
    integer :: lvl, maxlvl, cnt(4), ext(4), kinds(4)
    logical :: extset(4), need_value, just_opened, do_close, sub_ok
    character(len=1) :: c
    real(dp) :: x

    ok = .false.
    lvl = 0
    maxlvl = 0
    cnt = 0
    ext = 0
    kinds = 0
    extset = .false.
    need_value = .true.
    just_opened = .false.
    g_vfirst(ient) = g_nval + 1
    g_tlo(ient) = 0
    g_thi(ient) = 0
    walk: do
      call skip_ws(pos, .false.)
      c = ch_at(pos)
      do_close = .false.
      if (need_value) then
        if (c == '[') then
          if (lvl > 0) then
            if (kinds(lvl) == 1) return
            kinds(lvl) = 2
          end if
          if (lvl == 4) return
          lvl = lvl + 1
          maxlvl = max(maxlvl, lvl)
          cnt(lvl) = 0
          pos = pos + 1
          just_opened = .true.
          cycle walk
        else if (c == ']' .and. just_opened) then
          do_close = .true.
        else
          if (lvl == 0) return
          if (kinds(lvl) == 2) return
          kinds(lvl) = 1
          call parse_number(pos, sub_ok, x)
          if (.not. sub_ok) return
          g_nval = g_nval + 1
          g_val(g_nval) = x
          cnt(lvl) = cnt(lvl) + 1
          need_value = .false.
          just_opened = .false.
          cycle walk
        end if
      else
        if (c == ',') then
          pos = pos + 1
          need_value = .true.
          just_opened = .false.
          cycle walk
        else if (c == ']') then
          do_close = .true.
        else
          return
        end if
      end if
      if (do_close) then
        if (extset(lvl)) then
          if (ext(lvl) /= cnt(lvl)) return
        else
          ext(lvl) = cnt(lvl)
          extset(lvl) = .true.
        end if
        pos = pos + 1
        lvl = lvl - 1
        if (lvl == 0) exit walk
        cnt(lvl) = cnt(lvl) + 1
        need_value = .false.
        just_opened = .false.
      end if
    end do walk
    g_depth(ient) = maxlvl
    g_ext(:, ient) = ext
    ok = .true.
  end subroutine parse_array

  ! Parse g_text as one flat JSON object of numbers / numeric arrays.
  subroutine parse_text(ok)
    logical, intent(out) :: ok
    integer :: n, pos, ks, ke
    logical :: sub_ok
    real(dp) :: x

    ok = .false.
    n = len(g_text)
    allocate (g_klo(n), g_khi(n), g_depth(n), g_ext(4, n), g_vfirst(n), &
              g_tlo(n), g_thi(n), g_val(n))
    g_nent = 0
    g_nval = 0
    pos = 1
    call skip_ws(pos, .false.)
    if (ch_at(pos) /= '{') return
    pos = pos + 1
    call skip_ws(pos, .false.)
    if (ch_at(pos) == '}') then
      pos = pos + 1
    else
      members: do
        if (ch_at(pos) /= '"') return
        ks = pos + 1
        if (ks > n) return
        ke = index(g_text(ks:), '"')
        if (ke == 0) return
        ke = ks + ke - 2
        if (index(g_text(ks:ke + 1), achar(92)) > 0) return
        g_nent = g_nent + 1
        g_klo(g_nent) = ks
        g_khi(g_nent) = ke
        pos = ke + 2
        call skip_ws(pos, .false.)
        if (ch_at(pos) /= ':') return
        pos = pos + 1
        call skip_ws(pos, .false.)
        if (ch_at(pos) == '[') then
          call parse_array(pos, sub_ok, g_nent)
        else
          g_depth(g_nent) = 0
          g_ext(:, g_nent) = 0
          g_tlo(g_nent) = pos
          call parse_number(pos, sub_ok, x)
          g_thi(g_nent) = pos - 1
          if (sub_ok) then
            g_nval = g_nval + 1
            g_val(g_nval) = x
            g_vfirst(g_nent) = g_nval
          end if
        end if
        if (.not. sub_ok) return
        call skip_ws(pos, .false.)
        if (ch_at(pos) == ',') then
          pos = pos + 1
          call skip_ws(pos, .false.)
        else if (ch_at(pos) == '}') then
          pos = pos + 1
          exit members
        else
          return
        end if
      end do members
    end if
    call skip_ws(pos, .true.)
    if (pos <= n) return
    ok = .true.
  end subroutine parse_text

  ! ------------------------------------------------------------------
  ! typed getters (no default, no ok: a refusal stops the program)
  ! ------------------------------------------------------------------
  function find_entry(name) result(k)
    character(len=*), intent(in) :: name
    integer :: k
    integer :: j

    if (.not. allocated(g_klo)) then
      write (error_unit, '(A)') 'harness_fortran_cpu: no case loaded, cannot read ' // name
      error stop 1
    end if
    k = 0
    search: do j = 1, g_nent
      if (g_khi(j) - g_klo(j) + 1 == len(name)) then
        if (g_text(g_klo(j):g_khi(j)) == name) then
          k = j
          exit search
        end if
      end if
    end do search
    if (k == 0) then
      write (error_unit, '(A)') 'harness_fortran_cpu: the loaded case holds no ' // name
      error stop 1
    end if
  end function find_entry

  function scalar_entry(name) result(k)
    character(len=*), intent(in) :: name
    integer :: k

    k = find_entry(name)
    if (g_depth(k) /= 0) then
      write (error_unit, '(A)') 'harness_fortran_cpu: an array asked as a scalar: ' // name
      error stop 1
    end if
  end function scalar_entry

  subroutine array_entry(name, rank, ex, first)
    character(len=*), intent(in) :: name
    integer, intent(in) :: rank
    integer, intent(out) :: ex(4)
    integer, intent(out) :: first
    integer :: k, d

    k = find_entry(name)
    d = g_depth(k)
    if (d == 0) then
      write (error_unit, '(A)') 'harness_fortran_cpu: a scalar asked as an array: ' // name
      error stop 1
    end if
    if (d /= rank .and. .not. (d < rank .and. g_ext(d, k) == 0)) then
      write (error_unit, '(A)') 'harness_fortran_cpu: array depth mismatch: ' // name
      error stop 1
    end if
    ex = g_ext(:, k)
    first = g_vfirst(k)
  end subroutine array_entry

  function harness_fortran_cpu_mpi__get_real(name) result(x)
    character(len=*), intent(in) :: name
    real(dp) :: x
    integer :: k

    k = scalar_entry(name)
    x = g_val(g_vfirst(k))
  end function harness_fortran_cpu_mpi__get_real

  function harness_fortran_cpu_mpi__get_int(name) result(i)
    character(len=*), intent(in) :: name
    integer :: i
    integer :: k, ios
    character(len=:), allocatable :: tok

    i = 0
    k = scalar_entry(name)
    tok = g_text(g_tlo(k):g_thi(k))
    if (scan(tok, '.eE') > 0) then
      write (error_unit, '(A)') 'harness_fortran_cpu: not an integer token: ' // name
      error stop 1
    end if
    read (tok, *, iostat=ios) i
    if (ios /= 0) then
      write (error_unit, '(A)') 'harness_fortran_cpu: integer out of range: ' // name
      error stop 1
    end if
  end function harness_fortran_cpu_mpi__get_int

  function harness_fortran_cpu_mpi__get_bool(name) result(b)
    character(len=*), intent(in) :: name
    logical :: b
    integer :: k
    character(len=:), allocatable :: tok

    b = .false.
    k = scalar_entry(name)
    tok = g_text(g_tlo(k):g_thi(k))
    if (tok == '1') then
      b = .true.
    else if (tok == '0') then
      b = .false.
    else
      write (error_unit, '(A)') 'harness_fortran_cpu: not a boolean token: ' // name
      error stop 1
    end if
  end function harness_fortran_cpu_mpi__get_bool

  subroutine harness_fortran_cpu_mpi__get_array_r1(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:)
    integer :: ex(4), p, i

    call array_entry(name, 1, ex, p)
    allocate (a(ex(1)))
    do i = 1, ex(1)
      a(i) = g_val(p)
      p = p + 1
    end do
  end subroutine harness_fortran_cpu_mpi__get_array_r1

  subroutine harness_fortran_cpu_mpi__get_array_r2(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:, :)
    integer :: ex(4), p, i, j

    call array_entry(name, 2, ex, p)
    allocate (a(ex(1), ex(2)))
    do i = 1, ex(1)
      do j = 1, ex(2)
        a(i, j) = g_val(p)
        p = p + 1
      end do
    end do
  end subroutine harness_fortran_cpu_mpi__get_array_r2

  subroutine harness_fortran_cpu_mpi__get_array_r3(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:, :, :)
    integer :: ex(4), p, i, j, k

    call array_entry(name, 3, ex, p)
    allocate (a(ex(1), ex(2), ex(3)))
    do i = 1, ex(1)
      do j = 1, ex(2)
        do k = 1, ex(3)
          a(i, j, k) = g_val(p)
          p = p + 1
        end do
      end do
    end do
  end subroutine harness_fortran_cpu_mpi__get_array_r3

  subroutine harness_fortran_cpu_mpi__get_array_r4(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:, :, :, :)
    integer :: ex(4), p, i, j, k, l

    call array_entry(name, 4, ex, p)
    allocate (a(ex(1), ex(2), ex(3), ex(4)))
    do i = 1, ex(1)
      do j = 1, ex(2)
        do k = 1, ex(3)
          do l = 1, ex(4)
            a(i, j, k, l) = g_val(p)
            p = p + 1
          end do
        end do
      end do
    end do
  end subroutine harness_fortran_cpu_mpi__get_array_r4
end module harness_fortran_cpu_mpi_model
