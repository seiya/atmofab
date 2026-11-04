! A hand-written realization of `harness_fortran_cpu@0.10.0` §2 / §3.2's reading operations
! (issue #589, PR-2): `__parse_cases` keeping the spec path, `__read_case` and the typed getters,
! with the module parameters they reference. It is a test fixture, not a certified source: it
! shows the contract is implementable against what the host's case-file writer really produces.
! The certified harness model is generated. Its reader is superlinear in the file's size, so a
! timing measured over it says nothing of a generated one. Issue #589 PR-1b: the emitters and
! writers below are the certified `harness_fortran_cpu@0.9.0`'s (2026-11-02), so the host-rendered
! driver links and runs against the whole of §5.1.
module harness_fortran_cpu_model
  use, intrinsic :: iso_fortran_env, only: real64
  use, intrinsic :: ieee_arithmetic, only: ieee_is_nan, ieee_is_finite
  implicit none
  private

  integer, parameter, public :: dp = real64
  integer, parameter, public :: case_id_len = 64

  public :: harness_fortran_cpu__parse_cases, harness_fortran_cpu__read_case
  public :: harness_fortran_cpu__get_real, harness_fortran_cpu__get_int
  public :: harness_fortran_cpu__get_bool
  public :: harness_fortran_cpu__get_array_r1, harness_fortran_cpu__get_array_r2
  public :: harness_fortran_cpu__get_array_r3, harness_fortran_cpu__get_array_r4
  public :: harness_fortran_cpu__h_named
  public :: harness_fortran_cpu__emit_real, harness_fortran_cpu__emit_int
  public :: harness_fortran_cpu__emit_bool
  public :: harness_fortran_cpu__emit_array_r1, harness_fortran_cpu__emit_array_r2
  public :: harness_fortran_cpu__emit_array_r3, harness_fortran_cpu__emit_array_r4
  public :: harness_fortran_cpu__box, harness_fortran_cpu__write_snapshot
  public :: harness_fortran_cpu__write_perf

  integer, parameter :: max_depth = 4

  type :: harness_fortran_cpu__h_named
    character(len=:), allocatable :: name
    character(len=:), allocatable :: json
  end type harness_fortran_cpu__h_named

  type :: case_entry
    character(len=:), allocatable :: name
    integer :: depth = 0                ! 0 for a scalar
    integer :: extent(max_depth) = 0
    logical :: integer_token = .false.
    integer :: integer_value = 0
    logical :: integer_fits = .false.
    logical :: bool_token = .false.     ! exactly `1` or `0`
    real(dp), allocatable :: flat(:)    ! document order: the leading index outermost
  end type case_entry

  character(len=:), allocatable, save :: kept_spec_path
  logical, save :: case_loaded = .false.
  type(case_entry), allocatable, save :: entries(:)

contains

  subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)
    character(len=*), intent(in) :: tokens(:)
    integer, intent(in) :: ntokens
    character(len=case_id_len), intent(out) :: case_ids(:)
    integer, intent(out) :: ncases
    logical, intent(out) :: ok
    integer :: i, at

    ncases = 0
    ok = .false.
    at = 0
    do i = 1, ntokens
      if (trim(tokens(i)) == '--cases') then
        at = i
        exit
      end if
    end do
    if (at == 0 .or. at + 2 > ntokens) return
    kept_spec_path = trim(tokens(at + 1))
    do i = at + 2, ntokens
      ncases = ncases + 1
      case_ids(ncases) = tokens(i)
    end do
    ok = .true.
  end subroutine harness_fortran_cpu__parse_cases

  subroutine harness_fortran_cpu__read_case(case_id, ok)
    character(len=*), intent(in) :: case_id
    logical, intent(out) :: ok
    character(len=:), allocatable :: path, doc
    integer :: slash

    ok = .false.
    case_loaded = .false.
    if (allocated(entries)) deallocate (entries)
    if (.not. allocated(kept_spec_path)) return
    slash = index(kept_spec_path, '/', back=.true.)
    path = kept_spec_path(1:slash)//'case_inputs/'//trim(case_id)//'.json'
    call read_whole_file(path, doc, ok)
    if (.not. ok) return
    call parse_document(doc, ok)
    case_loaded = ok
    if (.not. ok .and. allocated(entries)) deallocate (entries)
  end subroutine harness_fortran_cpu__read_case

  function harness_fortran_cpu__get_real(name) result(x)
    character(len=*), intent(in) :: name
    real(dp) :: x
    integer :: k

    k = found(name)
    if (entries(k)%depth /= 0) call refuse(name, 'an array asked as a scalar')
    x = entries(k)%flat(1)
  end function harness_fortran_cpu__get_real

  function harness_fortran_cpu__get_int(name) result(i)
    character(len=*), intent(in) :: name
    integer :: i
    integer :: k

    k = found(name)
    if (entries(k)%depth /= 0) call refuse(name, 'an array asked as a scalar')
    if (.not. (entries(k)%integer_token .and. entries(k)%integer_fits)) then
      call refuse(name, 'not an integer token of the default integer')
    end if
    i = entries(k)%integer_value
  end function harness_fortran_cpu__get_int

  function harness_fortran_cpu__get_bool(name) result(b)
    character(len=*), intent(in) :: name
    logical :: b
    integer :: k

    k = found(name)
    if (entries(k)%depth /= 0) call refuse(name, 'an array asked as a scalar')
    if (.not. entries(k)%bool_token) call refuse(name, 'neither of the tokens 1 and 0')
    b = entries(k)%integer_value == 1
  end function harness_fortran_cpu__get_bool

  subroutine harness_fortran_cpu__get_array_r1(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:)
    integer :: k, e(max_depth)

    k = array_entry(name, 1, e)
    allocate (a(e(1)))
    a = entries(k)%flat
  end subroutine harness_fortran_cpu__get_array_r1

  subroutine harness_fortran_cpu__get_array_r2(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:, :)
    integer :: k, e(max_depth), i1, i2, at

    k = array_entry(name, 2, e)
    allocate (a(e(1), e(2)))
    at = 0
    do i1 = 1, e(1)
      do i2 = 1, e(2)
        at = at + 1
        a(i1, i2) = entries(k)%flat(at)
      end do
    end do
  end subroutine harness_fortran_cpu__get_array_r2

  subroutine harness_fortran_cpu__get_array_r3(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:, :, :)
    integer :: k, e(max_depth), i1, i2, i3, at

    k = array_entry(name, 3, e)
    allocate (a(e(1), e(2), e(3)))
    at = 0
    do i1 = 1, e(1)
      do i2 = 1, e(2)
        do i3 = 1, e(3)
          at = at + 1
          a(i1, i2, i3) = entries(k)%flat(at)
        end do
      end do
    end do
  end subroutine harness_fortran_cpu__get_array_r3

  subroutine harness_fortran_cpu__get_array_r4(name, a)
    character(len=*), intent(in) :: name
    real(dp), allocatable, intent(out) :: a(:, :, :, :)
    integer :: k, e(max_depth), i1, i2, i3, i4, at

    k = array_entry(name, 4, e)
    allocate (a(e(1), e(2), e(3), e(4)))
    at = 0
    do i1 = 1, e(1)
      do i2 = 1, e(2)
        do i3 = 1, e(3)
          do i4 = 1, e(4)
            at = at + 1
            a(i1, i2, i3, i4) = entries(k)%flat(at)
          end do
        end do
      end do
    end do
  end subroutine harness_fortran_cpu__get_array_r4

  ! --------------------------------------------------------------------------- the getters' lookup

  integer function found(name)
    character(len=*), intent(in) :: name
    integer :: k

    if (.not. case_loaded) call refuse(name, 'no case is loaded')
    do k = 1, size(entries)
      if (entries(k)%name == name) then
        found = k
        return
      end if
    end do
    found = 0
    call refuse(name, 'the case does not hold this name')
  end function found

  integer function array_entry(name, rank, e)
    character(len=*), intent(in) :: name
    integer, intent(in) :: rank
    integer, intent(out) :: e(max_depth)
    integer :: k

    k = found(name)
    if (entries(k)%depth == 0) call refuse(name, 'a scalar asked as an array')
    if (size(entries(k)%flat) == 0) then
      if (entries(k)%depth > rank) call refuse(name, 'a nesting depth other than the rank')
    else if (entries(k)%depth /= rank) then
      call refuse(name, 'a nesting depth other than the rank')
    end if
    e = entries(k)%extent
    array_entry = k
  end function array_entry

  subroutine refuse(name, why)
    character(len=*), intent(in) :: name, why

    write (*, '(a)') 'case file: '//name//': '//why
    error stop 2
  end subroutine refuse

  ! --------------------------------------------------------------------------------- the reading

  subroutine read_whole_file(path, doc, ok)
    character(len=*), intent(in) :: path
    character(len=:), allocatable, intent(out) :: doc
    logical, intent(out) :: ok
    integer :: unit, length, stat

    ok = .false.
    open (newunit=unit, file=path, access='stream', form='unformatted', status='old', &
          action='read', iostat=stat)
    if (stat /= 0) return
    inquire (unit=unit, size=length)
    allocate (character(len=length) :: doc)
    read (unit, iostat=stat) doc
    close (unit)
    ok = stat == 0
  end subroutine read_whole_file

  subroutine parse_document(doc, ok)
    character(len=*), intent(in) :: doc
    logical, intent(out) :: ok
    type(case_entry) :: one
    type(case_entry), allocatable :: grown(:)
    integer :: at, n

    ok = .false.
    at = 1
    n = 0
    allocate (entries(0))
    call skip_blank(doc, at)
    if (.not. take(doc, at, '{')) return
    call skip_blank(doc, at)
    if (.not. take(doc, at, '}')) then
      do
        call parse_member(doc, at, one, ok)
        if (.not. ok) return
        ok = .false.
        n = n + 1
        allocate (grown(n))
        grown(1:n - 1) = entries
        grown(n) = one
        call move_alloc(grown, entries)
        call skip_blank(doc, at)
        if (take(doc, at, '}')) exit
        if (.not. take(doc, at, ',')) return
      end do
    end if
    call skip_blank(doc, at)
    ok = at > len(doc)
  end subroutine parse_document

  subroutine parse_member(doc, at, one, ok)
    character(len=*), intent(in) :: doc
    integer, intent(inout) :: at
    type(case_entry), intent(out) :: one
    logical, intent(out) :: ok
    integer :: close_at, leaf_depth

    ok = .false.
    call skip_blank(doc, at)
    if (.not. take(doc, at, '"')) return
    close_at = index(doc(at:), '"')
    if (close_at == 0) return
    close_at = at + close_at - 1
    one%name = doc(at:close_at - 1)
    if (index(one%name, '\') /= 0) return
    at = close_at + 1
    call skip_blank(doc, at)
    if (.not. take(doc, at, ':')) return
    call skip_blank(doc, at)
    allocate (one%flat(0))
    if (at <= len(doc) .and. doc(min(at, len(doc)):min(at, len(doc))) == '[') then
      leaf_depth = 0
      one%extent = -1
      call parse_array(doc, at, 1, one, leaf_depth, ok)
      if (.not. ok) return
      where (one%extent < 0) one%extent = 0
    else
      call parse_number(doc, at, one, ok)
    end if
  end subroutine parse_member

  recursive subroutine parse_array(doc, at, level, one, leaf_depth, ok)
    character(len=*), intent(in) :: doc
    integer, intent(inout) :: at
    integer, intent(in) :: level
    type(case_entry), intent(inout) :: one
    integer, intent(inout) :: leaf_depth
    logical, intent(out) :: ok
    integer :: count
    logical :: saw_array, saw_number

    ok = .false.
    saw_array = .false.
    saw_number = .false.
    if (level > max_depth) return
    if (.not. take(doc, at, '[')) return
    one%depth = max(one%depth, level)
    count = 0
    call skip_blank(doc, at)
    if (.not. take(doc, at, ']')) then
      do
        call skip_blank(doc, at)
        if (at > len(doc)) return
        if (doc(at:at) == '[') then
          if (saw_number) return
          saw_array = .true.
          call parse_array(doc, at, level + 1, one, leaf_depth, ok)
        else
          if (saw_array) return
          saw_number = .true.
          if (leaf_depth /= 0 .and. leaf_depth /= level) return
          leaf_depth = level
          call parse_number(doc, at, one, ok)
        end if
        if (.not. ok) return
        ok = .false.
        count = count + 1
        call skip_blank(doc, at)
        if (take(doc, at, ']')) exit
        if (.not. take(doc, at, ',')) return
      end do
    end if
    if (one%extent(level) >= 0 .and. one%extent(level) /= count) return
    one%extent(level) = count
    ok = .true.
  end subroutine parse_array

  subroutine parse_number(doc, at, one, ok)
    character(len=*), intent(in) :: doc
    integer, intent(inout) :: at
    type(case_entry), intent(inout) :: one
    logical, intent(out) :: ok
    integer :: start, stat, digits_from
    logical :: integer_token
    real(dp) :: value

    ok = .false.
    start = at
    integer_token = .true.
    if (peek(doc, at) == '-') at = at + 1
    digits_from = at
    call skip_digits(doc, at)
    if (at == digits_from) return
    if (doc(digits_from:digits_from) == '0' .and. at > digits_from + 1) return
    if (peek(doc, at) == '.') then
      integer_token = .false.
      at = at + 1
      digits_from = at
      call skip_digits(doc, at)
      if (at == digits_from) return
    end if
    if (peek(doc, at) == 'e' .or. peek(doc, at) == 'E') then
      integer_token = .false.
      at = at + 1
      if (peek(doc, at) == '+' .or. peek(doc, at) == '-') at = at + 1
      digits_from = at
      call skip_digits(doc, at)
      if (at == digits_from) return
    end if
    read (doc(start:at - 1), *, iostat=stat) value
    if (stat /= 0) return
    if (.not. ieee_is_finite(value)) return
    one%flat = [one%flat, value]
    if (one%depth == 0) then
      one%integer_token = integer_token
      one%bool_token = doc(start:at - 1) == '1' .or. doc(start:at - 1) == '0'
      if (integer_token) then
        read (doc(start:at - 1), *, iostat=stat) one%integer_value
        one%integer_fits = stat == 0
      end if
    end if
    ok = .true.
  end subroutine parse_number

  character function peek(doc, at)
    character(len=*), intent(in) :: doc
    integer, intent(in) :: at

    peek = ' '
    if (at <= len(doc)) peek = doc(at:at)
  end function peek

  subroutine skip_digits(doc, at)
    character(len=*), intent(in) :: doc
    integer, intent(inout) :: at

    do while (at <= len(doc))
      if (doc(at:at) < '0' .or. doc(at:at) > '9') exit
      at = at + 1
    end do
  end subroutine skip_digits

  subroutine skip_blank(doc, at)
    character(len=*), intent(in) :: doc
    integer, intent(inout) :: at

    do while (at <= len(doc))
      if (index(' '//achar(9)//achar(10)//achar(13), doc(at:at)) == 0) exit
      at = at + 1
    end do
  end subroutine skip_blank

  logical function take(doc, at, c)
    character(len=*), intent(in) :: doc
    integer, intent(inout) :: at
    character, intent(in) :: c

    take = .false.
    if (at > len(doc)) return
    if (doc(at:at) /= c) return
    at = at + 1
    take = .true.
  end function take


  ! A real as a JSON token: the non-finite literals by branching on the value,
  ! otherwise the round-trip-lossless ES24.16E3 form.
  function harness_fortran_cpu__emit_real(x) result(s)
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
  end function harness_fortran_cpu__emit_real

  ! An integer as a JSON token.
  function harness_fortran_cpu__emit_int(i) result(s)
    integer, intent(in) :: i
    character(len=:), allocatable :: s
    character(len=32) :: buf

    write(buf, '(I0)') i
    s = trim(adjustl(buf))
  end function harness_fortran_cpu__emit_int

  ! A logical as the JSON literal true / false.
  function harness_fortran_cpu__emit_bool(b) result(s)
    logical, intent(in) :: b
    character(len=:), allocatable :: s

    if (b) then
      s = 'true'
    else
      s = 'false'
    end if
  end function harness_fortran_cpu__emit_bool

  ! Rank-1 array as a JSON array of emit_real tokens.
  function harness_fortran_cpu__emit_array_r1(a) result(s)
    real(dp), intent(in) :: a(:)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ','
      s = s // harness_fortran_cpu__emit_real(a(i))
    end do
    s = s // ']'
  end function harness_fortran_cpu__emit_array_r1

  ! Rank-2 array as nested JSON arrays, the leading index outermost.
  function harness_fortran_cpu__emit_array_r2(a) result(s)
    real(dp), intent(in) :: a(:, :)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ','
      s = s // harness_fortran_cpu__emit_array_r1(a(i, :))
    end do
    s = s // ']'
  end function harness_fortran_cpu__emit_array_r2

  ! Rank-3 array as nested JSON arrays, the leading index outermost.
  function harness_fortran_cpu__emit_array_r3(a) result(s)
    real(dp), intent(in) :: a(:, :, :)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ','
      s = s // harness_fortran_cpu__emit_array_r2(a(i, :, :))
    end do
    s = s // ']'
  end function harness_fortran_cpu__emit_array_r3

  ! Rank-4 array as nested JSON arrays, the leading index outermost.
  function harness_fortran_cpu__emit_array_r4(a) result(s)
    real(dp), intent(in) :: a(:, :, :, :)
    character(len=:), allocatable :: s
    integer :: i

    s = '['
    do i = 1, size(a, 1)
      if (i > 1) s = s // ','
      s = s // harness_fortran_cpu__emit_array_r3(a(i, :, :, :))
    end do
    s = s // ']'
  end function harness_fortran_cpu__emit_array_r4

  ! One named, already-serialized value.
  function harness_fortran_cpu__box(name, json) result(nv)
    character(len=*), intent(in) :: name
    character(len=*), intent(in) :: json
    type(harness_fortran_cpu__h_named) :: nv

    nv%name = name
    nv%json = json
  end function harness_fortran_cpu__box

  ! One flat JSON object per case at raw/state_snapshots/<case_id>.json: every
  ! boxed value under its name, then the sibling time key "t".
  subroutine harness_fortran_cpu__write_snapshot(case_id, values, time)
    character(len=*), intent(in) :: case_id
    type(harness_fortran_cpu__h_named), intent(in) :: values(:)
    real(dp), intent(in) :: time
    character(len=:), allocatable :: path
    character(len=:), allocatable :: doc
    integer :: i
    integer :: u

    doc = '{'
    do i = 1, size(values)
      doc = doc // '"' // values(i)%name // '": ' // values(i)%json // ', '
    end do
    doc = doc // '"t": ' // harness_fortran_cpu__emit_real(time) // '}'

    path = 'raw/state_snapshots/' // trim(case_id) // '.json'
    open(newunit=u, file=path, status='replace', action='write', form='formatted')
    write(u, '(a)') doc
    close(u)
  end subroutine harness_fortran_cpu__write_snapshot

  ! The run's performance document perf.json, written once.
  subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, &
      & walltime_sec, mpi_ranks, gpu_devices)
!$  use omp_lib, only: omp_get_max_threads
    character(len=*), intent(in) :: case_id
    character(len=*), intent(in) :: target
    integer, intent(in) :: steps
    integer, intent(in) :: cells_updated
    real(dp), intent(in) :: walltime_sec
    integer, intent(in) :: mpi_ranks
    integer, intent(in) :: gpu_devices
    integer :: nthreads
    integer :: degree
    integer :: u
    real(dp) :: throughput

    nthreads = 1
!$  nthreads = omp_get_max_threads()
    throughput = real(cells_updated, dp) / walltime_sec
    degree = mpi_ranks * nthreads * max(gpu_devices, 1)

    open(newunit=u, file='perf.json', status='replace', action='write', form='formatted')
    write(u, '(a)') '{'
    write(u, '(a)') '"case_id": "' // trim(case_id) // '",'
    write(u, '(a)') '"target": "' // trim(target) // '",'
    write(u, '(a)') '"walltime_sec": ' // harness_fortran_cpu__emit_real(walltime_sec) // ','
    write(u, '(a)') '"steps": ' // harness_fortran_cpu__emit_int(steps) // ','
    write(u, '(a)') '"cells_updated": ' // harness_fortran_cpu__emit_int(cells_updated) // ','
    write(u, '(a)') '"throughput_cells_per_sec": ' &
      & // harness_fortran_cpu__emit_real(throughput) // ','
    write(u, '(a)') '"parallelism": {"mpi_ranks": ' // harness_fortran_cpu__emit_int(mpi_ranks) &
      & // ', "threads_per_rank": ' // harness_fortran_cpu__emit_int(nthreads) &
      & // ', "gpu_devices": ' // harness_fortran_cpu__emit_int(gpu_devices) &
      & // ', "parallel_degree_total": ' // harness_fortran_cpu__emit_int(degree) // '},'
    write(u, '(a)') '"timestamp_utc": "' // utc_timestamp() // '"'
    write(u, '(a)') '}'
    close(u)
  end subroutine harness_fortran_cpu__write_perf

  ! ISO-8601 UTC timestamp from date_and_time with the zone offset applied.
  function utc_timestamp() result(ts)
    character(len=20) :: ts
    integer :: v(8)
    integer :: days
    integer :: minutes
    integer :: y
    integer :: m
    integer :: d

    call date_and_time(values=v)
    days = days_from_civil(v(1), v(2), v(3))
    minutes = v(5) * 60 + v(6) - v(4)
    days = days + floor(real(minutes, dp) / 1440.0_dp)
    minutes = modulo(minutes, 1440)
    call civil_from_days(days, y, m, d)
    write(ts, '(I4.4,"-",I2.2,"-",I2.2,"T",I2.2,":",I2.2,":",I2.2,"Z")') &
      & y, m, d, minutes / 60, modulo(minutes, 60), v(7)
  end function utc_timestamp

  ! Days since 1970-01-01 of a proleptic Gregorian date.
  function days_from_civil(year, month, day) result(z)
    integer, intent(in) :: year
    integer, intent(in) :: month
    integer, intent(in) :: day
    integer :: z
    integer :: y
    integer :: era
    integer :: yoe
    integer :: doy
    integer :: doe

    y = year
    if (month <= 2) y = y - 1
    if (y >= 0) then
      era = y / 400
    else
      era = (y - 399) / 400
    end if
    yoe = y - era * 400
    if (month > 2) then
      doy = (153 * (month - 3) + 2) / 5 + day - 1
    else
      doy = (153 * (month + 9) + 2) / 5 + day - 1
    end if
    doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
    z = era * 146097 + doe - 719468
  end function days_from_civil

  ! Proleptic Gregorian date of a day count since 1970-01-01.
  subroutine civil_from_days(days, year, month, day)
    integer, intent(in) :: days
    integer, intent(out) :: year
    integer, intent(out) :: month
    integer, intent(out) :: day
    integer :: z
    integer :: era
    integer :: doe
    integer :: yoe
    integer :: doy
    integer :: mp

    z = days + 719468
    if (z >= 0) then
      era = z / 146097
    else
      era = (z - 146096) / 146097
    end if
    doe = z - era * 146097
    yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365
    doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
    mp = (5 * doy + 2) / 153
    day = doy - (153 * mp + 2) / 5 + 1
    if (mp < 10) then
      month = mp + 3
    else
      month = mp - 9
    end if
    year = yoe + era * 400
    if (month <= 2) year = year + 1
  end subroutine civil_from_days

end module harness_fortran_cpu_model
