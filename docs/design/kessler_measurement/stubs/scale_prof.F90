module scale_prof
  implicit none
  private
  public :: PROF_rapstart, PROF_rapend
contains
  subroutine PROF_rapstart(name, level, disable_barrier)
    character(len=*), intent(in) :: name
    integer, intent(in), optional :: level
    logical, intent(in), optional :: disable_barrier
  end subroutine PROF_rapstart
  subroutine PROF_rapend(name, level, disable_barrier)
    character(len=*), intent(in) :: name
    integer, intent(in), optional :: level
    logical, intent(in), optional :: disable_barrier
  end subroutine PROF_rapend
end module scale_prof
