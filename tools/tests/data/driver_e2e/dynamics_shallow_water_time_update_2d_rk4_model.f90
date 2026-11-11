! The certified `dynamics_shallow_water_time_update_2d_rk4@0.1.1` model (fortran_cpu, pipeline
! dynamics-shallow-water-time-update-2d-rk4_20261105_001, source src_20261105_001; the source E2 of
! the system-tier plan linked), copied byte for byte below this note. No line exceeds 132 characters.
! Classical four-stage RK4 update of one step of length dt for a caller-supplied
! tendency procedure rhs: U_np1 = U_n + (dt/6) (k1 + 2 k2 + 2 k3 + k4).
module dynamics_shallow_water_time_update_2d_rk4_model
  use, intrinsic :: iso_fortran_env, only: real64
  implicit none
  private

  integer, parameter :: dp = real64

  public ::dynamics_shallow_water_time_update_2d_rk4__advance

  abstract interface
    subroutine dynamics_shallow_water_time_update_2d_rk4_rhs(ncomp, nx, ny, t, U, dUdt)
      import
      implicit none
      integer, intent(in) :: ncomp
      integer, intent(in) :: nx
      integer, intent(in) :: ny
      real(dp), intent(in) :: t
      real(dp), intent(in) :: U(ncomp, nx, ny)
      real(dp), intent(out) :: dUdt(ncomp, nx, ny)
    end subroutine dynamics_shallow_water_time_update_2d_rk4_rhs
  end interface

contains

  subroutine dynamics_shallow_water_time_update_2d_rk4__advance(ncomp, nx, ny, U_n, rhs, &
      t, dt, U_np1, guard_pass)
    integer, intent(in) :: ncomp
    integer, intent(in) :: nx
    integer, intent(in) :: ny
    real(dp), intent(in) :: U_n(ncomp, nx, ny)
    procedure(dynamics_shallow_water_time_update_2d_rk4_rhs) :: rhs
    real(dp), intent(in) :: t
    real(dp), intent(in) :: dt
    real(dp), intent(out) :: U_np1(ncomp, nx, ny)
    logical, intent(out) :: guard_pass

    real(dp), allocatable :: k1(:,:,:), k2(:,:,:), k3(:,:,:), k4(:,:,:)
    real(dp), allocatable :: U_s2(:,:,:), U_s3(:,:,:), U_s4(:,:,:)
    real(dp) :: half_dt, sixth_dt
    integer :: c, i, j

    ! step_01_input_guard: each clause checked independently, joined by OR
    guard_pass = .not. (dt <= 0.0_dp .or. ncomp < 1 .or. nx < 1 .or. ny < 1)
    if (.not. guard_pass) return

    allocate(k1(ncomp, nx, ny), k2(ncomp, nx, ny), k3(ncomp, nx, ny), k4(ncomp, nx, ny))
    allocate(U_s2(ncomp, nx, ny), U_s3(ncomp, nx, ny), U_s4(ncomp, nx, ny))

    half_dt = 0.5_dp * dt
    sixth_dt = dt / 6.0_dp

    ! step_02_stage1_tendency: k1 = R(t, U_n)
    call rhs(ncomp, nx, ny, t, U_n, k1)

    ! step_03_stage2_state: U_s2 = U_n + (dt/2) k1
    !$omp parallel do schedule(static) collapse(2) default(none) &
    !$omp& shared(ncomp, nx, ny, half_dt, U_n, k1, U_s2) private(c, i, j)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          U_s2(c, i, j) = U_n(c, i, j) + half_dt * k1(c, i, j)
        end do
      end do
    end do
    !$omp end parallel do

    ! step_04_stage2_tendency: k2 = R(t + dt/2, U_s2)
    call rhs(ncomp, nx, ny, t + half_dt, U_s2, k2)

    ! step_05_stage3_state: U_s3 = U_n + (dt/2) k2
    !$omp parallel do schedule(static) collapse(2) default(none) &
    !$omp& shared(ncomp, nx, ny, half_dt, U_n, k2, U_s3) private(c, i, j)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          U_s3(c, i, j) = U_n(c, i, j) + half_dt * k2(c, i, j)
        end do
      end do
    end do
    !$omp end parallel do

    ! step_06_stage3_tendency: k3 = R(t + dt/2, U_s3)
    call rhs(ncomp, nx, ny, t + half_dt, U_s3, k3)

    ! step_07_stage4_state: U_s4 = U_n + dt k3
    !$omp parallel do schedule(static) collapse(2) default(none) &
    !$omp& shared(ncomp, nx, ny, dt, U_n, k3, U_s4) private(c, i, j)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          U_s4(c, i, j) = U_n(c, i, j) + dt * k3(c, i, j)
        end do
      end do
    end do
    !$omp end parallel do

    ! step_08_stage4_tendency: k4 = R(t + dt, U_s4)
    call rhs(ncomp, nx, ny, t + dt, U_s4, k4)

    ! step_09_rk4_combine: U_np1 = U_n + (dt/6) (k1 + 2 k2 + 2 k3 + k4)
    !$omp parallel do schedule(static) collapse(2) default(none) &
    !$omp& shared(ncomp, nx, ny, sixth_dt, U_n, k1, k2, k3, k4, U_np1) private(c, i, j)
    do j = 1, ny
      do i = 1, nx
        do c = 1, ncomp
          U_np1(c, i, j) = U_n(c, i, j) + sixth_dt * (k1(c, i, j) + 2.0_dp * k2(c, i, j) &
              + 2.0_dp * k3(c, i, j) + k4(c, i, j))
        end do
      end do
    end do
    !$omp end parallel do

    guard_pass = .true.

    deallocate(k1, k2, k3, k4, U_s2, U_s3, U_s4)
  end subroutine dynamics_shallow_water_time_update_2d_rk4__advance

end module dynamics_shallow_water_time_update_2d_rk4_model
