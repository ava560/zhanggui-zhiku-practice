"""已核对句段才能流出；承诺收益和资料里没有的数字不能先展示。"""

from unittest import TestCase

from search.answer_stream import guard_sentence, release_sentences


class AnswerStreamTests(TestCase):
    def test_promise_is_replaced_and_supported_sentence_is_kept(self):
        released = release_sentences(
            ["基金净值是份额对应的资产价值。这只产品保证收益。"],
            evidence="基金净值是份额对应的资产价值。",
            question="什么是基金净值？",
        )

        self.assertEqual(
            released,
            [
                "基金净值是份额对应的资产价值。",
                "不承诺收益，也不提供买入、卖出或持有决定。",
            ],
        )
        self.assertNotIn("保证收益", "".join(released))

    def test_number_missing_from_evidence_is_not_released(self):
        released = release_sentences(
            ["营业收入是999亿元。"],
            evidence="平安银行 2026年 营业收入 100亿元",
            question="平安银行2026年营业收入是多少？",
        )

        self.assertEqual(released, ["有数字未能在已检索资料中核对，这里不列出。"])
        self.assertNotIn("999", released[0])

    def test_number_present_in_evidence_is_released(self):
        released = release_sentences(
            ["营业收入是100亿元。"],
            evidence="平安银行 2026年 营业收入 100亿元",
            question="平安银行2026年营业收入是多少？",
        )

        self.assertEqual(released, ["营业收入是100亿元。"])

    def test_general_chat_without_evidence_keeps_ordinary_numbers(self):
        released = release_sentences(["1+1等于2。"], evidence="", question="1+1等于几？")

        self.assertEqual(released, ["1+1等于2。"])

    def test_tail_without_punctuation_is_flushed_once(self):
        released = release_sentences(["基金净值", "是份额资产"], evidence="基金净值是份额资产", question="什么是基金净值")

        self.assertEqual(released, ["基金净值是份额资产"])

    def test_guard_sentence_rejects_each_forbidden_promise(self):
        evidence = "产品存在本金亏损可能。"
        for phrase in ("一定赚钱", "保本无风险", "稳赚不赔", "一定会上涨", "现在必须买入"):
            guarded = guard_sentence(f"这个产品{phrase}。", evidence, "这个产品保证赚钱吗")
            self.assertNotIn(phrase, guarded)
            self.assertIn("不承诺收益", guarded)
