"""两路检索 JSON 的 RRF 融合结果。"""

import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


def rrf_module():
    try:
        from search import node_rrf
    except ImportError as exc:
        raise AssertionError("缺少 search.node_rrf 节点") from exc
    return node_rrf


class RrfTests(TestCase):
    def test_sorts_each_source_by_similarity_then_merges_duplicate_chunks(self):
        node_rrf = rrf_module().node_rrf

        root = Path.cwd() / "test-output"
        baseline_path = root / "baseline.json"
        hyde_path = root / "hyde.json"
        output_path = root / "rrf.json"
        baseline_json = json.dumps({"top5": [
                {"chunk_id": 11, "item_name": "产品 A", "file_name": "a.pdf", "content": "A", "score": 0.7},
                {"chunk_id": 12, "item_name": "产品 A", "file_name": "a.pdf", "content": "B", "score": 0.95},
                {"chunk_id": 13, "item_name": "产品 A", "file_name": "a.pdf", "content": "C", "score": 0.8},
                {"chunk_id": 12, "item_name": "产品 A", "file_name": "a.pdf", "content": "B", "score": 0.1},
            ]})
        hyde_json = json.dumps({"top5": [
                {"chunk_id": 14, "item_name": "产品 A", "file_name": "a.pdf", "content": "D", "score": 0.72},
                {"chunk_id": 12, "item_name": "产品 A", "file_name": "a.pdf", "content": "B", "score": 0.89},
                {"chunk_id": 11, "item_name": "产品 A", "file_name": "a.pdf", "content": "A", "score": 0.93},
            ]})
        state = {
            "search_embedding_top5_json_path": str(baseline_path),
            "search_hyde_top5_json_path": str(hyde_path),
            "rrf_output_path": str(output_path),
        }
        files = {baseline_path: baseline_json, hyde_path: hyde_json}
        with (
            patch.object(Path, "read_text", autospec=True, side_effect=lambda path, **_: files[path]),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text") as write_json,
        ):
            result = node_rrf(state)
            saved = json.loads(write_json.call_args.args[0])

        chunks = result["rrf_chunks"]
        self.assertEqual([chunk["chunk_id"] for chunk in chunks], [12, 11, 13, 14])
        self.assertEqual(len(chunks), 4)
        self.assertAlmostEqual(chunks[0]["rrf_score"], 1 / 61 + 1 / 62)
        self.assertEqual(chunks[0]["source_ranks"], {"embedding": 1, "hyde": 2})
        self.assertEqual(saved["k"], 60)
        self.assertEqual(saved["chunks"], chunks)
        self.assertEqual(result["rrf_json_path"], str(output_path))

    def test_without_chunk_id_deduplicates_by_product_file_and_content(self):
        fuse_chunks = rrf_module().fuse_chunks

        baseline = [{"item_name": "产品 A", "file_name": "a.pdf", "content": "同一段", "score": 0.9}]
        hyde = [{"item_name": "产品 A", "file_name": "a.pdf", "content": "同一段", "score": 0.8}]

        chunks = fuse_chunks(baseline, hyde)

        self.assertEqual(len(chunks), 1)
        self.assertAlmostEqual(chunks[0]["rrf_score"], 2 / 61)

    def test_requires_two_distinct_json_files(self):
        node_rrf = rrf_module().node_rrf

        with self.assertRaises(ValueError):
            node_rrf({"search_hyde_top5_json_path": "hyde.json"})
        with self.assertRaises(ValueError):
            node_rrf({
                "search_embedding_top5_json_path": "same.json",
                "search_hyde_top5_json_path": "same.json",
            })

    def test_output_cannot_overwrite_either_source_json(self):
        node_rrf = rrf_module().node_rrf

        with (
            patch.object(Path, "read_text", return_value='{"top5": []}'),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
        ):
            with self.assertRaises(ValueError):
                node_rrf({
                    "search_embedding_top5_json_path": "baseline.json",
                    "search_hyde_top5_json_path": "hyde.json",
                    "rrf_output_path": "baseline.json",
                })
