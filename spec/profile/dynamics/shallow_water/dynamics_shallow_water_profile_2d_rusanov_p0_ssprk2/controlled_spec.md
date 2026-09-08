# Controlled Spec: 2D shallow water default profile (profile spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2`
- `spec_version`: `0.1.1`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Target `component` and compatibility range
The target `component` are the following.
- `dynamics_shallow_water_flux_2d_rusanov_p0` (`>=0.2.0 <1.0.0`)
- `dynamics_shallow_water_boundary_2d_periodic_copy` (`>=0.2.0 <1.0.0`)
- `dynamics_shallow_water_time_update_2d_ssprk2` (`>=0.4.0 <1.0.0`)

## 2. Application conditions
Adoption is explicit: an adopting `spec` names this `profile` in its own `deps.yaml`, and no automatic selection exists. This section states the conditions under which adopting it is correct, and the conditions under which it is not.
- Adopt when the adopting `spec` is of `family=shallow_water`, is two-dimensional, and its boundary treatment is periodic.
- Do not adopt for a boundary treatment other than periodic, for a dimension other than 2D, or when the adopting `spec` requires a discretization the §3 constraints exclude.

## 3. Parameter constraints
The discretization constraints are the following.
- interface flux: Rusanov
- reconstruction: `p0`
- time integration: `SSPRK2`
- boundary condition: periodic mapping

## 4. Fallback rules
When the compatibility condition of a target `component` is not satisfied, it is an error, and automatic switching to an alternative `profile` is forbidden. The host resolver enforces this: an unsatisfiable compatibility range refuses the run (`profile_unresolvable`), and two adopted `profile` requiring incompatible ranges of one `component` refuse it as well (`profile_component_conflict`). Neither degrades to a substitute.

## 5. Traceability
The adoption is recorded by the host in the adopting `spec`'s `<ir_ref>/dependency_graph.json`: `profiles[]` names this `profile` and its resolved `version`, and the `component` it selects appear in `all_nodes` at their resolved `version`. Each case of the adopting `spec`'s `IR` records `inputs.profile_selection` as `{profile_id, profile_version}`, which a deterministic gate pins against that record.
