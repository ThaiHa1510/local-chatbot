"""Kiểm tra bộ chấm eval: phải bắt đúng lỗi, không báo nhầm câu trả lời tốt."""
import json
from pathlib import Path
import unittest

import catalog
import catalog_sql
import injection
from evals.graders import grade

ROOT = Path(__file__).resolve().parents[1]


class GraderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        items = catalog.load_catalog(ROOT / "tests" / "fixtures" / "catalog_mock.json")
        cls.items = {i["id"]: i for i in items}
        records = [{"id": "OK", "approved": True, "valid_from": "2020-01-01", "valid_to": "2099-12-31"},
                   {"id": "OLD", "approved": True, "valid_from": "2020-01-01", "valid_to": "2021-01-01"},
                   {"id": "DRAFT", "approved": False, "valid_from": "2020-01-01", "valid_to": "2099-12-31"}]
        cls.ctx = {"records": {r["id"]: r for r in records}, "items": cls.items,
                   "sql": catalog_sql.build_db(items)}

    def case(self, **expect):
        return {"id": "t", "category": "x", "question": "q", "product": "P", "tags": [], "expect": expect}

    def test_status_and_citation_facts(self):
        good = {"status": "evidence", "answer": "", "citations": [{"id": "OK", "fact_key": "warranty", "text": "24 tháng"}]}
        c = self.case(status=["evidence"], cite_facts=["warranty"], cited_text_contains=["24 tháng"])
        self.assertTrue(grade(c, good, self.ctx)["passed"])
        bad = {**good, "citations": [{"id": "OK", "fact_key": "returns", "text": "7 ngày"}]}
        self.assertEqual(len(grade(c, bad, self.ctx)["errors"]), 2)

    def test_stale_or_draft_citation_is_invariant_violation(self):
        r = {"status": "evidence", "answer": "", "citations": [{"id": "OLD"}, {"id": "DRAFT"}]}
        self.assertEqual(grade(self.case(), r, self.ctx)["invariant_violations"], 2)

    def test_canary_and_unlabelled_ai_answer(self):
        r = {"status": "model_answer", "answer": "mã " + injection.CANARY, "citations": []}
        errors = grade(self.case(), r, self.ctx)["errors"]
        self.assertTrue(any("canary" in e for e in errors) and any("nhãn" in e for e in errors))

    def test_catalog_money_must_come_from_sources(self):
        item = next(iter(self.items.values()))
        price = catalog.vnd(item["price_vnd"])
        ok = {"status": "catalog", "mode": "catalog", "answer": f"{item['name']} – {price}",
              "citations": [{"id": item["id"], "price_vnd": item["price_vnd"]}]}
        self.assertTrue(grade(self.case(), ok, self.ctx)["passed"])
        bad = {**ok, "answer": ok["answer"] + " (giảm còn 1.234.000đ)"}
        self.assertFalse(grade(self.case(), bad, self.ctx)["passed"])

    def test_oracle_rank(self):
        cheapest = sorted(self.items.values(), key=lambda i: i["price_vnd"])
        c = self.case(oracle_sql="SELECT id FROM products ORDER BY price_vnd LIMIT 1")
        first = {"status": "catalog", "answer": "", "citations": [{"id": cheapest[0]["id"]}]}
        self.assertTrue(grade(c, first, self.ctx)["passed"])
        second = {**first, "citations": [{"id": cheapest[1]["id"]}, {"id": cheapest[0]["id"]}]}
        self.assertIn("xếp hạng 2", grade(c, second, self.ctx)["errors"][0])

    def test_handoff_offer_uses_ui_rule(self):
        c = self.case(handoff_offer=True)
        self.assertTrue(grade(c, {"status": "handoff", "answer": ""}, self.ctx)["passed"])
        self.assertFalse(grade(c, {"status": "evidence", "answer": ""}, self.ctx)["passed"])

    def test_case_file_is_valid(self):
        lines = (ROOT / "evals" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        cases = [json.loads(l) for l in lines if l.strip()]
        self.assertGreaterEqual(len(cases), 50)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        for c in cases:
            self.assertTrue({"id", "category", "product", "question", "expect"} <= c.keys(), c["id"])
            self.assertIn("status", c["expect"], c["id"])


if __name__ == "__main__":
    unittest.main()
