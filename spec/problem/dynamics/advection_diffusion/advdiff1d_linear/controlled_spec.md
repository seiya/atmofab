# Controlled Spec: 1D linear advection-diffusion problem (problem spec)

## 0. Meta information
- `spec_id`: `advdiff1d_linear`
- `spec_version`: `0.4.5`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Problem definition
The target is the 1D linear advection-diffusion equation
$$
\frac{\partial u}{\partial t} + \frac{\partial (a u)}{\partial x}
= \frac{\partial}{\partial x}\left(\nu \frac{\partial u}{\partial x}\right)
$$
The equation is treated in conservative form. The unknown variable is the scalar field $u(x,t)$. No forcing term is handled.

## 2. Definition of variables and coordinates
The coordinate system is 1D Cartesian coordinates, the coordinate name is `x`, and the unit is `m`. The state variable is `u`, its meaning is the passive-scalar concentration, its placement is the cell center, and its unit is the dimensionless `1`.

The equation of §1 is invariant under the reflection $x\to L-x$ with $a\to-a$, and, being linear, under the sign reversal $u\to-u$. The advection velocity `a` is a signed input (§6), so the reflected configuration is a case of this `problem`, and §5 states the equivariance of the discretization under it ([issue #513](https://github.com/seiya/atmofab/issues/513)).

## 3. Type definition of domain and boundary conditions
The domain is the interval $[0,L)$. The grid is a uniform cell-centered grid, and the grid width is defined by $dx=L/nx$. `L` and `nx` are runtime input.

The boundary condition is fixed to periodic boundary. The default input used for verification is defined in `tests.md`, and the whole of the user input is not fixed.

## 4. Dependent `component` and adopted `profile`
The adopted `profile` is `dynamics_advdiff_profile_1d_upwind_center2_euler1`, and it selects the following `component`, each of which is therefore a direct dependency of this `spec`. `<ir_ref>/dependency_graph.json` is the only exhaustive statement of that set. The runner harness is not in it: it belongs to the implementation target the node is built for, and the host adds it to every build of the node.
- `dynamics_advdiff_flux_1d_upwind_center2`
- `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `dynamics_advection_diffusion_time_update_1d_euler1`

No `component` is declared directly here: a `component` has exactly one source, so a `component` an adopted `profile` selects is not declared again in this `spec`'s own `deps.yaml`. A `component` no adopted `profile` selects would be declared there directly, together with its compatibility constraint.

## 5. Integration algorithm
The update step is fixed to the following order. Each step consumes, by name, the field output of the step before it.
1. Update the ghost region with `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`, with `ng = 1`. This `problem` `node` places the interior of $u^n$ at elements `ng + 1 … ng + nx` of a ghost-extended array of `nx + 2*ng` elements, passes it as `u_in` together with `nx` and `ng`, and receives `u_out` as the ghost-extended field $u^n_{-1}\dots u^n_{nx}$.
2. Compute the advection/diffusion flux with `dynamics_advdiff_flux_1d_upwind_center2__compute_flux`, passing the `u_out` of step 1 as its `u` together with `nx`, `ng`, the advection velocity `a` and the constant `nu` of §6, $dx=L/nx$, and the `dt` of the current step. It returns `flux_adv` and `flux_dif` at all `nx + 1` faces, and the total face flux is
$$
F_{j+1/2}=F^{adv}_{j+1/2}+F^{dif}_{j+1/2},\quad j=-1,\dots,nx-1
$$
3. This `problem` `node` builds the tendency from the flux difference of step 2:
$$
L_i=-\frac{F_{i+1/2}-F_{i-1/2}}{\Delta x},\quad i=0,\dots,nx-1
$$
In the flux arrays, $F_{i+1/2}$ is element `i + 2` and $F_{i-1/2}$ is element `i + 1`; $L_i$ is element `i + 1` of the tendency array.
4. Execute the forward Euler update with `dynamics_advection_diffusion_time_update_1d_euler1__advance`, passing `nx`, this `node`'s own interior state $u^n$ (`nx` values) as its `u_n`, the $L$ of step 3 as its `L_flux`, and the `dt` of the current step. Its `u_np1` is $u^{n+1}$.

The field outputs of steps 1–3 (the ghost-extended field, the face fluxes, the tendency) are each consumed by the next step, and an `IR` in which one of them is not consumed contradicts this section. The `guard_pass` output of each `component` is an input guard, not a field, and is outside this rule. The inputs §6 admits satisfy every `component` guard (`ng = 1`, `nx>=2`, `a\ne0`, `dt>0`). A caller treats the other outputs of a `component` that returns `guard_pass` false as undefined, and no later step consumes them.

The flux `component` takes the upwind cell from the side the flow comes from, so the composition is equivalent to the discrete update
$$
u^{n+1}_i=u^n_i-C\left(u^n_i-u^n_{i-1}\right)+D\left(u^n_{i+1}-2u^n_i+u^n_{i-1}\right)\quad(a>0)
$$
$$
u^{n+1}_i=u^n_i-C\left(u^n_{i+1}-u^n_i\right)+D\left(u^n_{i+1}-2u^n_i+u^n_{i-1}\right)\quad(a<0)
$$
with the periodic images $u^n_{-1}=u^n_{nx-1}$ and $u^n_{nx}=u^n_0$, and with $C$ and $D$ as defined below. `tests.md` 5-4 derives its amplification rate $G_{num}$ from this update.

The discretization holds the following invariants.
- **Mass conservation.** $\sum_i L_i\,\Delta x=-\left(F_{nx-1/2}-F_{-1/2}\right)=0$: under the periodic ghost cells of step 1 the two seam faces are the same expression applied to the same values, so $\sum_i u_i$ is conserved up to round-off.
- **Max-norm boundedness.** With $D\ge 0$ and $|C|+2D\le 1$, the discrete update above writes $u^{n+1}_i=(1-|C|-2D)\,u^n_i+(|C|+D)\,u^n_{i-1}+D\,u^n_{i+1}$ for $a>0$ and $u^{n+1}_i=(1-|C|-2D)\,u^n_i+D\,u^n_{i-1}+(|C|+D)\,u^n_{i+1}$ for $a<0$, a convex combination of three values of $u^n$, so $\max_i|u^n_i|$ does not increase with $n$ (up to round-off). The primary evidence of the CFL judgment in `tests.md` 5-4 rests on this bound.
- **Mirror equivariance.** The case with `a` → `-a` and the initial state $u'_i=u_{nx-1-i}$ has, at every step, the state $u'^{\,n}_i=u^n_{nx-1-i}$, up to round-off: the update for $a<0$ is the update for $a>0$ with the two neighbours exchanged. Combined with the sign reversal, an initial state odd under the reflection ($u_{nx-1-i}=-u_i$) keeps its initial state, and the case with `a` → `-a` alone has $u'^{\,n}_i=-u^n_{nx-1-i}$. The round-off is not zero in general: a contracted multiply-add or a vector remainder loop changes the last bit under a reordering.

The stability index is defined as
$$
\text{cfl_combined}=|C|+2D,\quad C=a\frac{\Delta t}{\Delta x},\quad D=\nu\frac{\Delta t}{\Delta x^2}
$$
For the threshold, refer to the judgment conditions of `tests.md`.

## 6. Model parameters and the runtime input contract
The physical constant is `nu=1.0e-2 m2/s`. The advection velocity `a` (`m/s`) is a runtime input whose default is `1.0`; its sign is the direction of the flow.

The runtime input requires the following.
- `L`, `nx`
- `a`
- `initial_condition`
- `t_start`, `t_end`
- `dt_rule`

`a=0` and `nx<2` are not allowed. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid non-periodic boundary. Forbid the addition of `limiter` / `clip` / `filter`. Forbid the runtime automatic switching of the discretization scheme. Forbid computing a case with $a<0$ by reflecting its state to $a>0$, computing there and reflecting the result back: the kernel evaluates `a` as given, and the flux `component` chooses the upwind side by its sign. `tests.md` compares the reflected case of §5 with its original as a symmetry pair (`docs/TESTS.md` §Symmetry pairs), and a kernel that maps one onto the other compares a code path with itself.

## 8. Traceability
The resolution result is recorded by the host: `<ir_ref>/ir_meta.json` carries the node's own `spec_kind` / `spec_id` / `spec_version`, and `<ir_ref>/dependency_graph.json` carries the resolved closure — `all_nodes` as `<spec_kind>/<spec_id>@<version>` for every node of it (this `spec` itself and the `component`; the runner harness is the target's, not a node of this closure), and `profiles[]` as the adopted `profile`, whose entry carries its own `node_key` for identity but is deliberately absent from `all_nodes`, because a `profile` is not a node.

The reference basis is LeVeque (2002). The signed advection velocity and the equivariance of §5 are those of [issue #513](https://github.com/seiya/atmofab/issues/513).

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/tests.md`, with `test_profile_version` of `0.5.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $u_{next}=F(u_{now}, params)$, and `ceil` and the periodic-index wrap are made explicit as non-differentiable operations.
