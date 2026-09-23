# Controlled Spec: 2D shallow water channel profile, `p1` reconstruction (profile spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_profile_2d_channel_p1_rk4`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `profile`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Target `component` and compatibility range
The target `component` are the following, each with the `operation` it publishes.
- `dynamics_shallow_water_reconstruction_2d_muscl_mc` (`>=0.1.0 <1.0.0`): `dynamics_shallow_water_reconstruction_2d_muscl_mc__reconstruct`
- `dynamics_shallow_water_flux_2d_rusanov` (`>=0.1.0 <1.0.0`): `dynamics_shallow_water_flux_2d_rusanov__compute_flux`
- `dynamics_shallow_water_boundary_2d_channel_mirror` (`>=0.1.0 <1.0.0`): `dynamics_shallow_water_boundary_2d_channel_mirror__apply`
- `dynamics_shallow_water_source_2d_coriolis` (`>=0.1.0 <1.0.0`): `dynamics_shallow_water_source_2d_coriolis__apply`
- `dynamics_shallow_water_time_update_2d_rk4` (`>=0.1.0 <1.0.0`): `dynamics_shallow_water_time_update_2d_rk4__advance`

## 2. Application conditions
Adoption is explicit: an adopting `spec` names this `profile` in its own `deps.yaml`, and no automatic selection exists. This section states the conditions under which adopting it is correct, and the conditions under which it is not.
- Adopt when the adopting `spec` is of `family=shallow_water`, is two-dimensional, has a flat bottom, carries the Coriolis source term, its boundary treatment is a channel — periodic in `x` and a free-slip wall (mirror ghost) in `y` — and it requires a second-order piecewise-linear reconstruction with a ghost width of two.
- The adopting `spec` may add an explicit forcing term of its own to the tendency, provided the forcing is evaluated at the stage state or the stage time of each `RK4` stage, in the same way as the Coriolis source. A `component` that evaluates such a forcing is not selected by this `profile`; the adopting `spec` declares it directly in its own `deps.yaml`.
- Do not adopt for a boundary treatment other than the channel above (periodic on every boundary, a wall in `x`), for a dimension other than 2D, for a bottom topography, for a forcing that is not evaluated explicitly at each stage, or when the adopting `spec` requires a discretization the §3 constraints exclude. The first-order `p0` reconstruction on the same channel is the adoption condition of `dynamics_shallow_water_profile_2d_channel_p0_rk4`, and the periodic-on-every-boundary case that of `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2`; the three `profile` are mutually exclusive.

## 3. Parameter constraints
The discretization constraints are the following.
- interface flux: Rusanov, evaluated on the interface states the reconstruction supplies
- reconstruction: `p1` — piecewise linear (`MUSCL`) with the `MC` limiter, applied to each conserved variable (`h`, `hu`, `hv`) separately
- ghost width: `ng=2`
- time integration: `RK4` (the tendency passed as a procedure argument; the time-update `component` evaluates it at the four stages)
- boundary condition: `x` periodic mapping, `y` mirror ghost (`h` and `hu` even, `hv` odd)
- Coriolis source: explicit, evaluated at each stage state, `f` supplied per interior row by the adopting `spec`
- additional forcing: none selected by this `profile`; an explicit forcing the adopting `spec` declares directly is evaluated at each stage (§2)

## 4. Fallback rules
When the compatibility condition of a target `component` is not satisfied, it is an error, and automatic switching to an alternative `profile` is forbidden. The host resolver enforces this, and each shape is refused by its own name: a §1 compatibility range that matches no catalog version stops the run as `dependency_unresolvable` naming the `component`; an adopting `spec` whose own constraint on THIS `profile` matches no catalog version stops it as `profile_unresolvable`; and two adopted `profile` requiring incompatible ranges of one `component` stop it as `profile_component_conflict`. None degrades to a substitute.

## 5. Traceability
The adoption is recorded by the host in the adopting `spec`'s `<ir_ref>/dependency_graph.json`: `profiles[]` names this `profile` and its resolved `version`, and the `component` it selects appear in `all_nodes` at their resolved `version`. Each case of the adopting `spec`'s `IR` records `inputs.profile_selection` as `{profile_id, profile_version}`, which a deterministic gate pins against that record.
