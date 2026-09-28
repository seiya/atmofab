# Generate rules — `mpi`

> **Audience: the maintainer of the `Generate` gates, and the operator reading a refusal.**
> No leaf reads this document: a producer is told these rules by the `mpi` prompt fragments
> (`tools/prompt_templates/backends/parallel/mpi/`), and a physics source is held to them by the
> code named below. Issue #316 (R4-c PR-4).

## 1. How a kernel is distributed

A program built for a target whose parallel backend is `mpi` runs as `execution.ranks` processes
started by the launcher (`LAUNCHER.md`), and its quality check re-runs it as one process. The
target's harness provides `distributed_state@1` (`docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`
§Harness capability ABI); the launch gate refuses an `mpi` target whose harness does not, and a
distributed harness under a backend with no launcher (`target_profile._process_model_violations`).

- The harness is the only code that calls the message-passing library. A `component` or
  `problem` source calls the harness's distributed-state operations instead: the rank queries,
  `__partition`, `__exchange_halo_r1` / `__exchange_halo_r2` and the four reductions.
- The host-rendered runner starts and ends the runtime, gathers every partitioned bound array
  onto rank 0 at each capture point, sums the ranks' updated-cell counts, and calls the writers
  on rank 0 only. Which arrays are partitioned, and how, is the checks module's partition
  metadata (`docs/backends/language/fortran/CHECKS_ABI.md` §1-c).
- A bundle that distributes its state declares `target_lowering_plan.state_residency:
  distributed`, `distributed_state@1` among its `capability_requirements`, and says how in the
  plan's `parallelization` (`"model": "mpi"`) and `decomposition` members.

## 2. What the gates refuse

| gate | refused | code |
|---|---|---|
| `Generate.gate` static check, the presence floor (`component` / `problem` only) | a model or checks source that reaches the library directly: a `use` of `mpi` / `mpi_f08`, an `include` of `mpif.h`, a call of or reference to a name that starts with `mpi_` — whatever the plan says | `tools/backends/parallel/mpi/directives.py` |
| same, unless the plan's `parallelization` model explicitly names `none` or another model | a plan whose `state_residency` is not `distributed`; sources with no statement (or one-line logical `if`) calling `<harness>__partition` or `<harness>__exchange_halo_r<k>` by that name; a checks module that sets no bound array's `sb_<var>_axis` to anything but a literal `0` | same |
| `Generate.gate` static check, the checks-source gate | a physics `use` of the harness module without `only:`, or naming a harness name other than the distributed-state operations for a physics source; a bound array's partition metadata left unpublished | `tools/validate_pipeline_semantics._validate_checks_source_files`, with the renderer's `physics_harness_uses` / `distributed_state_names` |
| bundle acceptance | the same unpublished metadata, before anything is written; a `distributed` residency without `distributed_state@N` | `tools/codegen_bundle.pure_bundle_contract_violation` |
| the run | a bound array whose `sb_<var>_axis` is outside `0` to its rank, or not allocated at a capture point (the runner); ranks' owned ranges that do not tile the global extent (the harness's gather); a gathered shape other than the IR's `shape_expr` (the post-execute snapshot gate) | the rendered runner, the harness, `validate_pipeline_semantics` |

The presence floor does not count loops: whether a kernel needs distributing does not depend on
whether its source spells a counted loop, and a whole-array source is held to it as well.

## 3. What no gate sees

The floor is a PRESENCE floor. A kernel that computes the whole global range on every rank and
reports a partition — a `__partition` call, a non-zero axis, owned ranges that tile — passes
every check above, and its run gives the right answer, which the one-process quality check
confirms. It is certified as distributed without distributing. No deterministic check tells it
apart: `perf.json#cells_updated` does come out larger (each rank reports the whole range; a
round-2 measurement read 128 against 32 for the distributed flux component at four ranks), but it
is the kernel's own report and nothing compares it. Its sibling reports a partition only in a
branch no case reaches — every case computed replicated, "too small to distribute", with
`sb_<var>_axis` set non-zero in the unreached branch, which the floor counts. The producer IS told
to replicate a case whose grid cannot give every rank the `ng` cells the halo exchange needs, so
the reviewer is told the same threshold and holds the kernel to it. Both are `Generate.verify`
G6's judgment, told in the `mpi` reviewer fragment: whether each rank computes only the cells
it owns, whether the partition metadata describes them, whether the halo exchange precedes every
stencil read with the spec's periodicity, and whether every global quantity a check or a metric
reads goes through a reduction. The same class as the OpenMP floor's "a directive on some loops
only".

A plan that declines MPI is the producer's own claim; the floor trusts it, and G6 holds it to
the kernel.
