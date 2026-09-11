#!/usr/bin/env python3
"""Canonical stage meta contracts shared by runtime and validators."""

from __future__ import annotations

from typing import Any

STAGE_META_FILENAME_BY_STEP: dict[str, str] = {
    "compile": "ir_meta.json",
    "generate": "source_meta.json",
}

# The stage meta a phase's CERTIFICATION is stamped into and read back from
# (`_stamp_certification` / `_phase_certified`). Deliberately a SECOND mapping rather than a
# `build` entry in `STAGE_META_FILENAME_BY_STEP`: that one drives
# `_validate_step_result_payload`'s "a pass step_result of a substep-bearing phase must declare
# exactly one final stage meta" rule, and build (no substeps, host-authored binary_meta) is not
# governed by it. Adding build there would newly reject every passing build step_result.
CERTIFYING_META_FILENAME_BY_STEP: dict[str, str] = {
    "compile": "ir_meta.json",
    "generate": "source_meta.json",
    "build": "binary_meta.json",
}

# Keys a stage meta MAY carry beyond its required set. Listed (rather than merely tolerated)
# because each is written by exactly one host writer and read by the certification predicate:
#   - `artifact_hashes` / `source_ir_id`: stamped by `write_step_result` on pass
#     (`_stamp_certification`), read by `_phase_certified`.
#   - the four `revoked` / `prior_verification_status` keys: written by `revoke_artifact`.
# `verification_status` takes one of `pass` | `fail` | `revoked`; it stays an unconstrained
# non-empty string in the type contract because a leaf-authored meta may legitimately record a
# phase-specific spelling, and the certification predicate compares against `pass` explicitly.
STAGE_META_OPTIONAL_KEYS: tuple[str, ...] = (
    "artifact_hashes",
    "source_ir_id",
    "prior_verification_status",
    "revoked_at",
    "revoked_by_agent_run_id",
    "revocation_reason",
)

STAGE_META_COMMON_REQUIRED_KEYS: tuple[str, ...] = (
    "attempt_count",
    "verification_status",
    "last_fail_reason",
    "debug_mode",
    "context_isolated",
)

STAGE_META_EXTRA_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "compile": (),
    "generate": (),
}


def required_meta_keys_for_step(step_token: str) -> tuple[str, ...]:
    """Return canonical required keys for a stage meta payload."""
    return STAGE_META_COMMON_REQUIRED_KEYS + STAGE_META_EXTRA_REQUIRED_KEYS.get(step_token, ())


def missing_required_meta_keys(meta_data: dict[str, Any], *, step_token: str) -> list[str]:
    """Compute missing required keys from canonical definition."""
    return [k for k in required_meta_keys_for_step(step_token) if k not in meta_data]


def stage_meta_type_violations(meta_data: dict[str, Any], *, step_token: str) -> list[str]:
    """Canonical VALUE-TYPE contract for a stage meta payload (ir_meta / source_meta).

    Returns bare violation clauses (no path/filename prefix) in canonical key order, so
    each caller can format them in its own idiom: the runtime raises
    ``f"{meta_filename} {clause}: {meta_ref}"``, the validator sweeps append
    ``f"{meta_path}:{clause}"``. Keeping the clauses prefix-free is what lets the three
    historical copies of these checks collapse into one definition.

    Only keys PRESENT in the payload are type-checked — a missing required key is
    ``missing_required_meta_keys``' responsibility, and reporting it twice would
    double-count the same defect.

    `last_fail_reason` is the clause this contract exists for: a verify leaf that records
    a structured incident dict there writes an immutable, unrepairable meta (E2E #4). The
    type is a single plain string (or null), never an object/array.
    """
    violations: list[str] = []
    contract_keys = required_meta_keys_for_step(step_token)

    def present(key: str) -> bool:
        return key in contract_keys and key in meta_data

    # `bool` is a subclass of `int` in Python, so a JSON `true` would satisfy a bare
    # isinstance(_, int). Every doc states this key is an integer; exclude bool so the
    # enforced contract is the documented one.
    attempt_count = meta_data.get("attempt_count")
    if present("attempt_count") and (
        isinstance(attempt_count, bool) or not isinstance(attempt_count, int)
    ):
        violations.append("attempt_count must be integer")
    if present("verification_status"):
        status = meta_data.get("verification_status")
        if not isinstance(status, str) or not status.strip():
            violations.append("verification_status must be non-empty string")
    if present("last_fail_reason"):
        reason = meta_data.get("last_fail_reason")
        if reason is not None and not isinstance(reason, str):
            violations.append("last_fail_reason must be string or null")
    if present("debug_mode") and not isinstance(meta_data.get("debug_mode"), bool):
        violations.append("debug_mode must be boolean")
    if present("context_isolated") and not isinstance(meta_data.get("context_isolated"), bool):
        violations.append("context_isolated must be boolean")
    # constraint_reason is CONDITIONALLY required: only a meta that declares it ran with a
    # non-isolated context must justify that. `is False` (not falsy) so a non-boolean
    # context_isolated is reported by its own clause above and not double-flagged here.
    if meta_data.get("context_isolated") is False:
        reason = meta_data.get("constraint_reason")
        if not isinstance(reason, str) or not reason.strip():
            violations.append(
                "requires non-empty constraint_reason when context_isolated=false"
            )
    # The two certification keys the host stamps on pass. Type-checked here — not merely in
    # the stamping writer — because `_phase_certified` REFUSES a phase whose hashes do not
    # re-compute, so a structurally wrong stamp would read as a tampered deliverable and send
    # the operator hunting for a change nobody made.
    if "artifact_hashes" in meta_data:
        hashes = meta_data.get("artifact_hashes")
        if not isinstance(hashes, dict) or not hashes:
            violations.append("artifact_hashes must be a non-empty object")
        else:
            bad = [
                str(k) for k, v in hashes.items()
                if not (isinstance(k, str) and k.strip())
                or not (isinstance(v, str) and v.startswith("sha256:") and len(v) > len("sha256:"))
            ]
            if bad:
                violations.append(
                    "artifact_hashes values must be 'sha256:<hex>' keyed by repo-relative "
                    f"path (offending keys: {sorted(bad)})"
                )
    if "source_ir_id" in meta_data:
        source_ir_id = meta_data.get("source_ir_id")
        if not isinstance(source_ir_id, str) or not source_ir_id.strip():
            violations.append("source_ir_id must be non-empty string")
    return violations
