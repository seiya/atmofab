# Controlled Spec: 1D periodic-boundary mapping (component spec)

## 0. Meta information
- `spec_id`: `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Responsibility and scope
This `component` is responsible only for the periodic-boundary ghost mapping of a 1D array.

## 2. input/output contract
The inputs are `nx`, `ng`, and the ghost-extended field `u_in` (rank 1, holding `nx + 2*ng` elements — the interior `nx` cells plus `ng` ghost cells at each end). The outputs are `u_out`, the same field after the periodic ghost mapping, and `guard_pass`, which reports whether the supplied grid sizes are valid (§4).

Input and output are SEPARATE arrays, not one field mapped in place. That is the same shape the 2D sibling `dynamics_shallow_water_boundary_2d_periodic_copy` publishes, and it keeps the operation free of the aliasing an `intent(inout)` argument would allow — a caller passing the same array for both would otherwise read cells the mapping had already overwritten. §5.1 is the pin; this paragraph describes it and must move with it.

## 3. Operation definition
The published `operation` is `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`. When `ng=1`, apply
$$
u_{-1}=u_{nx-1},\quad u_{nx}=u_0
$$

## 4. Failure conditions and constraints
Treat `nx<2` and `ng<1` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`. On a `major` compatibility break, separate the `spec_id`.

A change breaking compatibility of the published signature — the argument names, their order, their types, their ranks, or their `intent`s — is a **breaking change released under a new `spec_version`**, not a silent regeneration: a consumer is certified against the ABI it linked, and `docs/ORCHESTRATION.md` §13b makes a regenerated dependency source invalidate that consumer's readiness. A change to how the surface is CARRIED (the §5.1 / `IR public_api.signatures` REPRESENTATION) is likewise released under a new `spec_version` even when the ABI is byte-identical, because dependency freshness invalidates a stale certified `IR` only via its version.

### 5.1 Canonical interface block
The exact published surface, as a machine-readable **language-neutral** signature block (`module_parameters` / `types` / `procedures`). It describes the published operation abstractly — for every argument: its `name`, neutral `type` (`real` / `integer` / `logical`), `rank`, `intent`, and, for an explicit-shape array, its `dims` bound expressions; plus the value-pinned module parameters the signatures reference. The vocabulary is neutral throughout: a kind value is `float64`, never the Fortran `real64`. The target language's binding (here Fortran: `real(dp)`, assumed-shape `(:)` ranks, `integer, parameter :: dp = real64`, the `<spec_id>__` name) is produced by the language backend (`tools/backends/language/fortran/signatures`), not authored here — so this contract is not tied to Fortran. The generated model source must publish the symbol below with the signature this block describes (formatting, continuations, and comments may differ; the name, argument order, types, ranks, and `intent`s may not). The deterministic gates render this block to the target language and pin it: the `--stage compile` gate cross-checks its symbol set against §5 and pins the `IR`'s `public_api.signatures` / `public_api.module_parameters` == this block, and the `Generate.static` gate pins the generated model source against these signatures (normalized: comments stripped, continuations joined, case-folded, whitespace-insensitive).

`module_parameters` pins `dp` itself, not only the arguments that reference it. Without it a signature written `real(dp)` would match a source that obtained `dp` from any kind at all, so narrowing `dp` to `float32` would pass the stanza comparison — a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must carry the parameter DECLARATION (`integer, parameter :: dp = real64`), not a `use`-rename of it.

```yaml
module_parameters:
- name: dp
  value: float64
types: []
procedures:
- kind: subroutine
  name: dynamics_advection_diffusion_boundary_1d_periodic_copy__apply
  args:
  - name: nx
    rank: 0
    intent: in
    spec:
      type: integer
  - name: ng
    rank: 0
    intent: in
    spec:
      type: integer
  - name: u_in
    rank: 1
    intent: in
    spec:
      type: real
      kind: dp
  - name: u_out
    rank: 1
    intent: out
    spec:
      type: real
      kind: dp
  - name: guard_pass
    rank: 0
    intent: out
    spec:
      type: logical
```

## 6. Prohibitions
Forbid automatic fallback to a non-periodic boundary.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_boundary_1d_periodic_copy/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. The periodic-index wrap is made explicit as a discrete operation.
