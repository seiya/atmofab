# Controlled Spec: Kessler warm-rain microphysics of a column (component spec)

## 0. Meta information
- `spec_id`: `microphysics_kessler_warm_rain_1d_column`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `microphysics`
- `family`: `kessler`

## 1. Responsibility and scope
This `component` evaluates, on the interior cells of one vertical column, one explicit step of the Kessler-type warm-rain scheme over vapour `qv`, cloud water `qc` and rain water `qr` followed by the saturation adjustment, and the terminal velocity of the two hydrometeors. It does not transport the hydrometeors and does not apply the terminal velocity. The premise of the input state is a moist air parcel with non-negative `qv`, `qc`, `qr`, whose total heat capacities `cptot`, `cvtot` the caller maintains.

The source of the scheme is `ATMOS_PHY_MP_kessler_adjustment` and `ATMOS_PHY_MP_kessler_terminal_velocity` of the module `scale_atmos_phy_mp_kessler` in `scale-met/scale-dev` at commit `dc58155`, under the default thermodynamics setting `EXACT` in double precision.

Out of scope, each a recorded difference from that source or a part of it not taken:
- The source's `_setup`, `_cloud_fraction`, `_effective_radius`, `_qtrc2qhyd` and `_qhyd2qtrc`: the first writes log lines and aborts on a flag the module never sets; the other four are driver-tier diagnostics and conversions with no physics the scheme owns.
- `QC_t_sat`, which the source writes to the history output, is the output argument `pcsat` here.
- The termination of the program on a cell that does not converge: this `component` refuses the column instead (§4).
- The refusal of invalid input (§4): the source has no check on the index range or `dt`, and `terminal_velocity` none on `dens` or `refstate_dens`.
- The array form: the source's `(KA,IA,JA)` arrays and tracer dimension are rank-1 columns `qv`, `qc`, `qr`, `rhoq_c`, `rhoq_r`, `vterm_c`, `vterm_r` here, and its constants, read from module state, are arguments (§2).
- The refusal of negative moisture and of non-positive heat capacities (§4), which the source does not make. A driver that steps this `component` must fix negative moisture before the call, as the source's driver does: the output of one call can hold a value of order `-1e-19` in a limited variable, and the next call refuses it.
- A halo cell of an output array: the source leaves it undefined, and this `component` writes `0` there (§2).
- Single precision, which is not specified or measured here, and any setup or module state: every constant is an argument (§2).
- The log lines and the profiling calls of the source.

## 2. input/output contract
A column has `ka` cells numbered $k=1..ka$. The interior cells are $k=ks..ke$; the other cells are halo cells, which no `operation` reads. Every array argument has rank 1 and extent `ka`.

The constants are arguments of `__adjustment`, with no default value: `rvap`, `cpvap`, `cl`, `lhv0`, `psat0`, `tem00`, whose meanings and units are those of the dependency `microphysics_saturation_liquid_1d_column` (its Controlled Spec §2); `eps`, the floor of the saturation mixing ratio in the saturation ratio (dimensionless); `dwatr`, the density of liquid water (`kg/m3`).

**`adjustment`.** Inputs: `ka`, `ks`, `ke`, time step `dt` (`s`), density `dens` (`kg/m3`), pressure `pres` (`Pa`), the constants. Inputs updated in place: temperature `temp` (`K`), `qv`, `qc`, `qr` (`kg/kg`), total heat capacities `cptot`, `cvtot` (`J/kg/K`). Outputs: the tendency of the density of internal energy `rhoe_t` (`J/m3/s`), the number of evaporated cloud droplets `evaporate` (`1/m3/s`), the cloud-water production of the saturation step `pcsat` (`kg/kg/s`), and the input guard `guard_pass` (§4). The published arguments are exactly, in this order: `ka`, `ks`, `ke`, `dt`, `dens`, `pres`, `temp`, `qv`, `qc`, `qr`, `cptot`, `cvtot`, `rvap`, `cpvap`, `cl`, `lhv0`, `psat0`, `tem00`, `eps`, `dwatr`, `rhoe_t`, `evaporate`, `pcsat`, `guard_pass`.

