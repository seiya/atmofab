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
   and a target whose parallel backend starts its binary under a launcher is refused: this
   version runs at the local site, one process.
2. Applies the overrides to the base case and evaluates it with the host's evaluator
   (`primary_evidence.evaluate_case_inputs`), so every refusal a certified case meets (`n_step`,
   `dt`, the 4-ulp `t_end` rule, `history.every`, the extents) applies here too. Refused: a
   `base_case` that is no case of the IR or is a continuation; an override of a path that is
   not a numeric leaf of the base's `inputs`; a type change (an integer leaf given a real, a
   real given an integer, anything given a boolean); an override that changes nothing the
   binary reads (the case file with every other override applied is the same with and without
   it — a descriptive string's neighbour, a leaf no expression reads, the base's own value).
3. Writes `<out>/case_inputs/<base_case>.json` — the BASE's id, because a driver rendered in
   the list form stops on an id it does not know — and a copy of the certified IR as
   `<out>/spec.ir.yaml`, then runs the certified binary with `--cases <out>/spec.ir.yaml
   <base_case>` in `<out>`, under the target's launch environment over this process's own. It
   starts the process itself (no 3600 s bound of the build-runtime library); `--timeout-sec`
   is the only bound, and on it or on an interrupt the process group is killed. Nothing under
   the certified IR's `case_inputs/` is written.
4. `--restart-from <capture.json>` continues an earlier run of this tool: the capture (a
   history sample `<prev>/raw/state_snapshots/history/<base_case>__<K>.json`, or a final
   capture `<prev>/raw/state_snapshots/<base_case>.json`) must sit in an output directory of
   this tool whose `run_record.json` names the same node, target, binary and configuration.
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
   sha256 and step, the command and environment, the exit status, the start check, and the
   statement that the run certifies nothing. stdout and stderr go to `<out>/stdout.txt` and
   `<out>/stderr.txt`.

Exit status: 0 when the binary exited 0 (and, on a continuation, the start check held); 1 when
it did not, timed out, or the start check failed; 2 when the configuration or the selection
was refused before anything ran.

What it does not do (issue #632 §3): stop a run at a chosen step; extend a finished run (a
final capture is refused by `K < n_step` under the same configuration); continue with another
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
    overrides = doc.get("inputs") or {}
    if not isinstance(overrides, dict):
        raise ConfigurationRefused(f"--config {path}: inputs must be a mapping of dotted path "
                                   "-> number")
    for key, value in overrides.items():
        if not isinstance(key, str) or not key or any(not seg for seg in key.split(".")):
            raise ConfigurationRefused(f"--config {path}: inputs key {key!r} is not a dotted "
                                       "path")
        if not (_is_int(value) or (_is_real(value) and math.isfinite(value))):
            raise ConfigurationRefused(
                f"--config {path}: inputs.{key} is {value!r}, not an integer or a finite real")
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
    for path in sorted(overrides):
        rest = {k: v for k, v in overrides.items() if k != path}
        try:
            without = payload(rest)
        except PrimaryEvidenceError:
            continue   # the configuration is valid only with it: it changes what is read
        if without == evaluated:
            raise ConfigurationRefused(
                f"inputs.{path}: the override changes nothing the binary reads (the case file "
                "is the same with and without it): no expression of the case reads that leaf, "
                "or it is the value the case already has")
    return _with_case(ir, apply_overrides(original, overrides)), evaluated


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


