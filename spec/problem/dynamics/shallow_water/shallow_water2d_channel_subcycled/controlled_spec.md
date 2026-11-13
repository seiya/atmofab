# Controlled Spec: 2D shallow water channel, the member `shallow_water2d_channel` sub-cycled (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d_channel_subcycled`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Problem definition
The target is the 2D shallow water equation on a channel with the Coriolis force and the forcing of the forced translating low, of the member `problem` `shallow_water2d_channel` (its Controlled Spec §1, `spec/problem/dynamics/shallow_water/shallow_water2d_channel/controlled_spec.md`): the conservative form on a flat bottom, periodic along the axis `channel_axis` names and bounded by free-slip walls across it, with the `β`-plane Coriolis parameter and the test cases of Williamson et al. (1992) mapped to the channel. The equations, the domain, the variables and the inputs are the member's; §6 restates the inputs whole.

This `problem` is an **assembled model**: it does not discretize the equations itself. It composes the member, whose `__init` sets up a case and whose `__step` advances the state by one step of its scheme, and SUB-CYCLES the member: one step of this `problem` of length $\Delta t$ (`dt`) is `substeps` consecutive steps of the member, each of length $\Delta t/\mathrm{substeps}$ (§5). `substeps` is a case input (§6). The discrete solution is the composition of §5: with `substeps` $=m$ it is the member's discrete solution with the time step $\Delta t/m$, up to the rounding of the sub-step times (§5).

The equations of the member are invariant under the exchange of the two axes, the two mirrors and the translation along the channel, with the signed inputs transformed as the member's §1 states; the composition of §5 is the same operation on every sub-step, so it inherits each of the member's equivariances (§5).

## 2. Definition of variables and coordinates
The variables and coordinates are the member's (its §2), restated: 2D Cartesian coordinates `x`, `y` in `m`; the along-channel coordinate $s$ and the cross-channel coordinate $r$, $(s, r)=(x, y)$ for `channel_axis=x` and $(y, x)$ for `channel_axis=y`, with the domain lengths $L_s$, $L_r$ and the velocity components $u_s$, $u_r$ along and across the channel.
- `h`: water depth, cell-centered placement, unit `m`
- `hu`: `x`-direction momentum, cell-centered placement, unit `m2/s`
- `hv`: `y`-direction momentum, cell-centered placement, unit `m2/s`

The member's setup-time fields (the per-cell Coriolis parameter `f`, the forcing `Q` of `tc4_translating_low`, the background arrays) are the member's private data, established by the member's `__init` (§5); they are no variable of this `problem` and are in no capture. The derived variables are `u=hu/h`, `v=hv/h`, `c=sqrt(g*h)`; `h<=0` is invalid input.

## 3. Type definition of domain and boundary conditions
The domain is the member's (its §3): the channel $[0,L_x]\times[0,L_y]$, periodic along the channel and bounded by free-slip impermeable walls realized by the mirror ghost across it, on a uniform cell-centered finite-volume grid of `nx` × `ny` interior cells, $dx=L_x/nx$, $dy=L_y/ny$, cell centres $x_i=(i-\tfrac12)dx$, $y_j=(j-\tfrac12)dy$, two ghost cells per side (`ng=2`). The arrays are indexed $(i, j)$ in either orientation, the first array axis `x`. The boundary condition is applied inside the member's step; this `problem` applies none. The default input used for verification is defined in `tests.md`.

