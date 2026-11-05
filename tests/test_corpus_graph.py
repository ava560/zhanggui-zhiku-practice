"""金融资料范围、图谱构建和清理计划。"""

from unittest import TestCase
from unittest.mock import patch

from upload.corpus_scope import is_non_financial
from knowledge.ids import gid
from knowledge.registry import EntityRegistry
from search.query_plan import plan_question
from upload.knowledge_graph import (
    ProjectionConflict,
    build_financial_graph,
    node_build_knowledge_graph,
    write_financial_graph,
)
from upload.node_entry import node_entry
from upload.purge_non_financial import dropped_file_names, milvus_file_filters, minio_object_matches
from state import create_default_state


FUND_FILE = "华夏债券投资基金（华夏债券C）基金产品资料概要更新_new.md"
BANK_FILE = "2026-04-25 平安银行股份有限公司2026年第一季度报告_new.md"
DEVICE_FILE = "Aolynk CB304n Cable网桥 用户手册-5W100-整本手册_new.md"


def _chunk(file_name: str, content: str, chunk_id: str = "42", item_name: str = "") -> dict:
    return {
        "file_name": file_name,
        "item_name": item_name,
        "content": content,
        "chunk_id": chunk_id,
    }


class CorpusScopeTests(TestCase):
    def test_device_manuals_are_non_financial(self) -> None:
        names = [
            DEVICE_FILE,
            "H3C UG系列路由器 用户手册-R0130-6W102-整本手册_new.md",
            "M3070系列用户手册_new.md",
            "hak180产品安全手册_new.md",
            "万用表RS-12的使用_new.md",
            "华为平板 C3 用户指南-(BZD-AL00&AL10&W00,EMUI10.1_01,ZH-CN)_new.md",
            "华为显示器 B3-241H 用户指南-(SSN-24BZ,VGA,04,zh-cn)_new.md",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(is_non_financial(name))

    def test_financial_documents_stay(self) -> None:
        names = [
            BANK_FILE,
            FUND_FILE,
            "基金基础知识_new.md",
            "中国货币政策执行报告_new.md",
            "中国建设银行人民币理财产品风险揭示书（非保本）_new.md",
            "投资者问答_new.md",
            "贵州茅台酒股份有限公司2026 年第一季度报告_new.md",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(is_non_financial(name))

    def test_financial_file_is_kept_when_item_name_looks_like_a_device(self) -> None:
        self.assertFalse(is_non_financial(BANK_FILE, "华为显示器 B3-241H"))

    def test_entry_skips_device_manual_before_parsing(self) -> None:
        state = create_default_state(local_file_path=DEVICE_FILE)
        node_entry(state)
        self.assertEqual(state["import_skip_reason"], "non_financial")
        self.assertFalse(state["is_pdf_read_enabled"])
        self.assertFalse(state["is_md_read_enabled"])


class KnowledgeGraphTests(TestCase):
    def test_explicit_manager_is_an_affirmed_assertion(self) -> None:
        graph = build_financial_graph([
            _chunk("北极星债券资料.md", "北极星债券型证券投资基金的基金管理人：北极星基金管理有限公司。"),
        ])
        managed = graph.accepted("MANAGED_BY")
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0].subject_id, "fund_series:北极星债券型证券投资基金")
        self.assertEqual(managed[0].object_id, "organization:北极星基金管理有限公司")
        self.assertTrue(managed[0].evidence_ids)
        names = {item.canonical_name for item in graph.entities}
        self.assertIn("北极星基金管理有限公司", names)
        self.assertIn("北极星债券型证券投资基金", names)

    def test_unknown_company_enters_without_a_preset_list(self) -> None:
        graph = build_financial_graph([
            _chunk("星海一季报.md", "星海银行股份有限公司2026年第一季度报告显示营业收入增长。"),
        ])
        reports = graph.accepted("REPORTS_ON")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].subject_id, graph.documents[0].doc_version)
        self.assertEqual(reports[0].object_id, "organization:星海银行股份有限公司")

    def test_cooccurrence_does_not_create_management_or_control(self) -> None:
        graph = build_financial_graph([
            _chunk("同页.md", "星海银行股份有限公司与远航集团股份有限公司在同一页出现。"),
        ])
        self.assertEqual(graph.accepted("MANAGED_BY"), [])
        self.assertEqual(graph.accepted("SUBSIDIARY_OF"), [])
        self.assertGreaterEqual(len(graph.mentions), 2)

    def test_shareholding_is_not_control(self) -> None:
        graph = build_financial_graph([
            _chunk("持股.md", "星海银行股份有限公司持有远航集团股份有限公司5%股份。"),
        ])
        self.assertEqual(graph.accepted("SUBSIDIARY_OF"), [])

    def test_explicit_control_sentence_creates_subsidiary_assertion(self) -> None:
        graph = build_financial_graph([
            _chunk("集团.md", "星海银行股份有限公司系远航集团股份有限公司的控股子公司。"),
        ])
        edges = graph.accepted("SUBSIDIARY_OF")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].subject_id, "organization:星海银行股份有限公司")
        self.assertEqual(edges[0].object_id, "organization:远航集团股份有限公司")

    def test_planned_or_former_manager_is_not_an_affirmed_relation(self) -> None:
        planned = build_financial_graph([
            _chunk("拟任.md", "北极星债券型证券投资基金拟由北极星基金管理有限公司担任基金管理人。"),
        ])
        former = build_financial_graph([
            _chunk("离任.md", "北极星债券型证券投资基金不再由北极星基金管理有限公司担任基金管理人。"),
        ])
        self.assertEqual(planned.accepted("MANAGED_BY"), [])
        self.assertEqual(former.accepted("MANAGED_BY"), [])
        self.assertEqual(planned.assertions[0].polarity, "planned")
        self.assertEqual(former.assertions[0].polarity, "negated")

    def test_share_class_is_not_created_by_deleting_a_suffix(self) -> None:
        graph = build_financial_graph([
            _chunk(
                "份额.md",
                "北极星债券型证券投资基金（北极星债券C）的基金管理人：北极星基金管理有限公司。",
            ),
        ])
        shares = graph.accepted("SHARE_CLASS_OF")
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].subject_id, "fund_share:北极星债券C")
        self.assertEqual(shares[0].object_id, "fund_series:北极星债券型证券投资基金")
        managed = graph.accepted("MANAGED_BY")
        self.assertEqual(managed[0].subject_id, "fund_share:北极星债券C")
        self.assertFalse(any(item.entity_id.endswith("北极星债券") and item.entity_type == "fund" for item in graph.entities))

    def test_filename_alone_does_not_assign_a_report_subject(self) -> None:
        graph = build_financial_graph([
            _chunk("星海银行股份有限公司2026年第一季度报告_new.md", "目录"),
        ])
        self.assertEqual(graph.accepted("REPORTS_ON"), [])
        self.assertEqual(graph.entities, [])

    def test_device_manual_is_not_added_to_the_graph(self) -> None:
        graph = build_financial_graph([
            _chunk(DEVICE_FILE, "平安银行与华夏基金出现在设备说明书里。", item_name="Aolynk CB304n Cable网桥"),
        ])
        self.assertEqual(graph.documents, [])
        self.assertEqual(graph.assertions, [])

    def test_same_content_replays_and_changed_payload_conflicts(self) -> None:
        chunks = [_chunk("北极星债券资料.md", "北极星债券型证券投资基金的基金管理人：北极星基金管理有限公司。")]
        first = build_financial_graph(chunks)
        second = build_financial_graph(chunks)
        self.assertEqual(first.assertions[0].assertion_id, second.assertions[0].assertion_id)
        key = gid(first.kb_id, first.build_id, "KGAssertion", first.assertions[0].assertion_id)

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def run(self, query, **kwargs):
                self.calls.append((query, kwargs))

        replay = FakeSession()
        write_financial_graph(first, session=replay, existing_hashes={key: first.assertions[0].payload_hash})
        self.assertTrue(replay.calls)
        with self.assertRaises(ProjectionConflict):
            write_financial_graph(first, session=FakeSession(), existing_hashes={key: "different"})

    def test_writer_keeps_names_in_parameters(self) -> None:
        graph = build_financial_graph([
            _chunk("北极星债券资料.md", "北极星债券型证券投资基金的基金管理人：北极星基金管理有限公司。"),
        ])

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def run(self, query, **kwargs):
                self.calls.append((query, kwargs))

        session = FakeSession()
        write_financial_graph(graph, session=session)
        queries = "\n".join(query for query, _params in session.calls)
        self.assertNotIn("北极星基金管理有限公司", queries)
        predicates = [
            row.get("predicate")
            for _query, params in session.calls
            for row in params.get("rows") or []
        ]
        self.assertIn("MANAGED_BY", predicates)
        names = [
            row["canonical_name"]
            for _query, params in session.calls
            for row in params.get("rows") or []
            if row.get("canonical_name")
        ]
        self.assertIn("北极星基金管理有限公司", names)

    def test_new_registry_entity_is_visible_to_query_planning(self) -> None:
        registry = EntityRegistry(seeded=True)
        registry.provisional("organization", "星海银行股份有限公司")
        plan = plan_question("星海银行股份有限公司2024年的净利润是多少？", registry=registry)
        self.assertEqual(plan.entities[0].canonical_name, "星海银行股份有限公司")
        self.assertEqual(plan.entities[0].entity_id, "organization:星海银行股份有限公司")

    def test_graph_outage_does_not_fail_the_import(self) -> None:
        with patch("upload.knowledge_graph.write_financial_graph", side_effect=RuntimeError("down")):
            state = node_build_knowledge_graph({
                "chunks": [_chunk("北极星债券资料.md", "北极星债券型证券投资基金的基金管理人：北极星基金管理有限公司。")],
            })
        self.assertEqual(state["graph_status"], "unavailable")
        self.assertEqual(state["accepted_count"], 1)


class PurgePlanTests(TestCase):
    def test_only_device_files_are_selected(self) -> None:
        records = [
            {"file_name": DEVICE_FILE, "item_name": "Aolynk CB304n Cable网桥"},
            {"file_name": "M3070系列用户手册_new.md", "item_name": "M3070系列用户手册"},
            {"file_name": BANK_FILE, "item_name": "平安银行股份有限公司2026年第一季度报告"},
            {"file_name": "基金基础知识_new.md", "item_name": "基金投资者教育手册"},
        ]
        self.assertEqual(
            dropped_file_names(records),
            [DEVICE_FILE, "M3070系列用户手册_new.md"],
        )

    def test_filters_quote_file_names(self) -> None:
        filters = milvus_file_filters(['手册"特殊".md'])
        self.assertIn('file_name == "手册\\"特殊\\".md"', filters)

    def test_minio_match_uses_document_stem(self) -> None:
        self.assertTrue(minio_object_matches(
            "upload-images/M3070系列用户手册/page1.jpg",
            ["M3070系列用户手册_new.md"],
        ))
        self.assertFalse(minio_object_matches(
            "upload-images/平安银行股份有限公司2026年第一季度报告/page1.jpg",
            ["M3070系列用户手册_new.md"],
        ))
