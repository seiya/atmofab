"""The language-neutral structured signature form of `controlled_spec` §5.1 and the IR's
`public_api.signatures` / `public_api.interfaces` / `public_api.module_parameters`.

The form is language-NEUTRAL by definition (Objective B): a spec author and the Compile leaf write
it, and every language backend's `signatures` module RENDERS it into that language and compares the
result against a generated source. What the form admits is therefore not any one backend's
knowledge, and it lives here, in the neutral core, where every backend reaches it: the loader
(`load_structured_signatures`) and the fail-closed validation (`validate_struct` /
`validate_symbol` / `validate_procedure` / `validate_module_parameter`). Until issue #289 (R4-b
PR-4) it sat in the first language's backend, which was the only renderer; the second language's backend
would otherwise have had to import it from the first, or restate it — and two statements of one
vocabulary are two answers to "is this §5.1 well formed" the moment they drift, while the
target-free Compile gate renders every §5.1 in every language that declares `signatures`.

The vocabulary is written to be satisfiable in EVERY implemented target language, so where the
languages differ the strictest one's rule is the vocabulary's: names are compared
case-insensitively and a rank is bounded by the smallest maximum rank. A renderer may refuse more
(a name that is a reserved word of its language), and says so in its own module.

Stdlib only, plus PyYAML for the loader (imported where it is used).
"""

from __future__ import annotations

import re
from typing import Any

# --- struct vocabulary (documentation) ----------------------------------------------------------
#
# A ``spec`` (the type of an argument / result / component) is a mapping:
#   {"type": "real"|"integer"|"logical"|"string"|"derived"|"procedure",
#    "kind":  <str|None>,     # numeric KIND for real/integer/logical, e.g. "dp"; None = default kind
#    "len":   <str|None>,     # NEUTRAL string length token: "deferred", "assumed", "4",
#                             #   "case_id_len", ...  (no language's own length spelling)
#    "name":  <str|None>,     # derived-type name for "derived"
#    "alloc": <bool>,         # the storage is allocated at run time (never for "procedure")
#    "interface": <str|None>} # for "procedure": the name of the ``interfaces[]`` entry whose
#                             #   prototype the passed procedure must match
#
# An ``entity`` (a dummy argument, a function result, or a derived-type component):
#   {"name": <str>,
#    "rank": <int>,           # 0 scalar, 1 a rank-1 array, 2 a rank-2 array, ...
#    "intent": "in"|"out"|"inout"|None,   # arguments only; None for results and components
#    "spec": <spec>}
#   A "procedure"-typed entity is a dummy PROCEDURE: it is allowed only as a procedure's argument
#   (not a result, not a component, not an argument of an interface prototype), its rank is 0,
#   it carries no dims, and it carries no intent: a bound target language refuses a direction on
#   a procedure argument.
#
# A ``procedure``:
#   {"kind": "subroutine"|"function", "name": <str>, "args": [entity, ...],
#    "result": <entity|None>}   # result present iff kind == "function"
#
# An ``interface`` (a named procedure PROTOTYPE that a "procedure"-typed argument references):
#   the same mapping as a procedure. It is rendered as a prototype, never defined, and its own arguments may not be "procedure"-typed. Every entry must be referenced
#   by at least one argument (an unreferenced prototype is a surface nothing reads), and its
#   name must not collide with a procedure / type / module parameter.
#
# A ``type`` (published derived type):
#   {"name": <str>, "components": [entity, ...]}   # each entity has intent None
#
# A ``module_parameter`` (value-pinned integer parameter referenced by the signatures):
#   {"name": <str>, "base": "integer", "value": <str|int>}   # e.g. dp = float64, case_id_len = 64
#   The VALUE is a NEUTRAL token: a number, or the neutral kind tokens "float64" / "float32"
#   (no language's own kind spelling, and no expression).
#
# The whole signature block:
#   {"module_parameters": [module_parameter, ...],
#    "types": [type, ...],
#    "interfaces": [interface, ...],
#    "procedures": [procedure, ...]}
#

#: The reserved string-length tokens (matched case-insensitively, lowercase-canonical). A consequence
#: of a reserved vocabulary: a ``string`` length symbol may NOT be named ``deferred`` / ``assumed`` —
#: such a ``len`` is read as the reserved token, not a symbol reference.
NEUTRAL_LEN_TOKENS = frozenset({"deferred", "assumed"})
#: The reserved module-parameter kind VALUE tokens (matched case-insensitively).
NEUTRAL_KIND_TOKENS = frozenset({"float64", "float32"})


