import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent import ROOT, all_records, ask, load_records, retrieve, select_evidence


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.records = json.loads((ROOT / "data/mock_knowledge.json").read_text())

    def test_warranty_and_product_isolation(self):
        r = ask("Sản phẩm được bảo hành bao lâu?", "LAP-A14", self.records, today="2026-11-05")
        self.assertEqual(r["status"], "search_results")
        self.assertIn("24 tháng", r["citations"][0]["text"])
        self.assertTrue(all(c["product"] == "LAP-A14" for c in r["citations"]))

    def test_expired_and_unapproved_excluded(self):
        rows = retrieve("bảo hành đổi trả", "LAP-A14", self.records, today="2026-11-05")
        self.assertTrue(all(r["approved"] and r["valid_to"] >= "2026-11-05" for r in rows))

    def test_unknown_price_abstains(self):
        self.assertEqual(ask("Giá hôm nay là bao nhiêu?", "PHONE-P6", self.records)["status"], "handoff")

    def test_unknown_product_abstains(self):
        self.assertEqual(ask("bảo hành", "UNKNOWN", self.records)["reason"], "product_required")

    def test_conflict_abstains(self):
        self.assertEqual(ask("bảo hành bao lâu", "DEMO-CONFLICT", self.records)["reason"],
                         "conflicting_sources")

    def test_no_current_documents(self):
        self.assertEqual(ask("bảo hành", "LAP-A14", self.records, today="2030-01-01")["status"], "handoff")

    def test_model_failure_fails_closed(self):
        with patch("agent.select_evidence", side_effect=TimeoutError):
            result = ask("bảo hành bao lâu", "LAP-A14", self.records, "local")
        self.assertEqual(result["reason"], "model_unavailable")
        self.assertFalse(result["citations"])

    def test_model_cannot_add_generated_facts(self):
        candidate = retrieve("bảo hành bao lâu", "LAP-A14", self.records)[0]
        with patch("agent.select_evidence", return_value={
                "ids": [candidate["id"]], "conflict": False, "insufficient": False}):
            result = ask("bảo hành bao lâu", "LAP-A14", self.records, "local")
        self.assertEqual(result["citations"][0]["text"], candidate["text"])

    def test_model_abstention(self):
        with patch("agent.select_evidence", return_value={
                "ids": [], "conflict": False, "insufficient": True}):
            self.assertEqual(ask("bảo hành", "LAP-A14", self.records, "local")["status"], "handoff")

    def test_atomic_ingest_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "kb.sqlite3"
            load_records(ROOT / "data/mock_knowledge.json", db)
            bad = copy.deepcopy(self.records)
            bad[0]["valid_to"] = "invalid"
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                load_records(path, db)
            self.assertEqual(len(all_records(db)), len(self.records))

    def test_empty_question(self):
        with self.assertRaises(ValueError):
            ask("", "LAP-A14", self.records)

    def test_fabricated_source_is_rejected(self):
        raw = {"done": True, "message": {"content": json.dumps({
            "ids": ["INVENTED"], "insufficient": False, "conflict": False})}}
        with patch("agent.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(raw).encode())
            with self.assertRaises(ValueError):
                select_evidence("bảo hành", self.records[:1], "test")

    def test_invalid_boolean_is_rejected(self):
        raw = {"done": True, "message": {"content": json.dumps({
            "ids": [], "insufficient": "false", "conflict": False})}}
        with patch("agent.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(raw).encode())
            with self.assertRaises(ValueError):
                select_evidence("bảo hành", self.records[:1], "test")


if __name__ == "__main__":
    unittest.main()