## 4. Dependent `component` and adopted `profile`
This `spec` adopts no `profile` and declares no `component`. It declares one member `problem`, which is therefore its one direct dependency. `<ir_ref>/dependency_graph.json` is the only exhaustive statement of the resolved closure (the member's own `component`s are in it, as transitive dependencies). The runner harness is not in it: it belongs to the implementation target the node is built for.

- **Member `problem` `shallow_water2d_channel`**, compatibility constraint `>=0.4.1 <1.0.0`. The procedure names of §5.1 of the member's `Controlled Spec` are exactly `shallow_water2d_channel__init` and `shallow_water2d_channel__step`, which is a condition under which a `problem` is admitted as a member (`docs/CONTROLLED_SPEC.md`), and this `spec` takes both:
  - `shallow_water2d_channel__init`: the member's setup of a case (its §5): it validates the inputs, writes the discrete initial state `h`, `hu`, `hv`, evaluates the member's `dt` procedure over that state and writes `dt` and `n_step_plan`, establishes the member's private data, and returns its result `init_ok`. Its argument list is §5.1's `__init` below, argument for argument.
  - `shallow_water2d_channel__step(nx, ny, dt, t, h, hu, hv, ok)`: ONE step of the member's scheme of length `dt` at the time `t` over the state `h`, `hu`, `hv` (`inout`), returning the logical `ok`: `false` on the member's wet-domain error or a `false` result of a `component` it calls, in which case the content of the state is undefined.

Because the member publishes `__init`, this `spec` publishes `shallow_water2d_channel_subcycled__init` too; it calls the member's `__init` and nothing else of this model names the member's `__init` (§5).

## 5. Integration algorithm
The composition is fixed to the following.

**`shallow_water2d_channel_subcycled__init`** is the setup of a case. It calls `shallow_water2d_channel__init` ONCE, with its own arguments, each passed to the member's argument of the same name and position, and its outputs `h`, `hu`, `hv`, `dt`, `n_step_plan` and `init_ok` are the member's outputs of that call. So the initial state, the time step `dt` of this `problem` and the planned step count are the member's for the case's inputs, and `init_ok` is `false` exactly when the member refuses the case (the member's §6). It is the only place the member's `__init` is called.

**`shallow_water2d_channel_subcycled__step`** advances the state by ONE step of length $\Delta t$ (`dt`) starting at the time `t`, in this order.
1. **Guard of this `spec`.** When `substeps < 1`, the result `ok` is `false` and nothing further runs: the member is not called and the state is unchanged.
2. **The sub-steps.** With $\delta=\Delta t/\mathrm{substeps}$, for $j=1,\dots,\mathrm{substeps}$ in order, call `shallow_water2d_channel__step(nx, ny, δ, t + (j-1)δ, h, hu, hv, member_ok)` on the state itself, so that each call reads the state the previous call wrote. A `false` `member_ok` ends this call with the result `ok` `false`, and no further sub-step runs.
3. When every sub-step returned `true`, the result `ok` is `true`.

The time of the $j$-th sub-step is computed from `t`, $\delta$ and $j$ as above, not accumulated. The step `dt` and the time `t` are the driver's (`tests.md` §3): the driver calls this operation once per step with `t` $=t_{start}+n\,\Delta t$ at step $n$, so the $j$-th sub-step of step $n$ runs at $t_{start}+n\,\Delta t+(j-1)\,\delta$.

The sub-steps are a sequence: each reads the state the previous one wrote, so no two of them may run concurrently. This model has no loop over cells: every loop over cells is the member's.

The composition holds the following invariants.
- **One sub-step is the member.** With `substeps` $=1$, every call's state and result are exactly the member's step from the same inputs, and the run of a case is the member's run of the same case.
- **Sub-cycling is the member at the sub-step.** With `substeps` $=m$ and `dt_scale` $=s$, a case whose step count $N$ satisfies $N(s/m)=m\,N(s)$ — the member's step count at the `dt_scale` $s/m$ is $m$ times the step count at $s$ — runs the member's sequence of steps of the case with `substeps` $=1$ and `dt_scale` $=s/m$: the same step $\Delta t/m$ (for $m$ a power of two the two are the same binary64 value) and the same number of member steps. The sub-step times $t_{start}+n\,\Delta t+(j-1)\,\delta$ and $t_{start}+(mn+j-1)\,\delta$ are the same real number and may round differently, so the two final states are equal exactly where the member does not read the time (every `flow_profile` but `tc4_translating_low`), and equal up to round-off where it does.
- **Mass conservation, the wet domain and the equivariances** are the member's: each sub-step is a step of the member, and the composition transforms with the state.

Private state. This model keeps no value from one call of `shallow_water2d_channel_subcycled__step` to the next outside the state `h`, `hu`, `hv`: the sub-step count, the sub-step length and the sub-step times are computed from this call's arguments at every call. The member's private data is the member's and is established by the member's `__init`, which `shallow_water2d_channel_subcycled__init` calls in every case, in every process that runs the case.

The published `operation_id`s are exactly `shallow_water2d_channel_subcycled__init` and `shallow_water2d_channel_subcycled__step`. The time loop is not the operations': the host-rendered driver calls `shallow_water2d_channel_subcycled__init` once per case and `shallow_water2d_channel_subcycled__step` once per step (`tests.md` §3), and commits the state only on `ok = true`. A failure is that `false` result, never a stop of the program.

**The block.** The arguments `nx`, `ny`, `nx_g`, `ny_g`, `i0`, `j0` are the member's (its §5, the block): `nx`, `ny` are the interior counts of the block an operation is handed, `nx_g`, `ny_g` the case's interior counts, `i0`, `j0` the global indices of the block's first cell. This `spec` passes them through to the member unchanged; `shallow_water2d_channel_subcycled__step` is handed the block of its case's `__init`.

A change breaking compatibility of a published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable language-neutral signature block, under the rules of `docs/CONTROLLED_SPEC.md` §5.1. `dims` gives the bound expressions of an explicit-shape array. `shallow_water2d_channel_subcycled__init` has the member's `__init` argument list; `shallow_water2d_channel_subcycled__step` has the member's `__step` argument list with `substeps` after `t`.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: shallow_water2d_channel_subcycled__init
  args:
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ny
    rank: 0
    intent: in
    spec:
      type: integer
  - name: nx_g
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ny_g
    rank: 0
    intent: in
    spec:
      type: integer
  - name: L_x
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: L_y
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: ng
    rank: 0
    intent: in
    spec:
      type: integer
  - name: i0
    rank: 0
    intent: in
    spec:
      type: integer
  - name: j0
    rank: 0
    intent: in
    spec:
      type: integer
  - name: t_start
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: t_end
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: cfl_target
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: dt_scale
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: g
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: Omega
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: a
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: theta0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: along_axis
    rank: 0
    intent: in
    spec:
      type: integer
  - name: flow_profile_id
    rank: 0
    intent: in
    spec:
      type: integer
  - name: zonal_sign
    rank: 0
    intent: in
    spec:
      type: integer
  - name: gh0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: eta0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: shift_x_fraction
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: u_jet
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: h
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: hu
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: hv
    rank: 2
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: dt
    rank: 0
    intent: out
    spec:
      type: real
      kind: dp
  - name: n_step_plan
    rank: 0
    intent: out
    spec:
      type: integer
  - name: init_ok
    rank: 0
    intent: out
    spec:
      type: logical
- kind: subroutine
  name: shallow_water2d_channel_subcycled__step
  args:
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ny
    rank: 0
    intent: in
    spec:
      type: integer
  - name: dt
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: t
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: substeps
    rank: 0
    intent: in
    spec:
      type: integer
  - name: h
    rank: 2
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: hu
    rank: 2
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: hv
    rank: 2
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: ok
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Model parameters and the runtime input contract
### 6.1 The member's input contract, restated
Every input but `substeps` (6.2) is the member's and reaches the member's `__init` unchanged through §5's `__init`. The member's §6 is restated below whole, so that `tests.md` can state its reference solutions from this `spec` alone. Inside 6.1, a section number (§1, §2, §3, §5, §5.1) names the member's section, "this `problem`" is the member, and `shallow_water2d_channel__init` is the member's `__init`, which this `spec`'s `__init` calls with the same arguments.

The physical constants are those of Williamson et al. (1992), in SI units, with the signs that make the transformed configurations of §1 cases of this `problem`:
- `g = 9.80616 m/s2` (gravitational acceleration)
- `Omega` (unit `1/s`, the signed rotation rate; `Omega = 7.292e-5` for the Earth, and a negative value reverses the sense of rotation; `Omega = 0` is an error)
- `a = 6.37122e6 m` (radius)
- `gh0` (unit `m2/s2`), so `h0 = gh0/g` (reference depth): a case input whose default is `gh0 = 2.94e4` for `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`, and `gh0 = 1.0e5` for `tc4_translating_low`
- `zonal_sign` (`+1` or `-1`; the direction of the flow along the channel, default `+1`; any other value is an error)
- `u0 = zonal_sign*2*pi*a/(12*86400 s)` (reference velocity along the channel of `tc2_zonal_uniform`, `tc3_compact_jet` and `tc2_zonal_perturbed`; one revolution in 12 days)
- `theta0` (the signed reference latitude of the `β`-plane, default `pi/4`; $0<|\theta_0|<\pi/2$, and any other value is an error)

`h0`, `u0`, and the `f_0`, `β`, $\sigma$ below are derived from these inputs by the stated expressions wherever they are used; none of them is an independent input, and no rounded literal of a derived value stands in for the expression.

The orientation is specified by `channel_axis`, and only the values `x` and `y` are allowed (default `x`). It fixes the along- and cross-channel coordinates of §2 and the orientation sign $\sigma=+1$ for `channel_axis=x`, $\sigma=-1$ for `channel_axis=y`. The geostrophic balance of a flow along the channel, $g\,\nabla h=-f\,\hat k\times\mathbf u$, reads $g\,\partial_r h=-\sigma\,f\,u_s$ in either orientation.

The Coriolis profile is specified by `coriolis_profile`, and only the value `beta_plane` is allowed:
$$
f(r)=f_0+\beta\,\left(r-\tfrac{L_r}{2}\right),\qquad
f_0=2\,\Omega\sin\theta_0,\qquad
\beta=\frac{2\,\Omega\cos\theta_0}{a}
$$
evaluated once at setup at every interior cell centre, as the per-cell field $f_{i,j}=f(r_{i,j})$ with $r_{i,j}=y_j$ for `channel_axis=x` and $r_{i,j}=x_i$ for `channel_axis=y`.

The initial condition is specified by `flow_profile`, and only the 4 values `tc2_zonal_uniform`, `tc3_compact_jet`, `tc2_zonal_perturbed` and `tc4_translating_low` are allowed. The first two are steady flows along the channel, uniform in $s$, geostrophically balanced, with $u_r=0$ and $g\,\partial_r h=-\sigma\,f\,u_s$, discretized at the cell centres with the along-channel momentum $h\,u_s$ and the cross-channel momentum $0$; the third is the first with a depth perturbation that depends on $s$, and is not steady.
- `tc2_zonal_uniform` (Test Case 2): $u_s(r)=u_0$ and
$$
h(r)=h_0-\sigma\,\frac{u_0}{g}\left[f_0\left(r-\tfrac{L_r}{2}\right)+\frac{\beta}{2}\left(r-\tfrac{L_r}{2}\right)^2\right]
$$
- `tc3_compact_jet` (Test Case 3): with $r_b=L_r/8$, $r_e=7L_r/8$, $x_e=0.3$, $\zeta(r)=x_e\,(r-r_b)/(r_e-r_b)$ and $b(\zeta)=0$ for $\zeta\le0$, $b(\zeta)=e^{-1/\zeta}$ for $\zeta>0$,
$$
u_s(r)=u_0\,b(\zeta)\,b(x_e-\zeta)\,e^{4/x_e},\qquad
h(r)=h_0-\frac{\sigma}{g}\int_0^{r}f(r')\,u_s(r')\,dr'
$$
- `tc2_zonal_perturbed`: with $h_{TC2}(r)$ the `tc2_zonal_uniform` depth above and the parameters `eta0` (`m`, $\eta_0>0$) and `shift_x_fraction` (dimensionless, in $[0,1)$; the shift is along the channel, along `y` for `channel_axis=y` despite the name),
$$
h(s,r)=h_{TC2}(r)+\eta_0\,\sin\left(2\pi\left(\frac{s}{L_s}-\mathrm{shift\_x\_fraction}\right)\right),\qquad u_s=u_0,\qquad u_r=0
$$
discretized at the cell centres with the along-channel momentum $h\,u_0$ and the cross-channel momentum $0$. The perturbation is a wavenumber-one gravity-wave excitation of the channel: it is the unforced initial condition of this `problem` that is not uniform along the channel, so it is the one that exercises the along-channel interface flux and the periodic mapping without the forcing.

- `tc4_translating_low` (Test Case 4): the prescribed flow is a geostrophic background jet plus a Gaussian low that translates along the channel. With the parameter `u_jet` (`m/s`, $u_{jet}>0$; the jet maximum, the $u_0$ of Test Case 4), $U_j=\mathrm{zonal\_sign}\cdot u_{jet}$, $k=\pi/L_r$ and $h_0=gh_0/g$ with the `gh0` of the case, the background is
$$
\bar u(r)=U_j\sin^2(kr),\qquad
\bar h(r)=h_0-\sigma\,\frac{U_j}{g}\left[F(r)-F\!\left(\tfrac{L_r}{2}\right)\right]
$$
$$
F(r)=f_0\left(\frac r2-\frac{\sin 2kr}{4k}\right)+\beta\left(\frac{r^2}{4}-\frac{L_r\,r}{4}-\frac{(r-L_r/2)\sin 2kr}{4k}-\frac{\cos 2kr-1}{8k^2}\right)
$$
where $F(r)=\int_0^r f(r')\sin^2(kr')\,dr'$ in closed form, so $g\,\partial_r\bar h=-\sigma\,f\,\bar u$ and $\bar h(L_r/2)=h_0$: the background depth is referred to the channel centre, which makes the reflection across the channel (§5) a case of this `problem` without a change of `gh0`. $\bar u$ is the background velocity along the channel; its cross-channel component is zero. The low has the streamfunction
$$
\psi(s,r,t)=\psi_0\,e^{-\rho^2/R_{low}^2},\qquad
\rho^2=\left(\frac{L_s}{\pi}\right)^2\sin^2\xi+(r-r_c)^2,\qquad
\xi=\frac{\pi\,(s-s_{c0}-c_{tr}\,t)}{L_s}
$$
with $\psi_0=-0.03\,gh_0/f_0$, $R_{low}=2a/12.74244$ (so $R_{low}=1.0\times10^6\ \mathrm{m}$ to the digits given), $c_{tr}=U_j\cos\theta_0$, $s_{c0}=L_s/2$ and $r_c=L_r/2$, and its derivatives along and across the channel
$$
\psi_s=-\frac{L_s}{\pi R_{low}^2}\sin 2\xi\;\psi,\qquad
\psi_r=-\frac{2\,(r-r_c)}{R_{low}^2}\;\psi
$$
assigned to the derivatives in `x` and `y` by the orientation: $(\psi_x, \psi_y)=(\psi_s, \psi_r)$ for `channel_axis=x` and $(\psi_x, \psi_y)=(\psi_r, \psi_s)$ for `channel_axis=y` (the forcing `component`'s §3 states the same derivatives and the second derivatives its residuals need). The prescribed flow is, with the streamfunction convention $u=-\psi_y$, $v=\psi_x$ of either orientation,
$$
\texttt{channel\_axis}=x:\quad \tilde u=\bar u-\psi_y,\qquad \tilde v=\psi_x;\qquad\qquad
\texttt{channel\_axis}=y:\quad \tilde u=-\psi_y,\qquad \tilde v=\bar u+\psi_x
$$
$$
\tilde h=\bar h+\frac{f\,\psi}{g}
$$
and it is an exact solution of the equations of §1 with the forcing $Q$ that `dynamics_shallow_water_source_2d_tc4_forcing` §3 defines, with that `component`'s `S` as $Q$, its `along_axis` as in §5 step 6, its per-cell arguments `f`, `dfdr`, `ubar`, `dubar`, `hbar` the values at the cell centre of $f(r)$, $\beta$, $\bar u(r)$, $\bar u'(r)=U_j\,k\sin 2kr$ and $\bar h(r)$, and `x`, `y`, `g`, `L_along` $=L_s$, `psi0`, `R_low`, `c_tr`, `s_c0`, `r_c` the values above. The derivatives `dfdr` and `dubar` are the closed forms stated here and not differences of the arrays. The five per-cell arrays, the cell-centre coordinates and the parameters of the low are evaluated once at setup. The initial state is the prescribed flow at $t=0$ at the cell centres $(x_i, y_j)$: $h=\tilde h$, $hu=\tilde h\,\tilde u$, $hv=\tilde h\,\tilde v$. The reference solution at time $t$ is the prescribed flow at $t$; the centre of the low moves a distance $c_{tr}\,t$ along the channel, so $\tilde h(x,y,t)-\tilde h(x,y,0)=f\,(\psi(x,y,t)-\psi(x,y,0))/g$.

The integral of `tc3_compact_jet` is evaluated by composite Simpson quadrature on a uniform sub-grid of spacing $\Delta r/64$ (with $\Delta r=dy$ for `channel_axis=x`, $dx$ for `channel_axis=y`) from $r'=0$ to the $j$-th cell-centre cross-channel coordinate $r_j=(j-\tfrac12)\Delta r$, with $j$ the grid's line index (§5, the block), which spans $64j-32$ sub-intervals (an even count for every $j$); this quadrature is part of the definition of the discrete initial state and is not an implementation choice. $u_s$ has compact support in $(r_b, r_e)$ and its largest magnitude is $|u_0|$ at $\zeta=x_e/2$. Because $r_b+r_e=L_r$ and $u_s$ is symmetric about $L_r/2$, the $\beta$ term of $f$ integrates to zero over the support, and the depth beyond the jet ($r>r_e$) is the constant
$$
h_N=h_0-\sigma\,\frac{f_0\,u_0\,e^{4/x_e}}{g}\,\frac{r_e-r_b}{x_e}\,C,\qquad C=\int_0^{x_e}e^{-1/\zeta-1/(x_e-\zeta)}\,d\zeta=1.12064479227927\times10^{-7}
$$
which the discrete initial state reproduces at every line beyond $r_e$ to round-off: the integrand is smooth with compact support inside $[0,r_j]$ for those lines, so the composite rule's error on the complete integral is below round-off. The depth before the jet ($r<r_b$) is $h_0$ exactly, since the integral is zero there. Inside the jet the rule's error against the exact partial integral is below $2\times10^{-12}$ relative at every grid of `tests.md`. $C$ has no closed form; its value is defined by the digits given. The branch-free form $b(\zeta)=\exp\left(-1/\max(\zeta,\varepsilon)\right)$ with $\varepsilon=10^{-300}$ is equal to $b(\zeta)$ at every cell centre in double precision (for $\zeta\le0$ the exponent is $-10^{300}$ and the exponential underflows to exactly $0$; no cell centre has $\zeta=0$, because $r_b$ and $r_e$ lie on cell edges when the cross-channel extent is a multiple of 8, and the sweep of `tests.md` uses such extents), and either form may be used.

The runtime input requires the following.
- `L_x`, `L_y`, `nx`, `ny`, `ng`
- `channel_axis`
- `coriolis_profile`, and the constants `g`, `Omega`, `a`, `theta0`
- `flow_profile`, `zonal_sign`, and for `tc2_zonal_perturbed` its parameters `eta0`, `shift_x_fraction`, and for `tc4_translating_low` its parameter `u_jet`
- `gh0` (default: the value of the case's `flow_profile`, above)
- `t_start`, `t_end`
- `dt_rule`, with its `cfl_target` and `dt_scale`

The arguments of §5.1 are numbers, so two of these inputs reach `shallow_water2d_channel__init` by number: `channel_axis` as `along_axis`, the array axis along the channel of §5 step 6 (`1` for `x`, `2` for `y`), and `flow_profile` as `flow_profile_id` (`1` for `tc2_zonal_uniform`, `2` for `tc3_compact_jet`, `3` for `tc2_zonal_perturbed`, `4` for `tc4_translating_low`). `coriolis_profile` and `dt_rule` each have one allowed value and are no argument. `eta0`, `shift_x_fraction` and `u_jet` are arguments in every case, and a `flow_profile` that does not define one does not read it. The four further arguments `nx_g`, `ny_g`, `i0` and `j0` are no runtime input: `nx_g` and `ny_g` carry the values of `nx` and `ny`, and `i0` and `j0` locate the block an operation is handed (§5).

`along_axis` other than `1` or `2`, `flow_profile_id` outside `1` to `4`, `ng` other than `2`, `zonal_sign` other than `+1` or `-1`, `Omega = 0`, and `theta0` outside $0<|\theta_0|<\pi/2$ are errors at setup: `shallow_water2d_channel__init` writes `init_ok` as `false`.

In the initial state, `h>0` in all cells is required. The depth of each profile is extremal at a wall, and the transformations of §5 map the channel onto itself with the depth field reflected or exchanged, so they leave its minimum unchanged; the values below are those of `channel_axis=x` with every sign positive. For `tc2_zonal_uniform` this bounds the channel width: $h$ is smallest at the wall $r=L_r$, $h(L_r)=h_0-\dfrac{u_0}{g}\left[\dfrac{f_0 L_r}{2}+\dfrac{\beta L_r^2}{8}\right]$, which is positive for $L_r<1.046\times10^7\ \mathrm{m}$ with the constants above ($h(L_r)=1493\ \mathrm{m}$ at $L_r=6\times10^6\ \mathrm{m}$); for `tc2_zonal_perturbed` the bound is $h(L_r)>\eta_0$. For `tc4_translating_low`, $\bar h$ is smallest at the wall $r=L_r$, $\bar h(L_r)=h_0-\dfrac{u_{jet}}{g}\left[\dfrac{f_0L_r}{4}+\beta L_r^2\left(\dfrac1{16}-\dfrac1{4\pi^2}\right)\right]$ ($9199\ \mathrm{m}$ at $u_{jet}=40\ \mathrm{m/s}$, $L_r=8\times10^6\ \mathrm{m}$), and the low lowers the depth at its centre by $|f(r_c)\,\psi_0|/g=0.03\,h_0$ ($306\ \mathrm{m}$ at $gh_0=1.0\times10^5$). A channel outside the bound is an error at setup, not a value to clip: an initial state with `h<=0` in a cell writes `init_ok` as `false`. An undefined parameter is an error without implicit completion.


### 6.2 The input of this `spec`
- `substeps`: the number of member steps one step of §5 makes, an integer. A value below `1` is refused by step 1 of §5 at every call: the result is `false` and no member step runs. It is passed to `shallow_water2d_channel_subcycled__step` at every call and enters no other operation: the member's `dt` procedure, run by the member's `__init`, does not read it, so `dt` is the member's for the case's `dt_scale` and the member is advanced by `dt / substeps`.

The `dt` rule is the member's (its §3 in `tests.md`), evaluated by the member's `__init` over the initial state. A case whose sub-step $\Delta t/\mathrm{substeps}$ lies outside the member's domain of validity (its wet-domain invariant) is refused by the member's step, and so by step 2 of §5. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Everything the member's §7 forbids is forbidden here; the scheme of a member step is the member's, and this `spec` adds no term to it. Further:
- Forbid calling the member's `__step` more or fewer than `substeps` times per call of `shallow_water2d_channel_subcycled__step`, with a step other than `dt / substeps`, at a time other than `t + (j-1)·dt/substeps`, on a copy of the state whose result is not the state the next sub-step reads, or concurrently.
- Forbid continuing after a `false` result of a sub-step, and forbid a result other than `false` for that call.
- Forbid calling the member's `__init` anywhere but in `shallow_water2d_channel_subcycled__init`, more or less than once per call of it, with other arguments than that call's own, or under a condition; and forbid a result `init_ok` other than the member's.
- Forbid computing a case by transposing or reflecting its state, its grid or its inputs to the orientation or sign of another case, computing there and transforming the result back; the member computes each case in its own orientation, from the inputs as given (`docs/TESTS.md` §Symmetry pairs).
- Forbid carrying anything from one call of `shallow_water2d_channel_subcycled__step` to the next outside the state of §5: no step counter, clock, sub-step length, sub-step count, tendency or reference state the model keeps and a later call reads, and no value cached at the first call of a process or of a case. A value the model keeps privately is established by `shallow_water2d_channel_subcycled__init` from the current case's inputs, in every process that runs the case, or set by `shallow_water2d_channel_subcycled__step` from its own arguments at every call before it is read. `tests.md` continues a case from a state sample in a process of its own and judges its final state equal to the uninterrupted run's (`docs/TESTS.md`, the `restart` line), so a kernel that carries state between calls fails there.

## 8. Traceability
The resolution result is recorded by the host: `<ir_ref>/ir_meta.json` carries the node's own `spec_kind` / `spec_id` / `spec_version`, and `<ir_ref>/dependency_graph.json` carries the resolved closure — `all_nodes` as `<spec_kind>/<spec_id>@<version>` for every node of it: this `spec`, the member `problem` and the member's `component`s. No `profile` is adopted by this `spec`; the member's adopted `profile` resolves within the member.

The reference basis is the member's (Williamson et al. 1992, LeVeque 2002, Toro 2009, van Leer 1977; the member's §8). The assembled-model mechanism is that of the system tier of `docs/design/scale_migration.md` §Order item 3: Part 1 for a `problem` that composes a `problem`, and Part 2 ([issue #632](https://github.com/seiya/spec2science/issues/632)) for a member that publishes `__init` and for the restart test; this `spec` is Part 2's sample of both.

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d_channel_subcycled/tests.md`, with `test_profile_version` of `0.1.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `false`. The member's step is differentiable under the member's §10, and the composition is the member's step repeated; the guard and the sub-step count are integer branches on an input, not on the state. AD of an assembled model is not prepared in this version.
