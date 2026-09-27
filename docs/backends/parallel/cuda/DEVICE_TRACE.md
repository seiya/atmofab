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
sh -c '<frame>' kernel-trace-summary kernel_trace_cuda_gpu_kern_sum.csv kernel_trace.sqlite \
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
- `--force-export=true` rebuilds the export database (`kernel_trace.sqlite`) rather than reusing
  one already there. The frame around the stats run (`_FRESH_OUTPUT_SCRIPT` in the code) says
  what the stats run's exit code does not, both measured on 2026.3.2:
  - it first removes whatever is at the summary's path and at the database's, and fails when it
    cannot (a directory there, a directory that is not writable): `--force-overwrite=true`
    replaces a WRITABLE file at the summary's path but not a read-only one, which survives while
    `nsys stats` prints `ERROR: Unable to open output file for writing` and exits 0. The binary
    ran in the same directory just before, so without the removal the file read afterwards could
    be the binary's;
  - after a stats run that exited 0 it requires the database: a readable report is exported,
    with kernel data or without, and a report that is not readable (truncated, or other bytes)
    is not — while the stats run still exits 0 and writes an EMPTY summary, the same bytes as
    "no kernel data". Without the database the frame exits 3.

  The frame is part of the command, so the remote job carries it too.
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
`__attribute__((…))`, `[[…]]` and the CUDA attributes `__launch_bounds__(…)`, `__cluster_dims__(…)` and `__maxnreg__(…)` blanked (by name: a kernel may be named `__k__`) so their parenthesis is not
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
  cases (a `steady_clock` read after the arguments are parsed and before the first case,
  `tools/backends/language/cuda_cpp/runner.py`),
  so the profiler's process start-up and its report generation are outside it, and the tracing
  cost of the process's CUDA calls — its initialisation at the first call included — is inside
  it.
- A failure of the profiler itself (it cannot inject into the process) ends the run with a
  non-zero code that cannot be told from the binary's own, so it routes as the runner's failure
  (`[run_program failed: runtime_error]`), not as host tooling. Not observed at the site.
- The report's path is not emptied before the run, because the binary runs inside the profiling
  command. A READ-ONLY file the binary leaves at `kernel_trace.nsys-rep` survives: measured on
  2026.3.2, `nsys profile` then writes its report under `/tmp/nsys-<user>/`, exits 0, and the
  summary reads the binary's file. A file that is not a readable report fails the summary (no
  export database, exit 3, `deterministic_validate_error`); passing it off as one would take writing a valid report
  whose kernel records name the model's kernels, from a source whose file I/O `Generate.gate`
  already refuses. Recorded, not closed.
- A binary that cannot be started at all (not executable, a bad interpreter, missing) makes
  `nsys profile` exit 1 (measured on 2026.3.2; run bare, the same binary gives 126 / 127, which
  the remote executor refuses as a host failure, `remote_execution.LAUNCH_CODES`). So under the trace such a binary routes as the
  runner's failure. Build's binary is executable at its own site (the launch probe's machine
  check, scp keeping the mode), and no run has reached this.
- The profiler's start-up and its report writing share the run's time bound
  (`RUN_PROGRAM_TIMEOUT_SEC`) with the binary: a run near that bound times out under the trace
  first. Not measured; today's runs take seconds.
- nsys's progress lines go to the binary's stdout and its warnings to the binary's stderr
  (`Device-side CUDA Event completion trace …` at the site, CPU-sampling warnings here), so
  `stdout.log` and `stderr.log` carry them. They are audit logs that no gate and no judge reads.