class SignatureParseError(ValueError):
    """A §5.1 / IR signature a language backend cannot lower, or a malformed structured form
    (fail-closed at callers)."""


# --- validation: fail-closed on any malformed struct ---------------------------------------------
#
# The struct is authored by an LLM (the Compile leaf transcribing §5.1 into the IR, or a §5.1
# author); a malformed shape must produce a clean ``SignatureParseError`` that the gates turn into a
# repairable violation, NEVER an uncaught KeyError/TypeError that crashes the gate with a traceback
# (the "gate-only field fabricated by the leaf -> conductor crash" bug-class). Both render entry
# points validate first, so every downstream ``dict``/``list`` index is known-safe.

VALID_SPEC_TYPES = frozenset({"real", "integer", "logical", "string", "derived", "procedure"})
_VALID_INTENTS = frozenset({"in", "out", "inout"})
_SPEC_KEYS = frozenset({"type", "kind", "len", "name", "alloc", "interface"})
# The fields no renderer reads for each `type`. Indexed by every member of
# `VALID_SPEC_TYPES` — a type with no row would `KeyError` out of `_validate_spec` instead of
# failing closed, which `test_every_spec_type_has_an_inapplicable_row` pins.
INAPPLICABLE_SPEC_FIELDS = {
    "string": ("kind", "name", "interface"),
    "derived": ("kind", "len", "interface"),
    "real": ("len", "name", "interface"),
    "integer": ("len", "name", "interface"),
    "logical": ("len", "name", "interface"),
    "procedure": ("kind", "len", "name"),
}
_ENTITY_KEYS = frozenset({"name", "rank", "intent", "dims", "spec"})
_PROC_KEYS = frozenset({"kind", "name", "args", "result"})
_TYPE_KEYS = frozenset({"name", "components"})
_PARAM_KEYS = frozenset({"name", "base", "value"})


