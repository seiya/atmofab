// dynamics_shallow_water_flux_2d_rusanov_p0_model.cuh: the published surface of dynamics_shallow_water_flux_2d_rusanov_p0, rendered by the host from
// the node's IR public_api. Do not edit: define the procedures in the model source.
#pragma once
#include <string>
#include <vector>

#ifndef SPEC2SCIENCE_VIEW_DEFINED
#define SPEC2SCIENCE_VIEW_DEFINED
namespace spec2science {
// A rank-R, column-major, non-owning array view: `data` points at extent[0] * ... * extent[R-1]
// elements, the first index fastest.
template <class T, int R>
struct View {
    T* data;
    long extent[R];
};
// A rank-R array that OWNS its storage: `data` holds extent[0] * ... * extent[R-1] elements in
// the same column-major order.
template <class T, int R>
struct Array {
    std::vector<T> data;
    long extent[R];
};
// The threads per block every kernel of the program is launched with: the block size the host
// imposed on the run, which the host-rendered runner sets once, before the first case. 0 until
// then, so a launch that reads it before the runner sets it is refused by the device.
inline int launch_block_size = 0;
}  // namespace spec2science
#endif

// An operation declared __host__ __device__ is pointwise: a kernel may call it.
namespace dynamics_shallow_water_flux_2d_rusanov_p0_model {
using dp = double;
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
    bool& guard_ok);
}  // namespace dynamics_shallow_water_flux_2d_rusanov_p0_model
