# Runner output — the CUDA C++ binding

> **Audience: a runner-authoring `Generate` leaf of a node whose target profile names
> `toolchain.language: cuda_cpp`** (the `infrastructure` self-test). This document binds the
> language-neutral JSON serialization requirement of `docs/workflow/RUNNER_OUTPUT_CONTRACT.md` §4
> to CUDA C++ host code. The host inlines it after `RUNNER_OUTPUT_CONTRACT.md` wherever that
> document is inlined whole (the `harness` shape's two `generate` prompts), through the CUDA C++
> language backend's `prompt_fragments` capability (`tools/backends/language/cuda_cpp/prompts.py`).

## 1. JSON serialization in CUDA C++

JSON documents are written by host code. Enforcement is **format-syntactic**: `post_generate`
(`validate_pipeline_semantics --stage post_generate`) flags the mere presence of a forbidden
spelling in the runner source; it never inspects runtime output, so a runtime fixup does not pass.

- Do **not** use the `%a` / `%A` conversion (hexadecimal floating point) in the format literal of
  a printf-family call, or the `std::hexfloat` stream manipulator: neither produces a JSON number.
  Both are flagged (a literal passed to anything else is text, and is not read as a format).
- **Canonical safe idiom:** reals with `std::snprintf(buf, sizeof buf, "%.16e", x)` — seventeen
  significant digits in exponential form, a leading digit always present, and the value restored
  exactly by a standard parser; integers with `%d` / `%ld` / `%lld` matching the argument's type,
  or `std::to_string` of an integer; booleans by branching on the value and writing the literal
  `true` / `false`, never by printing the `bool` itself (a stream prints `1` / `0`).

  ```cpp
  #include <cmath>
  #include <cstdio>
  #include <string>

  std::string jnum(double x) {
    if (std::isnan(x)) return "NaN";                         // any sign bit
    if (std::isinf(x)) return x > 0 ? "Infinity" : "-Infinity";
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.16e", x);  // exponential, lossless; never %a
    return buf;
  }

  std::string jbool(bool b) { return b ? "true" : "false"; }  // literal true/false
  ```

- **A non-finite real** (a NaN, an infinity) is written as the literal token
  `RUNNER_OUTPUT_CONTRACT.md` §4 names — `NaN` (whatever the sign bit), `Infinity`, `-Infinity` —
  by branching with `std::isnan` / `std::isinf` (`<cmath>`) before the value reaches `%.16e`, as
  `jnum` above does. `%.16e` itself prints `nan` / `-nan` / `inf` / `-inf` (measured), none of
  which the host's reader accepts: the `post_execute` gate refuses the document as `invalid json`.
  Never write `null` for a non-finite value, and never throw or stop the program in the emitter
  over one.
