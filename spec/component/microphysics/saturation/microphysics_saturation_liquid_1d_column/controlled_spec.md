# Controlled Spec: liquid-water saturation and saturation adjustment of a column (component spec)

## 0. Meta information
- `spec_id`: `microphysics_saturation_liquid_1d_column`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `microphysics`
- `family`: `saturation`

## 1. Responsibility and scope
This `component` evaluates, on the interior cells of one vertical column, the saturation mixing ratio over liquid water at a given temperature and density, and the isochoric saturation adjustment that condenses vapour or evaporates cloud water until the cell is saturated or holds no cloud water. The premise of the input state is a moist air parcel of density `dens` holding vapour `qv` and cloud water `qc`, whose total heat capacities `cptot`, `cvtot` the caller maintains.

The source of the scheme is the liquid path of the procedures `moist_conversion_dens_liq`, `dens2qsat_liq` and `dqs_dtem_dens_liq` of the module `scale_atmos_saturation` and of `saturation_adjustment` of the module `scale_atmos_phy_mp_common` in `scale-met/scale-dev` at commit `dc58155`, under the default thermodynamics setting `EXACT` in double precision.

Out of scope, each a recorded difference from that source:
- The ice phase and the cold-rain branch: `QI` is absent and only the liquid branch exists.
- The termination of the program on a cell that does not converge: the source aborts, and leaves that cell's state at the last iterate, non-finite values included. This `component` returns a finite refusal state for that cell (§4) and leaves the action to its caller.
- A halo cell of an output array: the source leaves it undefined, and this `component` writes `0` there (§2).
- The refusal of invalid input (§4): the source has no check on the index range, `temp`, `dens`, `cptot` or `cvtot`.
- The array form: the source's `(KA,IA,JA)` arrays are rank-1 columns here, and its constants, read from module state, are arguments (§2).
- Single precision, which is not specified or measured here.
- The log lines and the profiling calls of the source.
- Setup and module state: every constant is an argument of the `operation` (§2), so this `component` has no setup step.

## 2. input/output contract
A column has `ka` cells numbered $k=1..ka$. The interior cells are $k=ks..ke$; the other cells are halo cells, which no `operation` reads. Every array argument has rank 1 and extent `ka`.

The thermodynamic constants are arguments of both `operation`s, with no default value:

| argument | meaning | unit |
|---|---|---|
| `rvap` | specific gas constant of water vapour | `J/kg/K` |
| `cpvap` | specific heat of water vapour at constant pressure | `J/kg/K` |
| `cl` | specific heat of liquid water | `J/kg/K` |
| `lhv0` | latent heat of vaporization at `tem00` | `J/kg` |
| `psat0` | saturation vapour pressure at `tem00` | `Pa` |
| `tem00` | reference temperature | `K` |

**`dens2qsat_liq`.** Inputs: `ka`, `ks`, `ke`, temperature `temp` (`K`), density `dens` (`kg/m3`), the constants. Outputs: saturation mixing ratio `qsat` (`kg/kg`) and the input guard `guard_pass` (§4). The published arguments are exactly, in this order: `ka`, `ks`, `ke`, `temp`, `dens`, `rvap`, `cpvap`, `cl`, `lhv0`, `psat0`, `tem00`, `qsat`, `guard_pass`.

**`saturation_adjustment`.** Inputs: `ka`, `ks`, `ke`, `dens`, the constants. Inputs updated in place: `temp`, vapour `qv` and cloud water `qc` (`kg/kg`), total heat capacities `cptot` and `cvtot` at constant pressure and volume (`J/kg/K`). Outputs: the density of internal energy released `rhoe_d` (`J/m3`), `converged` and `guard_pass`. The published arguments are exactly, in this order: `ka`, `ks`, `ke`, `dens`, `temp`, `qv`, `qc`, `cptot`, `cvtot`, `rvap`, `cpvap`, `cl`, `lhv0`, `psat0`, `tem00`, `rhoe_d`, `converged`, `guard_pass`.

**Halo cells.** On a call that `guard_pass` does not refuse, an input-updated array keeps its input value at every halo cell, and an output array holds `0` at every halo cell, so every array the `operation` publishes is defined at every cell.

The derived constants, used throughout §3, are
$$
c_{vv}=\mathtt{cpvap}-\mathtt{rvap},\qquad
L_{00}=\mathtt{lhv0}-(\mathtt{cpvap}-\mathtt{cl})\,\mathtt{tem00},\qquad
L(T)=\mathtt{lhv0}+(\mathtt{cpvap}-\mathtt{cl})(T-\mathtt{tem00}).
$$

