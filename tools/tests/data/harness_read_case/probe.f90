! The test program of `test_harness_read_case.py`: `probe --cases <spec> <case_id> -- <command>...`
! parses the argv through `__parse_cases` and reads the case; each command is `<getter>:<name>`
! and prints the name with the extents, then the values. `read:<case_id>` reads a case, and
! `empty_parse:` calls `__parse_cases` on a length-0 token list, and `short_parse:<path>` on
! `--cases <path>` with no case id.
program probe
  use harness_fortran_cpu_model, only: dp, case_id_len, harness_fortran_cpu__parse_cases, &
    harness_fortran_cpu__read_case, harness_fortran_cpu__get_real, harness_fortran_cpu__get_int, &
    harness_fortran_cpu__get_bool, harness_fortran_cpu__get_array_r1, &
    harness_fortran_cpu__get_array_r2, harness_fortran_cpu__get_array_r3, &
    harness_fortran_cpu__get_array_r4
  implicit none
  character(len=4096) :: tokens(64), command
  character(len=case_id_len) :: case_ids(64), no_ids(1)
  character(len=8) :: no_tokens(1)
  character(len=4000) :: other(2)
  character(len=64) :: op
  character(len=4000) :: name
  ! A getter that refuses stops the program, so it is never called inside an output statement: the
  ! runtime holds that unit's lock, and a stop there hangs on it (measured on gfortran).
  real(dp) :: x
  real(dp), allocatable :: a1(:), a2(:, :), a3(:, :, :), a4(:, :, :, :)
  integer :: argc, stop_at, i, colon, ncases, n
  logical :: ok

  argc = command_argument_count()
  stop_at = argc + 1
  do i = 1, argc
    call get_command_argument(i, tokens(i))
    if (trim(tokens(i)) == '--') then
      stop_at = i
      exit
    end if
  end do
  call harness_fortran_cpu__parse_cases(tokens, stop_at - 1, case_ids, ncases, ok)
  if (.not. ok) error stop 3
  do i = stop_at + 1, argc
    call get_command_argument(i, command)
    colon = index(command, ':')
    op = command(1:max(colon - 1, 0))
    name = command(colon + 1:)
    select case (trim(op))
    case ('read')
      call harness_fortran_cpu__read_case(trim(name), ok)
      write (*, '(a,1x,l1)') 'read', ok
    case ('empty_parse')
      call harness_fortran_cpu__parse_cases(no_tokens, 0, no_ids, n, ok)
      write (*, '(a,1x,l1)') 'empty_parse', ok
    case ('short_parse')
      other(1) = '--cases'
      other(2) = trim(name)
      call harness_fortran_cpu__parse_cases(other, 2, no_ids, n, ok)
      write (*, '(a,1x,l1)') 'short_parse', ok
    case ('real')
      x = harness_fortran_cpu__get_real(trim(name))
      write (*, '(a,1x,es24.16e3)') trim(name), x
    case ('int')
      n = harness_fortran_cpu__get_int(trim(name))
      write (*, '(a,1x,i0)') trim(name), n
    case ('bool')
      ok = harness_fortran_cpu__get_bool(trim(name))
      write (*, '(a,1x,l1)') trim(name), ok
    case ('r1')
      call harness_fortran_cpu__get_array_r1(trim(name), a1)
      write (*, '(a,*(1x,i0))') trim(name), shape(a1)
      write (*, '(*(es24.16e3,1x))') a1
    case ('r2')
      call harness_fortran_cpu__get_array_r2(trim(name), a2)
      write (*, '(a,*(1x,i0))') trim(name), shape(a2)
      write (*, '(*(es24.16e3,1x))') a2
    case ('r3')
      call harness_fortran_cpu__get_array_r3(trim(name), a3)
      write (*, '(a,*(1x,i0))') trim(name), shape(a3)
      write (*, '(*(es24.16e3,1x))') a3
    case ('r4')
      call harness_fortran_cpu__get_array_r4(trim(name), a4)
      write (*, '(a,*(1x,i0))') trim(name), shape(a4)
      write (*, '(*(es24.16e3,1x))') a4
    case default
      error stop 4
    end select
  end do
end program probe
