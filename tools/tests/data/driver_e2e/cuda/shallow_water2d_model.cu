// A hand edit of the certified cpp_gpu model of problem/shallow_water2d@0.4.7 (2026-10-29), cut to one
// committed step (issue #589 D3). A test fixture: the driver of PR-1b runs over it at a GPU site.
// shallow_water2d_model.cu: the 2D shallow water problem with bottom topography (p0 hydrostatic
// reconstruction, Rusanov flux component, reconstruction-consistent source S_b, SSPRK2 composed from two
// calls of the time-update component, periodic boundary component). The published operation integrates
// one case from its initial state over n_step_target steps (IR steps 01 .. 12).
#include "shallow_water2d_model.cuh"
#include "dynamics_shallow_water_boundary_2d_periodic_copy_model.cuh"
#include "dynamics_shallow_water_flux_2d_rusanov_p0_model.cuh"
#include "dynamics_shallow_water_time_update_2d_ssprk2_model.cuh"

#include <cuda_runtime.h>

#include <cmath>
#include <cstddef>
#include <vector>

namespace shallow_water2d_model {

namespace {

namespace bnd = dynamics_shallow_water_boundary_2d_periodic_copy_model;
namespace flx = dynamics_shallow_water_flux_2d_rusanov_p0_model;
namespace tup = dynamics_shallow_water_time_update_2d_ssprk2_model;

// Persistent device buffer (allocated once, grown when a larger grid needs more).
double* d_pool = nullptr;
std::size_t d_cap = 0;

// IR steps 02/03 (07/08): hydrostatic reconstruction at the x-interface (i+1/2,j) and the y-interface
// (i,j+1/2) of every cell, the wet-domain check, and one call of the Rusanov flux component per cell.
// ext holds the halo-extended stage state, component-planar, each plane (nx+2) x (ny+2).
__global__ void reconstruct_flux_kernel(const double* __restrict__ ext, const double* __restrict__ zext, int nx,
                                        int ny, double g, double* __restrict__ fstar, double* __restrict__ gstar,
                                        double* __restrict__ hs, double* __restrict__ flags) {
  const long nxl = static_cast<long>(nx);
  const long ncell = nxl * static_cast<long>(ny);
  const long k = static_cast<long>(blockIdx.x) * static_cast<long>(blockDim.x) + static_cast<long>(threadIdx.x);
  if (k >= ncell) {
    return;
  }
  const long i = k % nxl;
  const long j = k / nxl;
  const long nxt = nxl + 2;
  const long ntot = nxt * (static_cast<long>(ny) + 2);
  const long e0 = (i + 1) + nxt * (j + 1);
  const long ex = e0 + 1;
  const long ey = e0 + nxt;
  const double h0 = ext[e0];
  const double u0 = ext[ntot + e0] / h0;
  const double v0 = ext[2 * ntot + e0] / h0;
  const double hx = ext[ex];
  const double ux = ext[ntot + ex] / hx;
  const double vx = ext[2 * ntot + ex] / hx;
  const double hy = ext[ey];
  const double uy = ext[ntot + ey] / hy;
  const double vy = ext[2 * ntot + ey] / hy;
  const double z0 = zext[e0];
  const double zx = zext[ex];
  const double zy = zext[ey];
  const double zfx = fmax(z0, zx);
  const double hxl = fmax(0.0, h0 + z0 - zfx);
  const double hxr = fmax(0.0, hx + zx - zfx);
  const double zfy = fmax(z0, zy);
  const double hyb = fmax(0.0, h0 + z0 - zfy);
  const double hyt = fmax(0.0, hy + zy - zfy);
  hs[k] = hxl;
  hs[ncell + k] = hxr;
  hs[2 * ncell + k] = hyb;
  hs[3 * ncell + k] = hyt;
  if (!(hxl > 0.0) || !(hxr > 0.0) || !(hyb > 0.0) || !(hyt > 0.0)) {
    // wet-domain runtime error, raised before the value reaches the flux component
    flags[0] = 1.0;
    for (int c = 0; c < 3; ++c) {
      fstar[3 * k + c] = 0.0;
      gstar[3 * k + c] = 0.0;
    }
    return;
  }
  double ul[3] = {hxl, hxl * u0, hxl * v0};
  double ur[3] = {hxr, hxr * ux, hxr * vx};
  double ub[3] = {hyb, hyb * u0, hyb * v0};
  double ut[3] = {hyt, hyt * uy, hyt * vy};
  double fo[3] = {0.0, 0.0, 0.0};
  double go[3] = {0.0, 0.0, 0.0};
  double ax = 0.0;
  double ay = 0.0;
  bool guard_ok = true;
  flx::dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(
      atmofab::View<const double, 1>{ul, {3}}, atmofab::View<const double, 1>{ur, {3}},
      atmofab::View<const double, 1>{ub, {3}}, atmofab::View<const double, 1>{ut, {3}}, g,
      atmofab::View<double, 1>{fo, {3}}, atmofab::View<double, 1>{go, {3}}, ax, ay, guard_ok);
  if (!guard_ok) {
    flags[1] = 1.0;
  }
  for (int c = 0; c < 3; ++c) {
    fstar[3 * k + c] = fo[c];
    gstar[3 * k + c] = go[c];
  }
}

// IR step 04 (09): flux difference L_flux and the discrete source S_b, periodic index wrap.
// ls holds L_flux [3, nx, ny] followed by S_b [3, nx, ny].
__global__ void flux_difference_source_kernel(const double* __restrict__ fstar, const double* __restrict__ gstar,
                                              const double* __restrict__ hs, int nx, int ny, double g, double dx,
                                              double dy, double* __restrict__ ls) {
  const long nxl = static_cast<long>(nx);
  const long nyl = static_cast<long>(ny);
  const long ncell = nxl * nyl;
  const long k = static_cast<long>(blockIdx.x) * static_cast<long>(blockDim.x) + static_cast<long>(threadIdx.x);
  if (k >= ncell) {
    return;
  }
  const long i = k % nxl;
  const long j = k / nxl;
  const long im = (i == 0) ? nxl - 1 : i - 1;
  const long jm = (j == 0) ? nyl - 1 : j - 1;
  const long kim = im + nxl * j;
  const long kjm = i + nxl * jm;
  for (int c = 0; c < 3; ++c) {
    ls[3 * k + c] = -(fstar[3 * k + c] - fstar[3 * kim + c]) / dx - (gstar[3 * k + c] - gstar[3 * kjm + c]) / dy;
  }
  const double hxl = hs[k];
  const double hxr_m = hs[ncell + kim];
  const double hyb = hs[2 * ncell + k];
  const double hyt_m = hs[3 * ncell + kjm];
  double* sb = ls + 3 * ncell;
  sb[3 * k] = 0.0;
  sb[3 * k + 1] = g / (2.0 * dx) * (hxl * hxl - hxr_m * hxr_m);
  sb[3 * k + 2] = g / (2.0 * dy) * (hyb * hyb - hyt_m * hyt_m);
}

// IR step 11: U^{n+1} = 0.5*U_n + 0.5*U_2. U_n is the interior of the stage-1 halo array.
__global__ void ssprk2_combine_kernel(const double* __restrict__ ext, const double* __restrict__ u2, int nx, int ny,
                                      double* __restrict__ h, double* __restrict__ hu, double* __restrict__ hv) {
  const long nxl = static_cast<long>(nx);
  const long ncell = nxl * static_cast<long>(ny);
  const long k = static_cast<long>(blockIdx.x) * static_cast<long>(blockDim.x) + static_cast<long>(threadIdx.x);
  if (k >= ncell) {
    return;
  }
  const long i = k % nxl;
  const long j = k / nxl;
  const long nxt = nxl + 2;
  const long ntot = nxt * (static_cast<long>(ny) + 2);
  const long e0 = (i + 1) + nxt * (j + 1);
  h[k] = 0.5 * ext[e0] + 0.5 * u2[3 * k];
  hu[k] = 0.5 * ext[ntot + e0] + 0.5 * u2[3 * k + 1];
  hv[k] = 0.5 * ext[2 * ntot + e0] + 0.5 * u2[3 * k + 2];
}

// One stage (IR steps 01-05, resp. 06-10): boundary component on the host stage state, reconstruction,
// flux and source on the device, the time-update component on the host. u_in and u_out are [3, nx, ny].
bool run_stage(int nx, int ny, double g, double dx, double dy, double dt, const double* u_in, double* gin,
               double* gout, double* d_ext, const double* d_zext, double* d_f, double* d_g, double* d_hs,
               double* d_ls, double* d_flags, double* ls, double* flags, atmofab::View<const double, 2> z_b,
               double* u_out) {
  const long nxl = static_cast<long>(nx);
  const long nyl = static_cast<long>(ny);
  const long ncell = nxl * nyl;
  const int ng = 1;
  const int nx_total = nx + 2 * ng;
  const int ny_total = ny + 2 * ng;
  const long nxt = static_cast<long>(nx_total);
  const long ntot = nxt * static_cast<long>(ny_total);
  // Interior of the halo-extended input of the boundary component (a host reshape of the stage state).
  for (long j = 0; j < nyl; ++j) {
    for (long i = 0; i < nxl; ++i) {
      const long kc = i + nxl * j;
      const long e = (i + 1) + nxt * (j + 1);
      gin[e] = u_in[3 * kc];
      gin[ntot + e] = u_in[3 * kc + 1];
      gin[2 * ntot + e] = u_in[3 * kc + 2];
    }
  }
  bool valid = true;
  for (int c = 0; c < 3; ++c) {
    bool grid_valid = false;
    bnd::dynamics_shallow_water_boundary_2d_periodic_copy__apply(
        nx, ny, ng, nx_total, ny_total, atmofab::View<const double, 2>{gin + c * ntot, {nxt, ny_total}},
        atmofab::View<double, 2>{gout + c * ntot, {nxt, ny_total}}, grid_valid);
    valid = valid && grid_valid;
  }
  if (!valid) {
    return false;
  }
  const std::size_t ext_bytes = static_cast<std::size_t>(3 * ntot) * sizeof(double);
  const std::size_t ls_bytes = static_cast<std::size_t>(6 * ncell) * sizeof(double);
  if (cudaMemcpy(d_ext, gout, ext_bytes, cudaMemcpyHostToDevice) != cudaSuccess) {
    return false;
  }
  if (cudaMemset(d_flags, 0, 2 * sizeof(double)) != cudaSuccess) {
    return false;
  }
  const int block = atmofab::launch_block_size;
  const long grid = (block > 0) ? (ncell + block - 1) / block : 1;
  reconstruct_flux_kernel<<<static_cast<unsigned int>(grid), block>>>(d_ext, d_zext, nx, ny, g, d_f, d_g, d_hs,
                                                                       d_flags);
  if (cudaGetLastError() != cudaSuccess) {
    return false;
  }
  flux_difference_source_kernel<<<static_cast<unsigned int>(grid), block>>>(d_f, d_g, d_hs, nx, ny, g, dx, dy, d_ls);
  if (cudaGetLastError() != cudaSuccess) {
    return false;
  }
  if (cudaDeviceSynchronize() != cudaSuccess) {
    return false;
  }
  if (cudaMemcpy(ls, d_ls, ls_bytes, cudaMemcpyDeviceToHost) != cudaSuccess) {
    return false;
  }
  if (cudaMemcpy(flags, d_flags, 2 * sizeof(double), cudaMemcpyDeviceToHost) != cudaSuccess) {
    return false;
  }
  if (flags[0] != 0.0 || flags[1] != 0.0) {
    return false;
  }
  bool guard_pass = false;
  tup::dynamics_shallow_water_time_update_2d_ssprk2__advance(
      3, nx, ny, atmofab::View<const double, 3>{u_in, {3, nxl, nyl}},
      atmofab::View<const double, 3>{ls, {3, nxl, nyl}}, atmofab::View<const double, 3>{ls + 3 * ncell, {3, nxl, nyl}},
      z_b, dt, dx, dy, atmofab::View<double, 3>{u_out, {3, nxl, nyl}}, guard_pass);
  return guard_pass;
}

}  // namespace

void shallow_water2d__step(int nx, int ny, dp dx, dp dy, dp g, dp dt, dp t,
                           atmofab::View<const dp, 2> z_b, atmofab::View<dp, 2> h,
                           atmofab::View<dp, 2> hu, atmofab::View<dp, 2> hv, bool& ok) {
  ok = false;
  const long nxl = static_cast<long>(nx);
  const long nyl = static_cast<long>(ny);
  const long ncell = nxl * nyl;
  const long nxt = nxl + 2;
  const long nyt = nyl + 2;
  const long ntot = nxt * nyt;
  const int block = atmofab::launch_block_size;
  const long grid = (block > 0) ? (ncell + block - 1) / block : 1;

  // Device buffer: two halo stage states, z_b_ext, F*, G*, h*, L_flux|S_b, U_2, h|hu|hv, flags, block extrema.
  const std::size_t need = static_cast<std::size_t>(7 * ntot + 22 * ncell + 2 + 2 * grid);
  if (need > d_cap) {
    if (d_pool != nullptr) {
      (void)cudaFree(d_pool);
      d_pool = nullptr;
      d_cap = 0;
    }
    if (cudaMalloc(reinterpret_cast<void**>(&d_pool), need * sizeof(double)) != cudaSuccess) {
      d_pool = nullptr;
      return;
    }
    d_cap = need;
  }
  double* d_ext1 = d_pool;
  double* d_ext2 = d_ext1 + 3 * ntot;
  double* d_zext = d_ext2 + 3 * ntot;
  double* d_f = d_zext + ntot;
  double* d_g = d_f + 3 * ncell;
  double* d_hs = d_g + 3 * ncell;
  double* d_ls = d_hs + 4 * ncell;
  double* d_u2 = d_ls + 6 * ncell;
  double* d_h = d_u2 + 3 * ncell;
  double* d_hu = d_h + ncell;
  double* d_hv = d_hu + ncell;
  double* d_flags = d_hv + ncell;

  std::vector<double> gin(static_cast<std::size_t>(3 * ntot), 0.0);
  std::vector<double> gout(static_cast<std::size_t>(3 * ntot), 0.0);
  std::vector<double> zext(static_cast<std::size_t>(ntot), 0.0);
  std::vector<double> un(static_cast<std::size_t>(3 * ncell), 0.0);
  std::vector<double> u1(static_cast<std::size_t>(3 * ncell), 0.0);
  std::vector<double> u2(static_cast<std::size_t>(3 * ncell), 0.0);
  std::vector<double> ls(static_cast<std::size_t>(6 * ncell), 0.0);
  std::vector<double> flags(2, 0.0);

  const std::size_t cell_bytes = static_cast<std::size_t>(ncell) * sizeof(double);

  // z_b_ext: z_b with its periodic image in the ghost ring, corners included (host reshape, once per call).
  for (long jj = 0; jj < nyt; ++jj) {
    const long sj = (jj == 0) ? nyl - 1 : ((jj == nyl + 1) ? 0 : jj - 1);
    for (long ii = 0; ii < nxt; ++ii) {
      const long si = (ii == 0) ? nxl - 1 : ((ii == nxl + 1) ? 0 : ii - 1);
      zext[static_cast<std::size_t>(ii + nxt * jj)] = z_b.data[si + nxl * sj];
    }
  }
  if (cudaMemcpy(d_zext, zext.data(), static_cast<std::size_t>(ntot) * sizeof(double), cudaMemcpyHostToDevice) !=
      cudaSuccess) {
    return;
  }

  (void)t;
  bool dev_ok = true;
  {
    // U_n = [h, hu, hv] on the component axis (host reshape for the host callees).
    for (long k = 0; k < ncell; ++k) {
      un[static_cast<std::size_t>(3 * k)] = h.data[k];
      un[static_cast<std::size_t>(3 * k + 1)] = hu.data[k];
      un[static_cast<std::size_t>(3 * k + 2)] = hv.data[k];
    }
    // Stage 1 (steps 01-05): U_1 = U_n + dt*(L_flux(U_n) + S_b(U_n)).
    if (!run_stage(nx, ny, g, dx, dy, dt, un.data(), gin.data(), gout.data(), d_ext1, d_zext, d_f, d_g, d_hs, d_ls,
                   d_flags, ls.data(), flags.data(), z_b, u1.data())) {
      return;
    }
    // Stage 2 (steps 06-10): U_2 = U_1 + dt*(L_flux(U_1) + S_b(U_1)).
    if (!run_stage(nx, ny, g, dx, dy, dt, u1.data(), gin.data(), gout.data(), d_ext2, d_zext, d_f, d_g, d_hs, d_ls,
                   d_flags, ls.data(), flags.data(), z_b, u2.data())) {
      return;
    }
    // Step 11 on the device: U^{n+1} = 0.5*U_n + 0.5*U_2 (U_n is still on the device in d_ext1).
    if (cudaMemcpy(d_u2, u2.data(), static_cast<std::size_t>(3 * ncell) * sizeof(double), cudaMemcpyHostToDevice) !=
        cudaSuccess) {
      return;
    }
    ssprk2_combine_kernel<<<static_cast<unsigned int>(grid), block>>>(d_ext1, d_u2, nx, ny, d_h, d_hu, d_hv);
    if (cudaGetLastError() != cudaSuccess) {
      return;
    }
    if (cudaDeviceSynchronize() != cudaSuccess) {
      return;
    }
    dev_ok = cudaMemcpy(h.data, d_h, cell_bytes, cudaMemcpyDeviceToHost) == cudaSuccess;
    dev_ok = dev_ok && cudaMemcpy(hu.data, d_hu, cell_bytes, cudaMemcpyDeviceToHost) == cudaSuccess;
    dev_ok = dev_ok && cudaMemcpy(hv.data, d_hv, cell_bytes, cudaMemcpyDeviceToHost) == cudaSuccess;
    if (!dev_ok) {
      return;
    }
  }
  ok = true;
}

}  // namespace shallow_water2d_model
