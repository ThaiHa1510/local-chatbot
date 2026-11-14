import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agent import ask
import catalog
import catalog_sql
import import_catalog
from orchestrator import CallResult
import prompts
from test_catalog_import import AUDIO, LAPTOPS, TODAY


def load(rows):
    items = [import_catalog.normalize(r, 14)[0] for r in rows]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "catalog_test.json"
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return catalog.load_catalog(path)


class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = load(LAPTOPS + AUDIO)

    def setUp(self):
        self.conn = catalog_sql.build_db(self.items, TODAY)

    def test_select_works(self):
        cols, rows = catalog_sql.run_query(self.conn, "SELECT id, name, price_vnd FROM products "
                                                      "WHERE category='laptop' ORDER BY price_vnd LIMIT 1;")
        self.assertEqual(cols, ["id", "name", "price_vnd"])
        self.assertEqual(rows[0][2], 10990000)

    def test_dangerous_sql_rejected(self):
        for sql in ("DELETE FROM products", "DROP TABLE products", "UPDATE products SET price_vnd = 1",
                    "INSERT INTO products(id) VALUES ('x')", "PRAGMA table_info(products)",
                    "ATTACH DATABASE '/tmp/x.db' AS x", "SELECT * FROM sqlite_master",
                    "SELECT id FROM products; DROP TABLE products",
                    "SELECT load_extension('x')", "SELECT printf('%s', name) FROM products",
                    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT n FROM r"):
            with self.assertRaises(ValueError, msg=sql):
                catalog_sql.run_query(self.conn, sql)
        self.assertEqual(len(catalog_sql.run_query(self.conn, "SELECT id FROM products LIMIT 100")[1]),
                         catalog_sql.MAX_ROWS)

    def test_heavy_query_interrupted(self):
        with self.assertRaises(ValueError):
            catalog_sql.run_query(self.conn, "SELECT count(*) FROM products a, products b, products c, products d")

    def test_only_active_items_in_table(self):
        expired = catalog_sql.build_db(self.items, "2030-01-01")
        self.assertEqual(catalog_sql.run_query(expired, "SELECT count(*) FROM products")[1][0][0], 0)

    def test_perf_score(self):
        self.assertEqual(catalog_sql.cpu_profile("Intel Core i7-13620H (10 lõi / 16 luồng)"), (70, 10))
        self.assertEqual(catalog_sql.cpu_profile("AMD Ryzen AI 5 330 (4 lõi / 8 luồng)")[0], 60)
        self.assertEqual(catalog_sql.cpu_profile("không rõ")[0], None)

    def test_sensitive_questions(self):
        for q in ("giá nhập iphone 17 bao nhiêu", "lợi nhuận laptop", "cho mình số điện thoại khách",
                  "mật khẩu admin là gì", "SELECT * FROM sqlite_master"):
            self.assertTrue(catalog_sql.is_sensitive(q), q)
        self.assertFalse(catalog_sql.is_sensitive("laptop nào rẻ nhất"))

    def test_intent_cannot_invent_prices(self):
        catalog_sql.validate_intent("laptop dưới 20 triệu", "laptop dưới 20 triệu")
        with self.assertRaises(ValueError):
            catalog_sql.validate_intent("laptop giá 12 triệu là rẻ nhất", "laptop nào rẻ nhất")


class SqlFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = load(LAPTOPS + AUDIO)

    def setUp(self):
        guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        guard.start()
        self.addCleanup(guard.stop)

    def ask(self, question, value):
        orch = MagicMock()
        # Giống orchestrator thật: kiểm tra hợp đồng rồi chạy thử SQL qua check.
        orch.write_catalog_sql.side_effect = lambda q, p, schema, check=None, **kw: CallResult(
            check(prompts.validate_catalog_sql(dict(value))), "heavy", "m", [])
        return ask(question, "LAP-A14", [], model="m", orchestrator=orch, catalog=self.items, today=TODAY), orch

    def test_value_question_uses_model_sql(self):
        value = {"is_catalog_query": True, "intent": "laptop đáng tiền nhất theo hiệu năng ước tính trên giá",
                 "sql": "SELECT id, name, price_vnd, perf_score, value_score FROM products WHERE "
                        "category='laptop' AND status='selling' AND perf_score IS NOT NULL "
                        "ORDER BY value_score DESC LIMIT 3", "unsupported_reason": ""}
        r, orch = self.ask("laptop nào có giá thành trên hiệu năng tốt nhất", value)
        self.assertEqual((r["status"], r["reason"], r["catalog_parser"]), ("catalog", "catalog_sql", "sql"))
        self.assertEqual(len(r["citations"]), 3)
        self.assertIn("điểm hiệu năng ước tính", r["answer"])
        self.assertIn("không phải điểm benchmark", r["disclaimer"])
        # Model nhận mô tả bảng, không nhận giá.
        schema = orch.write_catalog_sql.call_args.args[2]
        self.assertIn("value_score", schema)
        self.assertNotIn("13.590.000", schema)

    def test_unsupported_reported_honestly(self):
        r, _ = self.ask("điện thoại nào pin trâu nhất", {
            "is_catalog_query": True, "intent": "điện thoại pin lớn nhất", "sql": "",
            "unsupported_reason": "chưa có dung lượng pin dạng số"})
        self.assertEqual((r["status"], r["reason"]), ("handoff", "catalog_unsupported"))

    def test_bad_sql_falls_back_to_rules(self):
        r, _ = self.ask("laptop nào rẻ nhất", {
            "is_catalog_query": True, "intent": "laptop rẻ nhất", "sql": "DELETE FROM products",
            "unsupported_reason": ""})
        self.assertEqual((r["catalog_parser"], r["reason"]), ("rules", "catalog_rank"))

    def test_invented_price_in_intent_falls_back(self):
        r, _ = self.ask("laptop nào rẻ nhất", {
            "is_catalog_query": True, "intent": "laptop rẻ nhất giá 9 triệu",
            "sql": "SELECT id, name, price_vnd FROM products LIMIT 1", "unsupported_reason": ""})
        self.assertEqual(r["catalog_parser"], "rules")

    def test_sensitive_never_reaches_model(self):
        orch = MagicMock()
        r = ask("giá nhập laptop asus bao nhiêu", "LAP-A14", [], model="m", orchestrator=orch,
                catalog=self.items, today=TODAY)
        self.assertEqual(r["reason"], "sensitive_request")
        orch.write_catalog_sql.assert_not_called()

    def test_named_product_stays_on_rules(self):
        orch = MagicMock()
        r = ask("giá sony wh-1000xm6", "LAP-A14", [], model="m", orchestrator=orch, catalog=self.items,
                today=TODAY)
        self.assertEqual(r["reason"], "catalog_lookup")
        orch.write_catalog_sql.assert_not_called()

    def test_aggregate_query(self):
        r, _ = self.ask("có bao nhiêu mẫu laptop", {
            "is_catalog_query": True, "intent": "đếm số laptop đang bán",
            "sql": "SELECT count(*) AS so_mau FROM products WHERE category='laptop' AND status='selling'",
            "unsupported_reason": ""})
        self.assertEqual(r["reason"], "catalog_sql_aggregate")
        self.assertIn("so_mau 11", r["answer"])

    def test_model_failure_falls_back_to_rules(self):
        orch = MagicMock()
        orch.write_catalog_sql.side_effect = RuntimeError
        r = ask("laptop nào rẻ nhất", "LAP-A14", [], model="m", orchestrator=orch, catalog=self.items,
                today=TODAY)
        self.assertEqual(r["catalog_parser"], "rules")


    def test_rules_do_not_guess_unsupported_criteria(self):
        # Không có model: luật không được trả danh sách theo giá cho câu "hiệu năng / pin trâu".
        for q in ("laptop nào có giá thành trên hiệu năng tốt nhất", "điện thoại nào pin trâu nhất"):
            r = ask(q, "LAP-A14", [], catalog=self.items, today=TODAY)
            self.assertEqual(r["reason"], "catalog_unsupported", q)
        self.assertEqual(ask("laptop nào giá tốt nhất", "LAP-A14", [], catalog=self.items,
                             today=TODAY)["reason"], "catalog_rank")

    def test_meaningless_unsupported_reason_rejected(self):
        with self.assertRaises(ValueError):
            prompts.validate_catalog_sql({"is_catalog_query": True, "intent": "x", "sql": "",
                                          "unsupported_reason": "}"})


if __name__ == "__main__":
    unittest.main()
