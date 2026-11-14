#!/usr/bin/env python3
"""Run a person-written configuration on a node's certified binary (issue #632, system tier
Part 2): a certified case as the base, with some of its numeric `inputs` overridden.

  python3 tools/run_configuration.py <spec_ref> --target <target_id> --config <file>
      --out <dir> [--restart-from <capture.json>] [--timeout-sec <N>]

Operator-only. The workflow never invokes it, no leaf runs, and no verdict is reached: a run
of this tool is NOT a certification of its configuration, and its `run_record.json` says so.

The configuration file (YAML)::

    base_case: <case_id of the certified IR>
    inputs:                      # optional; each key a dotted path under the case's `inputs`
      grid.nx: 64
      physics.nu: 2.0e-3

What it does, in order (each refusal exits 2 before anything runs):

1. Resolves the node's certified `Validate` output on the target with the workflow's own
   resolver (`DerivationResolver.select(node_key, "validate")`) and refuses when there is none,
   naming the resolver's reason. It does not fall back to "the latest passed pipeline". The
   binary must have been built at the local site (`binary_meta.json#environment.build_site`),
   `sites.yaml` must map the target to the local site (as a run reads it), the launch shape is
   the one at that site (`host_execution.launch_shape`, whose refusals are refusals here), and
   a target whose parallel backend starts its binary under a launcher is refused: this version
   runs at the local site, one process.
2. Applies the overrides to the base case and evaluates it with the host's evaluator
   (`primary_evidence.evaluate_case_inputs`), so every refusal a certified case meets (`n_step`,
   `dt`, the 4-ulp `t_end` rule, `history.every`, the extents) applies here too. Refused: a
   `base_case` that is no case of the IR or is a continuation; an override of a path that is
   not a numeric leaf of the base's `inputs`; a type change (an integer leaf given a real, a
   real given an integer, anything given a boolean); an override of an input the case reads
   by no name (no key of its case file, no `initial_state` or `derived` expression, no
   coordinate's `count` or `length`) and whose removal leaves the case file the same. An input
   read by name is never refused as a no-op, even where the evaluated case file does not move
   with it (a `min()` whose other branch binds).
3. Writes `<out>/case_inputs/<base_case>.json` — the BASE's id, because a driver rendered in
   the list form stops on an id it does not know — and a copy of the certified IR as
   `<out>/spec.ir.yaml`, then runs the certified binary with `--cases <out>/spec.ir.yaml
   <base_case>` in `<out>`, under the target's launch environment over this process's own. It
   starts the process itself (no 3600 s bound of the build-runtime library); `--timeout-sec`
   is the only bound. On the bound, on SIGINT, and on SIGTERM or SIGHUP to the tool (a `kill`,
   a closed terminal), the binary's process group is killed and the record says so; a SIGKILL
   to the tool cannot be caught, and the binary then outlives it with the record left
   `running` (the record carries the binary's process group as `binary_pgid`, and a
   continuation from that run is refused while the group is alive). A signal that arrives
   after the binary exited and before the record is final
   leaves the record `interrupted`. Nothing under the certified IR's `case_inputs/` is
   written. The run is `ok` only when the binary exits 0 and its final capture holds every
   state an entry's result dummy is bound to in the case, true: a kernel that refuses a
   configuration exits 0 and stops short of `n_step`.
4. `--restart-from <capture.json>` continues an earlier run of this tool: the capture (a
   history sample `<prev>/raw/state_snapshots/history/<base_case>__<K>.json`, or a final
   capture `<prev>/raw/state_snapshots/<base_case>.json`) must sit in an output directory of
   this tool whose `run_record.json` names the same node, target, IR, binary and
   configuration, and whose case file, its `restart.*` keys aside, is this run's evaluated case
   file. The IR's own continuation cases play no part.
   The capture is refused by the continuation writer's own refusals (`write_continuation_
   inputs`: its `step`, every restart variable present with the evaluated extents and finite,
   a finite time); `K` must be at least 1 and, where `n_step` is host data, below it; where
   `t_start` and `dt` are host data, `t_start + K * dt` must be within 4 ulp of the capture's
   time. The case file then carries `restart.step = K` and the `restart.<v>` keys. After the
   run, the host's start check (`continuation_start_violation`: the initial capture's `step`,
   time and every restart variable equal to the capture's) must hold, or the run fails.
5. `<out>` must be new or empty and outside `workspace/`. `<out>/run_record.json` is written
   before the run (status `running`) and again after it: the node key, the target, the
   selected ids, the binary's sha256, the configuration verbatim, the restart source with its
   sha256 and step, the command, the launch environment the tool set over its own, the exit
   status, the final capture's step and the kernel's outcome, the start check, and the
   statement that the run certifies nothing. stdout and stderr go to `<out>/stdout.txt` and
   `<out>/stderr.txt`.

Exit status: 0 when the run is `ok` (and, on a continuation, the start check held); 1 when the
binary exited non-zero, timed out, stopped short with a false result, or failed the start
check; 2 when the configuration or the selection was refused before anything was written,
and when the binary could not be started (status `not_started`; `<out>` then holds the case
file and the record); 128 + the signal number on SIGINT, SIGTERM or SIGHUP (130, 143,
129), with the record `interrupted`.

What it does not do (issue #632 §3): stop a run at a chosen step; extend a finished run (a
final capture is refused by `K < n_step` where `n_step` is host data, and by the driver at run
time, exit 1, where `init` writes it); continue with another
configuration or `dt`; run at a remote site or under a launcher; keep the series of a killed
segment (the driver writes the series at the end of a case). `docs/RUNBOOK.md` §"Running a
person-written configuration" is the operator's procedure.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The record this tool writes beside the run, and reads beside a restart source.
RUN_RECORD = "run_record.json"
#: What every record states (issue #632 O1: the run is not a certification).
NOT_A_CERTIFICATION = (
    "This run is not a certification of this configuration: the binary was certified on the "
    "cases of its IR, and no predicate was evaluated on this run.")
EXIT_RAN_FAILED = 1
EXIT_REFUSED = 2


class ConfigurationRefused(Exception):
    """A refusal before anything ran; the message names what and why."""


class Terminated(BaseException):
    """The tool received SIGTERM or SIGHUP while the binary ran (`_run`): the binary's process
    group was killed and the record says so."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum

