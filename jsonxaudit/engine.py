"""Cross-implementation JSON differential engine.

For every JSON *text* in the corpus we record, per implementation, one of:

* ``ok``    -- parsed, with a canonical value fingerprint
* ``error`` -- raised

Two aggregate hazards are then derived:

* ``value_divergence`` -- two implementations both accept the document but
  produce *different values* (silent, no exception anywhere).
* ``error_mix``        -- some implementations accept, others raise (the same
  document is portable only by accident).

The stdlib ``json`` module is used as the reference implementation.
"""

from __future__ import annotations

import math
from fractions import Fraction
import re
from dataclasses import dataclass, asdict

from .corpus import INT64_MIN, UINT64_MAX, sha256_text

IMPL_NAMES = ("json", "orjson", "ujson", "simplejson")
INT_LITERAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")


def _load_modules():
    mods = {"json": __import__("json")}
    for name in ("orjson", "ujson", "simplejson"):
        try:
            mods[name] = __import__(name)
        except ImportError:  # pragma: no cover - reported in README env section
            pass
    return mods


MODULES = _load_modules()
MISSING = tuple(n for n in IMPL_NAMES if n not in MODULES)


def canonical(value) -> str:
    """A total, type-tagged fingerprint of a parsed JSON value."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return f"int:{value}"
    if isinstance(value, float):
        if math.isnan(value):
            return "float:nan"
        if math.isinf(value):
            return "float:+inf" if value > 0 else "float:-inf"
        return f"float:{value.hex()}"
    if isinstance(value, str):
        return f"str:{value!r}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = sorted(f"{canonical(k)}={canonical(v)}" for k, v in value.items())
        return "{" + ",".join(parts) + "}"
    return f"{type(value).__name__}:{value!r}"


@dataclass
class Outcome:
    impl: str
    status: str            # "ok" | "error" | "missing"
    canon: str | None = None
    error: str | None = None
    dump_status: str = "n/a"
    dump_error: str | None = None
    roundtrip: str = "n/a"  # "same" | "different" | "error"


def probe_impl(impl: str, doc: str) -> Outcome:
    mod = MODULES.get(impl)
    if mod is None:
        return Outcome(impl=impl, status="missing")
    try:
        value = mod.loads(doc)
    except Exception as exc:  # noqa: BLE001 - the exception *is* the observation
        return Outcome(impl=impl, status="error", error=f"{type(exc).__name__}: {str(exc)[:120]}")
    fp = canonical(value)
    out = Outcome(impl=impl, status="ok", canon=fp)
    try:
        if impl == "orjson":
            dump = mod.dumps(value).decode("utf-8")
        else:
            dump = mod.dumps(value)
        out.dump_status = "ok"
    except Exception as exc:  # noqa: BLE001
        out.dump_status = "error"
        out.dump_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        out.roundtrip = "error"
        return out
    try:
        reparsed = mod.loads(dump)
        out.roundtrip = "same" if canonical(reparsed) == fp else "different"
    except Exception:  # noqa: BLE001
        out.roundtrip = "error"
    return out


def probe_doc(doc: str) -> dict:
    outcomes = {impl: probe_impl(impl, doc) for impl in IMPL_NAMES}
    live = {i: o for i, o in outcomes.items() if o.status != "missing"}
    ok = {i: o for i, o in live.items() if o.status == "ok"}
    errs = [i for i, o in live.items() if o.status == "error"]
    if not live:
        status = "no_impl"
    elif not errs and len({o.canon for o in ok.values()}) == 1:
        status = "agree"
    elif len(errs) == len(live):
        status = "all_error"
    elif errs:
        status = "error_mix"
    else:
        status = "value_divergence"
    ref = live.get("json")
    deviations = []
    if ref is not None and ref.status == "ok":
        for i, o in ok.items():
            if i != "json" and o.canon != ref.canon:
                deviations.append(i)
    return {
        "status": status,
        "hazard": status in ("value_divergence", "error_mix"),
        "silent_divergence": status == "value_divergence",
        "errors": {i: live[i].error for i in errs},
        "deviates_from_stdlib": deviations,
        "outcomes": {i: asdict(o) for i, o in outcomes.items()},
    }


def run_corpus(entries: list[dict]) -> tuple[list[dict], dict]:
    """Audit every entry; return (per-document records, summary)."""
    records = []
    for e in entries:
        rec = {"id": e["id"], "origin": e["origin"], "category": e["category"], "sha256": e["sha256"]}
        rec.update(probe_doc(e["doc"]))
        records.append(rec)

    by_status: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    impl_err: dict[str, int] = {i: 0 for i in IMPL_NAMES}
    impl_dev: dict[str, int] = {i: 0 for i in IMPL_NAMES}
    impl_rt_bad: dict[str, int] = {i: 0 for i in IMPL_NAMES}
    for r in records:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_origin[r["origin"]] = by_origin.get(r["origin"], 0) + 1
        for i, err in r["errors"].items():
            if err:
                impl_err[i] += 1
        for i in r["deviates_from_stdlib"]:
            impl_dev[i] += 1
        for i, o in r["outcomes"].items():
            if o["roundtrip"] == "different":
                impl_rt_bad[i] += 1

    summary = {
        "n_documents": len(records),
        "n_unique_sha256": len({r["sha256"] for r in records}),
        "by_origin": by_origin,
        "by_status": by_status,
        "n_hazard": sum(1 for r in records if r["hazard"]),
        "n_silent_divergence": sum(1 for r in records if r["silent_divergence"]),
        "errors_per_impl": impl_err,
        "deviations_from_stdlib_per_impl": impl_dev,
        "roundtrip_mismatch_per_impl": impl_rt_bad,
        "missing_impls": list(MISSING),
        "corpus_digest": sha256_text("".join(sorted(r["sha256"] for r in records))),
    }
    return records, summary


def integer_asymmetry(records: list[dict], entries_by_id: dict[str, dict]) -> dict:
    """Headline measurement: orjson's loads/dumps asymmetry on out-of-range ints.

    A document qualifies when it is an RFC 8259 integer literal whose value is
    outside orjson's documented range ([-2**63, 2**64-1]).
    """
    rows = []
    # The asymmetry is measured directly, not through the float that loads()
    # already produced: can orjson *serialise* the very integer it silently
    # coerced on the way in?
    orjson = MODULES.get("orjson")
    for r in records:
        e = entries_by_id[r["id"]]
        doc = e["doc"]
        if not INT_LITERAL_RE.match(doc):
            continue
        value = int(doc)
        if INT64_MIN <= value <= UINT64_MAX:
            continue
        o = r["outcomes"].get("orjson", {})
        row = {
            "id": r["id"],
            "digits": len(doc.lstrip("-")),
            "loads_status": o.get("status"),
            "loads_canon": o.get("canon"),
            "silently_coerced_to_float": (o.get("status") == "ok" and str(o.get("canon")).startswith("float:")),
        }
        rows.append(row)
        try:
            orjson.dumps(value)
            row["dumps_original_int"] = "ok"
            row["dumps_raises"] = False
        except Exception as exc:  # noqa: BLE001
            enc_err = getattr(orjson, "JSONEncodeError", TypeError)
            row["dumps_original_int"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            row["dumps_raises"] = isinstance(exc, enc_err)
        coerced = orjson.loads(entries_by_id[r["id"]]["doc"])
        row["reencoded"] = orjson.dumps(coerced).decode("utf-8")
        row["reencode_equals_input"] = (row["reencoded"] == doc)
        row["roundtrip_int_equals_original"] = (
            isinstance(coerced, int) and coerced == value
        )
        if isinstance(coerced, float):
            if math.isinf(coerced):
                row["relative_error"] = None  # complete loss; counted separately
                row["absolute_error"] = None
            else:
                # Exact: Fraction(float) is the true binary value, so this is
                # the real relative error, not a float-vs-float round trip.
                diff = abs(Fraction(coerced) - value)
                row["relative_error"] = float(diff / abs(value))
                row["absolute_error"] = float(diff)
        else:
            row["relative_error"] = 0.0 if coerced == value else 1.0
            row["absolute_error"] = 0.0 if coerced == value else abs(coerced - value)
    n = len(rows)
    return {
        "out_of_range_integer_documents": n,
        "silently_coerced_to_float": sum(1 for r in rows if r["silently_coerced_to_float"]),
        "silent_coercion_rate": (sum(1 for r in rows if r["silently_coerced_to_float"]) / n) if n else None,
        "dumps_raises": sum(1 for r in rows if r.get("dumps_raises")),
        "dumps_raise_rate": (sum(1 for r in rows if r.get("dumps_raises")) / n) if n else None,
        "roundtrip_preserves_int": sum(1 for r in rows if r.get("roundtrip_int_equals_original")),
        "reencode_equals_input": sum(1 for r in rows if r.get("reencode_equals_input")),
        "max_relative_error": max((r["relative_error"] for r in rows if r.get("relative_error") is not None), default=None),
        "max_absolute_error": max((r["absolute_error"] for r in rows if r.get("absolute_error") is not None), default=None),
        "coerced_to_infinity": sum(1 for r in rows if r.get("relative_error") is None),
        "agreement_with_stdlib": sum(1 for r in rows if r["loads_status"] == "ok" and r["loads_canon"] and r["loads_canon"].startswith("int:")),
        "rows": rows,
    }


def _coerce_big_ints(value):
    """Mirror orjson's behaviour: ints outside [-2**63, 2**64-1] become floats."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if INT64_MIN <= value <= UINT64_MAX:
            return value
        try:
            return float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
    if isinstance(value, list):
        return [_coerce_big_ints(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_coerce_big_ints(v) for v in value)
    if isinstance(value, dict):
        return {k: _coerce_big_ints(v) for k, v in value.items()}
    return value


def attribute_divergence(records: list[dict], entries_by_id: dict[str, dict]) -> dict:
    """Attribute every silent value divergence to the big-int coercion.

    For each divergent document, re-read it with the stdlib, apply the
    documented-range rule above, and check whether the resulting fingerprint is
    byte-identical to orjson's.  Disagreements that survive this transform are
    reported as unattributed.
    """
    causes = {"orjson_int_to_float": 0, "other": 0}
    examples = []
    for r in records:
        if not r["silent_divergence"]:
            continue
        orj = r["outcomes"].get("orjson", {})
        doc = entries_by_id[r["id"]]["doc"]
        predicted = None
        try:
            predicted = canonical(_coerce_big_ints(MODULES["json"].loads(doc)))
        except Exception:  # noqa: BLE001
            predicted = None
        if predicted is not None and predicted == orj.get("canon"):
            causes["orjson_int_to_float"] += 1
        else:
            causes["other"] += 1
            if len(examples) < 10:
                examples.append({
                    "id": r["id"],
                    "canons": {i: o.get("canon") for i, o in r["outcomes"].items() if o.get("status") == "ok"},
                    "predicted": predicted,
                })
    return {"causes": causes, "other_examples": examples}


def grammar_laxness(records: list[dict], entries_by_id: dict[str, dict]) -> list[dict]:
    """Which malformed documents does each impl *accept* (silently)?"""
    out = []
    for r in records:
        if r["category"].split(".")[0] != "malformed":
            continue
        if r["outcomes"].get("json", {}).get("status") != "error":
            continue  # only compare against stdlib rejecting it
        accepted = []
        for i in ("orjson", "ujson", "simplejson"):
            o = r["outcomes"].get(i, {})
            if o.get("status") == "ok":
                accepted.append({"impl": i, "canon": o.get("canon")})
        if accepted:
            out.append({
                "id": r["id"],
                "doc": entries_by_id[r["id"]]["doc"],
                "accepted_by": [a["impl"] for a in accepted],
                "values": {a["impl"]: a["canon"] for a in accepted},
            })
    return sorted(out, key=lambda x: x["id"])
