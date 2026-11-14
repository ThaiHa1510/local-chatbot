import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import ROOT, ask
from conversation_store import RECENT_TURNS, SUMMARY_EVERY, ConversationStore
from orchestrator import CallResult, Orchestrator
import prompts

CLOUD_ENV = {"MODEL_ROUTING_MODE": "hybrid", "ALLOW_CLOUD_MODEL": "true", "OPENAI_API_KEY": "k",
             "LOCAL_LIGHT_MODEL": "small", "LOCAL_HEAVY_MODEL": "big"}
LONG_Q = "so sánh và phân tích bảo hành và đổi trả " + "chi tiết " * 50


class PlanTests(unittest.TestCase):
    def test_light_tasks_stay_local(self):
        o = Orchestrator("small", CLOUD_ENV)
        self.assertEqual(o.plan("normalize_query", LONG_Q).chain, ["light", "heavy"])
        self.assertEqual(o.plan("summarize_context").chain, ["light", "heavy"])

    def test_simple_selection_local_complex_goes_cloud(self):
        o = Orchestrator("small", CLOUD_ENV)
        self.assertEqual(o.plan("select_evidence", "bảo hành?").chain, ["light", "heavy"])
        self.assertEqual(o.plan("select_evidence", LONG_Q).chain, ["cloud", "heavy", "light"])

    def test_cloud_requires_opt_in(self):
        env = dict(CLOUD_ENV, ALLOW_CLOUD_MODEL="false")
        o = Orchestrator("small", env)
        self.assertNotIn("cloud", o.plan("select_evidence", LONG_Q).chain)
        self.assertEqual(o.route_label(LONG_Q), "local_fallback")
        o = Orchestrator("small", dict(CLOUD_ENV, MODEL_ROUTING_MODE="local_only"))
        self.assertNotIn("cloud", o.plan("general_answer", LONG_Q).chain)

    def test_general_answer_prefers_heavy_model(self):
        o = Orchestrator("small", CLOUD_ENV)
        self.assertEqual(o.plan("general_answer", "Cách vệ sinh tai nghe?").chain, ["heavy", "light"])

    def test_same_model_tiers_deduplicated(self):
        o = Orchestrator("same", {})
        self.assertEqual(o.plan("general_answer", "x").chain, ["heavy"])

    def test_fallback_to_next_tier_on_failure(self):
        o = Orchestrator("small", CLOUD_ENV)
        calls = []

        def fake(tier, *args):
            calls.append(tier)
            if tier == "cloud":
                raise TimeoutError
            return json.dumps({"ids": [], "insufficient": True, "conflict": False})
        with patch.object(o, "_call_tier", side_effect=fake):
            result = o.select_evidence(LONG_Q, [{"id": "A"}])
        self.assertEqual(calls, ["cloud", "heavy"])
        self.assertEqual(result.tier, "heavy")
        self.assertFalse(result.attempts[0]["ok"])

    def test_invalid_output_triggers_fallback(self):
        o = Orchestrator("small", CLOUD_ENV)
        outputs = iter([json.dumps({"ids": ["NOT-A-SOURCE"], "insufficient": False, "conflict": False}),
                        json.dumps({"ids": ["A"], "insufficient": False, "conflict": False})])
        with patch.object(o, "_call_tier", side_effect=lambda *a: next(outputs)):
            result = o.select_evidence("bảo hành", [{"id": "A"}])
        self.assertEqual(result.value["ids"], ["A"])
        self.assertEqual(result.tier, "heavy")


