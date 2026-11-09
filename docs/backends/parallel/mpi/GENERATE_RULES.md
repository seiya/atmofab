# Generate rules — `mpi`

> **Audience: the maintainer of the `Generate` gates, and the operator reading a refusal.**
> No leaf reads this document: a producer is told these rules by the `mpi` prompt fragments
> (`tools/prompt_templates/backends/parallel/mpi/`), and a physics source is held to them by the
> code named below. Issue #316 (R4-c PR-4); the distributed driver, issue #610 (Plan B of #589).

## 1. How a case is distributed

A program built for a target whose parallel backend is `mpi` runs as `execution.ranks` processes
started by the launcher (`LAUNCHER.md`), arranged in the process grid the host imposes
(`execution.process_grid`, then each `execution.sweep.grids` entry), and its quality check
re-runs it as one process. The target's harness provides `distributed_state@2`
(`docs/workflow/CODEGEN_BUNDLE_CONTRACT.md` §Harness capability ABI); the launch gate refuses an
`mpi` target whose harness does not, and a distributed harness under a backend with no launcher
(`target_profile._process_model_violations`).

The host-rendered driver (`tools/backends/language/fortran/runner.py`, its distributed form)
distributes each case; the kernel decides none of it.

- **The axes are the spec's.** `drive.distribute` names the spatial axes of the node, transcribed
  from the `tests.md` `distribute:` line (`docs/TESTS.md` §Description format); axis k is
  partitioned along grid dimension k+1, so a one-dimensional node is partitioned along dimension 1
  only, and on a `(1, p)` grid every case of it runs replicated. The layout of every array (which
  dimension carries which axis as owned cells, cells plus one shared face, or cells plus a ghost
  margin) is derived from its `dims` tokens (`runner_ir.array_layouts`).
- **One decider.** Whether a case is distributed on a grid is `runner_ir.distribute_plan(ir,
  case, grid)`: some declared axis has more than one rank along its grid dimension, the cell
  count along every declared axis is at least the rank count times that axis's `min_block`, and
  the axis inputs are consistent integers. The renderer evaluates it for each grid of
  `runner_ir.distribute_grids(target)` and renders the answers as the driver's replication table;
  the driver stops on a grid the table does not list. A case it does not distribute is
  replicated: every rank holds the whole case and calls the operation on it.
- **Every rank reads the whole case.** On a distributed case the driver partitions each axis
  (`__partition`), copies every array that carries an axis into a fresh allocatable at its rank's
  block — the owned cells, the shared face of a `face` dimension, the ghost margin of a `ghost`
  one sliced from the global array — and calls the operation on the block. A rank-0 integer bound
  directly to an axis's `cells` input receives the block's count, and one bound to its `extended`
  input the block's count plus twice the ghost width (B4); `block.<axis>.first` is the block's
  first global index (B5); every other binding is unchanged.
- **The stop is rank-uniform.** Each call's logical result is the ranks' together (a refusal on
  any rank is the refusal); after an accepted `init`, each rank-0 real or integer it wrote must be
  finite on every rank and equal across the ranks, or the run stops naming it.
- **Captures.** At each capture point the driver gathers onto rank 0 every partitioned array it
  or the kernel writes, and boxes an array nothing writes from the global copy every rank read;
  rank 0 writes. A written array's series extremes are folded over the owned cells and reduced
  over the ranks with a non-finite flag; an unwritten array's are folded from its global copy.
  `perf.json#cells_updated` is computed on rank 0 from the global extents, and `mpi_ranks` is the
  rank count the run observes.
- **A `component`'s operations compute exactly what the spec says over the arrays they are
  handed**, and name no harness operation: the driver and every consumer call them on a rank's
  own block (issue #512 PR-4a's closing run: components that partitioned inside their operations
  made the problem wrong on every grid with `p1 > 1`).
