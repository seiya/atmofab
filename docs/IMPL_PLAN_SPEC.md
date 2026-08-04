# Implementation Plan (the `spec.ir.yaml.impl_defaults` section)

## Position
The `impl_defaults` section of `spec.ir.yaml` holds the default values for implementation discretion (B). In the core workflow, the stages from `Generate` onward use this value as a **fixed value**. Variant exploration of implementation discretion is the responsibility of the optional flow `Tune`, and `Tune` separately generates variant candidates with `spec.ir.yaml` as an invariant premise.

## Design Policy
Implementation discretion is expressed in a **2-layer structure (Abstract Knobs + Backend Overrides)**.

- **abstract**: the expression of "intent" that is less hardware/language dependent (easy to auto-explore)
- **backend**: backend-specific parameters such as OpenACC / CUDA Fortran / CUDA C++ (to land in an implementation)

This structure satisfies the following.
- Even if the optional flow `Tune` expands the exploration space, the expression is less likely to break down
- The concrete parameters needed for the implementation can be made explicit
- Even when a backend is added, the existing tuning history is less likely to be wasted

## 1. The boundary of generalization
- Generalize: the "intent" of loop transformation (tiling, fusion, parallel granularity, vectorization, the memory-layout policy, the async/overlap policy)
- Do not generalize: compiler-specific flags, GPU-architecture-specific details, the concrete way of writing a pragma/attribute
- Isolate these in `backend_overrides`

## 2. Required items
`spec.ir.yaml.impl_defaults` requires the following.

- `target.class` (cpu/gpu etc.)
- `target.backend` — the parallel-backend token, e.g. `openmp`, `cuda`, `mpi` (canonical field definition: `docs/workflow/phases/phase_01_compile.md`). The composite identifier such as `cpu_openmp_x86_64` belongs in `selected.backend_key`, NOT here: the `Generate.gate` `!$omp` floor keys off `target.backend == "openmp"`, so a composite value here silently disables it (the knob-name gate is backend-agnostic by design)
- `target.architecture` (e.g. `x86_64`, `aarch64`, `nvidia_sm80`)
- `toolchain.language` (`fortran` — the only implemented value; see the rules below)
- `toolchain.standard` (the language standard spelled the way the compiler names it — e.g. `f2008`, `c++17`; it is passed verbatim as `-std=<value>`, so `2008` is rejected by the compiler driver)
- `toolchain.build_system` (`make` — the only implemented value; see the rules below)
- `abstract` (language-independent knobs; the parallelization family has canonical key names — `parallelization` / `parallel_scope` / `parallel_granularity`, per `spec/schema/ir/impl_defaults.schema.json`)
- `backend_overrides` (language/backend-dependent knobs; under `openmp`: `num_threads` / `schedule` / `chunk_size` / `collapse` / `nested`, same canonical source)
- `selected.backend_key`

Rules:
- **The programming language must be fixed in `Compile`.**
- **The target architecture must be fixed in `Compile`.**
- **`toolchain.build_system` is `make` on every `spec_kind`, and `toolchain.language` is `fortran` on every `spec_kind` other than `infrastructure`.** That pair is the only implemented physical backend: the `runner` and `src/Makefile` are host-authored for `make` + `fortran` alone, and the deterministic `Compile.static` gate `_validate_toolchain_backend_supported` (`docs/workflow/phases/phase_01_compile.md`) fails any other pair, routing back to `Compile.generate` for a re-author. This holds regardless of `target.class` and regardless of any language the user names — a `controlled_spec` is language-neutral by construction, so it never pins a toolchain. Adding another backend is a repository-level change (a host-side `runner` renderer and `Makefile` writer for it), not a per-spec decision.
- `toolchain.language` is fixed at `Compile` time.
- When the user does not explicitly specify the loop parallelization method for `target.class=cpu`, the generator applies `OpenMP` to parallelizable loops.
- When the user explicitly specifies the loop parallelization method, that specification takes precedence. Forcing `OpenMP` onto a non-parallelizable loop is forbidden.
- `target.class` other than `cpu` / `gpu` does not change the `toolchain` rule above; it affects only the `target` / `abstract` completion.
- When `toolchain.language` / `toolchain.standard` / `toolchain.build_system` are undefined in `impl_defaults`, it is a `fail` in `Compile.verify`.
- When `target.architecture` is undefined, it is a `fail` in `Compile.verify`.
- `toolchain.build_system` is `make`. It is also the value an absent key defaults to, in both the conductor and the `Compile.static` gate — but the key is still **stated explicitly**, per the `Compile.verify` `fail` rule above and V6 (`docs/workflow/phases/phase_01_compile.md`); the shared default exists so that the gate and the conductor read the same IR the same way, not so the key may be omitted. The same holds for `toolchain.language` (`fortran`), which the `post_generate` lint and syntax-evidence gates also read.

## 3. Optional items (environment-dependent)
- `toolchain.compiler` / `toolchain.linker` are **optional**.
- State them only when you want to fix the compiler type/version (emphasizing CI reproducibility).
- When not fixed, use the execution environment's default compiler.
- With `build_system=make` ∧ `language=fortran`, the conductor-authored `src/Makefile` pins `FC` to `toolchain.compiler` when it is set (else `gfortran`), so a future non-gfortran build (e.g. Fujitsu `frt`) only needs this field plus a `run_syntax_check` compiler adapter (`mcp_servers/README.md`). The deterministic `Generate.gate` syntax check always runs its mandatory `gfortran -fsyntax-only` stage against `toolchain.standard` regardless of the build compiler (standard conformance is the contract; the build compiler is an implementation detail).
- The operation of directly calling `gcc` / `clang` / `gfortran` for a one-off build is forbidden; always build via `toolchain.build_system`.

## 4. Composition rules of the output (common across languages)
- Regardless of language, the generated code separates `model` (physics computation) and `runner` (input/output / judgment coordination).
- The `runner` calls the `model` via `call` / `use` / `import`.
- The physics-update logic must not be duplicated on the `runner` side.
