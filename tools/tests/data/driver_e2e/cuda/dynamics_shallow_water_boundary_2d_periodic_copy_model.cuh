// dynamics_shallow_water_boundary_2d_periodic_copy_model.cuh: the published surface of dynamics_shallow_water_boundary_2d_periodic_copy, rendered by the host from
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
namespace dynamics_shallow_water_boundary_2d_periodic_copy_model {
using dp = double;
void dynamics_shallow_water_boundary_2d_periodic_copy__apply(
    int nx,
    int ny,
    int ng,
    int nx_total,
    int ny_total,
    spec2science::View<const dp, 2> U_in,
    spec2science::View<dp, 2> U_out,
    bool& grid_valid);
}  // namespace dynamics_shallow_water_boundary_2d_periodic_copy_model
