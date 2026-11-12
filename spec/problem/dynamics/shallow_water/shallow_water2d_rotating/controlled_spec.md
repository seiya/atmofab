# Controlled Spec: 2D rotating shallow water, assembled from `shallow_water2d` and a Coriolis process (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d_rotating`
- `spec_version`: `0.1.1`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Problem definition
The target is the conservative 2D shallow water equation of the member `problem` `shallow_water2d` with a Coriolis source term added to the two momentum equations:
$$
\frac{\partial h}{\partial t}
+ \frac{\partial (hu)}{\partial x}
+ \frac{\partial (hv)}{\partial y}
= 0
$$
$$
\frac{\partial (hu)}{\partial t}
+ \frac{\partial }{\partial x}\left(hu^2 + \frac{1}{2} g h^2\right)
+ \frac{\partial (huv)}{\partial y}
= -gh\frac{\partial z_b}{\partial x} + f\,(hv)
$$
$$
\frac{\partial (hv)}{\partial t}
+ \frac{\partial (huv)}{\partial x}
+ \frac{\partial }{\partial y}\left(hv^2 + \frac{1}{2} g h^2\right)
= -gh\frac{\partial z_b}{\partial y} - f\,(hu)
$$
The Coriolis parameter $f(x,y)$ is a time-invariant field given per cell; it may vary along both axes.

This `problem` is an **assembled model**: it does not discretize the dynamics itself. It composes the member `problem` `shallow_water2d`, which advances every term but the Coriolis source, and a rotation process, the Coriolis source of the `component` `dynamics_shallow_water_source_2d_coriolis` integrated by the `component` `dynamics_shallow_water_time_update_2d_rk4`, by Lie (first-order) operator splitting, member first (§5). The rotation process is called every `rotation_every` member steps with a time step of `rotation_every` times the member's, and it is switched on and off at run time by an input (§5, §6). The splitting is a property of this `spec`: the discrete solution is the composition of §5, not a discretization of the equations above that converges faster than first order in time.

## 2. Definition of variables and coordinates
The coordinate system is 2D Cartesian coordinates, the coordinate names are `x`,`y`, and the unit is `m`.
- `h`: water depth, cell-centered placement, unit `m`
- `hu`: `x`-direction momentum, cell-centered placement, unit `m2/s`
- `hv`: `y`-direction momentum, cell-centered placement, unit `m2/s`
- `z_b`: bottom topography, cell-centered placement, unit `m`, time-invariant
- `f`: Coriolis parameter, cell-centered placement, unit `1/s`, time-invariant

The sign convention of `f` is the Coriolis `component`'s: `f>0` turns a `+x` momentum toward `-y` and a `+y` momentum toward `+x`.

The equations of §1 are invariant under the exchange of the two axes, $(h, hu, hv)(x, y)\to(h, hv, hu)(y, x)$ with $z_b(x, y)\to z_b(y, x)$ and $f(x,y)\to -f(y,x)$; under the reflection $x\to L_x-x$ with $hu\to-hu$ and $f\to -f$; under the reflection $y\to L_y-y$ with $hv\to-hv$ and $f\to -f$; and under a translation of every field along either axis (the domain is doubly periodic). A reflection and the exchange reverse the sense of rotation, so the Coriolis parameter of the transformed configuration is the negated, transformed field. Every field of the initial state and `f` are inputs (`tests.md`), so the transformed configuration of each is a case of this `problem`, and §5 states the equivariance of the composition under each.

## 3. Type definition of domain and boundary conditions
The domain is the member's: the orthogonal periodic domain $[0,L_x)\times[0,L_y)$ with a uniform cell-centered finite-volume grid, $dx=L_x/nx$, $dy=L_y/ny$, and periodic boundaries on all sides. The boundary condition is applied inside the member's step; the rotation process is pointwise and needs none.

The runtime inputs are the member's (grid, topography, initial state, time, `dt` rule, history stride) and this `spec`'s own: the field `f`, the selector `rotation` and the interval `rotation_every` (§6). The default input used for verification is defined in `tests.md`.

