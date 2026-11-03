"""HyDE 节点应保留原问题，用假设回答扩展知识块检索。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


class FakeMilvusClient:
    def __init__(self):
        self.chunk_queries = []

    def load_collection(self, collection_name):
        pass

    def query(self, *, collection_name, filter, output_fields, limit):
        assert collection_name == "kb_item_names"
        assert filter == 'item_name == "产品 A"'
        return [{"item_name": "产品 A"}]

    def hybrid_search(self, *, collection_name, reqs, ranker, limit, output_fields):
        assert [req._anns_field for req in reqs] == ["dense_vector", "sparse_vector"]
        assert reqs[0]._expr == reqs[1]._expr
        assert ranker._weights == [0.8, 0.2]
        if collection_name == "kb_item_names":
            assert reqs[0]._expr == 'item_name == "产品 A"'
            return [[{"id": 1, "distance": 1.0, "entity": {"item_name": "产品 A"}}]]
        assert collection_name == "kb_chunks"
        assert reqs[0]._expr == 'item_name in ["产品 A"]'
        self.chunk_queries.append(reqs[0]._data[0])
        return [[{
            "id": 12,
            "distance": 0.87,
            "entity": {"item_name": "产品 A", "content": "重启步骤", "file_name": "A.pdf"},
        }]]

    def close(self):
        pass


class SearchEmbeddingHydeTests(TestCase):
    def test_prompt_requests_roughly_300_characters_without_invented_parameters(self):
        from search.node_search_embedding_hyde import generate_hyde_answer

        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": " 参考回答 "}}]}
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "DEEPSEEK_API_KEY": ""}),
            patch("search.node_search_embedding_hyde.requests.post", return_value=response) as post,
        ):
            answer = generate_hyde_answer("如何重启？", ["产品 A"])

        self.assertEqual(answer, "参考回答")
        payload = post.call_args.kwargs["json"]
        prompt = json.dumps(payload["messages"], ensure_ascii=False)
        self.assertIn("如何重启？", prompt)
        self.assertIn("产品 A", prompt)
        self.assertIn("300", prompt)
        self.assertIn("不要编造", prompt)

    def test_uses_question_plus_hyde_answer_for_chunk_vectors_and_saves_json(self):
        from search.node_search_embedding_hyde import node_search_embedding_hyde

        client = FakeMilvusClient()
        settings = SimpleNamespace(
            item_name_collection="kb_item_names",
            chunks_collection="kb_chunks",
            search_top_k=5,
        )
        state = {
            "itemname_question": ["产品 A"],
            "question_rewritten": "如何重启？",
            "local_dir": str(Path.cwd() / "test-output"),
            "conversation_id": "conversation-1",
        }
        vectors = {
            "产品 A": ([1.0, 0.0], {1: 1.0}),
            "如何重启？\n先检查电源，再按说明操作。": ([0.3, 0.7], {2: 1.0}),
        }
        with (
            patch("search.node_search_embedding_hyde.generate_hyde_answer", return_value="先检查电源，再按说明操作。"),
            patch("search.node_search_embedding_hyde.load_search_settings", return_value=settings),
            patch("search.node_search_embedding_hyde.get_milvus_client", return_value=client),
            patch("search.node_search_embedding.generate_query_vectors", side_effect=lambda _s, text: vectors[text]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_json,
        ):
            result = node_search_embedding_hyde(state)

        saved = json.loads(write_json.call_args.args[0])
        self.assertEqual(result["question_rewritten"], "如何重启？")
        self.assertEqual(result["question_answer"], "如何重启？\n先检查电源，再按说明操作。")
        self.assertEqual(client.chunk_queries, [[0.3, 0.7]])
        self.assertEqual([hit["content"] for hit in result["search_top5"]], ["重启步骤"])
        self.assertEqual(saved["question_rewritten"], "如何重启？")
        self.assertEqual(saved["question_answer"], result["question_answer"])
        self.assertEqual(saved["top5"], result["search_top5"])
        self.assertTrue(result["search_top5_json_path"].endswith("_search_hyde_top5.json"))

    def test_unconfirmed_names_do_not_call_model(self):
        from search.node_search_embedding_hyde import node_search_embedding_hyde

        with patch("search.node_search_embedding_hyde.generate_hyde_answer") as model:
            with self.assertRaises(ValueError):
                node_search_embedding_hyde({
                    "itemname_question": ["产品 A"],
                    "question_rewritten": "如何重启？",
                    "awaiting_user_confirmation": True,
                })
        model.assert_not_called()

    def test_shared_product_name_is_encoded_only_once_across_both_search_nodes(self):
        from search.node_search_embedding import node_search_embedding
        from search.node_search_embedding_hyde import node_search_embedding_hyde

        client = FakeMilvusClient()
        settings = SimpleNamespace(
            item_name_collection="kb_item_names",
            chunks_collection="kb_chunks",
            search_top_k=5,
            bge_model_name="bge-m3",
            bge_device="cpu",
            bge_fp16=False,
            embedding_dim=2,
        )
        vectors = {
            "产品 A": ([1.0, 0.0], {1: 1.0}),
            "如何重启？": ([0.2, 0.8], {2: 1.0}),
            "如何重启？\n参考回答": ([0.3, 0.7], {3: 1.0}),
        }
        state = {
            "itemname_question": ["产品 A"],
            "question_rewritten": "如何重启？",
            "local_dir": str(Path.cwd() / "test-output"),
        }
        with (
            patch("search.node_search_embedding.generate_query_vectors", side_effect=lambda _s, text: vectors[text]) as encode,
            patch("search.node_search_embedding.load_search_settings", return_value=settings),
            patch("search.node_search_embedding_hyde.load_search_settings", return_value=settings),
            patch("search.node_search_embedding.get_milvus_client", return_value=client),
            patch("search.node_search_embedding_hyde.get_milvus_client", return_value=client),
            patch("search.node_search_embedding_hyde.generate_hyde_answer", return_value="参考回答"),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
        ):
            node_search_embedding(state)
            baseline_path = state["search_embedding_top5_json_path"]
            node_search_embedding_hyde(state)

        encoded_texts = [call.args[1] for call in encode.call_args_list]
        self.assertEqual(encoded_texts.count("产品 A"), 0)
        self.assertEqual(encoded_texts.count("如何重启？"), 1)
        self.assertEqual(encoded_texts.count("如何重启？\n参考回答"), 1)
        self.assertEqual(client.chunk_queries, [[0.2, 0.8], [0.3, 0.7]])
        self.assertEqual(state["search_embedding_top5_json_path"], baseline_path)
