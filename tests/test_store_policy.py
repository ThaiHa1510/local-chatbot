import json
import unittest
from unittest.mock import patch
from agent import ROOT, STORE_SCOPE, ask, retrieve


class StorePolicyTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch('agent.inspect_input', return_value={'allowed': True, 'reason': 'ok'})
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.records = json.loads((ROOT / "data/mock_knowledge.json").read_text())

    def test_shipping_available_for_every_product(self):
        for sku in ("LAP-A14", "PHONE-P6", "AUDIO-B2", "MON-M27"):
            result = ask("Có giao hàng tận nơi không?", sku, self.records)
            self.assertEqual(result["status"], "search_results")
            self.assertIn("shipping_scope", {c["fact_key"] for c in result["citations"]})
            self.assertTrue(all(c["product"] == STORE_SCOPE for c in result["citations"]))

    def test_address_and_opening_hours_are_both_covered(self):
        result = ask("Địa chỉ và giờ mở cửa của cửa hàng?", "LAP-A14", self.records)
        keys = {c["fact_key"] for c in result["citations"]}
        self.assertTrue({"store_address", "opening_hours"} <= keys)

    def test_store_location_phrasings_are_recognized(self):
        questions = (
            "Danh sách cửa hàng",
            "Các chi nhánh",
            "Cửa hàng gần đây",
            "Hệ thống cửa hàng",
        )
        for question in questions:
            with self.subTest(question=question):
                result = ask(question, "LAP-A14", self.records)
                self.assertEqual(result["status"], "search_results")
                self.assertIn("store_address",
                              {citation["fact_key"] for citation in result["citations"]})
                self.assertTrue(all(citation["product"] == STORE_SCOPE
                                    for citation in result["citations"]))

    def test_store_policy_does_not_leak_other_product_specs(self):
        rows = retrieve("Thông số kỹ thuật sản phẩm", "LAP-A14", self.records)
        self.assertTrue(rows)
        self.assertTrue(all(r["product"] in {"LAP-A14", STORE_SCOPE} for r in rows))
        self.assertFalse(any(r["product"] == "PHONE-P6" for r in rows))

    def test_price_still_abstains(self):
        result = ask("Giá hôm nay bao nhiêu?", "PHONE-P6", self.records)
        self.assertEqual(result["status"], "handoff")

    def test_store_scope_is_not_a_selectable_product(self):
        result = ask("Địa chỉ cửa hàng?", STORE_SCOPE, self.records)
        self.assertEqual(result["reason"], "product_required")

    def test_sensitive_data_guidance_is_retrievable(self):
        rows = retrieve("Có nên gửi mã OTP vào chatbot không?", "AUDIO-B2", self.records)
        self.assertIn("privacy", {r["fact_key"] for r in rows})


if __name__ == "__main__":
    unittest.main()
