# Controlled Spec: 1D advection-diffusion default profile (profile spec)

## 0. Meta information
- `spec_id`: `dynamics_advdiff_profile_1d_upwind_center2_euler1`
- `spec_version`: `0.1.1`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Target `component` and compatibility range
The target `component` are the following.
- `dynamics_advdiff_flux_1d_upwind_center2` (`>=0.2.0 <1.0.0`)
- `dynamics_advection_diffusion_boundary_1d_periodic_copy` (`>=0.2.0 <1.0.0`)
- `dynamics_advection_diffusion_time_update_1d_euler1` (`>=0.2.0 <1.0.0`)

## 2. Application conditions
Adoption is explicit: an adopting `spec` names this `profile` in its own `deps.yaml`, and no automatic selection exists. This section states the conditions under which adopting it is correct, and the conditions under which it is not.
- Adopt when the adopting `spec` is of `family=advection_diffusion`, is one-dimensional, and its boundary treatment is periodic.
- Do not adopt for a boundary treatment other than periodic, for a dimension other than 1D, or when the adopting `spec` requires a discretization the §3 constraints exclude.

## 3. Parameter constraints
The discretization constraints are the following.
- advection term: first-order upwind
- diffusion term: second-order central
- time integration: forward Euler
- boundary condition: periodic mapping

## 4. Fallback rules
When the compatibility condition of a target `component` is not satisfied, it is an error, and automatic switching to an alternative `profile` is forbidden. The host resolver enforces this, and each shape is refused by its own name: a §1 compatibility range that matches no catalog version stops the run as `dependency_unresolvable` naming the `component`; an adopting `spec` whose own constraint on THIS `profile` matches no catalog version stops it as `profile_unresolvable`; and two adopted `profile` requiring incompatible ranges of one `component` stop it as `profile_component_conflict`. None degrades to a substitute.

## 5. Traceability
The adoption is recorded by the host in the adopting `spec`'s `<ir_ref>/dependency_graph.json`: `profiles[]` names this `profile` and its resolved `version`, and the `component` it selects appear in `all_nodes` at their resolved `version`. Each case of the adopting `spec`'s `IR` records `inputs.profile_selection` as `{profile_id, profile_version}`, which a deterministic gate pins against that record.