def _require_nonempty_str(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignatureParseError(f"{ctx} must be a non-empty string (got {value!r})")
    return value


def _reject_unknown_keys(mapping: dict[str, Any], allowed: frozenset[str], ctx: str) -> None:
    # `key=str`: a YAML mapping can mix key types (e.g. an int key alongside string keys); a bare
    # `sorted` would raise `TypeError: '<' not supported between int and str` and escape the callers'
    # `except SignatureParseError`, crashing the gate instead of failing closed.
    unknown = sorted(set(mapping) - allowed, key=str)
    if unknown:
        raise SignatureParseError(
            f"{ctx} has unknown key(s) {unknown}; allowed: {sorted(allowed)} "
            "(a mistyped key must fail closed, not silently fall to a default)")


# The smallest maximum array rank among the implemented language backends (15); a larger value is
# malformed in some target, and, unbounded, would let a renderer amplify one integer into a
# multi-GB string (OOM/hang) instead of failing closed.
MAX_RANK = 15
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _require_identifier(value: Any, ctx: str) -> str:
    """A published NAME (symbol / argument / component / derived-type / parameter) must be a plain
    identifier. This is also the injection guard: names flow verbatim into rendered source text that
    a language backend re-parses into stanzas, so a name carrying a separator, a newline or a
    closing keyword could otherwise smuggle or split a stanza."""
    _require_nonempty_str(value, ctx)
    if not IDENTIFIER_RE.match(value):
        raise SignatureParseError(
            f"{ctx} must be an identifier [A-Za-z][A-Za-z0-9_]* (got {value!r})")
    return value


def _require_safe_token(value: Any, ctx: str) -> str:
    """A token a renderer places INSIDE a bracketed position — a kind, a SINGLE dimension bound —
    may be a number / a symbol / a simple expression, but must not carry a closing bracket (which
    would end the enclosing position and smuggle a declaration), a ``,`` (which would add dimensions
    to a single bound — ``dims: ['3,4']`` must not render as rank 2), a ``;`` (a statement
    separator), nor the other structural characters (``::``, a newline, a comment ``!``, an opening
    bracket)."""
    _require_nonempty_str(value, ctx)
    if "::" in value or any(ch in value for ch in "\n\r!(),;"):
        raise SignatureParseError(
            f"{ctx} must not contain structural characters (::, newline, !, parentheses, "
            f"comma, semicolon); got {value!r}")
    return value


def _require_len_token(value: Any, ctx: str) -> str:
    """A NEUTRAL string-length token — the closed grammar the neutral IR admits for a ``string``
    spec's ``len``: ``deferred`` (the length is set when the value is), ``assumed`` (the caller's
    length), a fixed decimal width (``4``), or a symbol identifier (``case_id_len``). Anything else —
    a language's own length spelling included — fails closed, so a stale §5.1 cannot pass through.
    ``deferred`` / ``assumed`` are RESERVED (matched case-insensitively, lowercase-canonical) and
    take precedence over the symbol-identifier branch, so a length symbol may not be named that.
    Every admitted form is structurally safe (no bracket / ``,`` / newline / ``!`` to smuggle a
    declaration), so this both fixes the vocabulary and subsumes the injection guard."""
    _require_nonempty_str(value, ctx)
    s = value.strip()
    if s.lower() in NEUTRAL_LEN_TOKENS:  # reserved token, case-insensitive (render lowercases)
        return s
    if s.isdigit() or IDENTIFIER_RE.match(s):
        return s
    raise SignatureParseError(
        f"{ctx} must be a neutral length token ('deferred'/'assumed'), a fixed decimal width, or a "
        f"symbol identifier (got {value!r})")


def _require_neutral_parameter_value(value: str, ctx: str) -> str:
    """A NEUTRAL module-parameter VALUE — the closed grammar the neutral IR admits: a decimal
    integer string, or the neutral kind tokens ``float64`` / ``float32``. An expression, and a
    language's own kind spelling, fail closed: those are exactly the residue of one target language
    the neutral IR must not carry.

    A closed numeric/token grammar also forecloses the older hazards for free: no ``::`` / ``;`` /
    newline / comment can smuggle a second declaration, and no character literal (whose case/
    whitespace-insensitive value pin would be unsound) can appear."""
    _require_nonempty_str(value, ctx)
    s = value.strip()
    low = s.lower()
    if low in NEUTRAL_KIND_TOKENS:
        return s
    if s.isdigit():
        return s
    raise SignatureParseError(
        f"{ctx} must be a number or a neutral kind token ('float64'/'float32'); a language's "
        f"own kind spelling or an expression has no neutral form (got {value!r})")


def _validate_spec(spec: Any, ctx: str) -> None:
    if not isinstance(spec, dict):
        raise SignatureParseError(f"{ctx}.spec must be a mapping (got {type(spec).__name__})")
    _reject_unknown_keys(spec, _SPEC_KEYS, f"{ctx}.spec")
    t = spec.get("type")
    # `isinstance(str)` BEFORE the frozenset membership: an unhashable `type: []` / `type: {}` would
    # otherwise raise a raw TypeError (unhashable) that escapes the callers' `except
    # SignatureParseError`, crashing the gate instead of failing closed.
    if not isinstance(t, str) or t not in VALID_SPEC_TYPES:
        raise SignatureParseError(
            f"{ctx}.spec.type must be one of {sorted(VALID_SPEC_TYPES)} (got {t!r})")
    # A field no renderer uses for this `type` would be SILENTLY DROPPED at render — so
    # §5.1 and the IR could carry different authored info yet render/compare equal (fail-open).
    # Reject any inapplicable field (present and non-None); `alloc` applies to every type but
    # `procedure`, where it is checked below.
    for bad in INAPPLICABLE_SPEC_FIELDS[t]:
        if spec.get(bad) is not None:
            raise SignatureParseError(
                f"{ctx}.spec.{bad} is not applicable to type '{t}' (it would be silently dropped at "
                "render, letting §5.1 and the IR differ yet compare equal)")
    if t == "string":
        _require_len_token(spec.get("len"), f"{ctx}.spec.len (string length is required)")
    elif t == "derived":
        _require_identifier(spec.get("name"), f"{ctx}.spec.name (derived type name is required)")
    elif t == "procedure":
        _require_identifier(
            spec.get("interface"), f"{ctx}.spec.interface (the referenced prototype name is required)")
    else:  # real / integer / logical: kind optional but a safe token when present
        if spec.get("kind") is not None:
            _require_safe_token(spec.get("kind"), f"{ctx}.spec.kind")
    alloc = spec.get("alloc")
    if alloc is not None and not isinstance(alloc, bool):
        # `not in (None, True, False)` would accept `alloc: 1` (1 == True) and render by truthiness;
        # require a real boolean.
        raise SignatureParseError(f"{ctx}.spec.alloc must be a boolean (got {alloc!r})")
    if t == "procedure" and alloc:
        raise SignatureParseError(
            f"{ctx}.spec.alloc is not applicable to type 'procedure' (a procedure argument is not "
            "allocated at run time)")


class _NameScope:
    """One scoping unit's name space, compared CASE-INSENSITIVELY because a bound target language's
    identifiers are: ``f`` and ``F`` are one name there, so a §5.1 that publishes both can never
    compile in it, and nothing but this refusal reports it before a billed run does (issue #278;
    issue #265 PR-5 drafted exactly that pair). The §5.1 vocabulary is written to be satisfiable in
    every target language, so the strictest one's rule is the vocabulary's."""

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._seen: dict[str, str] = {}

    def claim(self, name: str, ctx: str) -> None:
        low = name.lower()
        if low in self._seen:
            raise SignatureParseError(
                f"{ctx} '{name}' collides with another published name, {self._seen[low]}, in "
                f"{self._owner} (a bound "
                "target language's identifiers are case-insensitive, so two names differing only "
                "in case are one name there) — rename one of them")
        self._seen[low] = f"{ctx} '{name}'"


def _validate_entity(
    ent: Any, ctx: str, *, allow_intent: bool, allow_procedure: bool = False
) -> None:
    if not isinstance(ent, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(ent).__name__})")
    _reject_unknown_keys(ent, _ENTITY_KEYS, ctx)
    _require_identifier(ent.get("name"), f"{ctx}.name")
    _validate_spec(ent.get("spec"), ctx)
    if ent["spec"].get("type") == "procedure":
        # A dummy PROCEDURE: scalar, no dims, no intent, and only where a dummy procedure can
        # stand — a procedure's own argument list. Each refusal names the rule so a leaf's warm
        # retry can converge on it.
        if not allow_procedure:
            raise SignatureParseError(
                f"{ctx}.spec.type 'procedure' is allowed only for a procedure's argument (not a "
                "result, a derived-type component, or an argument of an interface prototype)")
        if ent.get("rank", 0) != 0 or isinstance(ent.get("rank", 0), bool):
            raise SignatureParseError(
                f"{ctx}.rank must be 0 (omitted) for a procedure-typed argument "
                f"(got {ent.get('rank')!r})")
        if ent.get("dims") is not None:
            raise SignatureParseError(f"{ctx}.dims is not applicable to a procedure-typed argument")
        if ent.get("intent") is not None:
            raise SignatureParseError(
                f"{ctx}.intent is not applicable to a procedure-typed argument (a bound target "
                f"language refuses a direction on a procedure argument; got {ent.get('intent')!r})")
    rank = ent.get("rank", 0)
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
        raise SignatureParseError(f"{ctx}.rank must be a non-negative integer (got {rank!r})")
    if rank > MAX_RANK:
        raise SignatureParseError(
            f"{ctx}.rank must be <= {MAX_RANK} (the smallest maximum array rank of the "
            f"implemented target languages; got {rank})")
    dims = ent.get("dims")
    if dims is not None:
        if not isinstance(dims, list) or not dims or not all(
                isinstance(d, str) and d.strip() for d in dims):
            raise SignatureParseError(
                f"{ctx}.dims must be a non-empty list of dimension strings (e.g. ['3', ':'])")
        if len(dims) > MAX_RANK:
            raise SignatureParseError(
                f"{ctx}.dims has {len(dims)} entries (the maximum array rank is {MAX_RANK})")
        for d in dims:
            _require_safe_token(d, f"{ctx}.dims entry")
        if "rank" in ent and rank != len(dims):
            raise SignatureParseError(
                f"{ctx}: rank ({rank}) disagrees with dims length ({len(dims)})")
    intent = ent.get("intent")
    if intent is not None:
        if not allow_intent:
            raise SignatureParseError(
                f"{ctx}.intent is not allowed here (a result / component carries no intent)")
        if not isinstance(intent, str) or intent not in _VALID_INTENTS:  # isinstance guards unhashable
            raise SignatureParseError(
                f"{ctx}.intent must be one of {sorted(_VALID_INTENTS)} (got {intent!r})")


