"""产品确认、普通对话分流和商品名称的回归测试。"""

from unittest import TestCase

from chat_service import infer_pending_decision
from product_name import canonical_product_name
from search.node_item_name_confirm import classify_matches, parse_llm_json
from upload.node_item_name_recognition import clean_item_name, fallback_item_name


class ChatRoutingTests(TestCase):
    def test_general_question_is_explicitly_marked_non_product(self):
        result = parse_llm_json(
            '{"is_product_question": false, "itemname_question": [], '
            '"question_rewritten": "1+1等于几？"}',
            "1+1等于几？",
        )
        self.assertFalse(result["is_product_question"])
        self.assertEqual(result["itemname_question"], [])

    def test_product_question_remains_product_even_without_extracted_name(self):
        result = parse_llm_json(
            '{"is_product_question": true, "itemname_question": [], '
            '"question_rewritten": "这款设备怎么开机？"}',
            "这款设备怎么开机？",
        )
        self.assertTrue(result["is_product_question"])

    def test_plain_yes_and_no_resolve_single_pending_candidate(self):
        pending = {"item_name_candidates": [{"itemname_question": "华为平板 C3"}]}
        self.assertEqual(infer_pending_decision("是", pending), {"item_name_user_confirmed": True})
        self.assertEqual(infer_pending_decision("不是", pending), {"item_name_user_confirmed": False})
        self.assertIsNone(infer_pending_decision("换个问题", pending))

    def test_old_document_title_is_displayed_as_product_but_kept_for_search(self):
        raw = "华为平板 C3 用户指南-(BZD-AL00&AL10&W00,EMUI10.1_01,ZH-CN)"
        self.assertEqual(canonical_product_name(raw), "华为平板 C3")
        decision = classify_matches(
            ["华为平板 C3"],
            {"华为平板 C3": [{"itemname_question": raw, "score": 0.74}]},
        )
        self.assertEqual(decision["item_name_candidates"][0]["itemname_question"], raw)
        self.assertEqual(decision["item_name_candidates"][0]["display_name"], "华为平板 C3")
        self.assertIn("华为平板 C3", decision["item_name_confirmation_message"])
        self.assertNotIn("用户指南", decision["item_name_confirmation_message"])

    def test_upload_backfill_uses_product_name_even_if_model_returns_document_title(self):
        raw = "华为平板 C3 用户指南-(BZD-AL00&AL10&W00,EMUI10.1_01,ZH-CN)"
        self.assertEqual(clean_item_name(raw), "华为平板 C3")
        self.assertEqual(
            fallback_item_name([{"title": raw}], raw + "_new.md"),
            "华为平板 C3",
        )
