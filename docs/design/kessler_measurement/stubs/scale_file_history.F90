module scale_file_history
  use scale_precision
  implicit none
  private
  public :: FILE_HISTORY_in
  !> The one field the Kessler adjustment writes (`QC_t_sat`); the driver reads it as `pcsat`.
  real(RP), public, allocatable :: captured(:,:,:)
  interface FILE_HISTORY_in
     module procedure FILE_HISTORY_in_3D
  end interface
contains
  subroutine FILE_HISTORY_in_3D(var, name, desc, unit, standard_name, dim_type, fill_halo, mask)
    real(RP), intent(in) :: var(:,:,:)
    character(len=*), intent(in) :: name, desc, unit
    character(len=*), intent(in), optional :: standard_name, dim_type
    logical, intent(in), optional :: fill_halo
    logical, intent(in), optional :: mask(:,:)
    captured = var
  end subroutine FILE_HISTORY_in_3D
end module scale_file_history
