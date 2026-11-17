"""CLI: python3 -m jsonxaudit audit|guard|show"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import engine
from .corpus import build_real_corpus, build_synthetic_corpus, sha256_text

HERE = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE = HERE / "evidence"


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=True, sort_keys=True) + "\n")
    return sha256_text(path.read_text(encoding="utf-8"))


def _compact_records(records: list[dict]) -> list[dict]:
    """Drop value fingerprints for documents every implementation agrees on.

    The real corpus contains multi-hundred-kB documents; keeping their full
    canonical fingerprints would make evidence/ real.jsonl ~50 MB while adding
    no information (the audit result is "agree").  Divergent documents keep the
    complete per-implementation evidence.
    """
    out = []
    for r in records:
        row = {k: r[k] for k in ("id", "origin", "category", "sha256", "status",
                                 "hazard", "silent_divergence", "deviates_from_stdlib")}
        row["errors"] = {i: e for i, e in r["errors"].items() if e}
        row["roundtrip_bad"] = [i for i, o in r["outcomes"].items()
                                if o["status"] == "ok" and o["roundtrip"] != "same"]
        if r["status"] != "agree":
            row["outcomes"] = r["outcomes"]
        out.append(row)
    return out


def _digest_summary(summary: dict) -> str:
    stable = {k: v for k, v in summary.items() if not k.startswith("_")}
    return sha256_text(json.dumps(stable, ensure_ascii=True, sort_keys=True))


def cmd_audit(args) -> int:
    ev = Path(args.evidence)
    entries = build_synthetic_corpus()
    by_id = {e["id"]: e for e in entries}
    records, summary = engine.run_corpus(entries)

    real_entries = []
    real_records, real_summary = [], {}
    real_roots = args.real_dir or []
    if real_roots:
        real_entries = build_real_corpus(real_roots, limit=args.real_limit)
        real_by_id = {e["id"]: e for e in real_entries}
        real_records, real_summary = engine.run_corpus(real_entries)
        by_id.update(real_by_id)

    asym = engine.integer_asymmetry(records + real_records, by_id)
    lax = engine.grammar_laxness(records + real_records, by_id)

    summary["orjson_integer_asymmetry"] = {k: v for k, v in asym.items() if k != "rows"}
    summary["impls"] = {i: (engine.MODULES[i].__name__ if i in engine.MODULES else None) for i in engine.IMPL_NAMES}
    summary["versions"] = {
        "json": getattr(engine.MODULES["json"], "__name__", "stdlib"),
        "orjson": getattr(engine.MODULES.get("orjson"), "__version__", None),
        "ujson": getattr(engine.MODULES.get("ujson"), "__version__", None),
        "simplejson": getattr(engine.MODULES.get("simplejson"), "__version__", None),
        "python": sys.version.split()[0],
    }
    summary["real_corpus"] = real_summary
    summary["grammar_laxness_documents"] = len(lax)
    summary["divergence_attribution"] = engine.attribute_divergence(records + real_records, by_id)

    digests = {
        "synthetic_records": _write_jsonl(ev / "raw" / "synthetic.jsonl", records),
        "real_records": _write_jsonl(ev / "raw" / "real.jsonl", _compact_records(real_records)) if real_records else None,
        "orjson_int_boundary": _write_jsonl(ev / "raw" / "orjson-int-boundary.jsonl", asym["rows"]),
        "grammar_laxness": _write_jsonl(ev / "raw" / "grammar-laxness.jsonl", lax),
    }
    summary["raw_digests"] = digests
    summary["report_sha256"] = _digest_summary(summary)
    (ev / "summary.json").write_text(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        _print_summary(summary)
    return 0


def _print_summary(s: dict) -> None:
    print(f"documents            : {s['n_documents']} unique={s['n_unique_sha256']} by_origin={s['by_origin']}")
    print(f"status               : {s['by_status']}")
    print(f"silent divergence    : {s['n_silent_divergence']}")
    print(f"hazards (any)        : {s['n_hazard']}")
    print(f"errors per impl      : {s['errors_per_impl']}")
    print(f"deviations from json : {s['deviations_from_stdlib_per_impl']}")
    print(f"roundtrip mismatches : {s['roundtrip_mismatch_per_impl']}")
    a = s["orjson_integer_asymmetry"]
    print(f"orjson out-of-range ints: n={a['out_of_range_integer_documents']} "
          f"silently->float={a['silently_coerced_to_float']} dumps_raises={a['dumps_raises']}")
    if s.get("real_corpus"):
        r = s["real_corpus"]
        print(f"real corpus          : n={r['n_documents']} status={r['by_status']}")
    print(f"report sha256        : {s['report_sha256']}")


def cmd_guard(args) -> int:
    doc = Path(args.file).read_text(encoding="utf-8")
    rec = engine.probe_doc(doc)
    if args.json:
        print(json.dumps(rec, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"status={rec['status']} portable={not rec['hazard']}")
        if rec["errors"]:
            for i, e in rec["errors"].items():
                print(f"  {i}: ERROR {e}")
        if rec["deviates_from_stdlib"]:
            print(f"  differs from stdlib json: {rec['deviates_from_stdlib']}")
            for i in rec["deviates_from_stdlib"]:
                print(f"    {i}: {rec['outcomes'][i]['canon']}")
            print(f"    json: {rec['outcomes']['json']['canon']}")
    # exit 1 when the document is interpreted differently by different impls
    return 1 if rec["hazard"] else 0


def cmd_show(args) -> int:
    s = json.loads((Path(args.evidence) / "summary.json").read_text(encoding="utf-8"))
    _print_summary(s)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jsonxaudit", description="JSON cross-implementation silent-drift audit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", help="run the full differential audit and write evidence/")
    a.add_argument("--evidence", default=str(DEFAULT_EVIDENCE))
    a.add_argument("--real-dir", action="append", default=[], help="root to scan for real *.json files")
    a.add_argument("--real-limit", type=int, default=400)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_audit)

    g = sub.add_parser("guard", help="check one JSON document for cross-impl divergence")
    g.add_argument("file")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_guard)

    s = sub.add_parser("show", help="re-print a previously written summary")
    s.add_argument("--evidence", default=str(DEFAULT_EVIDENCE))
    s.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
