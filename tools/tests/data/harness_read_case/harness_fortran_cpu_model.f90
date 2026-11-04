! A hand-written realization of `harness_fortran_cpu@0.10.0` §2 / §3.2's reading operations
! (issue #589, PR-2): `__parse_cases` keeping the spec path, `__read_case` and the typed getters,
! with the module parameters they reference. It is a test fixture, not a certified source: it
! shows the contract is implementable against what the host's case-file writer really produces.
! The certified harness model is generated. It holds no writer and no emitter, and its reader is
! superlinear in the file's size, so a timing measured over it says nothing of a generated one.
module harness_fortran_cpu_model
  use, intrinsic :: iso_fortran_env, only: real64
  use, intrinsic :: ieee_arithmetic, only: ieee_is_finite
  implicit none
  private

  integer, parameter, public :: dp = real64
  integer, parameter, public :: case_id_len = 64

  public :: harness_fortran_cpu__parse_cases, harness_fortran_cpu__read_case
  public :: harness_fortran_cpu__get_real, harness_fortran_cpu__get_int
  public :: harness_fortran_cpu__get_bool
  public :: harness_fortran_cpu__get_array_r1, harness_fortran_cpu__get_array_r2
  public :: harness_fortran_cpu__get_array_r3, harness_fortran_cpu__get_array_r4

  integer, parameter :: max_depth = 4

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

end module harness_fortran_cpu_model
