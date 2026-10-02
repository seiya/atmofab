# Static lint rule set — the `nvcc` linter backend

> **Audience: the maintainer of the `cuda_cpp` static-lint gate.** The declared rule set, the
> version range and the measurements behind them. The machine-readable copy is
> `tools/backends/linter/nvcc/lint.py`; where the two differ, the module governs.

## Requirements

- **The rule set is the flag list** `-std=c++17 -Xcompiler=-Wall,-Wextra,-Werror --Werror all-warnings`,
  run as `-Xcompiler -fsyntax-only -rdc=true -c` over each `.cu` under the lint target, handed to the
  driver by name (`SOURCE_SUFFIXES`; `mcp_servers/build_runtime_server.py` `_lint_command_over`).
  `-rdc=true` (relocatable device code) is the build's own mode
  (`docs/backends/language/cuda_cpp/BUNDLE_BINDING.md` §4): without it a kernel's call to a
  `__host__ __device__` operation another file defines is a `ptxas fatal : Unresolved extern
  function` here, on a source the build compiles and links.
- **Supported driver versions:** `>=13.0,<14.0` (`MIN_VERSION` / `BELOW_VERSION`), refused at launch
  outside it. The host C++ compiler the driver invokes is not bounded.
- **Launch self-check:** the declared flags over an empty translation unit (`-x cu /dev/null`) must
  exit 0.

## Decision Criteria

- **Exit status.** A run over sources exits 0 (clean), or 1, 2 or 255 (findings; which one
  depends on the diagnostic, see §Measurements; 255 is a device-assembler error, since
  `-Xcompiler -fsyntax-only` stops only the host compile). Under `-rdc=true` the device assembler
  no longer checks a kernel's static shared memory against the limit — the device link does — so
  that finding moved from this gate to Build's link step, a `Build fail` that triggers a Generate
  retry (`docs/workflow/phases/phase_03_build.md`). A refused flag also exits 1; the launch self-check is what keeps a
  build that refuses a declared flag from reaching the gate, so the gate reads 0 / 1 / 2 / 255 as a
  verdict and any other status as an unusable invocation.
- **Suppression.** No flag disables an in-source diagnostic pragma, so the language backend's
  static check refuses every one (`docs/backends/language/cuda_cpp/CHECKS_ABI.md` §5).

## Measurements

Measured 2026-09-26 with `nvcc` 13.4 (V13.4.92) and host `g++` 11.4, on sources with no GPU
present; re-measured 2026-10-02 with `-rdc=true` added (issue #380), same driver and host compiler,
every row unchanged except the shared-memory one:

| source | exit status |
|---|---|
| a kernel, its launch, no warning | 0 |
| an unused parameter | 1 (`-Werror=unused-parameter`, host compiler) |
| an unused variable | 1 (`#177-D`, front end) |
| a signed/unsigned comparison | 1 (host compiler) |
| a variable used before it is set | 2 (`#549-D`, front end) |
| a non-`void` function that can reach its end | non-zero (`#940-D`, front end) |
| a `switch` over an enum missing an enumerator | non-zero (`-Werror=switch`, host compiler) |
| a kernel over the shared-memory limit | 0 here (255 without `-rdc=true`); the device link refuses it, `nvlink error … uses too much shared data`, 255 |
| a kernel with invalid inline PTX | 255 (`ptxas fatal`, device assembler) |
| a kernel calling a `__host__ __device__` function another file defines | 0 (255 without `-rdc=true`, `ptxas fatal : Unresolved extern function`) |
| the clean source plus an unknown flag | 1 (`nvcc fatal : Unknown option`) |
| the declared flags over `-x cu /dev/null` | 0 |

Several sources in one invocation stop at the first that fails; no object file is left in the
source directory or a subdirectory.