def validate_procedure(proc: Any, ctx: str, *, allow_procedure_args: bool = True) -> None:
    """Validate a procedure entry, or — with ``allow_procedure_args=False`` — an ``interfaces[]``
    prototype, whose arguments may not themselves be dummy procedures (no nesting: the renderer
    and every comparison would otherwise recurse, and no consumer needs it)."""
    if not isinstance(proc, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(proc).__name__})")
    _reject_unknown_keys(proc, _PROC_KEYS, ctx)
    kind = proc.get("kind")
    if kind not in ("subroutine", "function"):
        raise SignatureParseError(f"{ctx}.kind must be 'subroutine' or 'function' (got {kind!r})")
    name = _require_identifier(proc.get("name"), f"{ctx}.name")
    args = proc.get("args", [])
    if not isinstance(args, list):
        raise SignatureParseError(f"{ctx}.args must be a list (got {type(args).__name__})")
    for i, arg in enumerate(args):
        _validate_entity(arg, f"{ctx}.args[{i}]", allow_intent=True,
                         allow_procedure=allow_procedure_args)
    result = proc.get("result")
    if kind == "function":
        if not isinstance(result, dict):
            raise SignatureParseError(f"{ctx} (function {name}) requires a mapping 'result'")
        _validate_entity(result, f"{ctx}.result", allow_intent=False)
    elif result is not None:
        raise SignatureParseError(f"{ctx} (subroutine {name}) must not carry a 'result'")
    # One scope, one name space, compared case-insensitively. A dummy may not repeat another dummy
    # or the procedure's own name, and a `result` may not repeat a dummy. The one legal equality is
    # a result spelled EXACTLY as the function, which `_render_procedure` emits as the implicit
    # result; the same name in another case renders `result(<name>)`, which the compiler refuses.
    scope = _NameScope(f"{kind} '{name}'")
    scope.claim(name, f"{ctx}.name")
    for i, arg in enumerate(args):
        scope.claim(arg["name"], f"{ctx}.args[{i}].name")
    if kind == "function" and result["name"] != name:
        scope.claim(result["name"], f"{ctx}.result.name")


