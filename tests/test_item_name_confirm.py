"""商品名确认节点读取与聊天服务相同的 MongoDB 消息字段。"""

import math
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from search.node_item_name_confirm import (
    classify_matches,
    hybrid_search_item_name,
    load_conversation_from_mongodb,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, field, direction):
        self.rows.sort(key=lambda row: row[field], reverse=direction < 0)
        return self

    def limit(self, count):
        self.rows = self.rows[:count]
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query):
        return FakeCursor(
            row for row in self.rows
            if all(row.get(key) == value for key, value in query.items())
        )


class FakeMongoClient:
    def __init__(self, rows):
        self.collection = FakeCollection(rows)

    def __getitem__(self, name):
        return self if name == "knowledge" else self.collection

    def close(self):
        pass


class ItemNameConfirmMongoTests(TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            mongo_uri="mongodb://example.invalid",
            mongo_database="knowledge",
            mongo_collection="conversation_history",
            mongo_user_id_field="user_id",
            mongo_conversation_id_field="conversation_id",
            mongo_messages_field="messages",
            mongo_role_field="role",
            mongo_content_field="content",
            mongo_question_field="current_question",
            mongo_answer_field="answer",
            mongo_created_at_field="created_at",
            mongo_history_limit=50,
            mongo_timeout_ms=1000,
        )
        self.rows = [
            {"user_id": "alice", "conversation_id": "c1", "seq": 1, "role": "user", "content": "CB304n 网桥", "created_at": "2026-10-03"},
            {"user_id": "alice", "conversation_id": "c1", "seq": 2, "role": "assistant", "content": "请问具体问题？", "created_at": "2026-10-02"},
            {"user_id": "alice", "conversation_id": "c1", "seq": 3, "role": "user", "content": "它如何配置？", "created_at": "2026-10-01"},
            {"user_id": "alice", "conversation_id": "c2", "seq": 4, "role": "user", "content": "别的会话", "created_at": "2026-10-04"},
            {"user_id": "bob", "conversation_id": "c1", "seq": 5, "role": "user", "content": "别的用户", "created_at": "2026-10-05"},
        ]

    def test_reads_latest_user_question_by_seq_with_scoped_history(self):
        client = FakeMongoClient(self.rows)
        with patch("pymongo.MongoClient", return_value=client):
            history, question = load_conversation_from_mongodb(
                self.settings, {"user_id": "alice", "conversation_id": "c1"}
            )

        self.assertEqual(question, "它如何配置？")
        self.assertEqual(history, [
            {"role": "user", "content": "CB304n 网桥"},
            {"role": "assistant", "content": "请问具体问题？"},
        ])

    def test_requires_both_user_and_conversation_scope(self):
        client = FakeMongoClient(self.rows)
        with patch("pymongo.MongoClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "user_id.*conversation_id"):
                load_conversation_from_mongodb(self.settings, {"user_id": "alice"})


class FakeMilvusClient:
    def __init__(self, forced_score=None):
        self.forced_score = forced_score

    def hybrid_search(self, **kwargs):
        ranker = kwargs["ranker"]
        similarities = [1.0, 1.0]
        if ranker._norm_score:
            similarities = [0.5 + math.atan(value) / math.pi for value in similarities]
        score = sum(
            weight * value
            for weight, value in zip(ranker._weights, similarities)
        )
        if self.forced_score is not None:
            score = self.forced_score
        return [[{
            "entity": {"item_name": "Aolynk CB304n", "file_name": "manual.pdf"},
            "distance": score,
        }]]


class ItemNameConfirmScoreTests(TestCase):
    def test_two_product_queries_show_each_candidate_once(self):
        first = "华为显示屏 B3242H"
        second = "华为显示屏 B3241H"
        product_a = "华为显示器 B3-242H"
        product_b = "华为显示器 B3-241H"
        decision = classify_matches([first, second], {
            first: [
                {"itemname_question": product_a, "score": 0.72},
                {"itemname_question": product_b, "score": 0.69},
            ],
            second: [
                {"itemname_question": product_b, "score": 0.73},
                {"itemname_question": product_a, "score": 0.71},
            ],
        })

        self.assertEqual(
            [candidate["itemname_question"] for candidate in decision["item_name_candidates"]],
            [product_a, product_b],
        )
        self.assertEqual(decision["item_name_candidates"][1]["score"], 0.73)

    def test_exact_dense_and_sparse_match_reaches_direct_threshold(self):
        settings = SimpleNamespace(item_name_collection="kb_item_names", search_top_k=5)
        with patch(
            "search.node_item_name_confirm.generate_query_vectors",
            return_value=([1.0, 0.0], {42: 1.0}),
        ):
            hits = hybrid_search_item_name(FakeMilvusClient(), settings, "Aolynk CB304n")

        decision = classify_matches(["Aolynk CB304n"], {"Aolynk CB304n": hits})
        self.assertEqual(decision["item_name_confirm_status"], "matched")
        self.assertEqual(decision["itemname_question"], ["Aolynk CB304n"])

    def test_score_just_below_threshold_is_not_rounded_up(self):
        settings = SimpleNamespace(item_name_collection="kb_item_names", search_top_k=5)
        with patch(
            "search.node_item_name_confirm.generate_query_vectors",
            return_value=([1.0, 0.0], {42: 1.0}),
        ):
            hits = hybrid_search_item_name(
                FakeMilvusClient(forced_score=0.8999999), settings, "Aolynk CB304n"
            )

        decision = classify_matches(["Aolynk CB304n"], {"Aolynk CB304n": hits})
        self.assertEqual(decision["item_name_confirm_status"], "need_confirmation")

    def test_middle_band_routes_one_or_multiple_candidates_to_user(self):
        one = classify_matches(
            ["CB304n"],
            {"CB304n": [{"itemname_question": "Aolynk CB304n", "score": 0.6}]},
        )
        multiple = classify_matches(
            ["CB304n"],
            {"CB304n": [
                {"itemname_question": "Aolynk CB304n", "score": 0.8},
                {"itemname_question": "Aolynk CB305n", "score": 0.7},
            ]},
        )

        self.assertEqual(one["item_name_confirm_status"], "need_confirmation")
        self.assertEqual(multiple["item_name_confirm_status"], "need_selection")
        self.assertTrue(one["awaiting_user_confirmation"])
        self.assertTrue(multiple["awaiting_user_confirmation"])

    def test_score_below_0_6_reports_product_not_found(self):
        decision = classify_matches(
            ["未知型号"],
            {"未知型号": [{"itemname_question": "Aolynk CB304n", "score": 0.59}]},
        )

        self.assertEqual(decision["item_name_confirm_status"], "not_found")
        self.assertEqual(decision["itemname_question"], [])
        self.assertEqual(decision["item_name_not_found"], ["未知型号"])
