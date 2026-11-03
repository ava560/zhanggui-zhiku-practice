"""RRF 知识块和网页结果的跨来源重排序。"""

import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def rerank_module():
    try:
        from search import node_rerank
    except ImportError as exc:
        raise AssertionError("缺少 search.node_rerank 节点") from exc
    return node_rerank


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores
        self.pairs = None
        self.normalized = None

    def compute_score(self, pairs, *, normalize):
        self.pairs = pairs
        self.normalized = normalize
        return self.scores


class ContentReranker:
    def __init__(self, scores):
        self.scores = scores

    def compute_score(self, pairs, *, normalize):
        return [self.scores[content] for _, content in pairs]


class RerankTests(TestCase):
    def test_comparison_keeps_evidence_for_both_products_after_score_gap(self):
        node = rerank_module().node_rerank
        root = Path.cwd() / "test-output"
        rrf_path, web_path = root / "compare_rrf.json", root / "compare_web.json"
        files = {
            rrf_path: json.dumps({"chunks": [
                {"chunk_id": 1, "item_name": "华为显示器 B3-242H", "content": "242H 手册"},
                {"chunk_id": 2, "item_name": "华为显示器 B3-241H", "content": "241H 手册"},
            ]}),
            web_path: json.dumps({"top5": [
                {"title": "B3-241H 与 B3-242H 支架区别", "content": "两款显示器支架不同", "url": "https://example.com/compare"},
            ]}),
        }
        state = {
            "question_rewritten": "比较 B3242H 和 B3241H",
            "search_matched_item_names": ["华为显示器 B3-242H", "华为显示器 B3-241H"],
            "rrf_json_path": str(rrf_path), "web_search_json_path": str(web_path),
        }
        with (
            patch.object(Path, "read_text", autospec=True, side_effect=lambda path, **_: files[path]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("search.node_rerank.get_reranker", return_value=ContentReranker({
                "242H 手册": 0.40, "241H 手册": 0.39, "两款显示器支架不同": 0.95,
            })),
        ):
            result = node(state)

        self.assertEqual(
            {chunk.get("item_name") for chunk in result["rerank_chunks"] if chunk["source"] == "rrf"},
            {"华为显示器 B3-242H", "华为显示器 B3-241H"},
        )

    def test_comparison_discards_web_page_about_another_model(self):
        node = rerank_module().node_rerank
        root = Path.cwd() / "test-output"
        rrf_path, web_path = root / "compare_rrf.json", root / "compare_web.json"
        files = {
            rrf_path: json.dumps({"chunks": [
                {"chunk_id": 1, "item_name": "华为显示器 B3-242H", "content": "242H 手册"},
                {"chunk_id": 2, "item_name": "华为显示器 B3-241H", "content": "241H 手册"},
            ]}),
            web_path: json.dumps({"top5": [
                {"title": "HUAWEI MateBook Pro", "content": "笔记本电脑屏幕", "url": "https://example.com/laptop"},
                {"title": "B3-241H 规格", "content": "显示器支架", "url": "https://example.com/monitor"},
            ]}),
        }
        state = {
            "question_rewritten": "比较 B3242H 和 B3241H",
            "search_matched_item_names": ["华为显示器 B3-242H", "华为显示器 B3-241H"],
            "rrf_json_path": str(rrf_path), "web_search_json_path": str(web_path),
        }
        with (
            patch.object(Path, "read_text", autospec=True, side_effect=lambda path, **_: files[path]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("search.node_rerank.get_reranker", return_value=ContentReranker({
                "242H 手册": 0.40, "241H 手册": 0.39,
                "笔记本电脑屏幕": 0.99, "显示器支架": 0.88,
            })),
        ):
            result = node(state)

        self.assertNotIn("HUAWEI MateBook Pro", [chunk.get("title") for chunk in result["rerank_chunks"]])

    def test_scores_query_content_pairs_and_keeps_web_provenance(self):
        node = rerank_module().node_rerank
        root = Path.cwd() / "test-output"
        rrf_path = root / "rrf.json"
        web_path = root / "web.json"
        output_path = root / "rerank.json"
        files = {
            rrf_path: json.dumps({"k": 60, "chunks": [
                {"chunk_id": 1, "item_name": "产品 A", "file_name": "a.pdf", "content": "知识 A", "rrf_score": 0.03, "source_ranks": {"embedding": 1}},
                {"chunk_id": 2, "item_name": "产品 A", "file_name": "a.pdf", "content": "知识 B", "rrf_score": 0.02, "source_ranks": {"hyde": 2}},
                {"chunk_id": 3, "item_name": "产品 A", "file_name": "a.pdf", "content": "知识 C", "rrf_score": 0.01, "source_ranks": {"embedding": 3}},
            ]}),
            web_path: json.dumps({"question_rewritten": "怎么重启？", "top5": [
                {"title": "官方步骤", "url": "https://example.com/guide", "content": "网页 A"},
                {"title": "其他网页", "url": "https://example.com/other", "content": "网页 B"},
            ]}),
        }
        model = FakeReranker([0.95, 0.90, 0.62, 0.88, 0.25])
        state = {
            "question_rewritten": "怎么重启？",
            "rrf_json_path": str(rrf_path),
            "web_search_json_path": str(web_path),
            "rerank_output_path": str(output_path),
        }
        with (
            patch.object(Path, "read_text", autospec=True, side_effect=lambda path, **_: files[path]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_json,
            patch("search.node_rerank.get_reranker", return_value=model),
        ):
            result = node(state)

        chunks = result["rerank_chunks"]
        self.assertEqual(model.pairs, [
            ["怎么重启？", "知识 A"],
            ["怎么重启？", "知识 B"],
            ["怎么重启？", "知识 C"],
            ["怎么重启？", "网页 A"],
            ["怎么重启？", "网页 B"],
        ])
        self.assertTrue(model.normalized)
        self.assertEqual([chunk["content"] for chunk in chunks], ["知识 A", "知识 B", "网页 A", "知识 C"])
        self.assertEqual([chunk["rerank_score"] for chunk in chunks], [0.95, 0.90, 0.88, 0.62])
        self.assertEqual(chunks[2]["source"], "web")
        self.assertEqual(chunks[2]["title"], "官方步骤")
        self.assertEqual(chunks[2]["url"], "https://example.com/guide")
        self.assertEqual(chunks[0]["chunk_id"], 1)
        self.assertEqual(chunks[0]["source"], "rrf")
        self.assertEqual(json.loads(write_json.call_args.args[0])["chunks"], chunks)
        self.assertEqual(result["rerank_json_path"], str(output_path))

    def test_keeps_at_most_ten_chunks(self):
        rerank_candidates = rerank_module().rerank_candidates
        knowledge = [{"chunk_id": index, "content": f"块 {index}"} for index in range(12)]
        model = FakeReranker([0.99 - index * 0.01 for index in range(12)])

        chunks = rerank_candidates("问题", knowledge, [], model)

        self.assertEqual(len(chunks), 10)
        self.assertEqual([chunk["chunk_id"] for chunk in chunks], list(range(10)))

    def test_gap_equal_to_point_three_is_kept_but_larger_gap_is_cut(self):
        rerank_candidates = rerank_module().rerank_candidates
        knowledge = [{"chunk_id": index, "content": f"块 {index}"} for index in range(3)]
        model = FakeReranker([0.9, 0.6, 0.29])

        chunks = rerank_candidates("问题", knowledge, [], model)

        self.assertEqual([chunk["chunk_id"] for chunk in chunks], [0, 1])

    def test_empty_question_rejected_before_model_load(self):
        node = rerank_module().node_rerank

        with patch("search.node_rerank.get_reranker") as load_model:
            with self.assertRaises(ValueError):
                node({"question_rewritten": "", "rrf_json_path": "rrf.json", "web_search_json_path": "web.json"})
        load_model.assert_not_called()

    def test_invalid_model_scores_are_not_written(self):
        rerank_candidates = rerank_module().rerank_candidates
        knowledge = [{"chunk_id": 1, "content": "有效内容"}]

        with self.assertRaises(ValueError):
            rerank_candidates("问题", knowledge, [], FakeReranker([float("nan")]))
