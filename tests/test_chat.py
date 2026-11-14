import json
import unittest
from unittest.mock import patch
from agent import ROOT, ask, retrieve, conversation_question


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch('agent.inspect_input', return_value={'allowed': True, 'reason': 'ok'})
        self.guard.start(); self.addCleanup(self.guard.stop)
        self.records = json.loads((ROOT / 'data/mock_knowledge.json').read_text())

    def test_reported_multi_topic_question_all_products(self):
        for sku in ['LAP-A14', 'PHONE-P6', 'AUDIO-B2', 'MON-M27']:
            result = ask('chính sách bảo hành và đổi trả', sku, self.records)
            keys = {r['fact_key'] for r in result['citations']}
            self.assertTrue({'warranty', 'returns'} <= keys, sku)

    def test_mixed_known_and_unknown_does_not_silently_drop_unknown(self):
        r = ask('bảo hành và giá hôm nay', 'LAP-A14', self.records)
        self.assertEqual(r['status'], 'handoff')

    def test_model_selecting_only_one_topic_is_rejected(self):
        warranty = next(r for r in self.records if r['product'] == 'LAP-A14' and r['fact_key'] == 'warranty')
        with patch('agent.select_evidence', return_value={'ids': [warranty['id']], 'insufficient': False, 'conflict': False}):
            r = ask('bảo hành và đổi trả', 'LAP-A14', self.records, model='test')
        self.assertEqual(r['status'], 'handoff')

    def test_explicit_followup_uses_previous_same_product(self):
        q, used = conversation_question('Chính sách đó có điều kiện gì?', 'LAP-A14',
                                        [{'product': 'LAP-A14', 'question': 'đổi trả'}])
        self.assertTrue(used)
        self.assertIn('đổi trả', q)

    def test_history_does_not_cross_products(self):
        q, used = conversation_question('Chính sách đó có điều kiện gì?', 'PHONE-P6',
                                        [{'product': 'LAP-A14', 'question': 'đổi trả'}])
        self.assertFalse(used)
        self.assertNotIn('đổi trả', q)

    def test_new_topic_not_polluted_by_old_history(self):
        r = ask('Giá hôm nay là bao nhiêu?', 'LAP-A14', self.records,
                history=[{'product': 'LAP-A14', 'question': 'bảo hành'}])
        self.assertEqual(r['status'], 'handoff')
        self.assertFalse(r['context_used'])

    def test_invalid_history_rejected(self):
        for history in ['text', [{'question': 'hi'}], [{'product': 'LAP-A14', 'question': 'hi'}] * 7]:
            with self.assertRaises(ValueError):
                ask('bảo hành', 'LAP-A14', self.records, history=history)

    def test_greeting_without_model_or_factual_claim(self):
        with patch('agent.select_evidence') as selector:
            result = ask('xin chào', 'LAP-A14', self.records, model='test')
        self.assertEqual(result['status'], 'conversation')
        self.assertFalse(result['citations'])
        selector.assert_not_called()

    def test_local_llm_repairs_typo_before_retrieval(self):
        with patch('agent.normalize_question_llm', return_value={
                'normalized': 'Danh sách cửa hàng', 'changed': True, 'language': 'vi'}):
            result = ask('Dnah sahc cưa hagn', 'PHONE-P6', self.records, model='test')
        self.assertEqual(result['status'], 'evidence')
        self.assertTrue(result['spelling_corrected'])
        self.assertEqual(result['normalized_question'], 'Danh sách cửa hàng')
        self.assertTrue(all(c['fact_key'] == 'store_address' for c in result['citations']))

    def test_normalizer_failure_falls_back_safely(self):
        with patch('agent.normalize_question_llm', side_effect=TimeoutError):
            result = ask('Dnah sahc cưa hagn', 'PHONE-P6', self.records, model='test')
        self.assertEqual(result['status'], 'handoff')


if __name__ == '__main__':
    unittest.main()
