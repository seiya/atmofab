// advdiff1d_linear model: one forward-Euler step of the 1D linear advection-diffusion problem,
// composed from the certified boundary, flux and time-update components (IR step_00..step_05).
#include "advdiff1d_linear_model.cuh"
#include "dynamics_advection_diffusion_boundary_1d_periodic_copy_model.cuh"
#include "dynamics_advdiff_flux_1d_upwind_center2_model.cuh"
#include "dynamics_advection_diffusion_time_update_1d_euler1_model.cuh"

#include <cuda_runtime.h>

#include <cstddef>
#include <vector>

namespace advdiff1d_linear_model {

namespace {

// step_00: ghost-extended pack, one thread per element of u_in (nx + 2*ng elements).
// Interior element ng + i holds u(i); the ghost elements are placeholders (0) that the
// periodic boundary component overwrites.
__global__ void pack_ghost_kernel(int nx, int ng, const double* u, double* u_in) {
  const int k = static_cast<int>(blockIdx.x * blockDim.x + threadIdx.x);
  if (k >= nx + 2 * ng) {
    return;
  }
  const int i = k - ng;
  u_in[k] = (i >= 0 && i < nx) ? u[i] : 0.0;
}

// step_03: tendency from the face-flux difference, one thread per cell.
// Face F_{i+1/2} is 0-based element i + 1 and F_{i-1/2} is element i of the flux arrays.
__global__ void tendency_kernel(int nx, double dx, const double* flux_adv, const double* flux_dif,
                                double* l_flux) {
  const int i = static_cast<int>(blockIdx.x * blockDim.x + threadIdx.x);
  if (i >= nx) {
    return;
  }
  const double f_right = flux_adv[i + 1] + flux_dif[i + 1];
  const double f_left = flux_adv[i] + flux_dif[i];
  l_flux[i] = -(f_right - f_left) / dx;
}

}  // namespace

void advdiff1d_linear__step(int nx, double a, double nu, double dx, double dt,
                            atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,
                            bool& ok) {
  ok = false;
  if (nx < 2 || u.extent[0] < nx || u_new.extent[0] < nx) {
    return;
  }
  const int ng = 1;
  const int nx_total = nx + 2 * ng;
  const int nx_face = nx + 1;
  const int block = 128;
  const std::size_t n_cells = static_cast<std::size_t>(nx);
  const std::size_t n_total = static_cast<std::size_t>(nx_total);
  const std::size_t n_face = static_cast<std::size_t>(nx_face);

  std::vector<double> u_in(n_total, 0.0);
  std::vector<double> u_out(n_total, 0.0);
  std::vector<double> flux_adv(n_face, 0.0);
  std::vector<double> flux_dif(n_face, 0.0);
  std::vector<double> l_flux(n_cells, 0.0);
  std::vector<double> u_np1(n_cells, 0.0);

  double* d_u = nullptr;
  double* d_u_in = nullptr;
  double* d_flux_adv = nullptr;
  double* d_flux_dif = nullptr;
  double* d_l_flux = nullptr;

  bool dev_ok = cudaMalloc(&d_u, n_cells * sizeof(double)) == cudaSuccess;
  if (dev_ok) {
    dev_ok = cudaMalloc(&d_u_in, n_total * sizeof(double)) == cudaSuccess;
  }
  if (dev_ok) {
    dev_ok = cudaMalloc(&d_flux_adv, n_face * sizeof(double)) == cudaSuccess;
  }
  if (dev_ok) {
    dev_ok = cudaMalloc(&d_flux_dif, n_face * sizeof(double)) == cudaSuccess;
  }
  if (dev_ok) {
    dev_ok = cudaMalloc(&d_l_flux, n_cells * sizeof(double)) == cudaSuccess;
  }

  // step_00: pack the interior state into the ghost-extended array on the device.
  if (dev_ok) {
    dev_ok = cudaMemcpy(d_u, u.data, n_cells * sizeof(double), cudaMemcpyHostToDevice) == cudaSuccess;
  }
  if (dev_ok) {
    pack_ghost_kernel<<<(nx_total + block - 1) / block, block>>>(nx, ng, d_u, d_u_in);
    dev_ok = cudaGetLastError() == cudaSuccess;
  }
  if (dev_ok) {
    dev_ok = cudaDeviceSynchronize() == cudaSuccess;
  }
  if (dev_ok) {
    dev_ok = cudaMemcpy(u_in.data(), d_u_in, n_total * sizeof(double), cudaMemcpyDeviceToHost) == cudaSuccess;
  }

  // step_01: periodic ghost fill.
  bool guard_pass_bc = false;
  if (dev_ok) {
    dynamics_advection_diffusion_boundary_1d_periodic_copy_model::
        dynamics_advection_diffusion_boundary_1d_periodic_copy__apply(
            nx, ng, atmofab::View<const double, 1>{u_in.data(), {static_cast<long>(nx_total)}},
            atmofab::View<double, 1>{u_out.data(), {static_cast<long>(nx_total)}}, guard_pass_bc);
  }

  // step_02: upwind advective and central diffusive face fluxes.
  bool guard_pass_flux = false;
  if (dev_ok && guard_pass_bc) {
    dynamics_advdiff_flux_1d_upwind_center2_model::dynamics_advdiff_flux_1d_upwind_center2__compute_flux(
        nx, ng, atmofab::View<const double, 1>{u_out.data(), {static_cast<long>(nx_total)}}, a, nu, dx, dt,
        atmofab::View<double, 1>{flux_adv.data(), {static_cast<long>(nx_face)}},
        atmofab::View<double, 1>{flux_dif.data(), {static_cast<long>(nx_face)}}, guard_pass_flux);
  }

  // step_03: tendency L_flux = -(F_{i+1/2} - F_{i-1/2}) / dx on the device.
  bool tendency_ok = dev_ok && guard_pass_bc && guard_pass_flux;
  if (tendency_ok) {
    tendency_ok = cudaMemcpy(d_flux_adv, flux_adv.data(), n_face * sizeof(double), cudaMemcpyHostToDevice) == cudaSuccess;
  }
  if (tendency_ok) {
    tendency_ok = cudaMemcpy(d_flux_dif, flux_dif.data(), n_face * sizeof(double), cudaMemcpyHostToDevice) == cudaSuccess;
  }
  if (tendency_ok) {
    tendency_kernel<<<(nx + block - 1) / block, block>>>(nx, dx, d_flux_adv, d_flux_dif, d_l_flux);
    tendency_ok = cudaGetLastError() == cudaSuccess;
  }
  if (tendency_ok) {
    tendency_ok = cudaDeviceSynchronize() == cudaSuccess;
  }
  if (tendency_ok) {
    tendency_ok = cudaMemcpy(l_flux.data(), d_l_flux, n_cells * sizeof(double), cudaMemcpyDeviceToHost) == cudaSuccess;
  }

  // step_04: forward Euler u_np1 = u + dt * L_flux on the interior state.
  bool guard_pass_time = false;
  if (tendency_ok) {
    dynamics_advection_diffusion_time_update_1d_euler1_model::dynamics_advection_diffusion_time_update_1d_euler1__advance(
        nx, u, atmofab::View<const double, 1>{l_flux.data(), {static_cast<long>(nx)}}, dt,
        atmofab::View<double, 1>{u_np1.data(), {static_cast<long>(nx)}}, guard_pass_time);
  }

  (void)cudaFree(d_l_flux);
  (void)cudaFree(d_flux_dif);
  (void)cudaFree(d_flux_adv);
  (void)cudaFree(d_u_in);
  (void)cudaFree(d_u);

  if (!(tendency_ok && guard_pass_time)) {
    return;
  }

  // step_05 (state part): commit u_np1 into the caller's host state.
  for (int i = 0; i < nx; ++i) {
    u_new.data[i] = u_np1[static_cast<std::size_t>(i)];
  }
  ok = true;
}

}  // namespace advdiff1d_linear_model