def _validate_type(tdef: Any, ctx: str) -> None:
    if not isinstance(tdef, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(tdef).__name__})")
    _reject_unknown_keys(tdef, _TYPE_KEYS, ctx)
    _require_identifier(tdef.get("name"), f"{ctx}.name")
    comps = tdef.get("components", [])
    # An empty component list is an opaque tag type, which a language backend's parser can produce
    # from a type definition with no component; accepting it keeps parse and validate symmetric.
    if not isinstance(comps, list):
        raise SignatureParseError(f"{ctx}.components must be a list (got {type(comps).__name__})")
    scope = _NameScope(f"derived type '{tdef['name']}'")
    for i, comp in enumerate(comps):
        _validate_entity(comp, f"{ctx}.components[{i}]", allow_intent=False)
        scope.claim(comp["name"], f"{ctx}.components[{i}].name")


def validate_module_parameter(mp: Any, ctx: str) -> None:
    if not isinstance(mp, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(mp).__name__})")
    _reject_unknown_keys(mp, _PARAM_KEYS, ctx)
    _require_identifier(mp.get("name"), f"{ctx}.name")
    base = mp.get("base")
    if base is not None and base != "integer":
        # Every renderer emits an integer-based parameter unconditionally, so any other `base`
        # would be authored but silently dropped — fail closed.
        raise SignatureParseError(
            f"{ctx}.base must be 'integer' (the only module-parameter base the renderer emits); "
            f"got {base!r}")
    value = mp.get("value")
    if isinstance(value, bool):  # bool is an int subclass; `value: true` is not a parameter value
        raise SignatureParseError(f"{ctx}.value must be a number or symbol, not a boolean")
    if isinstance(value, int):
        return  # rendered as its decimal string
    _require_neutral_parameter_value(value, f"{ctx}.value")


def validate_symbol(sig: Any, ctx: str = "signature") -> None:
    """Validate ONE published symbol (procedure or type) — the shape a language backend's
    ``render_symbol`` accepts. A struct that is neither is fail-closed."""
    if isinstance(sig, dict) and sig.get("kind") in ("subroutine", "function"):
        validate_procedure(sig, ctx)
    elif isinstance(sig, dict) and "components" in sig:
        _validate_type(sig, ctx)
    else:
        raise SignatureParseError(
            f"{ctx} is neither a procedure (kind: subroutine/function) nor a type (components: [...])")