- **A `problem`'s `__step` works on its block**, over ghost-extended temporaries of its own: it
  exchanges them with `__exchange_halo_r1` / `__exchange_halo_r2` along every partitioned axis
  before a stencil read crosses a block edge, with the spec's periodicity, along dimension 1 then
  dimension 2 for a corner; it restores each wall side by global index (`block.<axis>.first` and
  the case's count, carried by a `derived` entry) and leaves the other sides to the exchange.
- A bundle declares `target_lowering_plan.state_residency: distributed` and
  `distributed_state@2` among its `capability_requirements` on both kinds.

The shape comparison (`_shape_verdict`) holds each grid's verdict and captures to the singleton's
over the cases a cross-target predicate targets; `_shape_state_agreement` answers
`not_applicable` for any other case. Every `pass` case of the 17 bench IRs is such a target today.

## 2. What the gates refuse

| gate | refused | the party the message names | code |
|---|---|---|---|
| `Compile.static`, gate (n) | a `drive.distribute` block that is absent or malformed; an array whose layout does not derive from its `dims`; an axis input no rank-0 integer dummy binds; a ghost-laid `out` / `inout`; a rank-0 real / integer `out` of a one-call `step` on a node with axes; a record of a ghost-laid dummy | the IR: the Compile leaf's | `tools/primary_evidence.py` (`drive_violations`) |
| `Compile.static`, gate (o) | a `drive.distribute` that differs from the `tests.md` line, member by member; a missing, doubled or malformed line | the line where the line is malformed or absent (the operator's), else the IR | `tools/validate_pipeline_semantics._distribute_line_violations` |
| `Generate.gate` static check, the presence floor (`component` / `problem` only) | a model source that reaches the library directly: a `use` of `mpi` / `mpi_f08`, an `include` of `mpif.h`, a call of or reference to a name that starts with `mpi_`; on a `component`, a model source that names any harness operation | the model source | `tools/backends/parallel/mpi/directives.py` |
| same | a plan whose `state_residency` is not `distributed`; a `problem` whose model sources have no statement (or one-line logical `if`) calling the target harness's `<harness>__exchange_halo_r1` / `_r2` by that name | the bundle, the model source | same |
| `Generate.gate` static check, the source-isolation gate | a physics `use` of the harness module without `only:`, or naming a harness name other than the distributed-state operations for a physics source (`__partition` is the driver's) | the model source | `tools/validate_pipeline_semantics._validate_model_source_isolation`, with the renderer's `physics_harness_uses` |
| bundle acceptance | a `distributed` residency without `distributed_state@N` | the bundle | `tools/codegen_bundle.pure_bundle_contract_violation` |
| the run | a grid the replication table does not list; an `init` scalar that is non-finite on a rank or unequal across the ranks (the driver); contributed blocks that do not tile the global array, or an exchange on a block under `ng` (the harness); an `SPEC2SCIENCE_PROCESS_GRID` that is malformed or does not multiply to the rank count (the harness's start); a gathered shape other than the IR's `shape_expr` (the post-execute snapshot gate) | the run record | the driver, the harness, `validate_pipeline_semantics` |

A plan that declines MPI exempts nothing: the driver distributes from the IR whatever the plan's
`parallelization` says. The presence floor does not count loops.

## 3. Limits

- **What the line omits is not split.** An operator `distribute:` line that omits an axis leaves
  that axis whole on every rank; one that omits an axis's `extended` (or `ghost`) leaves an array
  laid along that ghost-extended count unsplit, and the kernel sees that whole extent beside a
  local count on the other members. No gate reads the line against the spec's placement
  (`docs/TESTS.md`); `Compile.verify` V3 (iv) and `skills/spec-input-check` do.
- **The global count reaches a kernel only through a `derived` entry.** Every rank-0 integer
  dummy bound directly to an axis's `cells` (resp. `extended`) input receives the block's count
  (resp. plus twice the ghost width), two dummies bound to one input alike; a dummy bound to a
  `derived` entry whose value is that input (`derived.nx_g: inputs.grid.nx`) receives the case's.
- **Replication by threshold.** A case whose cell count along a split axis is under the rank
  count times `min_block` runs replicated on that grid; `min_block` is the smallest extent the
  kernel's own guard admits. Of the nodes that declare axes, six `pass` cases of the bench run distributed on no grid of
  `fortran_cpu_mpi`: the flux's `l0_min_extent` (its spec's `nx >= 2`), and the `cells = 1` cases
  `l0_min_extent` of muscl, coriolis and rk4 and `l0_min_extent` / `l0_min_extent_axis` of tc4.
  No workflow gate reads whether a node's `pass` cases distribute; a suite row over the bench does
  (`test_spec_corpus_versions.py`).
- **A whole-field sum is the reduced sum over the reduced count, times the case's count.** A
  case the table replicates reaches every rank whole, a block of a node with fewer declared axes
  than the grid has dimensions reaches every rank along the other dimension, and a distributed
  block reaches one rank; a kernel cannot tell which without deciding from the grid (the
  reviewer's finding), so `__reduce_sum` over the ranks counts each cell once per rank holding
  it. Fixture model (3) summing its block's cell count through it, case `c6`, measured 6 on
  `(1, 1)`, `(2, 1)`, `(3, 1)` and `(4, 1)`, 12 on `(2, 2)` and `(1, 2)`, 18 on `(1, 3)` and 24
  on `(1, 4)` (2026-11-08). On every shape the driver produces, though, every cell of a case is
  held by the SAME number of ranks, so the `__reduce_sum` of the block's sum divided by the
  `__reduce_sum_int` of the block's cell count is the field's mean on every grid, and a total is
  that mean times the case's count from a `derived` binding. Fixture model (2) taking `dt` from
  that mean, from that total, and (axis `x` alone) from that mean again wrote the singleton's
  `dt` on all seven grids of the sweep (the certified `(2, 2)` and six others, of 2 to 4 ranks), `ny4` replicated on `(1, 4)` and `(1, 3)`
  included; dividing the reduced sum by the case's count instead wrote it 4 times the
  singleton's on `(1, 4)` and 3 times on `(1, 3)` for `ny4`, and on the one-axis node 2, 4, 3
  and 2 times on `(2, 2)`, `(1, 4)`, `(1, 3)` and `(1, 2)` for both cases (2026-11-08;
  `test_driver_render.DistributedDriverEndToEndTest.test_a_whole_field_mean_divides_by_the_reduced_count_of_the_cells_it_summed`
  pins the mean, the total and the un-normalised `dt`; its grids and tolerance are the row's). An extreme goes through
  `__reduce_min` / `__reduce_max`, which a copy does not change, and a reduced count of refusals
  compared against zero tells whether any rank refuses. Corpus use: on 2026-11-08 no
  `problem` model generated for `fortran_cpu_mpi` called `__reduce_sum` (two `component` models
  of 2026-10-17 did, and the floor now refuses a component that names a harness operation).
- **A node with `distribute: none`** is certified as the kernel's result on rank 0 of a replicated
  run, with the reduced result and the series extremes the only cross-rank facts in the record.
  Rank-independence of such a `component` rests on the floor (no harness name in its model); a
  future axis-less `problem` would have neither.
- **An interior block's upper face is in no record.** The face gather takes each shared face from
  the right neighbour's first position, so a kernel wrong at its last local face and right at the
  domain's last face agrees on every grid; the spec's value predicates over the domain's faces
  judge a face formula (`test_driver_render.DistributedDriverEndToEndTest` pins the silence).
- **A `loop` component with a halo** would need an exchange between steps that neither the kernel
  (no harness name) nor the driver (no periodicity) owns; none exists.
- **What a `problem` can exchange** is the harness's `exchange_halo_r1` / `_r2` (a rank-3 temporary
  one component slice at a time), and the harness stops an exchange on a block under `ng`, so a
  problem's `min_block` is at least its kernel's `ng`.
- **A scalar `out` of a one-call `step`** on a node with axes is refused at `Compile.static`: a
  block-local scalar has no global meaning the driver can gather.
- **A `record` of a `ghost`-laid dummy** is refused at `Compile.static`: only `cell` and `face`
  layouts are gathered.
- **A sequence-association mis-shape** (a global extent declared over a block) is a run failure or
  a shape disagreement, not a bounds diagnostic.
- **The `checked` row runs on `(1, 1)`**, where every case is replicated, so the bounds-checked
  binary exercises no block margin: a kernel reading past its block is diagnosed on the serial
  target and detected here only where the shape comparison sees it.
- **A kernel that computes over rank 0's cells alone, or decides its work from the grid**, passes
  every gate above; `Generate.verify` G6 judges it (the reviewer fragment), and the shape
  comparison over the sweep is what tells it on a case the table distributes.
