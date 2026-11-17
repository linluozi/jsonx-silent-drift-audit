"""Deterministic corpus builder for the JSON cross-implementation audit.

Every document is a JSON *text*.  Documents are de-duplicated by sha256 of
their UTF-8 (surrogatepass) encoding, so "N unique documents" in the report is
a real, independently verifiable number -- not a loop count.

Categories are deliberately labelled so the report can separate
(a) adversarial-but-RFC-8259-valid text and (b) malformed text.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

SEED = 20261117

# Signed 64-bit min / unsigned 64-bit max: orjson's documented int range.
INT64_MIN = -(2**63)
UINT64_MAX = 2**64 - 1


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()


def _dedup(entries):
    seen: dict[str, int] = {}
    out = []
    for e in entries:
        h = sha256_text(e["doc"])
        if h in seen:
            continue
        seen[h] = 1
        e["sha256"] = h
        out.append(e)
    return out


def _int_entries(rng: random.Random):
    out = []
    # in-range positive / negative
    for _ in range(150):
        n = rng.randrange(0, UINT64_MAX + 1)
        out.append({"category": "int.in_range", "doc": str(n)})
    for _ in range(80):
        n = rng.randrange(INT64_MIN, 0)
        out.append({"category": "int.in_range", "doc": str(n)})
    # just outside the unsigned max
    for _ in range(180):
        n = rng.randrange(UINT64_MAX + 1, UINT64_MAX + 1 + 10**12)
        out.append({"category": "int.over_uint64", "doc": str(n)})
    # just below the signed min
    for _ in range(120):
        n = rng.randrange(INT64_MIN - 10**12, INT64_MIN)
        out.append({"category": "int.below_int64", "doc": str(n)})
    # arbitrary precision, both signs
    for _ in range(120):
        digits = rng.randrange(21, 201)
        first = rng.randrange(1, 10)
        rest = "".join(str(rng.randrange(10)) for _ in range(digits - 1))
        sign = "-" if rng.random() < 0.5 else ""
        out.append({"category": "int.huge", "doc": f"{sign}{first}{rest}"})
    # exact boundaries
    edge = [0, -0, 1, -1, 2**53 - 1, 2**53, 2**53 + 1, 2**63 - 1, 2**63, -2**63, INT64_MIN - 1,
            UINT64_MAX, UINT64_MAX + 1, UINT64_MAX + 2, -(2**64), 10**19, 10**20, 10**30,
            10**77, -(10**30)]
    for n in edge:
        out.append({"category": "int.boundary", "doc": str(n)})
    return out


def _float_entries(rng: random.Random):
    out = []
    for _ in range(120):
        out.append({"category": "float.valid", "doc": repr(rng.uniform(-1e6, 1e6))})
    for _ in range(60):
        m = rng.randrange(1, 10**17)
        e = rng.randrange(-300, 300)
        out.append({"category": "float.exponent", "doc": f"{m}e{e}"})
    for s in ("0.1", "-0.0", "1.0", "1e308", "1e-308", "5e-324", "1.7976931348623157e308",
              "1e400", "-1e400", "1e-400", "-1e-400", "0e0", "-0e0", "1E+2", "1e+002",
              "0.30000000000000004", "9007199254740993.0"):
        out.append({"category": "float.edge", "doc": s})
    for s in ("NaN", "nan", "-NaN", "Infinity", "-Infinity", "inf", "+Infinity",
              "1e999999999999", "-1e999999999999"):
        out.append({"category": "float.nonstandard", "doc": s})
    return out


def _string_entries(rng: random.Random):
    out = []
    # valid escapes
    for cp in [0x00, 0x01, 0x07, 0x08, 0x09, 0x0A, 0x0C, 0x0D, 0x1F, 0x20, 0x22, 0x5C, 0x2F,
               0x7F, 0x80, 0xFF, 0x100, 0x2028, 0x2029, 0xFEFF, 0x1F600, 0x10FFFF]:
        out.append({"category": "string.escape", "doc": json.dumps(chr(cp), ensure_ascii=True)})
    # surrogate pairs (legal) and unpaired surrogates (grammar-legal \uXXXX)
    out.append({"category": "string.surrogate_pair", "doc": '"\\ud83d\\ude00"'})
    out.append({"category": "string.surrogate_pair", "doc": '"\\ud800\\udc00"'})
    for esc in ("\\ud800", "\\udc00", "\\udfff", "\\ud800x", "x\\udc00", "\\ud800\\ud800"):
        out.append({"category": "string.unpaired_surrogate", "doc": '"' + esc + '"'})
    # raw (non-escaped) content
    for s in ("héllo", "e\u0301", "\u00e9", "日本語", "a\u0000b", "a\tb", "line\nbreak",
              "quote\"inside", "back\\slash", "solidus/slash", "", " ", "\u200b", "\ufeff"):
        out.append({"category": "string.raw", "doc": json.dumps(s, ensure_ascii=False)})
    # random unicode strings
    for _ in range(60):
        n = rng.randrange(0, 40)
        s = "".join(chr(rng.choice([rng.randrange(0x20, 0x7F), rng.randrange(0x80, 0x2000),
                                    rng.randrange(0x1F300, 0x1F600)])) for _ in range(n))
        out.append({"category": "string.random", "doc": json.dumps(s, ensure_ascii=rng.random() < 0.5)})
    return out


def _structure_entries(rng: random.Random):
    out = []
    for d in range(1, 13):
        out.append({"category": "structure.depth", "doc": "[" * d + "0" + "]" * d})
        out.append({"category": "structure.depth", "doc": '{"a":' * d + "0" + "}" * d})
    for doc in ('{}', '[]', '{"a":1,"a":2}', '{"a":1,"a":2,"a":3}', '{"a":{"b":1,"b":2}}',
                '[1,2,3]', '[1,1,1]', '{"":1}', '{"a":null}', '{"a":true,"b":false}',
                '[[],{},[{}]]', '{"a":[1,{"b":[]}]}'):
        out.append({"category": "structure.shape", "doc": doc})
    for _ in range(60):
        obj = {f"k{i}": rng.choice([rng.randrange(-10**20, 10**20), None, True,
                                    rng.uniform(-1e3, 1e3), f"v{i}"]) for i in range(rng.randrange(1, 6))}
        out.append({"category": "structure.random", "doc": json.dumps(obj)})
    return out


def _malformed_entries(rng: random.Random):
    out = []
    fixed = ["", "  ", "01", "00", "-01", "+1", "1.", ".5", "1e", "1e+", "--1", "0x10",
             "0b1", "0o7", "'a'", "{'a':1}", '{"a":1,}', "[1,2,]", "[,1]", '{"a"}',
             '{"a":}', "tru", "fals", "nul", "TRUE", "None", "undefined", "{\"a\":1} x",
             "[1] [2]", "1 2", "\ufeff{\"a\":1}", "{\"a\":1}\ufeff", "//c\n1", "/*c*/1",
             "{\"a\":01}", "[01]", "{\"a\":1,,}", "\"unterminated", "[1,2", "{\"a\":1",
             "\x00", "a", "\u060c", "{,}", "[}", "]", "}", ":", ",", '"\\x41"', '"\\u12"',
             '"\\u"', '"\\"', "[1,2,3", "{\"a\":\"\\ud800\"}"]
    for s in fixed:
        out.append({"category": "malformed", "doc": s})
    for _ in range(60):
        base = json.dumps({"a": rng.randrange(10**6), "b": [1, 2, 3]})
        pos = rng.randrange(0, len(base))
        ch = rng.choice([",", "}", "]", "0", "x", "'", "", " "])
        out.append({"category": "malformed.mutated", "doc": base[:pos] + ch + base[pos:]})
    return out


def build_synthetic_corpus() -> list[dict]:
    rng = random.Random(SEED)
    entries = []
    entries += _int_entries(rng)
    entries += _float_entries(rng)
    entries += _string_entries(rng)
    entries += _structure_entries(rng)
    entries += _malformed_entries(rng)
    entries = _dedup(entries)
    for i, e in enumerate(entries):
        e["id"] = f"s{i:05d}"
        e["origin"] = "synthetic"
    return entries


def build_real_corpus(roots: list[str | Path], limit: int = 400, max_bytes: int = 300_000) -> list[dict]:
    """Collect real *.json documents from installed packages.

    The file is re-serialised with the stdlib so that whitespace is canonical
    and we audit *values*, not formatting.  Files that do not parse are kept
    as-is (they are then interesting malformed samples).
    """
    entries = []
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.json")):
            if len(entries) >= limit:
                break
            try:
                if p.stat().st_size > max_bytes:
                    continue
                raw = p.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                doc = json.dumps(json.loads(text))
            except Exception:
                doc = text
            entries.append({"category": "real.file", "doc": doc, "source": str(p)})
    seen = set()
    out = []
    for e in entries:
        h = sha256_text(e["doc"])
        if h in seen:
            continue
        seen.add(h)
        e["sha256"] = h
        e["origin"] = "real"
        out.append(e)
    for i, e in enumerate(out):
        e["id"] = f"r{i:05d}"
    return out