#: The signals that end the tool from outside (a `kill`, a closed terminal) and are turned
#: into `Terminated` from the first record write to the last, so that the binary's process
#: group dies with the tool and the record says so. SIGINT arrives as `KeyboardInterrupt`.
#: SIGKILL cannot be caught: the binary then outlives the tool, in a session of its own, and
#: the record stays `running`.
_TERMINATING = (signal.SIGTERM, signal.SIGHUP)
#: Held back while the binary is being started, so that none of them lands between the fork
#: and the binding of the process the kill needs; the child unblocks them before its exec.
_HELD = (*_TERMINATING, signal.SIGINT)


def _raise_terminated(signum: int, _frame: Any) -> None:
    raise Terminated(signum)


def _unhold() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _HELD)


# ----------------------------------------------------------------------------- helpers

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_real(value: Any) -> bool:
    return isinstance(value, float)


# ----------------------------------------------------------------------------- configuration

def read_configuration(path: Path) -> tuple[str, dict[str, Any], str]:
    """(base_case, overrides, the file's text) of a configuration file; refuses any other
    shape. Each override value must be an integer or a finite real (YAML spelling)."""
    try:
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationRefused(f"--config {path}: unreadable: {exc}") from None
    if not isinstance(doc, dict) or not set(doc) <= {"base_case", "inputs"} \
            or "base_case" not in doc:
        raise ConfigurationRefused(
            f"--config {path}: a mapping of `base_case` and, optionally, `inputs` is expected; "
            f"got {sorted(doc) if isinstance(doc, dict) else type(doc).__name__}")
    base = doc["base_case"]
    if not isinstance(base, str) or not base.strip():
        raise ConfigurationRefused(f"--config {path}: base_case must be a case id")
    overrides = doc.get("inputs")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigurationRefused(f"--config {path}: inputs must be a mapping of dotted path "
                                   "-> number")
    for key, value in overrides.items():
        if not isinstance(key, str) or not key or any(not seg for seg in key.split(".")):
            raise ConfigurationRefused(f"--config {path}: inputs key {key!r} is not a dotted "
                                       "path")
        if not (_is_int(value) or (_is_real(value) and math.isfinite(value))):
            hint = ""
            if isinstance(value, str):
                try:
                    float(value)
                    hint = (" (YAML reads an exponent without a sign or a decimal point, such "
                            "as 1e-3, as a string: write 1.0e-3)")
                except ValueError:
                    pass
            raise ConfigurationRefused(
                f"--config {path}: inputs.{key} is {value!r}, not an integer or a finite real"
                + hint)
    return base.strip(), dict(overrides), text


