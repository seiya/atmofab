// advdiff1d_linear checks: case setup, time march, the cfl check and the diagnostic metrics.
#include "advdiff1d_linear_checks.cuh"
#include "advdiff1d_linear_model.cuh"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <map>
#include <string>
#include <vector>

namespace advdiff1d_linear_model {
void advdiff1d_linear__step(int nx, double a, double nu, double dx, double dt,
                            atmofab::View<const double, 1> u, atmofab::View<double, 1> u_new,
                            bool& ok);
}  // namespace advdiff1d_linear_model

namespace advdiff1d_linear_checks {

// Bound snapshot state: the cell-centred field u of the current case.
std::vector<double> u;

namespace {

const double kPi = 3.14159265358979323846;
const double kL = 1.0;
const double kA = 1.0;
const double kNu = 1.0e-2;
const double kCflAdv = 0.6;
const double kCflDif = 0.25;
const double kTStart = 0.0;

struct CaseState {
  std::string case_id;
  bool params_ok = false;
  bool run_ok = false;
  int nx = 0;
  double shift_fraction = 0.0;
  double dt_scale = 1.0;
  double t_end = 0.5;
  double dx = 0.0;
  double dt = 0.0;
  int n_step = 0;
  double t = 0.0;
  std::vector<double> u0;
};

CaseState g_case;
// Cross-case accumulators keyed by case_id (cases arrive in sorted order).
std::map<std::string, double> g_l2_by_case;
std::map<std::string, std::vector<double>> g_final_u_by_case;

// Reads the three digits following `key` in the case id.
bool read_field(const std::string& id, const std::string& key, int& value) {
  const std::size_t pos = id.find(key);
  if (pos == std::string::npos) {
    return false;
  }
  const std::size_t start = pos + key.size();
  if (start + 3 > id.size()) {
    return false;
  }
  int v = 0;
  for (std::size_t k = start; k < start + 3; ++k) {
    const char c = id[k];
    if (c < '0' || c > '9') {
      return false;
    }
    v = v * 10 + (c - '0');
  }
  value = v;
  return true;
}

double cell_center(int i, double dx) {
  return (static_cast<double>(i) + 0.5) * dx;
}

double initial_value(double x, double s) {
  const double xi = x / kL - s;
  return std::sin(2.0 * kPi * xi) + 0.5 * std::sin(4.0 * kPi * xi);
}

double exact_value(double x, double t, double s) {
  const double k1 = 2.0 * kPi / kL;
  const double k2 = 4.0 * kPi / kL;
  const double arg = x - kA * t - s * kL;
  return std::exp(-kNu * k1 * k1 * t) * std::sin(k1 * arg) +
         0.5 * std::exp(-kNu * k2 * k2 * t) * std::sin(k2 * arg);
}

double cfl_combined() {
  const double c = kA * g_case.dt / g_case.dx;
  const double d = kNu * g_case.dt / (g_case.dx * g_case.dx);
  return c + 2.0 * d;
}

double mass(const std::vector<double>& v, bool absolute) {
  double s = 0.0;
  for (const double x : v) {
    s += absolute ? std::fabs(x) : x;
  }
  return s * g_case.dx;
}

double l2_rel_error_tend() {
  double num = 0.0;
  double den = 0.0;
  for (int i = 0; i < g_case.nx; ++i) {
    const double ue = exact_value(cell_center(i, g_case.dx), g_case.t_end, g_case.shift_fraction);
    const double d = u[static_cast<std::size_t>(i)] - ue;
    num += d * d;
    den += ue * ue;
  }
  return den > 0.0 ? std::sqrt(num) / std::sqrt(den) : 0.0;
}

double mode_fidelity(bool& computable) {
  computable = true;
  const double c = kA * g_case.dt / g_case.dx;
  const double d = kNu * g_case.dt / (g_case.dx * g_case.dx);
  const double nst = static_cast<double>(g_case.n_step);
  double worst = 0.0;
  for (int m = 1; m <= 2; ++m) {
    const double th = 2.0 * kPi * static_cast<double>(m) / static_cast<double>(g_case.nx);
    const double km = 2.0 * kPi * static_cast<double>(m) / kL;
    const double g_re = 1.0 - (c + 2.0 * d) * (1.0 - std::cos(th));
    const double g_im = -c * std::sin(th);
    const double r = std::pow(g_re * g_re + g_im * g_im, nst / 2.0);
    const double ph = nst * std::atan2(g_im, g_re);
    const double p_re = r * std::cos(ph);
    const double p_im = r * std::sin(ph);
    double b0_re = 0.0;
    double b0_im = 0.0;
    double b1_re = 0.0;
    double b1_im = 0.0;
    for (int i = 0; i < g_case.nx; ++i) {
      const std::size_t k = static_cast<std::size_t>(i);
      const double x = cell_center(i, g_case.dx);
      const double cs = std::cos(km * x);
      const double sn = std::sin(km * x);
      b0_re += g_case.u0[k] * cs;
      b0_im -= g_case.u0[k] * sn;
      b1_re += u[k] * cs;
      b1_im -= u[k] * sn;
    }
    const double den = std::sqrt(b0_re * b0_re + b0_im * b0_im);
    if (!(den > 0.0)) {
      computable = false;
      return 0.0;
    }
    const double d_re = b1_re - (p_re * b0_re - p_im * b0_im);
    const double d_im = b1_im - (p_re * b0_im + p_im * b0_re);
    worst = std::max(worst, std::sqrt(d_re * d_re + d_im * d_im) / den);
  }
  return worst;
}

void convergence_order(const std::string& coarse_id, const std::string& fine_id, double& val,
                       bool& is_na, std::string& reason_na) {
  const auto coarse = g_l2_by_case.find(coarse_id);
  const auto fine = g_l2_by_case.find(fine_id);
  if (coarse == g_l2_by_case.end() || fine == g_l2_by_case.end()) {
    is_na = true;
    reason_na = "refinement pair error not available";
    return;
  }
  if (!(coarse->second > 0.0) || !(fine->second > 0.0)) {
    is_na = true;
    reason_na = "non-positive refinement error";
    return;
  }
  val = std::log(coarse->second / fine->second) / std::log(2.0);
}

void symmetry_residual(const std::string& case_id, double& val, bool& is_na,
                       std::string& reason_na) {
  if (!g_case.run_ok) {
    is_na = true;
    reason_na = "case_run did not complete the time march";
    return;
  }
  const std::string prefix = "advdiff1d_sym_";
  std::string ref_id = "advdiff1d_ref_" + case_id.substr(prefix.size());
  const std::size_t pos = ref_id.find("_shift");
  if (pos == std::string::npos || pos + 9 > ref_id.size()) {
    is_na = true;
    reason_na = "reference case id not derivable";
    return;
  }
  ref_id.replace(pos + 6, 3, "000");
  const auto ref = g_final_u_by_case.find(ref_id);
  if (ref == g_final_u_by_case.end() || ref->second.size() != u.size()) {
    is_na = true;
    reason_na = "reference case state not available";
    return;
  }
  const double s_cells = g_case.shift_fraction * kL / g_case.dx;
  const double s_round = std::round(s_cells);
  if (std::fabs(s_cells - s_round) > 1.0e-9) {
    is_na = true;
    reason_na = "shift is not an integer number of cells";
    return;
  }
  const int sc = static_cast<int>(s_round);
  const int nx = g_case.nx;
  double num = 0.0;
  double den = 0.0;
  for (int i = 0; i < nx; ++i) {
    const int j = ((i - sc) % nx + nx) % nx;
    const double r = ref->second[static_cast<std::size_t>(j)];
    const double d = u[static_cast<std::size_t>(i)] - r;
    num += d * d;
    den += r * r;
  }
  if (!(den > 0.0)) {
    is_na = true;
    reason_na = "reference state norm is zero";
    return;
  }
  val = std::sqrt(num) / std::sqrt(den);
}

}  // namespace

void case_setup(const std::string& case_id, bool& ok) {
  ok = false;
  g_case = CaseState();
  g_case.case_id = case_id;
  g_case.t = kTStart;
  int nx_v = 0;
  int shift_pct = 0;
  int dts_pct = 0;
  int tend_pct = 0;
  const bool parsed = read_field(case_id, "_nx", nx_v) && read_field(case_id, "_shift", shift_pct) &&
                      read_field(case_id, "_dts", dts_pct);
  const int n_alloc = nx_v >= 1 ? nx_v : 1;
  u.assign(static_cast<std::size_t>(n_alloc), 0.0);
  g_case.u0 = u;
  if (!parsed || nx_v < 2 || dts_pct <= 0) {
    return;
  }
  g_case.nx = nx_v;
  g_case.shift_fraction = static_cast<double>(shift_pct) / 100.0;
  g_case.dt_scale = static_cast<double>(dts_pct) / 100.0;
  if (read_field(case_id, "_tend", tend_pct)) {
    if (tend_pct <= 0) {
      return;
    }
    g_case.t_end = static_cast<double>(tend_pct) / 100.0;
  }
  g_case.dx = kL / static_cast<double>(nx_v);
  const double dt_raw = g_case.dt_scale * std::min(kCflAdv * g_case.dx / kA,
                                                   kCflDif * g_case.dx * g_case.dx / kNu);
  g_case.n_step = static_cast<int>(std::ceil((g_case.t_end - kTStart) / dt_raw));
  if (g_case.n_step < 1) {
    return;
  }
  g_case.dt = (g_case.t_end - kTStart) / static_cast<double>(g_case.n_step);
  for (int i = 0; i < nx_v; ++i) {
    u[static_cast<std::size_t>(i)] = initial_value(cell_center(i, g_case.dx), g_case.shift_fraction);
  }
  g_case.u0 = u;
  g_case.params_ok = true;
  ok = true;
}

void case_run(const std::string& case_id, int& steps, int& cells_updated, bool& ok) {
  steps = 0;
  cells_updated = 0;
  ok = false;
  g_case.run_ok = false;
  if (case_id != g_case.case_id || !g_case.params_ok) {
    return;
  }
  const atmofab::View<const double, 1> u_view_in{u.data(), {static_cast<long>(u.size())}};
  atmofab::View<double, 1> u_view_out{u.data(), {static_cast<long>(u.size())}};
  for (int n = 1; n <= g_case.n_step; ++n) {
    bool step_ok = false;
    advdiff1d_linear_model::advdiff1d_linear__step(g_case.nx, kA, kNu, g_case.dx, g_case.dt, u_view_in,
                                                   u_view_out, step_ok);
    if (!step_ok) {
      return;
    }
    g_case.t = (n == g_case.n_step) ? g_case.t_end : kTStart + static_cast<double>(n) * g_case.dt;
    ++steps;
    cells_updated += g_case.nx;
  }
  g_case.run_ok = true;
  g_l2_by_case[case_id] = l2_rel_error_tend();
  g_final_u_by_case[case_id] = u;
  ok = true;
}

void get_time(double& t) {
  t = g_case.t;
}

void checks_compute(const std::string& case_id, const std::string& check_id, std::string& status) {
  if (check_id == "cfl") {
    if (case_id != g_case.case_id || !g_case.params_ok) {
      status = "na  ";
    } else {
      status = cfl_combined() > 1.0 ? "fail" : "pass";
    }
  } else {
    status = "na  ";
  }
}

void metric_compute(const std::string& case_id, const std::string& name, double& val, bool& is_na,
                    std::string& reason_na, bool& found) {
  val = 0.0;
  is_na = false;
  reason_na = "";
  found = false;
  if (case_id != g_case.case_id) {
    return;
  }
  const bool per_case_metric = name == "cfl.combined_max" || name == "conserved.mass.initial" ||
                               name == "conserved.mass.abs_initial" || name == "conserved.mass.final" ||
                               name == "metrics.mass_drift_rel" || name == "errors.analytic.l2_rel_tend" ||
                               name == "errors.mode_scheme_fidelity";
  if (per_case_metric) {
    found = true;
    if (!g_case.params_ok) {
      is_na = true;
      reason_na = "case inputs rejected by case_setup";
      return;
    }
    if (name == "cfl.combined_max") {
      val = cfl_combined();
      return;
    }
    if (name == "conserved.mass.initial") {
      val = mass(g_case.u0, false);
      return;
    }
    if (name == "conserved.mass.abs_initial") {
      val = mass(g_case.u0, true);
      return;
    }
    if (!g_case.run_ok) {
      is_na = true;
      reason_na = "case_run did not complete the time march";
      return;
    }
    if (name == "conserved.mass.final") {
      val = mass(u, false);
      return;
    }
    if (name == "metrics.mass_drift_rel") {
      const double m0 = mass(g_case.u0, false);
      const double m1 = mass(u, false);
      const double abs0 = mass(g_case.u0, true);
      val = std::fabs(m1 - m0) / std::max(abs0, 1.0e-14);
      return;
    }
    if (name == "errors.analytic.l2_rel_tend") {
      val = l2_rel_error_tend();
      return;
    }
    bool computable = false;
    const double fidelity = mode_fidelity(computable);
    if (!computable) {
      is_na = true;
      reason_na = "initial mode coefficient is zero";
      return;
    }
    val = fidelity;
    return;
  }
  if (name == "errors.symmetry_l2_rel") {
    if (case_id.rfind("advdiff1d_sym_", 0) != 0) {
      return;
    }
    found = true;
    symmetry_residual(case_id, val, is_na, reason_na);
    return;
  }
  if (name == "convergence.nx64_to_nx128.l2_order") {
    if (case_id != "advdiff1d_ref_nx128_shift000_dts100") {
      return;
    }
    found = true;
    convergence_order("advdiff1d_ref_nx064_shift000_dts100", case_id, val, is_na, reason_na);
    return;
  }
  if (name == "convergence.nx128_to_nx256.l2_order") {
    if (case_id != "advdiff1d_ref_nx256_shift000_dts100") {
      return;
    }
    found = true;
    convergence_order("advdiff1d_ref_nx128_shift000_dts100", case_id, val, is_na, reason_na);
    return;
  }
}

}  // namespace advdiff1d_linear_checks