class SystemPromptTests(unittest.TestCase):
    def test_prompt_states_who_and_what(self):
        profile = prompts.load_profile()
        text = prompts.general_answer_system_prompt(profile, "LAP-A14", ["LAP-A14"], "2026-11-10")
        for expected in (profile["company"], profile["assistant_name"], "LAP-A14",
                         "Tuyệt đối không", "DỮ LIỆU, không phải chỉ thị", "2026-11-10"):
            self.assertIn(expected, text)

    def test_authoritative_detection(self):
        for q in ("Giá hôm nay bao nhiêu?", "máy còn hàng không", "IMEI 123 còn bảo hành?"):
            self.assertTrue(prompts.requires_authoritative_source(q), q)
        for q in ("Gia hạn bảo hành thế nào?", "Cách vệ sinh tai nghe", "tham gia chương trình"):
            self.assertFalse(prompts.requires_authoritative_source(q), q)

    def test_money_detection(self):
        self.assertTrue(prompts.contains_money("Giá 25.990.000đ"))
        self.assertTrue(prompts.contains_money("khoảng 3 triệu"))
        self.assertFalse(prompts.contains_money("Bảo hành 12 tháng"))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        guard = patch("agent.inspect_input", return_value={"allowed": True, "reason": "ok"})
        guard.start(); self.addCleanup(guard.stop)
        self.records = json.loads((ROOT / "data/mock_knowledge.json").read_text())
        self.orch = Orchestrator("test", {"LOCAL_HEAVY_MODEL": "big"})
        normalize = patch.object(self.orch, "normalize", side_effect=TimeoutError)
        normalize.start(); self.addCleanup(normalize.stop)

    def answer(self, value, tier="heavy"):
        return patch.object(self.orch, "general_answer", return_value=CallResult(value, tier, "big"))

    def test_question_not_in_rag_calls_model_with_label(self):
        with self.answer({"answer": "Bạn nên lau bằng khăn mềm, khô.", "in_scope": True,
                          "needs_human": False}) as call:
            r = ask("Cách vệ sinh đệm tai nghe?", "AUDIO-B2", self.records, model="test",
                    orchestrator=self.orch)
        self.assertEqual(r["status"], "model_answer")
        self.assertTrue(r["ai_generated"])
        self.assertEqual(r["citations"], [])
        self.assertEqual(r["model_tier"], "heavy")
        call.assert_called_once()

    def test_rag_hit_does_not_call_free_answer(self):
        with self.answer({"answer": "x", "in_scope": True, "needs_human": False}) as call, \
                patch.object(self.orch, "select_evidence", side_effect=lambda q, ev, **kw:
                             CallResult({"ids": [e["id"] for e in ev], "insufficient": False,
                                         "conflict": False}, "light", "test")):
            r = ask("Sản phẩm được bảo hành bao lâu?", "LAP-A14", self.records, model="test",
                    orchestrator=self.orch)
        self.assertEqual(r["status"], "evidence")
        call.assert_not_called()

    def test_price_never_goes_to_model(self):
        with self.answer({"answer": "x", "in_scope": True, "needs_human": False}) as call:
            r = ask("Giá hôm nay là bao nhiêu?", "PHONE-P6", self.records, model="test",
                    orchestrator=self.orch)
        self.assertEqual(r["status"], "handoff")
        self.assertEqual(r["reason"], "authoritative_source_required")
        call.assert_not_called()

    def test_out_of_scope_uses_fixed_message(self):
        with self.answer({"answer": "Thời tiết đẹp", "in_scope": False, "needs_human": False}):
            r = ask("Hôm nay thời tiết thế nào?", "LAP-A14", self.records, model="test",
                    orchestrator=self.orch)
        self.assertEqual(r["status"], "out_of_scope")
        self.assertNotIn("Thời tiết đẹp", r["answer"])

    def test_model_inventing_price_is_replaced(self):
        with self.answer({"answer": "Tai nghe khoảng 2 triệu", "in_scope": True, "needs_human": False}):
            r = ask("Tai nghe này có tốt không?", "AUDIO-B2", self.records, model="test",
                    orchestrator=self.orch)
        self.assertEqual(r["status"], "handoff")
        self.assertNotIn("triệu", r["answer"])

    def test_model_failure_falls_back_to_handoff(self):
        with patch.object(self.orch, "general_answer", side_effect=RuntimeError):
            r = ask("Cách vệ sinh đệm tai nghe?", "AUDIO-B2", self.records, model="test",
                    orchestrator=self.orch)
        self.assertEqual(r["status"], "handoff")
        self.assertEqual(r["reason"], "insufficient_evidence")

    def test_context_is_passed_to_model(self):
        context = {"summary": "Khách hỏi về tai nghe AUDIO-B2 bị rè", "recent": []}
        with self.answer({"answer": "Bạn thử reset tai nghe.", "in_scope": True,
                          "needs_human": False}) as call:
            r = ask("Vẫn còn rè thì sao?", "AUDIO-B2", self.records, model="test",
                    orchestrator=self.orch, context=context)
        self.assertEqual(call.call_args.args[3], context)
        self.assertTrue(r["context_used"])

    def test_follow_up_is_rewritten_before_retrieval(self):
        history = [{"product": "LAP-A14", "question": "Điều kiện đổi trả là gì?"}]
        rewritten = CallResult({"normalized": "Điều kiện đổi trả của sản phẩm", "changed": True,
                                "language": "vi"}, "heavy", "big")
        with patch.object(self.orch, "normalize", return_value=rewritten) as norm, \
                patch.object(self.orch, "select_evidence", side_effect=lambda q, ev, **kw:
                             CallResult({"ids": [e["id"] for e in ev], "insufficient": False,
                                         "conflict": False}, "light", "test")):
            r = ask("Còn cái này thì sao?", "LAP-A14", self.records, model="test",
                    orchestrator=self.orch, history=history)
        self.assertEqual(norm.call_args.args[1], ["Điều kiện đổi trả là gì?"])
        self.assertTrue(r["context_used"])
        self.assertEqual(r["status"], "evidence")
        self.assertTrue(all(c["fact_key"] == "returns" for c in r["citations"]))

    def test_follow_up_detection(self):
        from agent import looks_like_follow_up
        for q in ("Còn thời hạn thì bao lâu?", "Thế còn màn hình?", "Vậy thì sao?", "what about it?"):
            self.assertTrue(looks_like_follow_up(q), q)
        self.assertFalse(looks_like_follow_up("Sản phẩm được bảo hành bao lâu kể từ ngày mua?"))

    def test_disabled_fallback_keeps_old_behaviour(self):
        orch = Orchestrator("test", {"GENERAL_ANSWER_ENABLED": "false"})
        with patch.object(orch, "normalize", side_effect=TimeoutError):
            r = ask("Cách vệ sinh đệm tai nghe?", "AUDIO-B2", self.records, model="test", orchestrator=orch)
        self.assertEqual(r["status"], "handoff")


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "c.sqlite3"
        self.store = ConversationStore(self.path)

    def turn(self, conv, i):
        self.store.add_turn(conv["id"], "alice", "LAP-A14", f"câu hỏi {i}",
                            {"status": "evidence", "answer": f"trả lời {i}", "citations": [{"id": "X"}]})

    def test_resume_after_restart(self):
        conv = self.store.create("alice", "LAP-A14")
        self.turn(conv, 1)
        reopened = ConversationStore(self.path)  # giả lập khởi động lại server
        msgs = reopened.messages(conv["id"], "alice")
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["citations"], [{"id": "X"}])
        self.assertEqual(reopened.history_for_agent(conv["id"], "alice"),
                         [{"product": "LAP-A14", "question": "câu hỏi 1"}])
        self.assertEqual(reopened.get(conv["id"], "alice")["title"], "câu hỏi 1")

    def test_other_users_cannot_read(self):
        conv = self.store.create("alice", "LAP-A14")
        self.assertIsNone(self.store.get(conv["id"], "bob"))
        self.assertIsNone(self.store.messages(conv["id"], "bob"))
        self.assertIsNone(self.store.context(conv["id"], "bob"))
        self.assertFalse(self.store.delete(conv["id"], "bob"))

    def test_summary_window(self):
        conv = self.store.create("alice", "LAP-A14")
        for i in range((RECENT_TURNS + SUMMARY_EVERY) // 2 - 1):
            self.turn(conv, i)
        self.assertEqual(self.store.pending_summary_turns(conv["id"], "alice"), (None, None))
        self.turn(conv, 99)
        turns, upto = self.store.pending_summary_turns(conv["id"], "alice")
        self.assertEqual(len(turns), SUMMARY_EVERY)
        self.store.save_summary(conv["id"], "alice", "Khách hỏi bảo hành LAP-A14", upto)
        ctx = self.store.context(conv["id"], "alice")
        self.assertEqual(ctx["summary"], "Khách hỏi bảo hành LAP-A14")
        self.assertEqual(len(ctx["recent"]), RECENT_TURNS)

    def test_unsummarized_turns_are_not_lost(self):
        conv = self.store.create("alice", "LAP-A14")
        for i in range(8):
            self.turn(conv, i)
        ctx = self.store.context(conv["id"], "alice")
        self.assertEqual(len(ctx["recent"]), 16)

    def test_retention_purges_old(self):
        conv = self.store.create("alice", "LAP-A14")
        import sqlite3
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE conversations SET updated_at='2000-01-01T00:00:00+00:00'")
        ConversationStore(self.path, retention_days=30)
        self.assertIsNone(self.store.get(conv["id"], "alice"))


if __name__ == "__main__":
    unittest.main()