def validate_struct(struct: dict[str, Any]) -> None:
    """Validate a whole ``{module_parameters, types, interfaces, procedures}`` struct, fail-closed.

    Beyond each entry's own shape: no two published names collide (case-insensitively — see
    `_NameScope`), across module parameters, types, prototypes and procedures alike; and the
    struct-level rules for ``interfaces[]``: every ``spec.interface`` reference resolves to an
    entry, and every entry is referenced by at least one argument. A single-symbol render
    (a backend's ``render_symbol``) cannot see the other entries, so these rules live here and
    nowhere else. The collisions INSIDE one procedure or type are its own validator's."""
    for i, mp in enumerate(struct.get("module_parameters") or []):
        validate_module_parameter(mp, f"module_parameters[{i}]")
    for i, tdef in enumerate(struct.get("types") or []):
        _validate_type(tdef, f"types[{i}]")
    for i, iface in enumerate(struct.get("interfaces") or []):
        validate_procedure(iface, f"interfaces[{i}]", allow_procedure_args=False)
    for i, proc in enumerate(struct.get("procedures") or []):
        validate_procedure(proc, f"procedures[{i}]")
    # Every published name shares the module's one name space (every entry is shape-valid at this
    # point, so the reads are safe).
    module_scope = _NameScope("the module")
    for key in STRUCT_TOP_KEYS:
        for i, entry in enumerate(struct.get(key) or []):
            module_scope.claim(entry["name"], f"{key}[{i}].name")
    # Reference integrity.
    iface_names = {
        iface["name"].lower(): iface["name"] for iface in struct.get("interfaces") or []}
    referenced: set[str] = set()
    for i, proc in enumerate(struct.get("procedures") or []):
        for j, arg in enumerate(proc.get("args") or []):
            spec = arg["spec"]
            if spec.get("type") != "procedure":
                continue
            ref = spec["interface"].lower()
            if ref not in iface_names:
                raise SignatureParseError(
                    f"procedures[{i}].args[{j}].spec.interface '{spec['interface']}' does not name "
                    f"an entry of interfaces[] (declared: {sorted(iface_names.values())})")
            referenced.add(ref)
    unreferenced = sorted(iface_names[k] for k in iface_names if k not in referenced)
    if unreferenced:
        raise SignatureParseError(
            f"interfaces[] entries {unreferenced} are referenced by no procedure argument (an "
            "unreferenced prototype is a published surface nothing reads; delete it or reference "
            "it from an argument's `spec: {type: procedure, interface: <name>}`)")


STRUCT_TOP_KEYS = ("module_parameters", "types", "interfaces", "procedures")


def load_structured_signatures(body: str) -> tuple[dict[str, Any], str | None]:
    """Load a structured (YAML) §5.1 signature block into the canonical struct. Returns
    ``(struct, error)``; ``error`` is non-``None`` (and ``struct`` empty) when the block is not a
    mapping of the expected shape — fail-closed at the gate. Every top key is optional but must be a
    list when present; an unknown top key is rejected so a typo cannot silently drop signatures."""
    import yaml  # local import: keep this module import-light for pure render/parse callers

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return ({}, f"structured §5.1 block is not valid YAML: {exc}")
    if not isinstance(data, dict):
        return ({}, "structured §5.1 block must be a YAML mapping "
                    "with keys module_parameters / types / interfaces / procedures")
    unknown = sorted(set(data) - set(STRUCT_TOP_KEYS), key=str)  # key=str: mixed key types (a YAML
    if unknown:                                                    # `1:` next to `foo:`) must not
        return ({}, f"structured §5.1 block has unknown key(s) {unknown}; "  # TypeError-crash sorted
                    f"allowed: {list(STRUCT_TOP_KEYS)}")
    struct: dict[str, Any] = {key: [] for key in STRUCT_TOP_KEYS}
    for key in STRUCT_TOP_KEYS:
        if key not in data:
            continue  # ABSENT key -> that category is empty (a pruned §5.1 may omit e.g. types)
        val = data[key]
        # A PRESENT-but-null (`module_parameters:` / `module_parameters: null`) must fail closed,
        # NOT be silently treated as empty: an empty module_parameters would drop the dp/case_id_len
        # value pins, letting a `dp = float32` / `case_id_len = 32` drift pass the parameter check.
        if not isinstance(val, list):
            return ({}, f"structured §5.1 block's '{key}' must be a list (got "
                        f"{type(val).__name__}); a present-but-null key must fail closed")
        struct[key] = val
    return (struct, None)
