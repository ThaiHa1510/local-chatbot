import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import ROOT, ask, all_records
import catalog

TODAY = "2026-11-11"


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Dữ liệu giả lập chỉ dùng cho test; bảng giá thật ở data/catalog*.json.
        cls.items = catalog.load_catalog(ROOT / "tests" / "fixtures" / "catalog_mock.json")
        cls.records = all_records()

    def setUp(self):
        # Guardrail NeMo được kiểm tra riêng ở test_security_runtime; ở đây chỉ kiểm tra danh mục.
        self.guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def ask(self, question, product="LAP-A14", **kw):
        return ask(question, product, self.records, catalog=self.items, today=TODAY, **kw)

    def skus(self, result):
        return [c["product"] for c in result["citations"]]

    # ---------------------------------------------------------- câu hỏi chính
    def test_cheapest_laptop(self):
        for question in ("laptop nào giá rẻ nhất", "laptop nao re nhat", "Laptop nào rẻ nhất?"):
            r = self.ask(question)
            self.assertEqual((r["status"], r["reason"]), ("catalog", "catalog_rank"), question)
            self.assertEqual(self.skus(r), ["LAP-B15"])
            self.assertIn("12.490.000đ", r["answer"])
            self.assertEqual(r["disclaimer"], catalog.PRICE_NOTE)

    def test_cheapest_ignores_unapproved_and_discontinued(self):
        r = self.ask("laptop rẻ nhất")
        self.assertNotIn("LAP-Z14", self.skus(r))  # chưa duyệt
        self.assertNotIn("LAP-C13", self.skus(r))  # ngừng bán

    def test_most_expensive(self):
        self.assertEqual(self.skus(self.ask("laptop đắt nhất")), ["LAP-X16"])

    def test_price_range_filters(self):
        self.assertEqual(self.skus(self.ask("điện thoại dưới 10 triệu")), ["PHONE-P4", "PHONE-P6"])
        self.assertEqual(self.skus(self.ask("laptop từ 10 đến 20 triệu")), ["LAP-B15", "LAP-A14"])
        self.assertEqual(self.skus(self.ask("laptop 10-20tr")), ["LAP-B15", "LAP-A14"])
        self.assertEqual(self.skus(self.ask("laptop trên 15tr")), ["LAP-A14", "LAP-X16"])
        self.assertEqual(self.skus(self.ask("laptop rẻ hơn 15 triệu")), ["LAP-B15"])

    def test_no_match_suggests_cheapest(self):
        r = self.ask("laptop dưới 5 triệu")
        self.assertEqual(r["reason"], "catalog_no_match")
        self.assertEqual(self.skus(r), ["LAP-B15"])

    def test_spec_filter_and_sort(self):
        self.assertEqual(self.skus(self.ask("laptop nào RAM 16GB trở lên rẻ nhất")), ["LAP-A14"])
        self.assertEqual(self.skus(self.ask("laptop nào màn hình to nhất")), ["LAP-X16"])
        self.assertEqual(self.skus(self.ask("màn hình nào rẻ nhất")), ["MON-M24"])

    def test_price_lookup_uses_selected_product(self):
        r = self.ask("Giá hôm nay là bao nhiêu?", "PHONE-P6")
        self.assertEqual((r["reason"], self.skus(r)), ("catalog_lookup", ["PHONE-P6"]))
        self.assertIn("9.490.000đ", r["answer"])

    def test_lookup_and_compare_by_name(self):
        self.assertEqual(self.skus(self.ask("giá A14 bao nhiêu", "PHONE-P6")), ["LAP-A14"])
        r = self.ask("so sánh A14 và B15")
        self.assertEqual(r["reason"], "catalog_compare")
        self.assertIn("6.500.000đ", r["answer"])

    def test_works_for_product_without_documents(self):
        # Câu hỏi danh mục không cần SKU có trong kho tài liệu.
        self.assertEqual(self.ask("laptop rẻ nhất", "UNKNOWN")["status"], "catalog")

    def test_policy_questions_keep_document_flow(self):
        for question in ("laptop bảo hành bao lâu", "phí giao hàng bao nhiêu tiền", "đổi trả thế nào"):
            self.assertNotEqual(self.ask(question)["mode"], "catalog", question)

    def test_without_catalog_price_still_hands_off(self):
        r = ask("Giá hôm nay là bao nhiêu?", "PHONE-P6", self.records)
        self.assertEqual(r["status"], "handoff")

    # ----------------------------------------------------------- an toàn dữ liệu
    def test_expired_prices_hand_off(self):
        r = ask("laptop rẻ nhất", "LAP-A14", self.records, catalog=self.items, today="2030-01-01")
        self.assertEqual((r["status"], r["reason"]), ("handoff", "catalog_missing"))
        self.assertFalse(r["citations"])

    def test_conflicting_prices_hand_off(self):
        items = copy.deepcopy(self.items)
        twin = copy.deepcopy(next(i for i in items if i["sku"] == "LAP-B15"))
        twin.update(id="CAT-LAP-B15-NEW", price_vnd=11990000)
        items.append(twin)
        r = ask("laptop rẻ nhất", "LAP-A14", self.records, catalog=items, today=TODAY)
        self.assertEqual(r["reason"], "conflicting_sources")

    def test_invalid_catalog_rejected(self):
        bad = copy.deepcopy(self.items)
        bad[0]["price_vnd"] = "18 triệu"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                catalog.load_catalog(path)

    def test_money_parsing(self):
        cases = {("15", "tr"): 15_000_000, ("15,5", "trieu"): 15_500_000, ("15.990.000", "d"): 15_990_000,
                 ("500", "k"): 500_000, ("15", None): 15_000_000, ("2", "cu"): 2_000_000}
        for args, expected in cases.items():
            self.assertEqual(catalog.parse_money(*args), expected, args)

    def test_spec_units_are_not_prices(self):
        self.assertNotIn("price_min", catalog.detect_filters("laptop ram tren 16 gb"))


if __name__ == "__main__":
    unittest.main()