**`terminal_velocity`.** Inputs: `ka`, `ks`, `ke`, `dens`, the densities of cloud water and of rain water `rhoq_c`, `rhoq_r` (`kg/m3`), the reference density profile `refstate_dens` (`kg/m3`). Outputs: `vterm_c`, `vterm_r` (`m/s`, negative downward) and `guard_pass`. The published arguments are exactly, in this order: `ka`, `ks`, `ke`, `dens`, `rhoq_c`, `rhoq_r`, `refstate_dens`, `vterm_c`, `vterm_r`, `guard_pass`.

**Halo cells.** On a call that `guard_pass` does not refuse, an input-updated array keeps its input value at every halo cell, and an output array holds `0` at every halo cell.

**Dependency.** `__adjustment` calls `microphysics_saturation_liquid_1d_column__dens2qsat_liq` and `microphysics_saturation_liquid_1d_column__saturation_adjustment`, and uses the dependency's definition of the derived constants $c_{vv}=\mathtt{cpvap}-\mathtt{rvap}$ and $L_{00}=\mathtt{lhv0}-(\mathtt{cpvap}-\mathtt{cl})\,\mathtt{tem00}$.

## 3. Operation definition
The published `operation`s are `microphysics_kessler_warm_rain_1d_column__adjustment` and `microphysics_kessler_warm_rain_1d_column__terminal_velocity`. Both are pointwise in $k$, except that `__terminal_velocity` reads `refstate_dens(ks)` in every cell.

**`adjustment`.** `__dens2qsat_liq` gives $q_s$ at the input `temp`, `dens`. For each interior cell, with $\rho$, $p$, $T$, $q_v$, $q_c$, $q_r$, $C_p$, $C_v$ its input values:

1. Rates, each in `kg/kg/s`:
$$
d_{auto}=10^{-3}\max(q_c-10^{-3},0),\qquad
d_{accr}=2.2\,q_c\,q_r^{0.875},
$$
$$
S=\frac{q_v}{\max(q_s,\mathtt{eps})},\qquad
d_{evap}=(1-\min(S,1))\,\left(1.6+124.9(\rho q_r)^{0.2046}\right)(\rho q_r)^{0.525}\,\frac{p\,q_s}{\rho\,(5.4\times10^{5}\,p\,q_s+2.55\times10^{8})}.
$$
2. Limited tendencies, in this order:
$$
\dot q_c=\max(-d_{auto}-d_{accr},\,-q_c/\mathtt{dt}),\quad
\dot q_r=\max(d_{auto}+d_{accr}-d_{evap},\,-q_r/\mathtt{dt}),\quad
\dot q_v=\max(-(\dot q_c+\dot q_r),\,-q_v/\mathtt{dt}).
$$
3. Update, where the $T$ update uses the $C_v$ before its own update:
$$
q_x\leftarrow q_x+\dot q_x\,\mathtt{dt}\ (x=v,c,r),\qquad
\dot e=-L_{00}\dot q_v,\qquad
C_p'=C_p+(\mathtt{cpvap}\,\dot q_v+\mathtt{cl}(\dot q_c+\dot q_r))\mathtt{dt},
$$
$$
C_v'=C_v+(c_{vv}\,\dot q_v+\mathtt{cl}(\dot q_c+\dot q_r))\mathtt{dt},\qquad
T\leftarrow(TC_v+\dot e\,\mathtt{dt})/C_v',\qquad
\mathtt{rhoe\_t}=\rho\,\dot e.
$$
4. Let $q_c^{*}$ be `qc` after step 3. `__saturation_adjustment` then updates `temp`, `qv`, `qc`, `cptot`, `cvtot` and returns `rhoe_d`.
5. `rhoe_t` $\leftarrow$ `rhoe_t` $+$ `rhoe_d`$/\mathtt{dt}$; `pcsat` $=($`qc`$-q_c^{*})/\mathtt{dt}$; `evaporate` $=\max(-$`pcsat`$,0)\,\rho\,/\left(\frac{4}{3}\pi\,\mathtt{dwatr}\,r_c^{3}\right)$ with the droplet radius $r_c=8\times10^{-6}$ `m`.

