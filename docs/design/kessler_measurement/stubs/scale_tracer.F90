module scale_tracer
  use scale_precision
  implicit none
  private
  public :: TRACER_regist
contains
  subroutine TRACER_regist(QS, NQ, NAME, DESC, UNIT, CV, CP, R, ENGI0, ADVC, MASS, REF)
    integer, intent(out) :: QS
    integer, intent(in) :: NQ
    character(len=*), intent(in) :: NAME(NQ), DESC(NQ), UNIT(NQ)
    real(RP), intent(in), optional :: CV(NQ), CP(NQ), R(NQ), ENGI0(NQ)
    logical, intent(in), optional :: ADVC(NQ), MASS(NQ)
    integer, intent(in), optional :: REF(NQ)
    QS = 1
  end subroutine TRACER_regist
end module scale_tracer