## 3. Operation definition
The published `operation`s are `microphysics_saturation_liquid_1d_column__dens2qsat_liq` and `microphysics_saturation_liquid_1d_column__saturation_adjustment`. Both are pointwise in $k$: a cell's result depends on that cell's inputs alone.

The saturation functions at temperature $T$ and density $\rho$ are
$$
q_s(T,\rho)=\frac{\mathtt{psat0}}{\rho\,\mathtt{rvap}\,T}\exp\!\left(\ln\frac{T}{\mathtt{tem00}}\,\frac{\mathtt{cpvap}-\mathtt{cl}}{\mathtt{rvap}}+\frac{L_{00}}{\mathtt{rvap}}\left(\frac{1}{\mathtt{tem00}}-\frac{1}{T}\right)\right),
$$
$$
\frac{dq_s}{dT}(T,\rho)=\frac{q_s(T,\rho)}{T}\left(\frac{L(T)}{\mathtt{rvap}\,T}-1\right).
$$

**`dens2qsat_liq`.** For $k=ks..ke$: `qsat(k)` $=q_s($`temp(k)`, `dens(k)`$)$.

**`saturation_adjustment`.** For each interior cell, let $T_0,q_{v0},q_{c0},C_{p0},C_{v0}$ be its input values of `temp`, `qv`, `qc`, `cptot`, `cvtot`, $\rho$ its `dens`, $q_\Sigma=q_{v0}+q_{c0}$ and $E_0=T_0C_{v0}+q_{v0}L_{00}$. The steps below are normative in this order, because the returned state pairs quantities of different iterates: a result that evaluates $q_s$ at the final temperature differs from it.

1. **Virtual evaporation.** Compute $C_p=C_{p0}+q_{c0}(\mathtt{cpvap}-\mathtt{cl})$, $C_v=C_{v0}+q_{c0}(c_{vv}-\mathtt{cl})$, $T=(E_0-L_{00}q_\Sigma)/C_v$. If $q_\Sigma\le q_s(T,\rho)$, the cell's result is `qv` $=q_\Sigma$, `qc` $=0$, `temp` $=T$, `cptot` $=C_p$, `cvtot` $=C_v$, the cell is converged, and the procedure continues at step 4. This step rewrites `temp` also when $q_{c0}=0$.
2. **Newton iteration.** Otherwise restart from $T=T_0$, $C_v=C_{v0}$, and repeat at most 100 times, in this order:
   - $q_s=q_s(T,\rho)$ and $q_s'=\frac{dq_s}{dT}(T,\rho)$;
   - $E_T=T(c_{vv}-\mathtt{cl})q_s'+C_v+L_{00}q_s'$, with $C_v$ still from the previous iteration;
   - $q_c=q_\Sigma-q_s$;
   - $C_v=C_{v0}+(c_{vv}-\mathtt{cl})(q_s-q_{v0})$;
   - $E=TC_v+q_sL_{00}$;
   - $\delta=(E-E_0)/E_T$ and $T=T-\delta$;
   - the cell is converged and the iteration stops when $|\delta|<10^{-6}$; the cell is not converged and the iteration stops when $T$ is not finite.

   A cell that reaches the 100th iteration without $|\delta|<10^{-6}$ is not converged.
3. **Result of the iteration.** `cptot` $=C_{p0}+(\mathtt{cpvap}-\mathtt{cl})(q_s-q_{v0})$, `qv` $=q_s$, `qc` $=q_c$, `cvtot` $=C_v$, `temp` $=T$. So `temp` is the last update, while `qv`, `qc`, `cvtot` belong to the iterate before it.
4. **Energy.** `rhoe_d` $=-L_{00}($`qv`$-q_{v0})\rho$.

A cell that is not converged takes the refusal state of §4 in place of steps 3 and 4. `converged` is true when every interior cell is converged; its value does not depend on the order in which the cells are evaluated.

