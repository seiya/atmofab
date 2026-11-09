// dynamics_shallow_water_source_2d_coriolis model: explicit pointwise Coriolis source of the 2D
// shallow-water momentum equations in momentum form. Step 01 (input guard) runs on the host;
// step 02 (the source on every interior cell) runs on the device, one thread per cell.
#include "dynamics_shallow_water_source_2d_coriolis_model.cuh"

#include <cstddef>
#include <cuda_runtime.h>

namespace {

// Persistent device buffer holding f, then U, then S; allocated once and grown on demand.
double* d_buf = nullptr;
std::size_t d_cap = 0;

// step_02: S(1)=0, S(2)=f*hv, S(3)=-f*hu at cell k (0-based linear interior index, first index
// fastest); U and S are column-major [ncomp, nx, ny], f is [nx, ny].
__global__ void coriolis_source_kernel(int ncomp, long ncell, const double* __restrict__ f,
                                       const double* __restrict__ u, double* __restrict__ s) {
  const long k = static_cast<long>(blockIdx.x) * static_cast<long>(blockDim.x) +
                 static_cast<long>(threadIdx.x);
  if (k >= ncell) {
    return;
  }
  const long base = static_cast<long>(ncomp) * k;
  const double fk = f[k];
  const double hu = u[base + 1];
  const double hv = u[base + 2];
  s[base] = 0.0;
  s[base + 1] = fk * hv;
  s[base + 2] = -fk * hu;
}

}  // namespace

namespace dynamics_shallow_water_source_2d_coriolis_model {

void dynamics_shallow_water_source_2d_coriolis__apply(
    int ncomp,
    int nx,
    int ny,
    spec2science::View<const dp, 2> f,
    spec2science::View<const dp, 3> U,
    spec2science::View<dp, 3> S,
    bool& guard_pass) {
  // step_01: input guard.
  guard_pass = (ncomp == 3) && (nx >= 1) && (ny >= 1);
  if (!guard_pass) {
    return;
  }

  // step_02: Coriolis source on the device.
  const std::size_t ncell = static_cast<std::size_t>(nx) * static_cast<std::size_t>(ny);
  const std::size_t nstate = static_cast<std::size_t>(ncomp) * ncell;
  const std::size_t need = ncell + 2 * nstate;
  if (need > d_cap) {
    if (d_buf != nullptr) {
      if (cudaFree(d_buf) != cudaSuccess) {
        return;
      }
      d_buf = nullptr;
      d_cap = 0;
    }
    if (cudaMalloc(reinterpret_cast<void**>(&d_buf), need * sizeof(double)) != cudaSuccess) {
      d_buf = nullptr;
      d_cap = 0;
      return;
    }
    d_cap = need;
  }
  double* d_f = d_buf;
  double* d_u = d_buf + ncell;
  double* d_s = d_buf + ncell + nstate;

  if (cudaMemcpy(d_f, f.data, ncell * sizeof(double), cudaMemcpyHostToDevice) != cudaSuccess) {
    return;
  }
  if (cudaMemcpy(d_u, U.data, nstate * sizeof(double), cudaMemcpyHostToDevice) != cudaSuccess) {
    return;
  }

  const int block = spec2science::launch_block_size;
  const long n = static_cast<long>(ncell);
  const int grid = static_cast<int>((n + block - 1) / block);
  coriolis_source_kernel<<<grid, block>>>(ncomp, n, d_f, d_u, d_s);
  if (cudaGetLastError() != cudaSuccess) {
    return;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) {
    return;
  }
  if (cudaMemcpy(S.data, d_s, nstate * sizeof(double), cudaMemcpyDeviceToHost) != cudaSuccess) {
    return;
  }
}

}  // namespace dynamics_shallow_water_source_2d_coriolis_model
