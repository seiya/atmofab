"""Redact a recorded pure launch request into a tracked fixture.

Usage:
    python3 tools/tests/data/conductor_launch_requests/redact_launch_request.py \
        workspace/orchestrations/<oid>/launches/<arid>.request.json

Writes `<step>_<substep>.request.json` beside this script. A faithful pure request is a few
hundred KB, almost all of it the inlined `pure_context` documents and the rendered
`launch_prompt_full`; both are replaced by a placeholder that keeps the byte count and the
sha256 of the original, so the KEY SET of `pure_context` (which the pure validator requires per
shape) survives while the content — reproducible from the run it names — does not. Every other
field is copied verbatim: those are what `test_reproduces_every_real_substep_payload` compares
against `build_launch_request`. A deterministic request has neither field and is copied as is.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def placeholder(value: object) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    b = raw.encode("utf-8")
    return f"<redacted: {len(b)} bytes sha256:{hashlib.sha256(b).hexdigest()}>"


def redact(req: dict) -> dict:
    out = dict(req)
    if isinstance(out.get("pure_context"), dict):
        out["pure_context"] = {k: placeholder(v) for k, v in out["pure_context"].items()}
    if "launch_prompt_full" in out:
        out["launch_prompt_full"] = placeholder(out["launch_prompt_full"])
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    req = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    name = f"{req['step']}_{req.get('substep') or 'step'}"
    dest = HERE / f"{name}.request.json"
    dest.write_text(json.dumps(redact(req), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
