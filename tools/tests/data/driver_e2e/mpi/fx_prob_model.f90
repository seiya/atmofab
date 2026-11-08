! Fixture model (2) of the distributed driver's end to end (issue #610, PR-B2): a stencil
! `problem` on the block it is handed. Periodic along x, walls along y. `__init` writes the
! state from the GLOBAL index (`i0`, `j0`, the block's offsets), and a step size and an integer
! count of the field's cells (`n_cells`, channel's `n_step_plan` in shape) from the global counts
! (`nx_g`, `ny_g`, bound to `derived` values); with `refuse_init` 1 refuses on the
! block holding the global last cell after writing it, a block-local `init` refusal (the shape
! of a depth check over the block); `__step` fills a ghost-extended temporary,
! exchanges it along dimension 1 then 2 (the corners), restores each wall by global index (never
! by the grid coordinate), and refuses at step `refuse_at` on the block holding the global last
! cell, a rank-local refusal after the exchange (`refuse_at` -1: never; `stepno` is the count of
! steps committed before the call).
module fx_prob_model
  use, intrinsic :: iso_fortran_env, only: real64
  use harness_fortran_cpu_mpi_model, only: harness_fortran_cpu_mpi__exchange_halo_r2
  implicit none
  private
  integer, parameter :: dp = real64
  public :: fx_prob__init, fx_prob__step
contains
  subroutine fx_prob__init(nx, ny, nx_g, ny_g, i0, j0, refuse_init, h, dt, n_cells, ok)
    integer, intent(in) :: nx, ny, nx_g, ny_g, i0, j0, refuse_init
    real(dp), intent(out) :: h(nx, ny)
    real(dp), intent(out) :: dt
    integer, intent(out) :: n_cells
    logical, intent(out) :: ok
    integer :: i, j
    ok = nx >= 2 .and. ny >= 2
    dt = 0.0_dp
    n_cells = 0
    if (.not. ok) return
    do j = 1, ny
      do i = 1, nx
        h(i, j) = 10.0_dp + sin(0.7_dp * (i0 + i - 1)) + cos(0.3_dp * (j0 + j - 1)) &
          + 0.01_dp * (i0 + i - 1) * (j0 + j - 1)
      end do
    end do
    dt = 0.8_dp / real(nx_g + ny_g, dp)
    n_cells = nx_g * ny_g
    if (refuse_init == 1 .and. i0 + nx - 1 == nx_g .and. j0 + ny - 1 == ny_g) ok = .false.
  end subroutine fx_prob__init

  subroutine fx_prob__step(nx, ny, nx_g, ny_g, i0, j0, refuse_at, stepno, dt, h, ok)
    integer, intent(in) :: nx, ny, nx_g, ny_g, i0, j0, refuse_at, stepno
    real(dp), intent(in) :: dt
    real(dp), intent(inout) :: h(nx, ny)
    logical, intent(out) :: ok
    real(dp) :: he(0:nx + 1, 0:ny + 1)
    integer :: i, j
    he = 0.0_dp
    he(1:nx, 1:ny) = h
    call harness_fortran_cpu_mpi__exchange_halo_r2(he, 1, 1, 1, .true.)
    call harness_fortran_cpu_mpi__exchange_halo_r2(he, 2, 2, 1, .false.)
    if (j0 == 1) he(:, 0) = he(:, 1)
    if (j0 + ny - 1 == ny_g) he(:, ny + 1) = he(:, ny)
    ok = .not. (stepno == refuse_at .and. i0 + nx - 1 == nx_g .and. j0 + ny - 1 == ny_g)
    if (.not. ok) return
    do j = 1, ny
      do i = 1, nx
        h(i, j) = he(i, j) + dt * (he(i - 1, j) + 2.0_dp * he(i + 1, j) + he(i, j - 1) &
          + 0.5_dp * he(i, j + 1) - 4.5_dp * he(i, j))
      end do
    end do
  end subroutine fx_prob__step
end module fx_prob_model
