// The operations the driver's tests call (issue #589 PR-1b), the Fortran fixture's twin. Host code
// only, so the rows run on a machine with no device.
#include "drv_toy_model.cuh"

#include <cmath>
#include <limits>

namespace drv_toy_model {

void drv_toy__init(int nx, int mode, int& n_plan, dp& dt_plan, spec2science::View<dp, 1> u,
                   bool& init_ok) {
    n_plan = 3;
    dt_plan = 0.25;
    for (long i = 0; i < nx; ++i) {
        u.data[i] = 1.0;
    }
    init_ok = mode != 10;
    if (mode == 11) {
        n_plan = 0;
    }
    if (mode == 12) {
        dt_plan = 0.0;
    }
}

void drv_toy__step(int nx, int mode, dp dt, dp t, int stepno, spec2science::View<dp, 1> u,
                   spec2science::View<dp, 1> w, bool& ok) {
    for (long i = 0; i < nx; ++i) {
        u.data[i] = mode == 5 ? dt : u.data[i] + dt;
        if (mode != 2 || i > 0) {
            w.data[i] = u.data[i] + t - t;
        }
    }
    if (mode == 3) {
        return;
    }
    ok = true;
    if (mode == 1 && stepno == 2) {
        for (long i = 0; i < nx; ++i) {
            u.data[i] = -99.0;
            w.data[i] = -99.0;
        }
        ok = false;
    }
    if (mode == 4 && stepno == 1) {
        u.data[1] = std::numeric_limits<double>::quiet_NaN();
    }
}

void drv_toy__apply(int nx, int mode, spec2science::View<const dp, 1> a, spec2science::View<dp, 1> b,
                    spec2science::View<dp, 1> c, dp& s, bool& ok) {
    if (mode == 2) {
        ok = true;
        return;
    }
    dp total = 0.0;
    for (long i = 0; i < nx; ++i) {
        b.data[i] = 2.0 * a.data[i];
        c.data[i] += 1.0;
        total += a.data[i];
    }
    s = total;
    if (mode == 3) {
        return;
    }
    ok = mode != 1;
}

void drv_toy__advance(int nx, dp t, dp dt, spec2science::View<const dp, 1> u_n, drv_rhs_proto rhs,
                      spec2science::View<dp, 1> u_np1, bool& ok) {
    ok = dt > 0.0;
    if (!ok) {
        for (long i = 0; i < nx; ++i) {
            u_np1.data[i] = u_n.data[i];
        }
        return;
    }
    std::vector<dp> k1(static_cast<std::size_t>(nx)), k2(static_cast<std::size_t>(nx));
    std::vector<dp> stage(static_cast<std::size_t>(nx));
    rhs(nx, t, u_n, spec2science::View<dp, 1>{k1.data(), {nx}});
    for (long i = 0; i < nx; ++i) {
        stage[static_cast<std::size_t>(i)] = u_n.data[i] + dt * k1[static_cast<std::size_t>(i)];
    }
    rhs(nx, t + dt, spec2science::View<const dp, 1>{stage.data(), {nx}},
        spec2science::View<dp, 1>{k2.data(), {nx}});
    for (long i = 0; i < nx; ++i) {
        const std::size_t k = static_cast<std::size_t>(i);
        u_np1.data[i] = u_n.data[i] + 0.5 * dt * (k1[k] + k2[k]);
    }
}

}  // namespace drv_toy_model