The numbers of steps 1 and 5 are constants of the scheme and are part of this definition.

**`terminal_velocity`.** `rhoq_c` is not read: the argument is part of the published signature, and the definition does not use it. For $k=ks..ke$: `vterm_c(k)` $=0$. With $q_r=$`rhoq_r(k)`$/$`dens(k)` and $z=1$ when $q_r<10^{-12}$, $z=0$ otherwise,
$$
\mathtt{vterm\_r}(k)=-36.34\,\left(\mathtt{dens}(k)(q_r+z)\right)^{0.1364}\,\frac{\mathtt{refstate\_dens}(ks)}{\mathtt{refstate\_dens}(k)}\,(1-z).
$$

**Conservation.** Where the $\dot q_v$ limiter does not bind, step 3 conserves $q_v+q_c+q_r$ and the moist internal energy $TC_v+q_vL_{00}$, and `__saturation_adjustment` conserves both as its Controlled Spec §3 states. So `rhoe_t`$\,\mathtt{dt}=-\rho L_{00}(q_v^{final}-q_v^{input})$. Where the $\dot q_v$ limiter binds, total water is not conserved, as in the source.

**Not defined here.** The CFL number is not defined, because the `operation`s have no transport. A column admits no spatial symmetry of the scheme: `__adjustment` is pointwise, and `__terminal_velocity` couples cells only through the reference level $ks$, so a translation with a translated reference profile gives the same result translated.

## 4. Failure conditions and constraints
**`adjustment`** treats as invalid input: `ks<1`, `ke>ka`, `ks>ke`, `dt<=0`; at an interior cell `qv<0`, `qc<0`, `qr<0`, `cptot<=0` or `cvtot<=0`; and `guard_pass=false` from either call of the dependency, which covers `dens<=0` and `temp<=0`. It also refuses when `__saturation_adjustment` returns `converged=false`.

**`terminal_velocity`** treats as invalid input: `ks<1`, `ke>ka`, `ks>ke`, and `dens<=0` or `refstate_dens<=0` at an interior cell.

On a refusal `guard_pass` is false, every input-updated array holds its input value at every cell, the whole column included, since step 3 has already rewritten the state when the dependency refuses, and the output arrays are not defined. On any other call `guard_pass` is true.

## 5. Public API and compatibility
The published `operation_id`s are exactly: `microphysics_kessler_warm_rain_1d_column__adjustment` and `microphysics_kessler_warm_rain_1d_column__terminal_velocity`.

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
  name: microphysics_kessler_warm_rain_1d_column__adjustment
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
  - name: dt
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: dens
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: pres
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
  - name: qr
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
  - name: eps
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: dwatr
    rank: 0
    intent: in
    spec:
      type: real
      kind: dp
  - name: rhoe_t
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: evaporate
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: pcsat
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
  name: microphysics_kessler_warm_rain_1d_column__terminal_velocity
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
  - name: rhoq_c
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: rhoq_r
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: refstate_dens
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: vterm_c
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
    dims:
    - ka
  - name: vterm_r
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
```

## 6. Prohibitions
Forbid a default value for a constant, and forbid reading a constant from anything but the arguments. Forbid a saturation computation other than the dependency's two `operation`s. Forbid reordering the limiters of §3 step 2, and forbid clipping a result to non-negative beyond them. Forbid terminating the program on a refusal, and forbid publishing a non-finite value. Forbid reading or writing a halo cell other than as §2 states, and forbid silently completing an invalid input.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and, by the host, in the adopting `spec`'s `<ir_ref>/dependency_graph.json` (`all_nodes`, at the resolved `version`). The constants the suite fixes fall into the `case` section of `spec.ir.yaml` as case inputs.

## 8. tests reference
The corresponding `tests.md` is `spec/component/microphysics/kessler/microphysics_kessler_warm_rain_1d_column/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `max` and `min` of §3 steps 1 and 2, the step $z$ of `terminal_velocity`, the powers of $q_r$ at $q_r=0$, the dependency's iteration, and the refusal of §4 are made explicit as non-differentiable operations.
