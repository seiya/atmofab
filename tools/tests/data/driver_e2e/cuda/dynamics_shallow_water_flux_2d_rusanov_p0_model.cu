// dynamics_shallow_water_flux_2d_rusanov_p0 model: first-order (p0) Rusanov flux of the 2D
// shallow water equations at one x interface (U_L | U_R) and one y interface (U_B | U_T).
// The operation is pointwise and __host__ __device__: a consumer's kernel calls it once per
// interface, so it launches nothing itself and allocates nothing.
#include "dynamics_shallow_water_flux_2d_rusanov_p0_model.cuh"

#include <cmath>

namespace dynamics_shallow_water_flux_2d_rusanov_p0_model {

__host__ __device__ void dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux(
    spec2science::View<const dp, 1> U_L,
    spec2science::View<const dp, 1> U_R,
    spec2science::View<const dp, 1> U_B,
    spec2science::View<const dp, 1> U_T,
    dp g,
    spec2science::View<dp, 1> F_star,
    spec2science::View<dp, 1> G_star,
    dp& a_x,
    dp& a_y,
    bool& guard_ok) {
  // step_01_input_guard: every supplied depth must be positive.
  const dp h_L = U_L.data[0];
  const dp h_R = U_R.data[0];
  const dp h_B = U_B.data[0];
  const dp h_T = U_T.data[0];
  guard_ok = (h_L > 0.0) && (h_R > 0.0) && (h_B > 0.0) && (h_T > 0.0);
  if (!guard_ok) {
    return;
  }

  const dp hu_L = U_L.data[1];
  const dp hv_L = U_L.data[2];
  const dp hu_R = U_R.data[1];
  const dp hv_R = U_R.data[2];
  const dp hu_B = U_B.data[1];
  const dp hv_B = U_B.data[2];
  const dp hu_T = U_T.data[1];
  const dp hv_T = U_T.data[2];

  // derived_field_rules: normal velocities and gravity-wave speeds.
  const dp u_L = hu_L / h_L;
  const dp u_R = hu_R / h_R;
  const dp v_B = hv_B / h_B;
  const dp v_T = hv_T / h_T;
  const dp c_L = sqrt(g * h_L);
  const dp c_R = sqrt(g * h_R);
  const dp c_B = sqrt(g * h_B);
  const dp c_T = sqrt(g * h_T);

  // step_02_wave_speeds: Rusanov wave speeds (absolute normal velocity, explicit max).
  a_x = fmax(fabs(u_L) + c_L, fabs(u_R) + c_R);
  a_y = fmax(fabs(v_B) + c_B, fabs(v_T) + c_T);

  // physical fluxes F(U) = [hu, hu^2/h + g h^2/2, hu hv/h], G(U) = [hv, hu hv/h, hv^2/h + g h^2/2]
  const dp F_phys_L0 = hu_L;
  const dp F_phys_L1 = hu_L * hu_L / h_L + 0.5 * g * h_L * h_L;
  const dp F_phys_L2 = hu_L * hv_L / h_L;
  const dp F_phys_R0 = hu_R;
  const dp F_phys_R1 = hu_R * hu_R / h_R + 0.5 * g * h_R * h_R;
  const dp F_phys_R2 = hu_R * hv_R / h_R;
  const dp G_phys_B0 = hv_B;
  const dp G_phys_B1 = hu_B * hv_B / h_B;
  const dp G_phys_B2 = hv_B * hv_B / h_B + 0.5 * g * h_B * h_B;
  const dp G_phys_T0 = hv_T;
  const dp G_phys_T1 = hu_T * hv_T / h_T;
  const dp G_phys_T2 = hv_T * hv_T / h_T + 0.5 * g * h_T * h_T;

  // step_03_flux_x: dissipation with a_x.
  F_star.data[0] = 0.5 * (F_phys_L0 + F_phys_R0) - 0.5 * a_x * (h_R - h_L);
  F_star.data[1] = 0.5 * (F_phys_L1 + F_phys_R1) - 0.5 * a_x * (hu_R - hu_L);
  F_star.data[2] = 0.5 * (F_phys_L2 + F_phys_R2) - 0.5 * a_x * (hv_R - hv_L);

  // step_04_flux_y: dissipation with a_y.
  G_star.data[0] = 0.5 * (G_phys_B0 + G_phys_T0) - 0.5 * a_y * (h_T - h_B);
  G_star.data[1] = 0.5 * (G_phys_B1 + G_phys_T1) - 0.5 * a_y * (hu_T - hu_B);
  G_star.data[2] = 0.5 * (G_phys_B2 + G_phys_T2) - 0.5 * a_y * (hv_T - hv_B);
}

}  // namespace dynamics_shallow_water_flux_2d_rusanov_p0_model
