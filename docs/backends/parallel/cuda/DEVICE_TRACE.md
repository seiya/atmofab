# Device trace — `cuda`

> **Audience: the operator who runs a `cuda` target, and the maintainer of `Validate.execute`.**
> No leaf reads this document. It is the `cuda` binding of the `device_trace` capability; the
> execute path is `docs/workflow/phases/phase_04_validate.md`, and the code is
> `tools/backends/parallel/cuda/trace.py` (issue #307).

## 1. What runs

A binary built for the `cuda` parallel model runs under Nsight Systems at `Validate.execute`,
and a second command reads the trace it wrote. Both run in the run's working directory, at the
site that executes the run (this host for the local site, the job directory's `run/` for a
remote one), in this order:

```
nsys profile -t cuda -o kernel_trace --force-overwrite=true <binary> --cases <spec.ir.yaml> <case_id>...
nsys stats -r cuda_gpu_kern_sum -f csv -o kernel_trace --force-export=true --force-overwrite=true kernel_trace.nsys-rep
make test    # the quality check, unchanged and untraced
```

- `nsys profile` returns the binary's exit code, so a runner that fails is the content failure it
  was before. The summary runs only after a run that exited 0, and the quality check only after a
  summary that exited 0 (at a remote site the job's chain rule; locally the same order).
- The summary is written to `kernel_trace_cuda_gpu_kern_sum.csv` (`-o X` gives
  `X_cuda_gpu_kern_sum.csv`). The conductor promotes it to the run node's `kernel_trace.csv` and
  records the command that wrote it as `trial_meta.json#kernel_trace`. The `.nsys-rep` report
  and its `.sqlite` export are not kept.
- `--force-export=true` rebuilds the export rather than reusing one already there, and
  `--force-overwrite=true` replaces a summary already at that path: a file written there before
  the command ran does not survive it.
- A summary command that exits non-zero, or exits 0 without writing the file, fails the substep
  as `deterministic_validate_error`: it is host tooling, not the kernel.
- `trial_meta.json#environment.launch.argv_prefix` records the `nsys profile …` prefix, and the
  post-execute gate binds the binary AFTER it to the node's build.

## 2. What the summary says

The CSV's columns are `Time (%)`, `Total Time (ns)`, `Instances`, `Avg (ns)`, `Med (ns)`,
`Min (ns)`, `Max (ns)`, `StdDev (ns)`, `Name`. The reader (`kernel_instances`) reads `Name` and
`Instances` by column name and refuses a header without them (`SummaryUnreadable`).

- `Name` is demangled: `plain_k(double *, long)` (CSV-quoted, since it holds a comma),
  `ns::ns_k(int *)`, and one row per template instantiation, `void tmpl_k<double>(T1 *)`. The
  reader reduces each to its base name (`base_kernel_name`: the trailing parameter list and
  template argument list removed, the last word, its last `::` segment) and sums the instances
  of one base name.
- A kernel that never ran has no row.
- **A report with no kernel data gives an EMPTY file and exit 0**: `nsys stats` prints
  `SKIPPED: kernel_trace.sqlite does not contain CUDA kernel data.` and the exit code says
  nothing. The reader answers `None` for it (and for a header with no row).

`defined_kernels` reads a CUDA C++ source for the names of the `__global__` functions it defines
or declares, over the language backend's code view (comments and literal contents masked), with
`__launch_bounds__(…)`, `__attribute__((…))` and `[[…]]` blanked so their parenthesis is not
taken for the parameter list.

Measured: on Nsight Systems 2026.3.2 without a GPU (issue #307 plan §0-2), and at the `cpp_gpu`
site on Nsight Systems 2025.1.3 with an L40S (issue #307 comment 5851969504): the columns, the
name forms above, the overwrite of a file placed at the summary's path, and the empty file for a
binary built for another device architecture, whose every launch failed.

## 3. What the site must provide

`nsys` on the PATH of the machine that executes the run: a remote site's non-interactive login
(the launch probe asks it, `missing_required_site_tools`), or this host when the local site
executes the run (asked with `shutil.which` at launch, same reason). A run that stops before
`Validate` is asked nothing. The version range measured is §2's.

## 4. What it does not do

- The trace covers the whole process: which case launched a kernel is not recorded.
- Kernels of two namespaces with one base name are counted as one.
- The quality check (`make test`) is not traced.
- `perf.json` comes from the traced run: the runner's wall-clock is its own clock around the
  cases (a `steady_clock` read at the top of `main`, `tools/backends/language/cuda_cpp/runner.py`),
  so the profiler's process start-up and its report generation are outside it, and the tracing
  cost of the process's CUDA calls — its initialisation at the first call included — is inside
  it.
- nsys's progress lines go to the binary's stdout, so `stdout.log` carries them. It is an audit
  log that no gate and no judge reads.
