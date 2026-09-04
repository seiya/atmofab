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

**Index origin.** §3 and `tests.md` state the mapping in the field's own index convention `u_{-ng} … u_{nx-1+ng}`, where `u_0 … u_{nx-1}` are the interior cells. The published arrays carry no lower bound of their own — §5.1 pins `u_in` / `u_out` as rank-1 with no `dims`, so the callee sees position `1` first — and the correspondence is positional: `u_j` is element `j + ng + 1`. So `u_{-1}` is element `ng`, `u_0` is element `ng + 1`, and `u_{nx}` is element `nx + ng + 1`. Stating this is not optional: an earlier version of this section carried the origin in the declaration `u(-ng:nx-1+ng)` and the issue #153 rewrite dropped it, leaving §3's `u_{-1}` and the `tests.md` judgments addressing positions the pinned signature does not define — which a producer and its checks would then have resolved the same wrong way together, so the wrap checks would pass for the wrong reason.

**Interior cells are copied.** `u_out` is a separate `intent(out)` array, so every element it publishes must be written: the interior `u_0 … u_{nx-1}` are copied from `u_in` unchanged, and the ghost cells take the periodic images §3 gives. Under the previous in-place form the copy was implied by the field not being rewritten; with separate arrays it is not, and an unwritten interior is undefined rather than unchanged.

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

`module_parameters` pins `dp` itself, not only the arguments that reference it. The stanza comparison pins an argument's type only SYMBOLICALLY — `real(dp)` matches a source that obtained `dp` from any kind at all — so what `dp` MEANS is pinned here and nowhere else, and narrowing it to `float32` would otherwise be a silent ABI change of exactly the class §5.1 exists to prevent. Pinning it means the generated source must BIND this name exactly once, by the parameter DECLARATION the block pins: an aliasing or plain `use` of the name is refused, and so is a second declaration of it anywhere in the file, including one inside a contained procedure (which would change that procedure's own dummy declarations, and so its ABI). The gate asks "does this statement bind the name" with the validator's own Fortran declaration reader rather than a pattern of its own, so the attribute form in any attribute order, a `kind=` in the type specification, and the `parameter (...)` statement form are all one question. What it does not see is a name supplied by an unrestricted `use`, which brings a binding no declaration reader can compare; the lint rule `C121` refuses that construct in the same `Generate.gate` substep, measured rather than assumed. An earlier version of this paragraph claimed the rule refused every further spelling "by construction"; it did not — three ordinary spellings were accepted until issue #153 round 3 measured them.

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