**Conservation.** $q_v+q_c$ is conserved on both branches. $TC_v+q_vL_{00}$ is conserved by step 1. On the Newton branch the returned state pairs the last $T$ with the last iterate's $C_v$ and $q_s$. With $\delta$, $E_T$, $q_s'$ and $T^{it}$ the last iterate's correction, derivative, slope and temperature, and $C_v^{prev}$ the $C_v$ it started from, the drift is exactly
$$
\left(TC_v+q_vL_{00}\right)-E_0=\delta\,(E_T-C_v)=\delta\left(q_s'\,(L_{00}+T^{it}(c_{vv}-\mathtt{cl}))+C_v^{prev}-C_v\right),
$$
and $|\delta|<10^{-6}$ bounds it. The term $C_v^{prev}-C_v$ vanishes as the iteration converges.

**Not defined here.** The CFL number is not defined, because the `operation`s have no transport and no time step. A column admits no spatial symmetry of the scheme: the `operation`s are pointwise, so a reversal or a translation of the column gives the same result reversed or translated, with no coupling between cells to test.

## 4. Failure conditions and constraints
**`dens2qsat_liq`** treats `ks<1`, `ke>ka`, `ks>ke`, and `temp<=0` or `dens<=0` at an interior cell as invalid input.

**`saturation_adjustment`** treats `ks<1`, `ke>ka`, `ks>ke`, and `dens<=0`, `temp<=0`, `cptot<=0` or `cvtot<=0` at an interior cell as invalid input. It carries no check on the sign of `qv` or `qc`: a caller's limiter can leave a value of order `-1e-19`, on which the definition stays finite.

On invalid input `guard_pass` is false, every input-updated array holds its input value at every cell, and the output arrays and `converged` are not defined. On valid input `guard_pass` is true.

A cell that is not converged (§3) is not invalid input: `guard_pass` stays true, `converged` is false, and at that cell `temp`, `qv`, `qc`, `cptot`, `cvtot` hold their input values and `rhoe_d` is `0`. The other cells follow §3. The state a caller receives is therefore finite on every path. Non-convergence is reachable and chaotic in the input: at `temp=277.8`, `dens=1`, `qc=0`, `qv=0.66357706133254214` the iteration ends without a finite temperature, and whether a larger or smaller `qv` converges is not monotone in `qv` (`tests.md` §2 of this node lists measured values).

## 5. Public API and compatibility
The published `operation_id`s are exactly: `microphysics_saturation_liquid_1d_column__dens2qsat_liq` and `microphysics_saturation_liquid_1d_column__saturation_adjustment`.

A change breaking compatibility of a published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, and so is a change to how the surface is carried. An update that keeps every signature and every result is a `minor` update.

### 5.1 Canonical interface block
The exact published surface as a machine-readable, language-neutral signature block. The rules are those of `docs/CONTROLLED_SPEC.md` §5.1: the deterministic gates render this block to the target language and pin the `IR`'s `public_api` and the generated model source against it.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: microphysics_saturation_liquid_1d_column__dens2qsat_liq
  args:
  - name: ka
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ks
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ke
    rank: 0
    intent: in
    spec:
      type: integer
  - name: temp
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: dens
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: rvap
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: cpvap
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: cl
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: lhv0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: psat0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: tem00
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: qsat
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: guard_pass
    rank: 0
    intent: out
    spec:
      type: logical
- kind: subroutine
  name: microphysics_saturation_liquid_1d_column__saturation_adjustment
  args:
  - name: ka
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ks
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ke
    rank: 0
    intent: in
    spec:
      type: integer
  - name: dens
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: temp
    rank: 1
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: qv
    rank: 1
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: qc
    rank: 1
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: cptot
    rank: 1
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: cvtot
    rank: 1
    intent: inout
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: rvap
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: cpvap
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: cl
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: lhv0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: psat0
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: tem00
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: rhoe_d
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: converged
    rank: 0
    intent: out
    spec:
      type: logical
  - name: guard_pass
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid a default value for a constant, and forbid reading a constant from anything but the arguments. Forbid evaluating $q_s$ at the final temperature in place of the iterate §3 names. Forbid a change of the iteration limit or of the convergence threshold, an iteration or relaxation other than §3, and any automatic switch to another scheme. Forbid clipping a result to non-negative: `qc` is what §3 gives, of either sign. Forbid terminating the program on a cell that does not converge, and forbid publishing a non-finite value. Forbid reading or writing a halo cell other than as §2 states, and forbid silently completing an invalid input.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and, by the host, in the adopting `spec`'s `<ir_ref>/dependency_graph.json` (`all_nodes`, at the resolved `version`). The constants the suite fixes fall into the `case` section of `spec.ir.yaml` as case inputs.

## 8. tests reference
The corresponding `tests.md` is `spec/component/microphysics/saturation/microphysics_saturation_liquid_1d_column/tests.md`, with `test_profile_version` of `0.3.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The branch of §3 step 1, the convergence test and the iteration count of step 2, and the refusal of §4 are made explicit as non-differentiable operations.
