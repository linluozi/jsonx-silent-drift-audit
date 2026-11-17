"""Behaviour tests for the JSON cross-implementation silent-drift audit.

Run:  python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jsonxaudit import engine  # noqa: E402
from jsonxaudit.corpus import (  # noqa: E402
    INT64_MIN,
    UINT64_MAX,
    build_real_corpus,
    build_synthetic_corpus,
    sha256_text,
)

BIG = [UINT64_MAX + 1, UINT64_MAX + 2, 2**80, 10**30, 10**77, INT64_MIN - 1, -(10**30)]
# Values that cannot be represented exactly as an IEEE-754 double, so the
# silent coercion loses digits (not just the int type).
NON_EXACT = [UINT64_MAX + 2, INT64_MIN - 1, 10**30, 10**77, -(10**30)]


@unittest.skipUnless("orjson" in engine.MODULES and "ujson" in engine.MODULES
                     and "simplejson" in engine.MODULES,
                     "requires orjson+ujson+simplejson (see README install step)")
class TestAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = build_synthetic_corpus()
        cls.records, cls.summary = engine.run_corpus(cls.entries)

    # --- corpus integrity -------------------------------------------------
    def test_corpus_minimum_and_uniqueness(self):
        hashes = {e["sha256"] for e in self.entries}
        self.assertGreaterEqual(len(self.entries), 1000, "corpus must have >=1000 documents")
        self.assertEqual(len(hashes), len(self.entries), "every document must be sha256-unique")
        self.assertEqual(self.summary["n_unique_sha256"], len(self.entries))

    def test_corpus_covers_boundaries(self):
        cats = {e["category"] for e in self.entries}
        for needed in ("int.boundary", "int.over_uint64", "int.below_int64", "int.huge",
                       "float.nonstandard", "string.unpaired_surrogate", "malformed",
                       "structure.depth"):
            self.assertIn(needed, cats)

    # --- the headline defect ---------------------------------------------
    def test_orjson_loads_silently_coerces_out_of_range_ints(self):
        for n in BIG:
            with self.subTest(n=n):
                value = engine.MODULES["orjson"].loads(str(n))
                self.assertIsInstance(value, float)
        for n in NON_EXACT:
            with self.subTest(non_exact=n):
                self.assertNotEqual(float(n), n, "value loses digits as a double")

    def test_orjson_dumps_refuses_the_same_integers(self):
        import orjson
        for n in BIG:
            with self.subTest(n=n):
                with self.assertRaises(orjson.JSONEncodeError):
                    orjson.dumps(n)

    def test_reference_impls_keep_arbitrary_precision(self):
        for n in BIG:
            with self.subTest(n=n):
                for impl in ("json", "ujson", "simplejson"):
                    value = engine.MODULES[impl].loads(str(n))
                    self.assertIsInstance(value, int)
                    self.assertEqual(value, n)

    def test_integer_asymmetry_metric(self):
        by_id = {e["id"]: e for e in self.entries}
        a = engine.integer_asymmetry(self.records, by_id)
        self.assertGreaterEqual(a["out_of_range_integer_documents"], 400)
        self.assertEqual(a["silently_coerced_to_float"], a["out_of_range_integer_documents"])
        self.assertEqual(a["dumps_raises"], a["out_of_range_integer_documents"])
        self.assertEqual(a["roundtrip_preserves_int"], 0)
        self.assertEqual(a["reencode_equals_input"], 0)
        self.assertEqual(a["agreement_with_stdlib"], 0)
        self.assertEqual(a["silent_coercion_rate"], 1.0)
        self.assertGreater(a["max_relative_error"], 1e-17)
        self.assertGreater(a["max_absolute_error"], 0.0)

    def test_all_silent_divergence_is_attributed_to_the_int_defect(self):
        attr = engine.attribute_divergence(self.records, {e["id"]: e for e in self.entries})
        n_silent = sum(1 for r in self.records if r["silent_divergence"])
        self.assertEqual(n_silent, attr["causes"]["orjson_int_to_float"])
        self.assertEqual(attr["causes"]["other"], 0)
        self.assertEqual(attr["other_examples"], [])

    # --- secondary findings ----------------------------------------------
    def test_ujson_accepts_leading_zero_numbers(self):
        self.assertEqual(engine.MODULES["ujson"].loads("01"), 1)
        with self.assertRaises(json.JSONDecodeError):
            json.loads("01")

    def test_simplejson_accepts_utf8_bom(self):
        doc = "\ufeff{\"a\": 1}"
        self.assertEqual(engine.MODULES["simplejson"].loads(doc), {"a": 1})
        with self.assertRaises(json.JSONDecodeError):
            json.loads(doc)

    def test_orjson_rejects_unpaired_surrogates_loudly(self):
        for doc in ('"\\ud800"', '"\\udc00"'):
            with self.subTest(doc=doc):
                self.assertEqual(engine.probe_impl("orjson", doc).status, "error")
                self.assertEqual(engine.probe_impl("json", doc).status, "ok")

    # --- determinism ------------------------------------------------------
    def test_audit_is_deterministic(self):
        recs2, summ2 = engine.run_corpus(build_synthetic_corpus())
        self.assertEqual(self.summary["corpus_digest"], summ2["corpus_digest"])
        self.assertEqual(self.summary["by_status"], summ2["by_status"])
        self.assertEqual(
            sha256_text(json.dumps(self.summary, sort_keys=True)),
            sha256_text(json.dumps(summ2, sort_keys=True)),
        )

    # --- the guard API ----------------------------------------------------
    def test_guard_flags_hazardous_document(self):
        rec = engine.probe_doc(str(UINT64_MAX + 1))
        self.assertEqual(rec["status"], "value_divergence")
        self.assertTrue(rec["hazard"])
        self.assertEqual(rec["deviates_from_stdlib"], ["orjson"])

    def test_guard_accepts_portable_document(self):
        rec = engine.probe_doc('{"id": 9223372036854775807, "name": "x"}')
        self.assertFalse(rec["hazard"])
        self.assertEqual(rec["status"], "agree")

    # --- real corpus path -------------------------------------------------
    def test_real_corpus_scanning(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.json"
            p.write_text('{"a": 1, "b": [true, null]}', encoding="utf-8")
            (Path(td) / "bad.json").write_text("not json", encoding="utf-8")
            entries = build_real_corpus([td])
            self.assertEqual(len(entries), 2)
            self.assertTrue(all(e["origin"] == "real" for e in entries))
            recs, _ = engine.run_corpus(entries)
            self.assertEqual(len(recs), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
