module scale_prc
  implicit none
  private
  public :: PRC_abort
contains
  subroutine PRC_abort
    error stop "PRC_abort"
  end subroutine PRC_abort
end module scale_prc
