"""Hỏi thông số của một sản phẩm cụ thể phải trả bảng thông số của đúng máy đó."""
import unittest

import catalog


def item(sku, name, price, specs=None, details=None, aliases=(), manufacturer="An Việt"):
    return {"id": "CAT-" + sku, "sku": sku, "category": "laptop", "name": name, "aliases": list(aliases),
            "price_vnd": price, "original_price_vnd": None, "status": "selling", "specs": specs or {},
            "details": details or {}, "manufacturer": manufacturer, "source": "x", "valid_from": "2020-01-01",
            "valid_to": "2099-12-31", "approved": True, "mock": True}


ITEMS = [
    item("TB-15A", "Laptop Thunderobot T-Book 15A", 13590000, {"ram_gb": 8, "battery_wh": 45.6},
         {"cpu": "AMD Ryzen 3 3200U", "ram_type": "DDR4", "battery": "4000mAh, 45.6Wh",
          "ports": "1 x USB-A 2.0 2 x USB-A 3.0 1 x HDMI"}, ["thunderobot t-book 15a"], "Thunderobot"),
    item("PODS-WIRED", "Tai nghe AirPods 5 Sạc Có Dây", 3790000, aliases=["apple airpods 5 sac co day"],
         manufacturer="Apple"),
    item("PODS-WIRELESS", "Tai nghe AirPods 5 Sạc Không Dây", 4290000, aliases=["apple airpods 5 sac khong day"],
         manufacturer="Apple"),
]


class SpecQuestionTests(unittest.TestCase):
    def parse(self, q):
        return catalog.parse(q, "", ITEMS, today="2026-11-14")

    def test_spec_questions_route_to_specs(self):
        for q in ["thông số kỹ thuật Laptop Thunderobot T-Book 15A", "T-Book 15A dùng chip gì?",
                  "thunderobot t-book 15a cấu hình thế nào"]:
            spec = self.parse(q)
            self.assertEqual((spec["kind"], spec["skus"]), ("specs", ["TB-15A"]), q)

    def test_price_and_rank_questions_unchanged(self):
        self.assertEqual(self.parse("giá T-Book 15A")["kind"], "lookup")
        self.assertNotEqual(self.parse("laptop rẻ nhất")["kind"], "specs")

    def test_variant_choice_and_brandless_alias(self):
        self.assertEqual(self.parse("airpods 5 sạc có dây có chống ồn không")["skus"], ["PODS-WIRED"])
        self.assertEqual(self.parse("airpods 5 sạc không dây pin bao lâu")["skus"], ["PODS-WIRELESS"])
        self.assertEqual(self.parse("so sánh airpods 5 sạc có dây và airpods 5 sạc không dây")["kind"], "compare")

    def test_spec_sheet_content(self):
        r = catalog.answer(self.parse("T-Book 15A dùng chip gì?"), ITEMS, today="2026-11-14")
        self.assertEqual((r["status"], r["reason"]), ("catalog", "catalog_specs"))
        self.assertIn("CPU: AMD Ryzen 3 3200U", r["answer"])
        self.assertIn("RAM: 8 GB (DDR4)", r["answer"])
        self.assertEqual(r["answer"].count("Pin:"), 1)  # không lặp Wh và mô tả pin
        self.assertIn("1 x USB-A 2.0; 2 x USB-A 3.0; 1 x HDMI", r["answer"])


if __name__ == "__main__":
    unittest.main()
