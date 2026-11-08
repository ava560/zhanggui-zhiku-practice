"""最终答案节点：检索内容作答、图片链接选择和 MongoDB 留档。"""

import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from tests.test_chat_service import FakeCollection


def answer_module():
    try:
        from search import node_answer_output
    except ImportError as exc:
        raise AssertionError("缺少 search.node_answer_output 节点") from exc
    return node_answer_output


class FakeMongoClient:
    def __init__(self):
        self.chats = FakeCollection()
        self.messages = FakeCollection()
        self.closed = False

    def __getitem__(self, name):
        if name == "knowledge":
            return self
        if name == "conversations":
            return self.chats
        if name == "conversation_history":
            return self.messages
        raise KeyError(name)

    def close(self):
        self.closed = True


class AnswerOutputTests(TestCase):
    def test_model_context_excludes_internal_search_metadata(self):
        generate = answer_module().generate_answer
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps({"answer": "产品 A 的说明", "image_urls": []})}}]}
        chunks = [{
            "source": "rrf", "item_name": "产品 A", "file_name": "A.md", "content": "产品 A 的资料",
            "chunk_id": 74321, "rerank_score": 0.912345, "source_ranks": {"embedding": 1},
        }]
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}),
            patch("search.node_answer_output.requests.post", return_value=response) as post,
        ):
            answer, _ = generate("介绍产品 A", "介绍产品 A", chunks)

        prompt = post.call_args.kwargs["json"]["messages"][-1]["content"]
        self.assertEqual(answer, "产品 A 的说明")
        self.assertIn("产品 A 的资料", prompt)
        self.assertIn("最近十条", prompt)
        self.assertIn("会话记忆", prompt)
        self.assertIn("长期记忆", prompt)
        self.assertNotIn("74321", prompt)
        self.assertNotIn("0.912345", prompt)
        self.assertNotIn("source_ranks", prompt)

    def setUp(self):
        self.client = FakeMongoClient()
        self.client.chats.rows.append({
            "user_id": "alice", "conversation_id": "c1", "next_seq": 1,
            "title": "如何配置？", "created_at": "2026-10-01", "updated_at": "2026-10-01",
        })
        self.client.messages.rows.append({
            "user_id": "alice", "conversation_id": "c1", "seq": 1,
            "role": "user", "content": "如何配置？", "created_at": "2026-10-01",
        })
        self.state = {
            "user_id": "alice", "conversation_id": "c1",
            "current_question": "如何配置？", "question_rewritten": "产品 A 如何配置？",
        }
        self.mongo_env = {
            "MONGO_URL": "mongodb://example.invalid",
            "MONGODB_URI": "",
            "MONGO_DB_NAME": "knowledge",
            "MONGODB_DATABASE": "",
            "MONGO_CHATS_COLLECTION": "conversations",
            "MONGO_CONVERSATION_COLLECTION": "conversation_history",
            "MONGODB_CONVERSATION_COLLECTION": "",
        }

    def test_no_chunks_returns_exact_message_and_archives_without_llm(self):
        node = answer_module().node_answer_output
        self.state["rerank_chunks"] = []
        with (
            patch.dict(os.environ, self.mongo_env),
            patch("pymongo.MongoClient", return_value=self.client),
            patch("search.node_answer_output.generate_answer") as llm,
        ):
            result = node(self.state)

        llm.assert_not_called()
        self.assertEqual(result["final_answer"], "暂无查询结果")
        self.assertEqual(result["answer_image_urls"], [])
        self.assertEqual(result["answer_message_seq"], 2)
        self.assertEqual([(row["role"], row["content"], row["seq"]) for row in self.client.messages.rows], [
            ("user", "如何配置？", 1),
            ("assistant", "暂无查询结果", 2),
        ])
        self.assertTrue(self.client.closed)

    def test_answer_uses_question_and_chunks_and_only_returns_existing_image_url(self):
        node = answer_module().node_answer_output
        self.state["rerank_chunks"] = [
            {
                "source": "rrf", "chunk_id": 7, "item_name": "产品 A", "file_name": "A.md",
                "content": "按说明配置。![配置图](https://img.example.com/config.png)",
                "rerank_score": 0.96,
            },
            {
                "source": "web", "title": "官网说明", "url": "https://example.com/guide",
                "content": "也可以参考官网说明。", "rerank_score": 0.84,
            },
        ]
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps({
            "answer": "请按说明配置产品 A。",
            "image_urls": ["https://img.example.com/config.png", "https://fake.example.com/a.png"],
        })}}]}
        with (
            patch.dict(os.environ, {**self.mongo_env, "OPENAI_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}),
            patch("pymongo.MongoClient", return_value=self.client),
            patch("search.node_answer_output.requests.post", return_value=response) as post,
        ):
            result = node(self.state)

        prompt = json.dumps(post.call_args.kwargs["json"]["messages"], ensure_ascii=False)
        self.assertIn("如何配置？", prompt)
        self.assertIn("产品 A 如何配置？", prompt)
        self.assertIn("按说明配置", prompt)
        self.assertIn("也可以参考官网说明", prompt)
        self.assertIn("https://img.example.com/config.png", prompt)
        self.assertEqual(result["answer_image_urls"], ["https://img.example.com/config.png"])
        self.assertIn("https://img.example.com/config.png", result["final_answer"])
        self.assertNotIn("https://fake.example.com/a.png", result["final_answer"])
        self.assertEqual(self.client.messages.rows[-1]["content"], result["final_answer"])
        self.assertEqual(self.client.messages.rows[-1]["user_id"], "alice")
        self.assertEqual(self.client.messages.rows[-1]["conversation_id"], "c1")
        self.assertEqual(self.client.chats.rows[0]["next_seq"], 2)

    def test_final_answer_redacts_configured_secret_in_selected_image_url(self):
        node = answer_module().node_answer_output
        self.state["rerank_chunks"] = [{"content": "图片说明"}]
        with (
            patch.dict(os.environ, {**self.mongo_env, "OPENAI_API_KEY": "secret-value-123"}),
            patch("pymongo.MongoClient", return_value=self.client),
            patch("search.node_answer_output.generate_answer", return_value=(
                "请查看图片", ["https://img.example.com/a.png?token=secret-value-123"],
            )),
        ):
            result = node(self.state)

        self.assertNotIn("secret-value-123", result["final_answer"])
        self.assertEqual(result["answer_image_urls"], [])
        self.assertEqual(self.client.messages.rows[-1]["content"], result["final_answer"])

    def test_model_failure_does_not_archive_or_set_final_answer(self):
        node = answer_module().node_answer_output
        self.state["rerank_chunks"] = [{"source": "rrf", "content": "资料"}]
        with (
            patch.dict(os.environ, self.mongo_env),
            patch("search.node_answer_output.generate_answer", side_effect=RuntimeError("模型失败")),
            patch("pymongo.MongoClient", return_value=self.client),
        ):
            with self.assertRaises(RuntimeError):
                node(self.state)

        self.assertEqual(len(self.client.messages.rows), 1)
        self.assertNotIn("final_answer", self.state)

    def test_missing_conversation_scope_never_writes_to_mongo(self):
        node = answer_module().node_answer_output
        self.state.pop("user_id")
        self.state["rerank_chunks"] = []
        with (
            patch.dict(os.environ, self.mongo_env),
            patch("pymongo.MongoClient", return_value=self.client),
        ):
            with self.assertRaisesRegex(ValueError, "user_id"):
                node(self.state)

        self.assertEqual(len(self.client.messages.rows), 1)

    def test_existing_message_collection_can_be_selected_from_state(self):
        persist = answer_module().persist_answer_to_mongodb
        self.state["mongo_collection"] = "conversation_history"
        with (
            patch.dict(os.environ, self.mongo_env),
            patch("pymongo.MongoClient", return_value=self.client),
        ):
            sequence = persist(self.state, "已归档回答")

        self.assertEqual(sequence, 2)
        self.assertEqual(self.client.messages.rows[-1]["content"], "已归档回答")
        self.assertEqual(self.client.messages.rows[-1]["created_at"].count("T"), 1)

    def test_reads_rerank_json_when_chunks_are_not_in_state(self):
        node = answer_module().node_answer_output
        self.state["rerank_json_path"] = str(Path.cwd() / "test-output" / "rerank.json")
        payload = json.dumps({"question_rewritten": "产品 A 如何配置？", "chunks": [
            {"source": "rrf", "chunk_id": 7, "content": "文件中的答案", "rerank_score": 0.9},
        ]})
        with (
            patch.dict(os.environ, self.mongo_env),
            patch("pymongo.MongoClient", return_value=self.client),
            patch.object(Path, "read_text", return_value=payload),
            patch(
                "search.node_answer_output.generate_answer",
                side_effect=lambda _q, _r, chunks, related_files=None, **_context: (chunks[0]["content"], []),
            ),
        ):
            result = node(self.state)

        self.assertEqual(result["final_answer"], "文件中的答案")
        self.assertEqual(self.client.messages.rows[-1]["content"], "文件中的答案")

    def test_model_unverified_download_url_is_removed_without_losing_answer(self):
        generate = answer_module().generate_answer
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps({
            "answer": "图片地址：https://fake.example.com/unknown.png",
            "image_urls": [],
        })}}]}
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}),
            patch("search.node_answer_output.requests.post", return_value=response),
        ):
            answer, image_urls = generate("如何配置？", "产品 A 如何配置？", [{
                "source": "rrf", "content": "![配置图](https://img.example.com/config.png)",
            }])

        self.assertIn("图片地址", answer)
        self.assertNotIn("https://fake.example.com/unknown.png", answer)
        self.assertEqual(image_urls, [])

    def test_mongo_field_names_follow_existing_configurable_schema(self):
        persist = answer_module().persist_answer_to_mongodb
        self.client.chats.rows = [{"uid": "alice", "cid": "c1", "next_seq": 4}]
        custom_env = {
            **self.mongo_env,
            "MONGO_USER_ID_FIELD": "uid",
            "MONGO_CONVERSATION_ID_FIELD": "cid",
            "MONGO_ROLE_FIELD": "speaker",
            "MONGO_CONTENT_FIELD": "body",
            "MONGO_CREATED_AT_FIELD": "when",
        }
        with (
            patch.dict(os.environ, custom_env),
            patch("pymongo.MongoClient", return_value=self.client),
        ):
            sequence = persist(self.state, "自定义字段回答")

        row = self.client.messages.rows[-1]
        self.assertEqual(sequence, 5)
        self.assertEqual(row["uid"], "alice")
        self.assertEqual(row["cid"], "c1")
        self.assertEqual(row.get("speaker"), "assistant")
        self.assertEqual(row.get("body"), "自定义字段回答")
        self.assertIn("T", row.get("when", ""))
