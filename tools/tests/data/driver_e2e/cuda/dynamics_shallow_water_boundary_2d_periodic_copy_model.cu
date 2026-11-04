// dynamics_shallow_water_boundary_2d_periodic_copy model: periodic ghost-cell mapping of a
// ghost-extended 2D field, first along x (first index) then along y (second index), into a
// separate output array. The state lives on the host; inside one call the field is copied to
// the device once, mapped by two kernels (copy + x wrap fused, then y wrap) and copied back once.
#include "dynamics_shallow_water_boundary_2d_periodic_copy_model.cuh"

#include <cstddef>
#include <cuda_runtime.h>

namespace dynamics_shallow_water_boundary_2d_periodic_copy_model {

namespace {

// Persistent device buffers, allocated on first need and grown when a call needs more.
double* d_in_buf = nullptr;
double* d_out_buf = nullptr;
std::size_t d_capacity = 0;

// step_02_copy_and_x_wrap: one thread per padded cell (0-based i0, j0). Interior rows
// j0 = ng..ng+ny-1 get their x ghost cells from the periodic image (period nx); every other
// cell is copied unchanged from U_in.
__global__ void periodic_copy_x_wrap_kernel(const double* __restrict__ in, double* __restrict__ out,
                                            long nx_total, long n, int nx, int ny, int ng) {
  const long idx = static_cast<long>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (idx >= n) {
    return;
  }
  const long i0 = idx % nx_total;
  const long j0 = idx / nx_total;
  const long row = j0 * nx_total;
  const bool interior_row = j0 >= ng && j0 < ng + ny;
  if (interior_row && i0 < ng) {
    // U_out(k,j) = U_in(k+nx,j)
    out[idx] = in[row + i0 + nx];
  } else if (interior_row && i0 >= ng + nx && i0 < ng + nx + ng) {
    // U_out(ng+nx+k,j) = U_in(ng+k,j)
    out[idx] = in[row + i0 - nx];
  } else {
    out[idx] = in[idx];
  }
}

// step_03_y_wrap: one thread per column i0 = 0..nx_total-1. Each thread runs the IR's ordered
// k loop (k0 = 0..ng-1) over its own column, so a ghost row written earlier in the loop is read
// later exactly as the sequential pass does (this matters when ng > ny); columns are independent.
__global__ void periodic_y_wrap_kernel(double* __restrict__ out, long nx_total, int ny, int ng) {
  const long i0 = static_cast<long>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i0 >= nx_total) {
    return;
  }
  for (long k0 = 0; k0 < ng; ++k0) {
    // U_out(i,k) = U_out(i,k+ny)
    out[i0 + nx_total * k0] = out[i0 + nx_total * (k0 + ny)];
    // U_out(i,ng+ny+k) = U_out(i,ng+k)
    out[i0 + nx_total * (ng + ny + k0)] = out[i0 + nx_total * (ng + k0)];
  }
}

// Grow the persistent buffers to hold n elements; false when the device refuses.
bool ensure_capacity(std::size_t n) {
  if (n <= d_capacity && d_in_buf != nullptr && d_out_buf != nullptr) {
    return true;
  }
  if (d_in_buf != nullptr) {
    (void)cudaFree(d_in_buf);
    d_in_buf = nullptr;
  }
  if (d_out_buf != nullptr) {
    (void)cudaFree(d_out_buf);
    d_out_buf = nullptr;
  }
  d_capacity = 0;
  if (cudaMalloc(reinterpret_cast<void**>(&d_in_buf), n * sizeof(double)) != cudaSuccess) {
    d_in_buf = nullptr;
    return false;
  }
  if (cudaMalloc(reinterpret_cast<void**>(&d_out_buf), n * sizeof(double)) != cudaSuccess) {
    d_out_buf = nullptr;
    return false;
  }
  d_capacity = n;
  return true;
}

}  // namespace

void dynamics_shallow_water_boundary_2d_periodic_copy__apply(
    int nx,
    int ny,
    int ng,
    int nx_total,
    int ny_total,
    atmofab::View<const dp, 2> U_in,
    atmofab::View<dp, 2> U_out,
    bool& grid_valid) {
  // step_01_grid_guard
  grid_valid = !(nx < 2 || ny < 2 || ng < 1);
  if (!grid_valid) {
    return;
  }

  const long n = static_cast<long>(nx_total) * static_cast<long>(ny_total);
  const std::size_t bytes = static_cast<std::size_t>(n) * sizeof(double);
  if (!ensure_capacity(static_cast<std::size_t>(n))) {
    return;
  }
  if (cudaMemcpy(d_in_buf, U_in.data, bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
    return;
  }

  const int block = atmofab::launch_block_size;

  // step_02_copy_and_x_wrap
  const long grid_x = (n + block - 1) / block;
  periodic_copy_x_wrap_kernel<<<static_cast<unsigned int>(grid_x), block>>>(
      d_in_buf, d_out_buf, static_cast<long>(nx_total), n, nx, ny, ng);
  if (cudaGetLastError() != cudaSuccess) {
    return;
  }

  // step_03_y_wrap (after step_02 in stream order)
  const long m = static_cast<long>(nx_total);
  const long grid_y = (m + block - 1) / block;
  periodic_y_wrap_kernel<<<static_cast<unsigned int>(grid_y), block>>>(
      d_out_buf, m, ny, ng);
  if (cudaGetLastError() != cudaSuccess) {
    return;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) {
    return;
  }

  if (cudaMemcpy(U_out.data, d_out_buf, bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
    return;
  }
}

}  // namespace dynamics_shallow_water_boundary_2d_periodic_copy_model
