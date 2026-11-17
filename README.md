# jsonx-silent-drift-audit

A measured, byte-level comparison of four JSON parsers — CPython `json`, **orjson**, `ujson`,
`simplejson` — on one question: **which differences are *silent* (parsing succeeds and returns a
different value / type) rather than loud (an exception)?** The headline result is a reproducible
asymmetry in orjson 3.13.0.

Upstream: https://github.com/ijl/orjson

> This is an **audit/measurement study, not a performance optimization**. There is no patch, no
> speedup claim, and no `-opt-lab` promise in this repository. The deliverable is a differential
> engine, a guard CLI, and raw evidence.

## Headline finding: orjson's out-of-range integers are asymmetric

orjson documents its integer range (`README.md` §int: signed 64-bit min to unsigned 64-bit max,
`-9223372036854775807 … 18446744073709551615`). The *behaviour outside* that range is not
symmetric between the two directions, and `orjson.loads` exposes no option to change it:

```python
>>> import orjson
>>> orjson.loads("18446744073709551617")     # 2**64 + 1, legal RFC 8259 integer
1.8446744073709552e+19                        # silently a FLOAT, no exception
>>> orjson.dumps(2**64 + 1)
orjson.JSONEncodeError: Integer exceeds 64-bit range   # loud on the way out
```

Measured on **428 sha256-unique out-of-range integer documents** (the audit corpus):

| quantity | value |
| --- | --- |
| out-of-range integer documents | 428 |
| `loads` returned a float silently | **428 / 428 (100%)** |
| `loads` raised | 0 |
| `dumps` of the same integer raised `JSONEncodeError` | **428 / 428 (100%)** |
| round-trip preserved the integer | **0 / 428** |
| re-encoded text identical to the input literal | **0 / 428** |
| stdlib `json` / `ujson` / `simplejson` returned an exact `int` | 428 / 428 each |

The worst corpus member loses **5.96e182** in absolute terms; the relative error is bounded by the
IEEE-754 half-ulp floor (`max_relative_error = 1.110e-16`) because the value is merely *rounded into
a double* — the point is that it happens **without any signal**. This is reachable for any legal
JSON integer literal above `2**64-1` (128-bit IDs, arbitrary-precision numeric payloads, wei-style
amounts), and for negatives below `-2**63`.

Secondary, smaller divergences (all measured, all in `evidence/raw/grammar-laxness.jsonl`):

| document | accepted by | value |
| --- | --- | --- |
| `01`, `00`, `-01` | ujson | `1`, `0`, `-1` (stdlib/orjson/simplejson reject) |
| `1.` | ujson | `1.0` |
| `{"a":01}`, `[01]` | ujson | nested leading zero accepted |
| `\ufeff{"a":1}` (UTF-8 BOM) | simplejson | `{"a": 1}` (others reject) |

## Cross-implementation matrix

1180 sha256-unique synthetic documents (seed 20261117), plus 268 real `*.json` files harvested from
installed packages and re-serialised with the stdlib.

| corpus | documents (sha256-unique) | agree | silent value divergence | error mix | all error |
| --- | --- | --- | --- | --- | --- |
| synthetic (adversarial + boundaries) | 1180 | 629 | **452** | 21 | 78 |
| real (installed packages) | 268 | 268 | 0 | 0 | 0 |

Every one of the 452 synthetic silent divergences is attributed to the big-int coercion:
`divergence_attribution = {orjson_int_to_float: 452, other: 0}` with zero unattributed examples.
`ujson` and `simplejson` match the stdlib *value* on every document they accept; their
disagreements are all loud (accept vs. reject), never silent value changes.

**Honest limitation**: the 268 real-world documents contain no out-of-range integers, so the
real-corpus divergence rate is **0**. The finding is a hazard *class* demonstrated on
adversarial-but-legal inputs and on 24 random structures that embed out-of-range integers — it is
not a claim that ordinary `package.json`-style files already break.

## 本轮创新

First time the JSON cross-implementation *value* axis is classified by **silent vs loud** rather
than by "does it raise", and the first measurement of orjson's **loads/dumps asymmetry** on
out-of-range integers: the same library that raises `JSONEncodeError` on serialising a 65-bit
integer silently rounds it to a float when parsing one, with no option to make `loads` strict.
Nothing in the innovation ledger (or in orjson's issues/README) documents the parse-side silent
coercion; the README only states the supported range. The tool also settles a broader question —
across 1180 unique documents, orjson is the *only* implementation that silently changes parsed
values; ujson/simplejson diverge only by accepting documents the stdlib rejects.

## Quality gates (G1–G5 mapping for a no-patch audit)

| gate | status | evidence / command |
| --- | --- | --- |
| G1 upstream tests | **N/A** — no upstream source is modified, so there is no patched tree to retest. orjson's suite is Rust/`cargo` and was not run. | — |
| G2 differential consistency | **PASS** — 1180 unique documents × 4 implementations; audit is bit-reproducible (summary sha256 below); 0 unattributed divergences; 14/14 tests. | `python3 -m unittest discover -s tests -t .` |
| G3 / G3b speedup | **N/A** — no speedup is claimed or measured. | — |
| G4 large-scale regression | **N/A** — no optimization. | — |
| G5 memory | **N/A** — no optimization. | — |

Reproducibility: running the audit twice produces a byte-identical `evidence/summary.json`
(`report_sha256 = 70e2a2cea398a2585d638a45e2366f0c4f7876815fcc8a10ff793abd160351fe`). Raw digests:
`synthetic.jsonl cd9a5a9f…`, `real.jsonl 274b7659…`, `orjson-int-boundary.jsonl d4b04d8f…`,
`grammar-laxness.jsonl e39edf4d…` (full values in `evidence/summary.json`).

## Reproduce

```bash
python3 -m venv .venv && .venv/bin/pip install orjson ujson simplejson
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python -m jsonxaudit audit --real-dir /usr/lib/python3/dist-packages --real-limit 400
```

Guard any document (exit code 1 when implementations disagree):

```bash
echo '18446744073709551617' > /tmp/doc.json && .venv/bin/python -m jsonxaudit guard /tmp/doc.json
```

## Layout

```
jsonxaudit/corpus.py   deterministic corpus builder (sha256 de-dup, seed 20261117)
jsonxaudit/engine.py   differential engine, canonical fingerprint, attribution
jsonxaudit/cli.py      audit / guard / show
tests/test_audit.py    14 behaviour tests
evidence/summary.json  aggregate result + raw digests
evidence/raw/*.jsonl   per-document raw outcomes (1.2 MB + 0.2 MB + 0.07 MB)
```

## Environment

- CPU: AMD EPYC 7K62 4 vCPU; memory 3723 MB; CPython 3.12.3 (no 3.10/3.11/PyPy installed, so no
  interpreter cross-check).
- orjson 3.13.0, ujson 6.1.0, simplejson 4.3.0, stdlib `json` (3.12.3).
- Corpus seed: 20261117. No timing is involved, so no `perf_counter`/warm-up applies.
- Exceptions are observations, not failures: `malformed` documents are *expected* to raise.

## License

MIT for this repository's code (see `LICENSE`). The audited libraries keep their own licenses —
orjson is Apache-2.0; see `ujson` and `simplejson` `LICENSE.txt` for theirs. No upstream code is
vendored or modified here.
