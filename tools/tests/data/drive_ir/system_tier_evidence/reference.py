#!/usr/bin/env python3
"""Independent reference of `problem/shallow_water2d_rotating` (system tier PR-S2), numpy, no
repository import: the member's Controlled Spec §5 scheme on FLAT topography (periodic ghost,
`p0` interface states, Rusanov flux, the two-stage SSPRK2 of its step 5; with `z_b = 0` the
hydrostatic reconstruction is the identity and `S_b = 0`), then, when `rotation == 1` and
`mod(step + 1, k) == 0` (`step` the committed count before the call), one classical RK4 step
of length `k*dt` of the Coriolis source `[0, f*hv, -f*hu]`. Lie splitting, member first.

Written from the Controlled Specs, not from a generated source. `order="swapped"` runs the
rotation BEFORE the member (mutant 1), for the margin of judgment 6.
"""
from __future__ import annotations

import numpy as np

G = 9.81


def _flux_x(h, hu, hv, g):
    u = hu / h
    return hu, hu * u + 0.5 * g * h * h, hu * (hv / h)


def _flux_y(h, hu, hv, g):
    v = hv / h
    return hv, hu * v, hv * v + 0.5 * g * h * h


def rhs(h, hu, hv, dx, dy, g=G):
    """L_flux of the member's step 4 on a doubly periodic flat grid (axis 0 = x, axis 1 = y)."""
    out = []
    # x interfaces: i+1/2 between cell i and i+1 (periodic)
    hR, huR, hvR = (np.roll(a, -1, axis=0) for a in (h, hu, hv))
    FL = _flux_x(h, hu, hv, g)
    FR = _flux_x(hR, huR, hvR, g)
    ax = np.maximum(np.abs(hu / h) + np.sqrt(g * h), np.abs(huR / hR) + np.sqrt(g * hR))
    Fs = [0.5 * (fl + fr) - 0.5 * ax * (ur - ul)
          for fl, fr, ul, ur in zip(FL, FR, (h, hu, hv), (hR, huR, hvR))]
    hT, huT, hvT = (np.roll(a, -1, axis=1) for a in (h, hu, hv))
    GB = _flux_y(h, hu, hv, g)
    GT = _flux_y(hT, huT, hvT, g)
    ay = np.maximum(np.abs(hv / h) + np.sqrt(g * h), np.abs(hvT / hT) + np.sqrt(g * hT))
    Gs = [0.5 * (gb + gt) - 0.5 * ay * (ut - ub)
          for gb, gt, ub, ut in zip(GB, GT, (h, hu, hv), (hT, huT, hvT))]
    for F, Gf in zip(Fs, Gs):
        out.append(-(F - np.roll(F, 1, axis=0)) / dx - (Gf - np.roll(Gf, 1, axis=1)) / dy)
    return out


def member_step(h, hu, hv, dx, dy, dt, g=G):
    U = (h, hu, hv)
    L0 = rhs(*U, dx, dy, g)
    U1 = tuple(a + dt * l for a, l in zip(U, L0))
    L1 = rhs(*U1, dx, dy, g)
    U2 = tuple(a + dt * l for a, l in zip(U1, L1))
    return tuple(0.5 * a + 0.5 * b for a, b in zip(U, U2))


def coriolis(h, hu, hv, f):
    return np.zeros_like(h), f * hv, -f * hu


def rk4_rotation(h, hu, hv, f, K):
    U = (h, hu, hv)
    k1 = coriolis(*U, f)
    k2 = coriolis(*(a + 0.5 * K * k for a, k in zip(U, k1)), f)
    k3 = coriolis(*(a + 0.5 * K * k for a, k in zip(U, k2)), f)
    k4 = coriolis(*(a + K * k for a, k in zip(U, k3)), f)
    return tuple(a + K / 6.0 * (p + 2 * q + 2 * r + s) for a, p, q, r, s in zip(U, k1, k2, k3, k4))


def dt_rule(h, hu, hv, dx, dy, t_start, t_end, cfl_target=0.45, dt_scale=1.0, g=G):
    """The member's tests.md §3 rule: n_step = ceil(T / dt_raw), dt = T / n_step."""
    c = np.sqrt(g * h)
    lam0 = np.max((np.abs(hu / h) + c) / dx + (np.abs(hv / h) + c) / dy)
    dt_raw = dt_scale * cfl_target / lam0
    n = int(np.ceil((t_end - t_start) / dt_raw))
    return n, (t_end - t_start) / n


def run(h, hu, hv, f, *, dx, dy, dt, n_step, rotation=1, every=1, order="stated",
        history_every=0, g=G, member=True):
    """Returns (final (h, hu, hv), [(step, h, hu, hv) history samples])."""
    hist = []
    U = (h.copy(), hu.copy(), hv.copy())
    for n in range(1, n_step + 1):
        step = n - 1
        fire = rotation == 1 and (step + 1) % every == 0
        if order == "swapped" and fire:
            U = rk4_rotation(*U, f, every * dt)
        if member:
            U = member_step(*U, dx, dy, dt, g)
        if order == "stated" and fire:
            U = rk4_rotation(*U, f, every * dt)
        if history_every and n % history_every == 0:
            hist.append((n, *U))
    return U, hist
