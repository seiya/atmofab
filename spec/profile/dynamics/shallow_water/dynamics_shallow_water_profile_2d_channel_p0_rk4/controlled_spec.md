# Controlled Spec: 2D shallow water channel profile (profile spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_profile_2d_channel_p0_rk4`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Target `component` and compatibility range
The target `component` are the following, each with the `operation` it publishes.
- `dynamics_shallow_water_flux_2d_rusanov_p0` (`>=0.2.0 <1.0.0`): `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
- `dynamics_shallow_water_boundary_2d_channel_mirror` (`>=0.2.0 <1.0.0`): `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
- `dynamics_shallow_water_source_2d_coriolis` (`>=0.2.0 <1.0.0`): `dynamics_shallow_water_source_2d_coriolis__apply`
- `dynamics_shallow_water_time_update_2d_rk4` (`>=0.1.0 <1.0.0`): `dynamics_shallow_water_time_update_2d_rk4__advance`

## 2. Application conditions
Adoption is explicit: an adopting `spec` names this `profile` in its own `deps.yaml`, and no automatic selection exists. This section states the conditions under which adopting it is correct, and the conditions under which it is not.
- Adopt when the adopting `spec` is of `family=shallow_water`, is two-dimensional, has a flat bottom, carries the Coriolis source term, and its boundary treatment is a channel: periodic along one coordinate axis and a free-slip wall (mirror ghost) at both ends of the other, in either orientation.
- Do not adopt for a boundary treatment other than the channel above (periodic on every boundary, a wall on both axes, or a wall at one end of an axis only), for a dimension other than 2D, for a bottom topography, for a forcing other than the Coriolis source, or when the adopting `spec` requires a discretization the §3 constraints exclude. The periodic-on-every-boundary case is the adoption condition of `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2`; the two `profile` are mutually exclusive.

## 3. Parameter constraints
The discretization constraints are the following.
- interface flux: Rusanov
- reconstruction: `p0`
- time integration: `RK4` (the tendency passed as a procedure argument; the time-update `component` evaluates it at the four stages)
- boundary condition: periodic mapping along the channel and mirror ghost across it, the wall axis passed to the boundary `component` as its `wall_axis` input (`dynamics_shallow_water_boundary_2d_channel_mirror` `0.2.0`); `h` and the along-channel momentum even, the cross-channel momentum odd (`hu` even and `hv` odd for walls in `y`, `hu` odd and `hv` even for walls in `x`). The orientation is an input of the adopting `spec`; the component set is the same in either orientation
- Coriolis source: explicit, evaluated at each stage state, `f` supplied per interior cell by the adopting `spec` (`dynamics_shallow_water_source_2d_coriolis` `0.2.0` takes an `nx` × `ny` field)

## 4. Fallback rules
When the compatibility condition of a target `component` is not satisfied, it is an error, and automatic switching to an alternative `profile` is forbidden. The host resolver enforces this, and each shape is refused by its own name: a §1 compatibility range that matches no catalog version stops the run as `dependency_unresolvable` naming the `component`; an adopting `spec` whose own constraint on THIS `profile` matches no catalog version stops it as `profile_unresolvable`; and two adopted `profile` requiring incompatible ranges of one `component` stop it as `profile_component_conflict`. None degrades to a substitute.

## 5. Traceability
The adoption is recorded by the host in the adopting `spec`'s `<ir_ref>/dependency_graph.json`: `profiles[]` names this `profile` and its resolved `version`, and the `component` it selects appear in `all_nodes` at their resolved `version`. Each case of the adopting `spec`'s `IR` records `inputs.profile_selection` as `{profile_id, profile_version}`, which a deterministic gate pins against that record.
