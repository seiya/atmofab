"""Derivation keys, output hashes and eligible-output selection (Z5, issue #250).

`docs/design/zero_base_architecture.md` §A1 keeps three identities apart for every phase
output: the **derivation key** (a content hash over the phase's contract inputs and the
version of the transformation — "this work under these contracts"), the **attempt id** (the
`agent_run_id` of each execution, recorded whatever the outcome), and the **output hash** (a
content hash over the produced deliverables). This module is the PURE half of that model:
it hashes, it compares, and it selects; it reads no file and knows no directory layout. The
runtime (`tools/orchestration_runtime.py`) resolves the inputs of a concrete node from disk
and stamps what this module computes into the phase's certifying stage meta; the conductor
records the key on every launch (`agent_runs.jsonl`) and on every terminal step_result.

What the key is over, and what it is deliberately NOT over (the plan on issue #250 records
each decision):

* ONE phase is ONE derivation (`compile` / `generate` / `build` / `validate`); the finer
  A1 stages (assemble, execute, verdict) are folded into the phase that runs them.
* The `inputs` mapping holds HASHES and identifiers only, never a document body, so it can
  be stamped beside the key and diffed against a later recomputation ("why is this stale":
  `first_differing_input`).
* The model that produced an output is NOT in the key: certification is decided by the run,
  not by the author, and keying on the model would re-certify the whole corpus on a
  provider change (`AGENTS.md` §Development premises, no vendor lock-in). It stays a
  per-attempt record. Advisory inputs — the exemplar above all — are excluded on A1's own
  rule. The execution environment is recorded (`trial_meta.json#environment`) and not
  keyed, because no verdict predicate depends on it yet.
* The static documents the host inlines into a pure prompt are a property of the
  TRANSFORMATION, not of the node: they enter through the version tuple, which the drift
  tests pin (`tools/tests/test_pure_prompt_contract_drift.py`,
  `tools/tests/test_derivation_transformation_drift.py`), so a maintainer decides once —
  bump or re-pin — instead of a typo fix invalidating every certified node.

`DERIVATION_KEY_VERSION` is the version of the KEY'S OWN construction (which inputs a phase
hashes, in which shape). Changing that rule changes every key at once, which is the intended
cost of changing it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

#: The version of the key construction rule itself (`derivation_key`'s payload shape and the
#: per-phase input sets the runtime resolves). Bump when the RULE changes; every key changes.
DERIVATION_KEY_VERSION = 1

#: The version of the eligible-output selection policy (`select_eligible`). v1 = the latest
#: attempt among the eligible outputs of one key. A performance-aware policy is the `Tune`
#: flow's exit and takes the next number; the outputs it chooses among are the same set.
SELECTION_POLICY_VERSION = 1

#: The version of the compile-inlined documents (`phase_01_compile.md`, the two IR examples,
#: the `impl_defaults` schema). Pinned separately from `PURE_PROMPT_CONTRACT_VERSION` because a
#: bump of that one has two side effects this one must not have (it stops `_resolve_exemplar_source`
#: offering earlier-version exemplars and refuses `--resume` across it — the reason the prompt
#: drift test refuses to pin these four); a bump HERE costs exactly one re-derivation of every
#: node's Compile, and nothing downstream whose IR comes out byte-identical.
COMPILE_INLINED_DOCUMENTS_VERSION = "compile-docs-1"

#: The versions of the DETERMINISTIC transformations. Each is the identity of the implementing
#: code, pinned by `tools/tests/test_derivation_transformation_drift.py` as a digest of the
#: functions / files that implement it; an edit there fails that test until a maintainer
#: either re-pins (behaviour-preserving) or bumps the constant (every key of that phase moves).
RENDER_VERSION = "render-1"      # host-rendered runner + build control file (Generate)
BUILD_VERSION = "build-1"        # build-runtime server `compile_project` + the in-process build
EXECUTE_VERSION = "execute-1"    # `run_program` / `run_quality_checks` + the in-process execute
VERDICT_VERSION = "verdict-1"    # `tools/verdict_evaluator.py` + the derived-artifact author

#: The phases that ARE derivations, in pipeline order.
DERIVATION_STEPS: tuple[str, ...] = ("compile", "generate", "build", "validate")


def transformation_versions() -> dict[str, tuple[str, ...]]:
    """The transformation version tuple of each phase.

    Resolved lazily (the two imports are of modules that must not be imported by this one at
    load time for the runtime's own import order) and returned fresh, so a test that patches a
    constant sees its patch. The tuple is what `derivation_key` hashes under `"transformation"`.
    """
    from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
    from tools.raw_evidence_excerpt import RAW_EXCERPT_POLICY_VERSION
    return {
        "compile": ("pure", PURE_PROMPT_CONTRACT_VERSION, COMPILE_INLINED_DOCUMENTS_VERSION),
        "generate": ("pure", PURE_PROMPT_CONTRACT_VERSION, RENDER_VERSION),
        "build": (BUILD_VERSION,),
        "validate": (EXECUTE_VERSION, VERDICT_VERSION, "pure", PURE_PROMPT_CONTRACT_VERSION,
                     f"raw-excerpt-{RAW_EXCERPT_POLICY_VERSION}"),
    }


def canonical_json_bytes(obj: Any) -> bytes:
    """ONE canonical serialisation for every hash this module and its callers take: sorted
    keys, no whitespace, UTF-8 with non-ASCII kept as-is. The runtime used to spell this three
    ways; a hash over a differently-spelled serialisation of the same object is a different hash.

    Raises `TypeError` for a value `json` cannot serialise (a `Path`, a `set`): a caller that
    hashes a non-JSON value would otherwise hash its `repr`, which is not a contract."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """`"sha256:<hex>"` over `data` — the same string form `_compute_sha256` and
    `config_sha256` produce, so a hash is recognisable as one wherever it is stamped."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def output_hash(artifact_hashes: Mapping[str, str], *, stage_dir: str) -> str:
    """The output hash of a certified phase: one digest over its `artifact_hashes` map
    (repo-relative deliverable path -> `sha256:<hex>`), order-independent, with every path
    taken RELATIVE to `stage_dir` — the directory the certifying stage meta lives in.

    Relative, because the output of a phase is CONTENT: a re-derivation that produces
    byte-identical deliverables under a fresh stage id (`source/src_20260102_001/` instead of
    `source/src_20260101_001/`) is the same output, and every downstream key that binds it
    must stay unchanged — the one property A1 asks of the identity ("re-certifying a
    dependency whose source bytes are unchanged invalidates nobody"). A repo-relative key
    would put the attempt's id into the hash and lose it.

    Keyed by the relative PATH as well as by content, because a phase's output is the set of
    named deliverables — two sources whose bytes are swapped between files are not the same
    output. Sorted by `canonical_json_bytes`, so insertion order does not matter.

    Refuses an empty map, a malformed entry, and a path outside `stage_dir` (a deliverable a
    phase declares outside its own stage directory cannot be addressed relative to it — that
    is a contract defect to surface, never a key to guess): an output hash over nothing, or
    over the wrong thing, would let an unstamped or foreign meta satisfy a downstream key."""
    if not isinstance(artifact_hashes, Mapping) or not artifact_hashes:
        raise ValueError("output_hash: artifact_hashes must be a non-empty mapping")
    base = str(stage_dir).strip().strip("/")
    if not base:
        raise ValueError("output_hash: stage_dir must be a non-empty repo-relative directory")
    relative: dict[str, str] = {}
    for path, digest in artifact_hashes.items():
        if not (isinstance(path, str) and path.strip()):
            raise ValueError(f"output_hash: artifact path must be a non-empty string, got {path!r}")
        if not (isinstance(digest, str) and digest.startswith("sha256:") and len(digest) > len("sha256:")):
            raise ValueError(f"output_hash: digest of {path!r} must be 'sha256:<hex>', got {digest!r}")
        norm = path.strip().strip("/")
        if not norm.startswith(base + "/") or norm == base:
            raise ValueError(
                f"output_hash: deliverable {path!r} is not under the stage directory {base!r}")
        relative[norm[len(base) + 1:]] = digest
    return sha256_hex(canonical_json_bytes(relative))


def derivation_key(step: str, inputs: Mapping[str, Any]) -> str:
    """The derivation key of `step` under `inputs`.

    `inputs` is the per-phase contract-input mapping the runtime resolves (hashes and
    identifiers only). The key hashes `{key_version, step, transformation, inputs}` canonically,
    so the same inputs under a different transformation version are a different derivation, and
    a change to the construction rule (`DERIVATION_KEY_VERSION`) moves every key."""
    step_token = str(step).strip().lower()
    versions = transformation_versions()
    if step_token not in versions:
        raise ValueError(
            f"derivation_key: unknown step {step!r}; expected one of {DERIVATION_STEPS}")
    if not isinstance(inputs, Mapping):
        raise TypeError("derivation_key: inputs must be a mapping")
    payload = {
        "key_version": DERIVATION_KEY_VERSION,
        "step": step_token,
        "transformation": list(versions[step_token]),
        "inputs": dict(inputs),
    }
    return sha256_hex(canonical_json_bytes(payload))


def first_differing_input(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    """The dotted path of the FIRST input (in sorted-key order) at which `recorded` and
    `current` differ, or `None` when they are equal. This is the diagnostic half of the key:
    a stale phase is reported as `derivation_key_mismatch:<this path>` so the operator reads
    WHICH input moved instead of two opaque digests.

    Lists are compared element-wise (`closure[2].source`), a length difference is reported at
    the list itself, and a key present on one side only is reported at that key."""
    def walk(a: Any, b: Any, path: str) -> str | None:
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for k in sorted(set(a) | set(b), key=str):
                sub = f"{path}.{k}" if path else str(k)
                if k not in a or k not in b:
                    return sub
                found = walk(a[k], b[k], sub)
                if found is not None:
                    return found
            return None
        if isinstance(a, Sequence) and isinstance(b, Sequence) and not isinstance(a, str) and not isinstance(b, str):
            if len(a) != len(b):
                return path or "<root>"
            for i, (x, y) in enumerate(zip(a, b)):
                found = walk(x, y, f"{path}[{i}]")
                if found is not None:
                    return found
            return None
        return None if a == b else (path or "<root>")
    return walk(recorded, current, "")


class Candidate(NamedTuple):
    """One certified output of a derivation key, as `select_eligible` sees it: the attempt
    that produced it (its `agent_run_id`, or the stage directory's id when no attempt is
    recorded), the ordering token the runtime gives it (a `(date, seq)` tuple from the stage
    id today — the same order `_latest_meta_under` uses), its output hash, and the stage
    directory it lives in (opaque to this module; handed back to the caller)."""
    attempt_id: str
    order: tuple[Any, ...]
    output_hash: str
    ref: str


def select_eligible(candidates: Sequence[Candidate],
                    policy_version: int = SELECTION_POLICY_VERSION) -> Candidate | None:
    """Choose among the ELIGIBLE outputs of one derivation key (the caller has already
    dropped every non-pass, revoked or hash-mismatching output). `None` when there is none.

    Policy v1: the latest attempt — the greatest `order`, with `attempt_id` as the final
    tie-breaker so two candidates with equal `order` still resolve deterministically rather
    than by directory-listing order. No other policy exists yet; an unknown version is refused
    rather than silently answered by v1, because the version is stamped as a record of WHICH
    rule chose."""
    if policy_version != SELECTION_POLICY_VERSION:
        raise ValueError(
            f"select_eligible: unknown selection policy version {policy_version!r} "
            f"(this build implements {SELECTION_POLICY_VERSION})")
    if not candidates:
        return None
    return max(candidates, key=lambda c: (tuple(c.order), c.attempt_id))