def _leaf(inputs: Any, dotted: str) -> tuple[dict[str, Any], str] | None:
    """(the mapping holding the leaf, its key) of ``inputs.<dotted>``, None when absent."""
    cur = inputs
    segs = dotted.split(".")
    for seg in segs[:-1]:
        if not isinstance(cur, dict) or not isinstance(cur.get(seg), dict):
            return None
        cur = cur[seg]
    if not isinstance(cur, dict) or segs[-1] not in cur:
        return None
    return cur, segs[-1]


def apply_overrides(case: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``case`` with each override written into its `inputs`. Refuses a path that is
    no numeric leaf of the base's inputs and a type change."""
    out = copy.deepcopy(case)
    inputs = out.get("inputs")
    for path, value in sorted(overrides.items()):
        found = _leaf(inputs, path)
        if found is None:
            raise ConfigurationRefused(
                f"inputs.{path} is not a key of case {case.get('case_id')!r}'s inputs")
        holder, key = found
        old = holder[key]
        if not (_is_int(old) or _is_real(old)):
            raise ConfigurationRefused(
                f"inputs.{path} is {type(old).__name__} in case {case.get('case_id')!r}, not a "
                "number: only a numeric leaf is overridden")
        if _is_int(old) != _is_int(value):
            raise ConfigurationRefused(
                f"inputs.{path} is {'an integer' if _is_int(old) else 'a real'} in case "
                f"{case.get('case_id')!r} and the configuration gives {value!r}: a type change "
                f"is refused (write {'an integer' if _is_int(old) else f'{float(value)!r}'})")
        holder[key] = value
    return out


def _with_case(ir: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """``ir`` with the entry of ``case``'s id replaced by ``case``."""
    out = copy.deepcopy(ir)
    tcs = out["case"]["test_case_set"]
    for i, entry in enumerate(tcs):
        if isinstance(entry, dict) and str(entry.get("case_id", "")).strip() == case["case_id"]:
            tcs[i] = case
    return out


def evaluate_configuration(ir: dict[str, Any], base: str, overrides: dict[str, Any]
                           ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the IR with the base case overridden, the base's evaluated case-file payload). Refuses
    a base that is no case or is a continuation, the evaluator's refusals, and an override
    that changes nothing the binary reads (leave-one-out over the payload)."""
    from tools.primary_evidence import (
        PrimaryEvidenceError,
        _cases_by_id,
        evaluate_case_inputs,
        is_continuation,
    )
    cases = _cases_by_id(ir)
    if base not in cases:
        raise ConfigurationRefused(
            f"base_case {base!r} is not a case of the certified IR (cases: {sorted(cases)})")
    if is_continuation(cases[base]):
        raise ConfigurationRefused(
            f"base_case {base!r} is a continuation case; name its base case and pass "
            "--restart-from")
    original = {**cases[base], "case_id": base}

    def payload(chosen: dict[str, Any]) -> dict[str, Any]:
        ir2 = _with_case(ir, apply_overrides(original, chosen))
        return evaluate_case_inputs(ir2)[base]

    try:
        evaluated = payload(overrides)
    except PrimaryEvidenceError as exc:
        raise ConfigurationRefused(f"the evaluator refuses the configuration: {exc}") from None
    read = read_input_paths(ir, original, evaluated)
    for path in sorted(overrides):
        if path in read:
            continue
        # A reader `read_input_paths` does not know is still caught here: the override counts
        # when the case file moves with it.
        rest = {k: v for k, v in overrides.items() if k != path}
        try:
            without = payload(rest)
        except PrimaryEvidenceError:
            continue   # the configuration is valid only with it: it changes what is read
        if without == evaluated:
            raise ConfigurationRefused(
                f"inputs.{path}: no key of the case file is that input, no initial_state or "
                "derived expression and no coordinate of the case reads it, and the case file "
                "is the same with and without it: the override changes nothing the binary "
                "reads")
    return _with_case(ir, apply_overrides(original, overrides)), evaluated


def read_input_paths(ir: dict[str, Any], case: dict[str, Any], payload: dict[str, Any]
                     ) -> frozenset[str]:
    """The `inputs.*` paths the base case's evaluation reads by name: each `inputs.<path>` key
    of its case file (what the drive binds, `history.every`, a procedure's inputs), each
    `inputs` name of its `initial_state` and `derived` expressions, and each coordinate's
    `count` / `length` given as `inputs.<path>`. An override of one of them is never refused
    as a no-op, whatever the evaluated values (a `min()` whose other branch binds still reads
    it)."""
    from tools.primary_evidence import expr_names, parse_expr, snapshot_schema
    out = {key[len("inputs."):] for key in payload if key.startswith("inputs.")}
    for block in ("initial_state", "derived"):
        entries = case.get(block)
        for text in (entries.values() if isinstance(entries, dict) else ()):
            if isinstance(text, str):
                out.update(r.name for r in expr_names(parse_expr(text)) if r.kind == "inputs")
    coords = (snapshot_schema(ir) or {}).get("coordinates")
    for spec in (coords if isinstance(coords, list) else ()):
        for key in ("count", "length"):
            raw = spec.get(key) if isinstance(spec, dict) else None
            if isinstance(raw, str) and raw.startswith("inputs."):
                out.add(raw[len("inputs."):])
    return frozenset(out)


# ----------------------------------------------------------------------------- selection

class Selection:
    """The certified output a configuration runs on."""

    def __init__(self, node_key: str, profile: Any, ir_ref: str, pipeline_ref: str,
                 binary_id: str, run_id: str, binary: Path) -> None:
        self.node_key = node_key
        self.profile = profile
        self.ir_ref = ir_ref
        self.pipeline_ref = pipeline_ref
        self.binary_id = binary_id
        self.run_id = run_id
        self.binary = binary


def select_certified(repo_root: Path, spec_ref: str, target_id: str) -> Selection:
    """The node's certified `Validate` output on ``target_id`` (the workflow's resolver), its
    binary present and built at the local site; refuses a launcher target."""
    from tools.host_execution import LOCAL_SITE, declares_launcher
    from tools.orchestration_runtime import DerivationResolver
    from tools.target_profile import TargetProfileError, load_target_profile
    from tools.workflow_conductor import resolve_node

    try:
        node_key, _spec_path = resolve_node(repo_root, spec_ref)
    except (ValueError, KeyError, OSError) as exc:
        raise ConfigurationRefused(f"spec_ref {spec_ref!r}: {exc}") from None
    try:
        profile = load_target_profile(repo_root, target_id)
    except TargetProfileError as exc:
        raise ConfigurationRefused(f"--target {target_id!r}: {exc}") from None
    if declares_launcher(profile.parallel_backend):
        raise ConfigurationRefused(
            f"target {target_id}: parallel backend {profile.parallel_backend} starts its binary "
            "under a launcher, which this version does not run (local site, one process)")
    sel = DerivationResolver(repo_root, target=profile).select(node_key, "validate")
    if not sel.ok:
        raise ConfigurationRefused(
            f"{node_key} has no certified Validate output on target {target_id}: {sel.reason}")
    spec_id = node_key.split("/", 1)[1].split("@", 1)[0]
    binary_dir = repo_root / str(sel.pipeline_ref) / "binary" / str(sel.binary_id)
    binary = binary_dir / "bin" / f"{spec_id}_runner"
    if not binary.is_file():
        raise ConfigurationRefused(f"the certified binary {binary} is absent")
    try:
        meta = json.loads((binary_dir / "binary_meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationRefused(f"{binary_dir}/binary_meta.json is unreadable: {exc}") \
            from None
    env = meta.get("environment") if isinstance(meta, dict) else None
    built = env.get("build_site") if isinstance(env, dict) else None
    where = built.get("site") if isinstance(built, dict) else None
    if where != LOCAL_SITE:
        raise ConfigurationRefused(
            f"binary {sel.binary_id} was built at site {where!r}, not {LOCAL_SITE!r} "
            "(binary_meta.json#environment.build_site): a binary runs at the site that built it, "
            "and this version runs at the local site only")
    return Selection(node_key, profile, str(sel.ir_ref), str(sel.pipeline_ref),
                     str(sel.binary_id), str(sel.run_id), binary)


# ----------------------------------------------------------------------------- restart

def _source_run_dir(capture: Path, base: str) -> tuple[Path, str]:
    """(the tool output directory a capture sits in, its kind: `history` or `final`); refuses
    a path that is not a capture of ``base`` laid out by the binary."""
    if capture.parent.name == "history" and capture.name.startswith(f"{base}__"):
        snaps, kind = capture.parent.parent, "history"
    elif capture.name == f"{base}.json":
        snaps, kind = capture.parent, "final"
    else:
        raise ConfigurationRefused(
            f"--restart-from {capture}: not a capture of {base!r} (expected "
            f"<run>/raw/state_snapshots/history/{base}__<K>.json or "
            f"<run>/raw/state_snapshots/{base}.json)")
    if snaps.name != "state_snapshots" or snaps.parent.name != "raw":
        raise ConfigurationRefused(
            f"--restart-from {capture}: not under <run>/raw/state_snapshots/")
    return snaps.parent.parent, kind


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _without_restart(payload: Any) -> Any:
    from tools.runner_ir import RESTART_KEY_PREFIX
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if not k.startswith(RESTART_KEY_PREFIX)}


def _check_source_record(prev: Path, sel: Selection, binary_sha: str, base: str,
                         overrides: dict[str, Any], payload: dict[str, Any]) -> None:
    """The restart source is an earlier run of this tool on the same node, target, IR, binary
    and configuration, and its case file, its `restart.*` keys aside, is this run's evaluated
    case file (a continuation admits no other `dt`, and this version no other input)."""
    try:
        rec = json.loads((prev / RUN_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationRefused(
            f"--restart-from: {prev / RUN_RECORD} is unreadable ({exc}); a restart source is a "
            "capture an earlier run of this tool wrote") from None
    cfg = rec.get("configuration") if isinstance(rec, dict) else None
    pgid = rec.get("binary_pgid") if isinstance(rec, dict) else None
    if isinstance(rec, dict) and rec.get("status") == "running" and isinstance(pgid, int) \
            and _group_alive(pgid):
        raise ConfigurationRefused(
            f"--restart-from: the run at {prev} is still running (its record says `running` and "
            f"process group {pgid} is alive): kill it first (kill -KILL -{pgid})")
    try:
        their_case = json.loads((prev / "case_inputs" / f"{base}.json")
                                .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        their_case = None
    mine = {"node_key": sel.node_key, "target_id": sel.profile.target_id,
            "ir_ref": sel.ir_ref, "binary_sha256": binary_sha, "base_case": base,
            "inputs": overrides, "case_file": _without_restart(payload)}
    theirs = {"node_key": rec.get("node_key"), "target_id": rec.get("target_id"),
              "ir_ref": rec.get("ir_ref"), "binary_sha256": rec.get("binary_sha256"),
              "base_case": cfg.get("base_case") if isinstance(cfg, dict) else None,
              "inputs": cfg.get("inputs") if isinstance(cfg, dict) else None,
              "case_file": _without_restart(their_case)}
    differ = sorted(k for k in mine if mine[k] != theirs[k])
    if differ:
        raise ConfigurationRefused(
            f"--restart-from: the run at {prev} differs from this one in {differ}: a "
            "continuation runs the same node, target, IR, binary and configuration")


def _host_scalar(payload: dict[str, Any], binding: Any) -> float | None:
    from tools.primary_evidence import _DERIVED
    if binding.kind == "inputs":
        key = f"inputs.{binding.ref}"
    elif binding.kind == _DERIVED:
        key = f"{_DERIVED}.{binding.ref}"
    else:
        return None
    value = payload.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def continuation_payload(ir2: dict[str, Any], base: str, payload: dict[str, Any],
                         capture: Path, stage: Path) -> tuple[dict[str, Any], int]:
    """(the continuation's case-file payload, K). The capture is staged as the base's history
    sample at K under ``stage`` (a scratch directory) and handed to the continuation writer
    through a continuation case of ``ir2``, so its refusals are that writer's."""
    from tools.primary_evidence import (
        CASE_INPUTS_DIRNAME,
        RESTART_DIRNAME,
        STEP_CAPTURE,
        PrimaryEvidenceError,
        _cases_by_id,
        drive_model,
        is_continuation,
        snapshot_schema,
        write_continuation_inputs,
    )
    from tools.spec_input_gates import CASE_ID_TOKEN_RE
    model = drive_model(ir2)
    if model.block.loop is None:
        raise ConfigurationRefused("--restart-from: the node's drive declares no loop; there is "
                                   "no step to continue from")
    try:
        doc = json.loads(capture.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationRefused(f"--restart-from {capture}: unreadable: {exc}") from None
    step = doc.get(STEP_CAPTURE) if isinstance(doc, dict) else None
    if not (isinstance(step, (int, float)) and not isinstance(step, bool)
            and float(step).is_integer() and step >= 1):
        raise ConfigurationRefused(
            f"--restart-from {capture}: carries step {step!r}, not an integer >= 1")
    k = int(step)
    loop = model.block.loop
    n_step = _host_scalar(payload, loop["n_step"]) if "n_step" in loop else None
    if n_step is not None and not k < n_step:
        raise ConfigurationRefused(
            f"--restart-from {capture}: step {k} is not below n_step {int(n_step)}: a finished "
            "run is not extended")
    t_start = _host_scalar(payload, loop["t_start"]) if "t_start" in loop else None
    dt = _host_scalar(payload, loop["dt"]) if "dt" in loop else None
    tv = (snapshot_schema(ir2) or {}).get("time_variable")
    t_cap = doc.get(tv.strip()) if isinstance(tv, str) and tv.strip() else None
    if t_start is not None and dt is not None and isinstance(t_cap, (int, float)) \
            and not isinstance(t_cap, bool):
        want = t_start + k * dt
        bound = 4 * float(np.spacing(abs(float(t_cap))))
        if not abs(want - float(t_cap)) <= bound:
            raise ConfigurationRefused(
                f"--restart-from {capture}: t_start + {k} * dt = {want!r} is not the capture's "
                f"{tv.strip()} {t_cap!r} within 4 ulp: the capture is not of this configuration")
    cases = _cases_by_id(ir2)
    cid = next(f"c{i}" for i in range(len(cases) + 1) if f"c{i}" not in cases)
    assert CASE_ID_TOKEN_RE.match(cid)
    # The IR's own continuation cases are dropped: the writer would ask each of them for a
    # restart point of its own, and only this run's is staged.
    ir3 = copy.deepcopy(ir2)
    ir3["case"]["test_case_set"] = [
        c for c in ir3["case"]["test_case_set"] if not is_continuation(c)]
    ir3["case"]["test_case_set"].append({"case_id": cid, "restart": {"from": base, "step": k}})
    sample = stage / "raw" / "state_snapshots" / "history" / f"{base}__{k}.json"
    sample.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(capture, sample)
    try:
        files, refused = write_continuation_inputs(ir3, stage)
    except PrimaryEvidenceError as exc:
        raise ConfigurationRefused(f"--restart-from {capture}: {exc}") from None
    if refused or [f.case_id for f in files] != [cid]:
        raise ConfigurationRefused(
            f"--restart-from {capture}: "
            f"{refused.get(cid) or f'the continuation writer wrote {[f.case_id for f in files]}'}")
    written = stage / RESTART_DIRNAME / CASE_INPUTS_DIRNAME / f"{cid}.json"
    return json.loads(written.read_text(encoding="utf-8")), k


# ----------------------------------------------------------------------------- the run

def _check_out(repo_root: Path, out: Path) -> Path:
    out = out.resolve()
    workspace = (repo_root / "workspace").resolve()
    if out == workspace or workspace in out.parents:
        raise ConfigurationRefused(
            f"--out {out} is under {workspace}: the workflow's tree holds certified runs only")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ConfigurationRefused(f"--out {out} exists and is not an empty directory")
    return out


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float | None,
         started: Any = None) -> tuple[int | None, bool]:
    """(the exit status, None when killed; whether the timeout fired). The child runs in a
    session of its own, and its process group is killed on the timeout and on any exception
    while it runs (`Terminated`, `KeyboardInterrupt`). The signals of `_HELD` are blocked from
    before the fork until the process is bound, so a signal sent then is delivered inside the
    `try` that kills it. ``started(pid)`` is called with the binary's pid, which is its
    process group's id, before the wait. Raises `OSError` when the binary cannot be started."""
    with (cwd / "stdout.txt").open("wb") as out, (cwd / "stderr.txt").open("wb") as err:
        signal.pthread_sigmask(signal.SIG_BLOCK, _HELD)
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err,
                                    start_new_session=True,
                                    preexec_fn=_unhold)  # noqa: PLW1509 (one thread)
        except BaseException:
            _unhold()
            raise
        try:
            _unhold()   # a signal held since the fork is delivered here, inside the try
            if started is not None:
                started(proc.pid)
            return proc.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            _kill(proc)
            return None, True
        except BaseException:
            _kill(proc)
            raise


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _launch(repo_root: Path, profile: Any) -> Any:
    """The launch shape of the target at the site `sites.yaml` maps it to, as a run's; refused
    when that site is not the local one or cannot run the target."""
    from tools.execution_sites import SitesConfigError, load_sites
    from tools.host_execution import LaunchUnavailable, launch_shape
    from tools.target_profile import TargetProfileError
    try:
        site = load_sites(repo_root).site_for(profile.target_id)
    except (SitesConfigError, TargetProfileError) as exc:
        raise ConfigurationRefused(f"sites.yaml: {exc}") from None
    if not site.is_local:
        raise ConfigurationRefused(
            f"target {profile.target_id} runs at site {site.site_id} (sites.yaml): this version "
            "runs at the local site only")
    try:
        return launch_shape(profile, site)
    except LaunchUnavailable as exc:
        raise ConfigurationRefused(f"target {profile.target_id}: {exc}") from None


def result_states(ir: dict[str, Any], base: str) -> list[str]:
    """The states the result dummies of the entries that run ``base`` are bound to (`init` and
    the `step` entries of its case, `DriveBlock.entries_for`): `result: ok` with
    `arguments.ok: state.status` names `status`."""
    from tools.primary_evidence import drive_model
    names: list[str] = []
    for entry in drive_model(ir).block.entries_for(base):
        binding = entry.arguments.get(entry.result)
        if binding is not None and binding.kind == "state" and binding.ref not in names:
            names.append(binding.ref)
    return names


def _kernel_outcome(ir: dict[str, Any], out: Path, base: str) -> tuple[Any, str | None]:
    """(the final capture's step, None when the run reached its end, else why not): a final
    capture must exist, and every state an entry's result dummy is bound to in this case
    (`result_states`) must be in it and true. A kernel that refused the configuration exits 0
    and leaves its result false, with fewer steps than planned. A logical `out` that is not an
    entry's result (a convergence flag) is not read."""
    path = out / "raw" / "state_snapshots" / f"{base}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"no readable final capture ({exc})"
    if not isinstance(doc, dict):
        return None, "the final capture is not a JSON object"
    names = result_states(ir, base)
    step = doc.get("step")
    missing = [n for n in names if n not in doc]
    if missing:
        return step, f"the final capture holds no {', '.join(missing)}"
    false = [n for n in names if doc[n] != 1]
    if false:
        return step, (f"the kernel refused the configuration: {', '.join(false)} false in the "
                      f"final capture, at step {step!r}")
    return step, None


def run_configuration(repo_root: Path, spec_ref: str, target_id: str, config: Path, out: Path,
                      restart_from: Path | None = None, timeout_sec: float | None = None
                      ) -> int:
    """The tool (module docstring). Raises `ConfigurationRefused` before anything runs."""
    from tools.primary_evidence import (
        CASE_INPUTS_DIRNAME,
        ContinuationFile,
        _write_case_files,
        continuation_start_violation,
    )
    repo_root = Path(repo_root).resolve()
    out = _check_out(repo_root, Path(out))
    base, overrides, config_text = read_configuration(Path(config))
    sel = select_certified(repo_root, spec_ref, target_id)
    ir_path = repo_root / sel.ir_ref / "spec.ir.yaml"
    try:
        ir = yaml.safe_load(ir_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationRefused(f"the certified IR {ir_path} is unreadable: {exc}") from None
    if not isinstance(ir, dict) or not isinstance(ir.get("drive"), dict):
        raise ConfigurationRefused(
            f"{sel.node_key}: the certified IR declares no drive; a configuration overrides the "
            "inputs of a driven node's case")
    ir2, payload = evaluate_configuration(ir, base, overrides)
    binary_sha = _sha256(sel.binary)
    launch = _launch(repo_root, sel.profile)

    restart: dict[str, Any] | None = None
    cont: ContinuationFile | None = None
    if restart_from is not None:
        capture = Path(restart_from).resolve()
        prev, kind = _source_run_dir(capture, base)
        _check_source_record(prev, sel, binary_sha, base, overrides, payload)
        with tempfile.TemporaryDirectory(prefix="run_configuration_") as stage:
            payload, k = continuation_payload(ir2, base, payload, capture, Path(stage))
        restart = {"source": str(capture), "source_kind": kind, "sha256": _sha256(capture),
                   "step": k, "source_run": str(prev)}
        # the start check compares the run's initial capture with the capture itself
        cont = ContinuationFile(base, base, k, capture,
                                out / CASE_INPUTS_DIRNAME / f"{base}.json")
    # nothing is written under <out> before every refusal has had its turn
    out.mkdir(parents=True, exist_ok=True)
    _write_case_files({base: payload}, out / CASE_INPUTS_DIRNAME)
    shutil.copyfile(ir_path, out / "spec.ir.yaml")
    for sub in ("initial", "history", "series"):
        (out / "raw" / "state_snapshots" / sub).mkdir(parents=True, exist_ok=True)

    # The launcher is refused above, so the launch prefix holds at most a device trace's
    # profiling half, which a person's run does not take.
    cmd = [str(sel.binary), "--cases", str(out / "spec.ir.yaml"), base]
    # The closure's external libraries (issue #644): their directories ahead of the inherited
    # loader path, as the certifying run had them; recorded as the directories alone.
    from tools import external_library as xl
    try:
        libraries = xl.node_libraries(repo_root, spec_ref, sel.ir_ref, sel.node_key, sel.profile)
    except (xl.LibraryUnusable, xl.LibraryDeclarationError) as exc:
        raise ConfigurationRefused(f"{sel.node_key}: {exc}") from None
    env = {**os.environ, **launch.env, **xl.loader_env(sel.profile, libraries, os.environ)}
    record: dict[str, Any] = {
        "statement": NOT_A_CERTIFICATION,
        "node_key": sel.node_key, "target_id": sel.profile.target_id,
        "target_sha256": sel.profile.sha256, "ir_ref": sel.ir_ref,
        "pipeline_ref": sel.pipeline_ref, "binary_id": sel.binary_id,
        "validate_run_id": sel.run_id, "binary": str(sel.binary), "binary_sha256": binary_sha,
        "configuration": {"base_case": base, "inputs": overrides, "file": str(Path(config).resolve()),
                          "text": config_text},
        "restart": restart, "command": cmd, "launch_env": dict(sorted(launch.env.items())),
        "library_dirs": list(xl.library_dirs(libraries)),
        "cwd": str(out), "timeout_sec": timeout_sec, "started_at": _now(),
        "status": "running", "exit_status": None, "timed_out": False, "start_check": None,
        "final_step": None, "kernel": None, "binary_pgid": None,
    }
    previous = {s: signal.signal(s, _raise_terminated) for s in _TERMINATING}
    try:
        _write_json(out / RUN_RECORD, record)

        def started(pid: int) -> None:
            record["binary_pgid"] = pid
            _write_json(out / RUN_RECORD, record)

        try:
            rc, timed_out = _run(cmd, out, env, timeout_sec, started)
        except OSError as exc:
            record.update(status="not_started", finished_at=_now(), error=str(exc))
            _write_json(out / RUN_RECORD, record)
            return EXIT_REFUSED
        record.update(exit_status=rc, timed_out=timed_out, finished_at=_now())
        failed = rc != 0
        if not failed:
            record["final_step"], why = _kernel_outcome(ir, out, base)
            record["kernel"] = why or "reached its end"
            failed = why is not None
        if not failed and cont is not None:
            why = continuation_start_violation(ir2, out, cont)
            record["start_check"] = why or "held"
            failed = why is not None
        record["status"] = "failed" if failed else "ok"
        _write_json(out / RUN_RECORD, record)
        return EXIT_RAN_FAILED if failed else 0
    except (Terminated, KeyboardInterrupt) as exc:
        # Whatever was recorded before the signal is kept; the status says the tool was
        # stopped before it finished judging the run.
        record.update(status="interrupted", finished_at=_now(),
                      signal=exc.signum if isinstance(exc, Terminated) else int(signal.SIGINT))
        _write_json(out / RUN_RECORD, record)
        raise
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_configuration.py",
        epilog="The refusals, the record and the exit status are stated in the module "
               "docstring (python3 -c 'import tools.run_configuration as m; print(m.__doc__)').",
        description="Run a person-written configuration (a certified case plus overrides of "
                    "its numeric inputs) on a node's certified binary. Operator-only; the run "
                    "is not a certification. See the module docstring and docs/RUNBOOK.md.")
    ap.add_argument("spec_ref", help="the node's spec directory, as run_workflow.py takes it")
    ap.add_argument("--target", required=True, help="the target profile id (spec/targets/)")
    ap.add_argument("--config", required=True, type=Path,
                    help="YAML: base_case: <case_id>, inputs: {<dotted path>: <number>}")
    ap.add_argument("--out", required=True, type=Path,
                    help="a new or empty directory outside workspace/")
    ap.add_argument("--restart-from", type=Path, default=None,
                    help="a history sample (or final capture) an earlier run of this tool wrote "
                         "for the same configuration: continue from it")
    ap.add_argument("--timeout-sec", type=float, default=None,
                    help="kill the run after this many seconds (default: no bound)")
    ap.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.timeout_sec is not None and not (math.isfinite(a.timeout_sec) and a.timeout_sec > 0):
        ap.error("--timeout-sec must be a positive number")
    try:
        rc = run_configuration(a.repo_root, a.spec_ref, a.target, a.config, a.out,
                               a.restart_from, a.timeout_sec)
    except ConfigurationRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Terminated as exc:
        print(f"terminated by signal {exc.signum}: the run's process group was killed",
              file=sys.stderr)
        return 128 + exc.signum
    except KeyboardInterrupt:
        print("interrupted: the run's process group was killed", file=sys.stderr)
        return 128 + int(signal.SIGINT)
    record = json.loads((a.out.resolve() / RUN_RECORD).read_text(encoding="utf-8"))
    print(f"{record['status']}: exit {record['exit_status']}"
          + (" (timed out)" if record["timed_out"] else "")
          + (f"; {record['error']}" if record.get("error") else "")
          + (f"; kernel: {record['kernel']}" if record.get("kernel") else "")
          + (f"; start check: {record['start_check']}" if record["start_check"] else "")
          + f"; record {a.out.resolve() / RUN_RECORD}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