## 4. Dependent `component` and adopted `profile`
This `spec` adopts no `profile`. It declares one member `problem` and two `component`s directly, each of which is therefore a direct dependency of this `spec`. `<ir_ref>/dependency_graph.json` is the only exhaustive statement of the resolved closure (the member's own `component`s are in it, as transitive dependencies). The runner harness is not in it: it belongs to the implementation target the node is built for.

- **Member `problem` `shallow_water2d`**, compatibility constraint `>=0.5.1 <1.0.0`. This `spec` takes from it the operation `shallow_water2d__step`: ONE step of the bottom-topography shallow water equation without rotation (its §5), over `h`, `hu`, `hv` (`inout`) and `z_b` (`in`), with the grid, the spacings, `g`, `dt` and `t`, returning a logical result. The procedure names of §5.1 of the member's `Controlled Spec` are exactly `shallow_water2d__step`, which is the condition under which a `problem` is admitted as a member.
- **`component` `dynamics_shallow_water_source_2d_coriolis`**, compatibility constraint `>=0.2.0 <1.0.0`: the operation `dynamics_shallow_water_source_2d_coriolis__apply`, the pointwise source $[0,\ f\,hv,\ -f\,hu]$ of a conserved-variable field `U` ordered `[h, hu, hv]` along its first extent, with a guard result.
- **`component` `dynamics_shallow_water_time_update_2d_rk4`**, compatibility constraint `>=0.1.1 <1.0.0`: the operation `dynamics_shallow_water_time_update_2d_rk4__advance`, one classical four-stage Runge-Kutta step of a tendency procedure over a field `U` of `ncomp` components, with a guard result.

The application order is §5's: the member, then, on a step where the rotation process fires, one RK4 step of the Coriolis source.

## 5. Integration algorithm
One call of `shallow_water2d_rotating__step` advances the state `h`, `hu`, `hv` by ONE member step of length $\Delta t$ (`dt`), and on a step where the rotation process fires, additionally by one rotation step of length $k\,\Delta t$, $k$ = `rotation_every`. The call receives `step`, the driver's committed step count before this call (`0` on the first call of a case that starts at its initial state, `K` on the first call of a continuation case that `tests.md` continues from step `K`), and fires the rotation process when `rotation = 1` and $\mathrm{mod}(\mathrm{step}+1,\ k)=0$, that is, after the member steps $k, 2k, 3k, \ldots$ of the case. Over a run of $N$ steps the rotation process is called $\lfloor N/k \rfloor$ times.

The step is fixed to the following order.
1. **Guards of this `spec`.** When `rotation` is neither `0` nor `1`, or `rotation_every < 1`, the result is `false` and nothing further runs: neither the member nor a `component` is called.
2. **The member.** Call `shallow_water2d__step` ONCE, with this call's `nx`, `ny`, `dx`, `dy`, `g`, `dt`, `t`, `z_b` and the state `h`, `hu`, `hv` themselves, so that the member's output is the state the rest of the step reads. A `false` result of the member is this step's `false` result, and nothing further runs.
3. **The rotation process, when it fires.** When `rotation = 0`, or when it does not fire on this step, the step ends here with result `true`, and the state is exactly the member's output. When it fires, take ONE step of `dynamics_shallow_water_time_update_2d_rk4__advance` of length $k\,\Delta t$, starting at time `t`, over the field $U=[h, hu, hv]$ (`ncomp = 3`, the components along the first extent, `nx` × `ny` interior cells, no ghost cells) packed from the member's output, with the tendency
$$
R(t, U) = S(U),\qquad S_{\cdot,i,j}=\left[0,\ f_{i,j}\,(hv)_{i,j},\ -f_{i,j}\,(hu)_{i,j}\right]
$$
which is `dynamics_shallow_water_source_2d_coriolis__apply` of the stage state RK4 hands to the tendency, with THIS call's `f`. The RK4 result unpacked into `h`, `hu`, `hv` is the state after the step.

The requirements on the rotation process, whatever means of its language the model uses to meet them:
- The tendency RK4 evaluates is the Coriolis source of the state it is handed, with this call's `f`, in each of the four stages. The RK4 tendency prototype carries neither `f` nor a logical: the model carries them by private state `__step` sets from its own arguments at every call before RK4 (and resets before RK4 and reads after it, for the flag), or by a tendency that reads this call's `f` directly. A value of `f` from an earlier call, or from an earlier case, is not this call's `f`.
- A `false` guard of the Coriolis operation in any stage, and a `false` guard of RK4, each make this step's result `false`. Neither can overwrite a `false` result of the member, because step 2 ends the call there.
- `U` is packed and unpacked over the interior cells with the extents `nx` × `ny` of this call: cell $(i,j)$ of `h`, `hu`, `hv` is cell $(i,j)$ of `U`.

The rotation process conserves `h` exactly (the source has no mass component, and an RK4 step of a zero tendency is the identity), so mass conservation is the member's.

The composition holds the following invariants.
- **The process off is the member.** With `rotation = 0`, every call's state is exactly the state the member's step gives from the same inputs: `f` and `rotation_every` do not enter the result.
- **The rotation alone is the RK4 polynomial.** On a state the member leaves unchanged (a flat bottom, uniform `h`, uniform `hu`, `hv`) and a uniform `f`, the complex momentum $w=hu+\mathrm{i}\,hv$ after $m$ rotation calls is $w_0\,P(-\mathrm{i}\,f\,k\,\Delta t)^m$ with $P(z)=1+z+z^2/2+z^3/6+z^4/24$, up to round-off.
- **Mass conservation.** $\sum_{i,j}h_{i,j}$ is conserved up to round-off.
- **Axis-exchange equivariance.** The case with `nx` ↔ `ny`, `L_x` ↔ `L_y`, the initial state transposed with its two momentum components exchanged ($(h, hu, hv)'_{i,j}=(h, hv, hu)_{j,i}$) and $f'_{i,j}=-f_{j,i}$ has, at every step, the state of the original case transposed the same way, up to round-off.
- **Mirror equivariance in `x`.** The case with the initial state reflected along `x` with `hu` sign-reversed ($(h, hu, hv)'_{i,j}=(h, -hu, hv)_{nx+1-i,j}$) and $f'_{i,j}=-f_{nx+1-i,j}$ has, at every step, the state of the original case reflected the same way, up to round-off.
- **Mirror equivariance in `y`.** The same with the reflection along `y`, `hv` sign-reversed and $f'_{i,j}=-f_{i,ny+1-j}$.
- **Translation equivariance.** The case with every field moved by a whole number of cells along either axis has, at every step, the state of the original case moved the same way, up to round-off.

The round-off of the equivariances is not zero in general, for the reasons the member's §5 states.

The published `operation_id`s are exactly: `shallow_water2d_rotating__step`. One call advances the state `h`, `hu`, `hv` (`inout`) by ONE step of this section, over the time-invariant `z_b` and `f` (`in`), with the grid `nx`, `ny`, the spacings `dx`, `dy`, the gravitational acceleration `g`, the step `dt`, the time `t` at the start of the step, the committed step count `step`, the selector `rotation` and the interval `rotation_every` (all `in`), and returns the logical `ok`: `true` when the step completed, `false` on a failure of this section (a guard of step 1, a `false` result of the member, or a `false` guard of the Coriolis operation or of RK4), in which case the content of `h`, `hu`, `hv` is undefined. A failure is that `false` result, never a stop of the program. The initial state, `f`, the step count and `dt` are the host's (`tests.md` §2, §3); the host's driver calls the operation once per step and commits the state only on `ok = true`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`), in the form of a `component`'s §5.1.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: shallow_water2d_rotating__step
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
  - name: dx
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: dy
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
  - name: step
    rank: 0
    intent: in
    spec:
      type: integer
  - name: rotation
    rank: 0
    intent: in
    spec:
      type: integer
  - name: rotation_every
    rank: 0
    intent: in
    spec:
      type: integer
  - name: f
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
  - name: z_b
    rank: 2
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - nx
    - ny
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
The physical constants and the bottom topography are the member's: `g=9.81 m/s2`, `H_0=1.0 m`, `topography_profile` in `williamson_tc5_cone` and `flat` with the cone of the member's §6. The member's runtime input contract applies unchanged (its §6), and this `spec` adds:
- `rotation`: the selector of the rotation process, an integer, `1` on and `0` off. Any other value is refused by step 1 of §5.
- `rotation_every`: the interval $k$ of the rotation process in member steps, an integer of at least `1`. A smaller value is refused by step 1 of §5. It is read whatever the value of `rotation`.
- `f`: the Coriolis parameter per cell, unit `1/s`. It is host data: `tests.md` states it as a closed form over the inputs, and the host evaluates it per case and hands it to the driver with the initial state.

The `dt` rule is the member's, host data: `tests.md` §3 states it, and `f` does not enter it. A case in which $\max_{i,j}|f_{i,j}|\,k\,\Delta t > 2\sqrt{2}$ lies outside the stability interval of RK4 on the imaginary axis, and outside the domain of validity of this `spec`; no step refuses it. The member's conditions on the initial state (`h>0`, the free surface above the bottom of the face neighbours) apply. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Everything the member's §7 forbids is forbidden here; in particular the scheme of the member's step is the member's, and this `spec` adds no term to it. Further:
- Forbid calling the member more or less than once per call of `__step`, with a time step other than `dt`, or on a copy of the state whose result is not the state the rest of the step reads.
- Forbid integrating the rotation process with anything but ONE call of `dynamics_shallow_water_time_update_2d_rk4__advance` of length $k\,\Delta t$ per firing step (no sub-steps, no other integrator, no closed-form rotation), and forbid any other firing rule than §5's.
- Forbid reading `f` other than per cell, from this call's argument: a constant, a mean, a value retained from an earlier call or an earlier case.
- Forbid computing a case by transposing or reflecting its state, its grid, its `f` or its topography to the orientation of another case, computing there and transforming the result back; the kernel computes each case in its own orientation, from the inputs as given (`docs/TESTS.md` §Symmetry pairs).
- Forbid carrying anything from one call of `shallow_water2d_rotating__step` to the next outside the state of §5: no step counter, clock, tendency, stage value or reference state the model keeps and a later call reads, and no value cached at the first call of a process or of a case. A value the model keeps privately is set by `shallow_water2d_rotating__step` from its own arguments at every call before it is read. `tests.md` continues a case from a state sample in a process of its own and judges its final state equal to the uninterrupted run's (`docs/TESTS.md`, the `restart` line), so a kernel that carries state between calls fails there.

## 8. Traceability
The resolution result is recorded by the host: `<ir_ref>/ir_meta.json` carries the node's own `spec_kind` / `spec_id` / `spec_version`, and `<ir_ref>/dependency_graph.json` carries the resolved closure — `all_nodes` as `<spec_kind>/<spec_id>@<version>` for every node of it: this `spec`, the member `problem`, the member's `component`s and this `spec`'s two `component`s. No `profile` is adopted by this `spec`; the member's adopted `profile` resolves within the member.

The reference basis is that of the member (Williamson et al. 1992, Audusse et al. 2004, LeVeque 2002, Toro 2009), with the inertia-gravity (Poincaré) wave of the linearised rotating shallow water equations for the frequency judgment of `tests.md` (Gill 1982, *Atmosphere-Ocean Dynamics*, §7.3). The assembled-model mechanism (a `problem` that composes `problem`s) is that of the system tier, Part 1, of `docs/design/scale_migration.md` §Order item 3.

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d_rotating/tests.md`, with `test_profile_version` of `0.2.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `false`. The member's step is differentiable under the member's §10, and the rotation process is linear in the state; the selector and the firing rule are integer branches on inputs, not on the state. AD of an assembled model is not prepared in this version.
