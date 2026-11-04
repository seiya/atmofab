// dynamics_shallow_water_time_update_2d_ssprk2 model: one frozen-RHS SSPRK2 update
// U_np1 = 1/2 U_n + 1/2 (U_1 + dt rhs), U_1 = U_n + dt rhs, rhs = L_flux + S_b.
// State is host resident; inside the call the three inputs are copied to persistent device
// buffers once, two kernels (stage 1, stage 2 combine) run over every element, rhs and U_1
// stay on the device between them, and U_np1 is copied back once.
#include "dynamics_shallow_water_time_update_2d_ssprk2_model.cuh"

#include <cstddef>
#include <cuda_runtime.h>

namespace dynamics_shallow_water_time_update_2d_ssprk2_model {

namespace {

// Persistent device work storage: six blocks of n doubles (U_n, L_flux, S_b, rhs, U_1, U_np1).
double* g_dev = nullptr;
std::size_t g_dev_capacity = 0;

// step_02 + step_03: rhs = L_flux + S_b, U_1 = U_n + dt * rhs, element (c, i, j) with
// c < ncomp, i < nx, j < ny (column-major, c fastest).
__global__ void ssprk2_stage1_kernel(int ncomp, int nx, int ny, const double* u_n,
                                     const double* l_flux, const double* s_b, double dt,
                                     double* rhs, double* u_1) {
  const long e = static_cast<long>(blockIdx.x) * blockDim.x + threadIdx.x;
  const long n = static_cast<long>(ncomp) * nx * ny;
  if (e >= n) {
    return;
  }
  const long c = e % ncomp;
  const long i = (e / ncomp) % nx;
  const long j = e / (static_cast<long>(ncomp) * nx);
  const long k = c + static_cast<long>(ncomp) * (i + static_cast<long>(nx) * j);
  rhs[k] = l_flux[k] + s_b[k];
  u_1[k] = u_n[k] + dt * rhs[k];
}

// step_04: U_np1 = 0.5 * U_n + 0.5 * (U_1 + dt * rhs).
__global__ void ssprk2_stage2_kernel(int ncomp, int nx, int ny, const double* u_n,
                                     const double* u_1, const double* rhs, double dt,
                                     double* u_np1) {
  const long e = static_cast<long>(blockIdx.x) * blockDim.x + threadIdx.x;
  const long n = static_cast<long>(ncomp) * nx * ny;
  if (e >= n) {
    return;
  }
  const long c = e % ncomp;
  const long i = (e / ncomp) % nx;
  const long j = e / (static_cast<long>(ncomp) * nx);
  const long k = c + static_cast<long>(ncomp) * (i + static_cast<long>(nx) * j);
  u_np1[k] = 0.5 * u_n[k] + 0.5 * (u_1[k] + dt * rhs[k]);
}

}  // namespace

void dynamics_shallow_water_time_update_2d_ssprk2__advance(
    int ncomp,
    int nx,
    int ny,
    atmofab::View<const dp, 3> U_n,
    atmofab::View<const dp, 3> L_flux,
    atmofab::View<const dp, 3> S_b,
    atmofab::View<const dp, 2> z_b,
    dp dt,
    dp dx,
    dp dy,
    atmofab::View<dp, 3> U_np1,
    bool& guard_pass) {
  (void)z_b;  // z_b is inert at L0 (derived_field_rules z_b).

  // step_01_input_guard
  guard_pass = (dt > 0.0) && (dx > 0.0) && (dy > 0.0);
  if (!guard_pass) {
    return;
  }

  const std::size_t n = static_cast<std::size_t>(ncomp) * static_cast<std::size_t>(nx) *
                        static_cast<std::size_t>(ny);
  const std::size_t need = 6 * n;
  if (g_dev == nullptr || g_dev_capacity < need) {
    if (g_dev != nullptr) {
      (void)cudaFree(g_dev);
      g_dev = nullptr;
      g_dev_capacity = 0;
    }
    if (cudaMalloc(reinterpret_cast<void**>(&g_dev), need * sizeof(double)) != cudaSuccess) {
      g_dev = nullptr;
      return;
    }
    g_dev_capacity = need;
  }
  double* d_un = g_dev;
  double* d_lf = g_dev + n;
  double* d_sb = g_dev + 2 * n;
  double* d_rhs = g_dev + 3 * n;
  double* d_u1 = g_dev + 4 * n;
  double* d_unp1 = g_dev + 5 * n;
  const std::size_t bytes = n * sizeof(double);

  if (cudaMemcpy(d_un, U_n.data, bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
    return;
  }
  if (cudaMemcpy(d_lf, L_flux.data, bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
    return;
  }
  if (cudaMemcpy(d_sb, S_b.data, bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
    return;
  }

  // Launch shape: atmofab::launch_block_size threads per block, a grid covering every element.
  const int block = atmofab::launch_block_size;
  const int grid = static_cast<int>((n + static_cast<std::size_t>(block) - 1) /
                                    static_cast<std::size_t>(block));

  ssprk2_stage1_kernel<<<grid, block>>>(ncomp, nx, ny, d_un, d_lf, d_sb, dt, d_rhs, d_u1);
  if (cudaGetLastError() != cudaSuccess) {
    return;
  }
  ssprk2_stage2_kernel<<<grid, block>>>(ncomp, nx, ny, d_un, d_u1, d_rhs, dt, d_unp1);
  if (cudaGetLastError() != cudaSuccess) {
    return;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) {
    return;
  }
  // On a failed copy-back U_np1 is left without its result (fail closed).
  (void)cudaMemcpy(U_np1.data, d_unp1, bytes, cudaMemcpyDeviceToHost);
}

}  // namespace dynamics_shallow_water_time_update_2d_ssprk2_model
