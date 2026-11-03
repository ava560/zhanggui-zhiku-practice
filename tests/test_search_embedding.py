"""用已确认商品限制 chunk 混合检索，并保存全局 Top 5。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


def search_node():
    try:
        from search.node_search_embedding import node_search_embedding
    except ModuleNotFoundError as exc:
        raise AssertionError("缺少 search.node_search_embedding 节点") from exc
    return node_search_embedding


class FakeMilvusClient:
    def __init__(self):
        self.loaded = []
        self.chunk_search_count = 0
        self.name_queries = []

    def load_collection(self, collection_name):
        self.loaded.append(collection_name)

    def query(self, *, collection_name, filter, output_fields, limit):
        assert collection_name == "kb_item_names"
        assert output_fields == ["item_name"]
        assert limit == 1
        name = json.loads(filter[len("item_name == "):])
        self.name_queries.append(name)
        return [{"item_name": name}] if name in {"产品 A", "产品 B"} else []

    def hybrid_search(self, *, collection_name, reqs, ranker, limit, output_fields):
        assert [req._anns_field for req in reqs] == ["dense_vector", "sparse_vector"]
        assert reqs[0]._expr == reqs[1]._expr
        assert ranker._weights == [0.8, 0.2]
        assert ranker._norm_score is False

        expression = reqs[0]._expr
        if collection_name == "kb_item_names":
            assert expression.startswith("item_name == ")
            name = json.loads(expression[len("item_name == "):])
            if name not in {"产品 A", "产品 B"}:
                return [[]]
            return [[{
                "id": 1,
                "distance": 1.0,
                "entity": {"item_name": name},
            }]]

        assert collection_name == "kb_chunks"
        assert expression.startswith("item_name in ")
        names = json.loads(expression[len("item_name in "):])
        assert reqs[0]._data == [[0.3, 0.7]]
        self.chunk_search_count += 1
        rows = [
            ("产品 A", "A1", 0.99),
            ("产品 B", "B1", 0.98),
            ("产品 A", "A2", 0.97),
            ("产品 B", "B2", 0.96),
            ("产品 A", "A3", 0.95),
            ("产品 B", "B3", 0.94),
            ("其他商品", "X1", 1.0),
        ]
        hits = [
            {
                "id": index,
                "distance": score,
                "entity": {
                    "item_name": name,
                    "content": content,
                    "file_name": f"{name}.pdf",
                },
            }
            for index, (name, content, score) in enumerate(rows, start=1)
            if name in names
        ]
        return [sorted(hits, key=lambda hit: hit["distance"], reverse=True)[:limit]]

    def close(self):
        pass


class SearchEmbeddingTests(TestCase):
    def test_exact_name_check_does_not_vectorize_each_name(self):
        node = search_node()
        client = FakeMilvusClient()
        settings = SimpleNamespace(item_name_collection="kb_item_names", chunks_collection="kb_chunks", search_top_k=5)
        vectorized = []

        def vectors(_settings, text):
            vectorized.append(text)
            return [0.3, 0.7], {3: 1.0}

        with (
            patch("search.node_search_embedding.load_search_settings", return_value=settings),
            patch("search.node_search_embedding.get_milvus_client", return_value=client),
            patch("search.node_search_embedding.generate_query_vectors", side_effect=vectors),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
        ):
            result = node({"itemname_question": ["产品 A", "产品 B"], "question_rewritten": "比较这两款产品"})

        self.assertEqual(client.name_queries, ["产品 A", "产品 B"])
        self.assertEqual(vectorized, ["比较这两款产品"])
        self.assertEqual(result["search_matched_item_names"], ["产品 A", "产品 B"])

    def test_vectorizes_both_inputs_and_saves_global_top_five(self):
        node = search_node()
        client = FakeMilvusClient()
        settings = SimpleNamespace(
            item_name_collection="kb_item_names",
            chunks_collection="kb_chunks",
            search_top_k=5,
        )

        def vectors(_settings, text):
            return {
                "产品 A": ([1.0, 0.0], {1: 1.0}),
                "产品 B": ([0.0, 1.0], {2: 1.0}),
                "如何重启？": ([0.3, 0.7], {3: 1.0}),
            }[text]

        state = {
            "itemname_question": ["产品 A", "产品 B"],
            "question_rewritten": "如何重启？",
            "local_dir": str(Path.cwd() / "test-output"),
            "conversation_id": "conversation-1",
        }
        with (
            patch("search.node_search_embedding.load_search_settings", return_value=settings),
            patch("search.node_search_embedding.get_milvus_client", return_value=client),
            patch("search.node_search_embedding.generate_query_vectors", side_effect=vectors),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_json,
        ):
            result = node(state)

        saved = json.loads(write_json.call_args.args[0])

        self.assertEqual(client.chunk_search_count, 1)
        self.assertEqual(result["search_matched_item_names"], ["产品 A", "产品 B"])
        self.assertEqual([hit["content"] for hit in result["search_top5"]], [
            "A1", "B1", "A2", "B2", "A3",
        ])
        self.assertEqual(saved["question_rewritten"], "如何重启？")
        self.assertEqual(saved["top5"], result["search_top5"])
        self.assertEqual(result["search_embedding_top5_json_path"], result["search_top5_json_path"])

    def test_unconfirmed_or_empty_item_names_cannot_search_all_chunks(self):
        node = search_node()
        with self.assertRaises(ValueError):
            node({"itemname_question": [], "question_rewritten": "如何重启？"})
        with self.assertRaises(ValueError):
            node({
                "itemname_question": ["产品 A"],
                "question_rewritten": "如何重启？",
                "awaiting_user_confirmation": True,
            })

    def test_missing_product_returns_empty_results_without_chunk_search(self):
        node = search_node()
        client = FakeMilvusClient()
        settings = SimpleNamespace(
            item_name_collection="kb_item_names",
            chunks_collection="kb_chunks",
            search_top_k=5,
        )
        state = {
            "itemname_question": ["不存在的产品"],
            "question_rewritten": "如何使用？",
            "local_dir": str(Path.cwd() / "test-output"),
        }
        with (
            patch("search.node_search_embedding.load_search_settings", return_value=settings),
            patch("search.node_search_embedding.get_milvus_client", return_value=client),
            patch("search.node_search_embedding.generate_query_vectors", return_value=([1.0, 0.0], {1: 1.0})),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_json,
        ):
            result = node(state)

        self.assertEqual(client.chunk_search_count, 0)
        self.assertEqual(result["search_missing_item_names"], ["不存在的产品"])
        self.assertEqual(result["search_top5"], [])
        self.assertEqual(json.loads(write_json.call_args.args[0])["top5"], [])
