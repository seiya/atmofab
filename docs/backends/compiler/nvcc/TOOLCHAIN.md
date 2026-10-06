# Host toolchain — `nvcc`

> **Audience: the operator who sets up a host and an execution site for a `cuda_cpp` target, and
> the maintainer of the launch-time host probe.** No leaf reads this document. It is the `nvcc`
> binding of the roles `docs/RUNBOOK.md` §0-1 states neutrally (`missing_required_host_tools`);
> the code is `tools/backends/compiler/nvcc/syntax.py` and `tools/backends/linter/nvcc/lint.py`,
> and the probe that asks for the program is `tools/host_prerequisites.py`.

## 1. Roles

The CUDA compiler driver serves three roles on a target whose `toolchain.language` is
`cuda_cpp` (`cpp_gpu` in this tree):

| role | binding |
|---|---|
| the `static lint` tool (`linter/nvcc`, every warning an error) | `docs/backends/linter/nvcc/RULES.md` |
| the mandatory `Generate.gate` syntax-only stage (the `cuda_cpp` language backend's `MANDATORY_SYNTAX_COMPILER`) | `tools/backends/compiler/nvcc/syntax.py` |
| the compiler the build control file pins (`NVCC`, the `cuda_cpp` control file's compiler variable, `tools/backends/language/cuda_cpp/control_file.py`) | `docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §4 |

The probe lists `nvcc` once, under its first role (the linter). Its absence used to surface at
`Generate.gate`, after `Compile` and `Generate.generate` had been billed. Its supported versions
are the `nvcc` linter's range, refused at launch outside it
(`unsupported_required_host_tool_versions`; `docs/backends/linter/nvcc/RULES.md`
§Requirements).

## 2. Where it is needed

`nvcc` comes with the CUDA toolkit, which also supplies the runtime the build links. No install
line is given here: the toolkit is installed per the vendor's instructions for the platform.

- **This host** runs `Generate.gate`, so it needs `nvcc` for the lint and the syntax-only stage.
  The phases up to `Generate` run on a host with no GPU.
- **The execution site** that executes the `gpu` class BUILDS the binary (issue #333), with that
  site's `nvcc`, which must then be on the site's non-interactive `PATH` where the build runs — or
  put there by the site's `setup` lines in `sites.yaml` (`docs/examples/sites.example.yaml`).

## 3. What the build derivation key records

The build derivation key records the compiler as `nvcc`'s `release` line on THIS host (the one
`Generate.gate` runs) and the target's `hardware.architecture`, not the host C++ compiler `nvcc`
drives, so changing that compiler alone reuses a certified build. The site's own `nvcc` version is
recorded in `binary_meta.json#environment.compiler_version`, not keyed.