def _check_source_record(prev: Path, sel: Selection, binary_sha: str, base: str,
                         overrides: dict[str, Any]) -> None:
    """The restart source is an earlier run of this tool on the same node, target, binary and
    configuration (a continuation admits no other `dt`, and this version no other input)."""
    try:
        rec = json.loads((prev / RUN_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationRefused(
            f"--restart-from: {prev / RUN_RECORD} is unreadable ({exc}); a restart source is a "
            "capture an earlier run of this tool wrote") from None
    cfg = rec.get("configuration") if isinstance(rec, dict) else None
    mine = {"node_key": sel.node_key, "target_id": sel.profile.target_id,
            "binary_sha256": binary_sha, "base_case": base, "inputs": overrides}
    theirs = {"node_key": rec.get("node_key"), "target_id": rec.get("target_id"),
              "binary_sha256": rec.get("binary_sha256"),
              "base_case": cfg.get("base_case") if isinstance(cfg, dict) else None,
              "inputs": cfg.get("inputs") if isinstance(cfg, dict) else None}
    differ = sorted(k for k in mine if mine[k] != theirs[k])
    if differ:
        raise ConfigurationRefused(
            f"--restart-from: the run at {prev} differs from this one in {differ}: a "
            "continuation runs the same node, target, binary and configuration")


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
        _cases_by_id,
        drive_model,
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
    ir3 = copy.deepcopy(ir2)
    ir3["case"]["test_case_set"].append({"case_id": cid, "restart": {"from": base, "step": k}})
    sample = stage / "raw" / "state_snapshots" / "history" / f"{base}__{k}.json"
    sample.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(capture, sample)
    files, refused = write_continuation_inputs(ir3, stage)
    if refused or len(files) != 1:
        raise ConfigurationRefused(
            f"--restart-from {capture}: {refused.get(cid, 'no continuation file was written')}")
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


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float | None
         ) -> tuple[int | None, bool]:
    """(the exit status, None when killed; whether the timeout fired). The child runs in a
    session of its own, and its process group is killed on the timeout or an interrupt."""
    with (cwd / "stdout.txt").open("wb") as out, (cwd / "stderr.txt").open("wb") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=err,
                                start_new_session=True)
        try:
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


def run_configuration(repo_root: Path, spec_ref: str, target_id: str, config: Path, out: Path,
                      restart_from: Path | None = None, timeout_sec: float | None = None
                      ) -> int:
    """The tool (module docstring). Raises `ConfigurationRefused` before anything runs."""
    from tools.host_execution import launch_shape
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
    launch = launch_shape(sel.profile)

    restart: dict[str, Any] | None = None
    cont: ContinuationFile | None = None
    if restart_from is not None:
        capture = Path(restart_from).resolve()
        prev, kind = _source_run_dir(capture, base)
        _check_source_record(prev, sel, binary_sha, base, overrides)
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
    env = {**os.environ, **launch.env}
    record: dict[str, Any] = {
        "statement": NOT_A_CERTIFICATION,
        "node_key": sel.node_key, "target_id": sel.profile.target_id,
        "target_sha256": sel.profile.sha256, "ir_ref": sel.ir_ref,
        "pipeline_ref": sel.pipeline_ref, "binary_id": sel.binary_id,
        "validate_run_id": sel.run_id, "binary": str(sel.binary), "binary_sha256": binary_sha,
        "configuration": {"base_case": base, "inputs": overrides, "file": str(Path(config)),
                          "text": config_text},
        "restart": restart, "command": cmd, "launch_env": dict(sorted(launch.env.items())),
        "cwd": str(out), "timeout_sec": timeout_sec, "started_at": _now(),
        "status": "running", "exit_status": None, "timed_out": False, "start_check": None,
    }
    _write_json(out / RUN_RECORD, record)
    try:
        rc, timed_out = _run(cmd, out, env, timeout_sec)
    except BaseException:
        record.update(status="interrupted", finished_at=_now())
        _write_json(out / RUN_RECORD, record)
        raise
    record.update(exit_status=rc, timed_out=timed_out, finished_at=_now())
    failed = rc != 0
    if not failed and cont is not None:
        why = continuation_start_violation(ir2, out, cont)
        record["start_check"] = why or "held"
        failed = why is not None
    record["status"] = "failed" if failed else "ok"
    _write_json(out / RUN_RECORD, record)
    return EXIT_RAN_FAILED if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_configuration.py",
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
    record = json.loads((a.out.resolve() / RUN_RECORD).read_text(encoding="utf-8"))
    print(f"{record['status']}: exit {record['exit_status']}"
          + (" (timed out)" if record["timed_out"] else "")
          + (f"; start check: {record['start_check']}" if record["start_check"] else "")
          + f"; record {a.out.resolve() / RUN_RECORD}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
