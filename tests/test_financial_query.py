"""金融问题规划、证据状态、切分和检索范围。"""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from chat_service import ChatService, infer_pending_decision
from graph_relations import RelationEdge, business_edges_from_mentions, query_relations
from search.entity_resolution import resolve_catalog_hits
from search.evidence import assess_support, boundary_reply, build_citation
from search.query_plan import apply_mode, plan_question
from upload.financial_chunks import detect_content_type, split_by_document_type


class QueryPlanTests(TestCase):
    def test_greeting_stays_chat_but_greeting_prefix_does_not_hide_concept(self):
        greeting = plan_question("你好")
        concept = plan_question("你好，基金净值是什么")

        self.assertEqual(greeting.route, "general_chat")
        self.assertFalse(greeting.needs_retrieval)
        self.assertEqual(concept.route, "knowledge")
        self.assertEqual(concept.intent, "concept")
        self.assertTrue(concept.needs_retrieval)
        self.assertEqual(concept.entities, [])
        self.assertIsNone(concept.clarification)

    def test_concept_question_does_not_ask_for_a_product(self):
        plan = plan_question("什么是基金净值？")

        self.assertEqual(plan.route, "knowledge")
        self.assertEqual(plan.intent, "concept")
        self.assertEqual(plan.entities, [])
        self.assertIsNone(plan.clarification)
        self.assertFalse(plan.hyde_enabled)

    def test_single_company_period_stays_on_knowledge_path(self):
        plan = plan_question("分析平安银行2026年一季度业绩")

        self.assertEqual(plan.route, "knowledge")
        self.assertEqual(plan.entities[0].canonical_name, "平安银行")
        self.assertEqual(plan.entities[0].entity_type, "company")
        self.assertEqual(plan.period["year"], 2026)
        self.assertEqual(plan.period["quarter"], 1)
        self.assertIsNone(plan.clarification)

    def test_two_company_metric_list_is_program_parallel(self):
        plan = plan_question("平安银行与招商银行一季度业绩比较")

        names = [item.canonical_name for item in plan.entities]
        self.assertEqual(plan.route, "program_parallel")
        self.assertEqual(names, ["平安银行", "招商银行"])
        self.assertIsNone(plan.period["year"])
        self.assertEqual(plan.period["quarter"], 1)

    def test_multi_dimension_comparison_uses_research(self):
        plan = plan_question("比较平安银行和招商银行的盈利能力、资产质量和经营风险")

        self.assertEqual(plan.route, "research")
        self.assertEqual(plan.task_type, "company_comparison")
        self.assertEqual(
            [item.canonical_name for item in plan.entities],
            ["平安银行", "招商银行"],
        )

    def test_fast_mode_does_not_start_multi_agent(self):
        plan = plan_question("比较平安银行和招商银行的盈利能力、资产质量和经营风险")
        routed = apply_mode(plan, "fast")

        self.assertEqual(routed.route, "program_parallel")

    def test_bare_ping_an_asks_for_business_entity_not_a_file(self):
        plan = plan_question("平安的业绩怎么样？")

        self.assertIsNotNone(plan.clarification)
        self.assertNotIn("分数", plan.clarification)
        self.assertNotIn(".pdf", plan.clarification)
        shown = [item["display_name"] for item in plan.clarification_options]
        self.assertEqual(shown, ["平安银行", "中国平安"])

    def test_shared_alias_is_not_limited_to_one_company_name(self) -> None:
        from knowledge.registry import EntityRegistry

        registry = EntityRegistry(seeded=False)
        registry.provisional("organization", "星海银行")
        registry.provisional("organization", "星海保险")
        registry.share_alias("星海", ["organization:星海银行", "organization:星海保险"])

        plan = plan_question("星海的业绩怎么样？", registry=registry)

        self.assertEqual(
            [item["display_name"] for item in plan.clarification_options],
            ["星海银行", "星海保险"],
        )
        self.assertEqual(plan.entities, [])

    def test_session_entity_resolves_ping_an_and_pronoun_follow_up(self):
        first = plan_question("分析平安银行2026年一季度业绩")
        follow = plan_question("它的主要风险呢？", first.session_memory)

        self.assertEqual(follow.entities[0].canonical_name, "平安银行")
        self.assertEqual(follow.topic, "risk")
        self.assertEqual(follow.period["year"], 2026)
        self.assertIsNone(follow.clarification)
        self.assertEqual(follow.route, "knowledge")

    def test_explicit_switch_replaces_the_active_entity(self):
        first = plan_question("华夏债券C的赎回费用是多少？")
        switched = plan_question("换成招商银行的主要风险", first.session_memory)

        self.assertEqual(first.entities[0].canonical_name, "华夏债券C")
        self.assertEqual(first.entities[0].share_class, "C")
        self.assertEqual(switched.entities[0].canonical_name, "招商银行")
        self.assertEqual(switched.session_memory["resolved_entities"][0]["canonical_name"], "招商银行")

    def test_today_nav_requires_realtime_evidence(self):
        plan = plan_question("今天某基金的最新净值是多少？")

        self.assertEqual(plan.freshness_requirement, "realtime")
        self.assertEqual(plan.intent, "freshness")
        self.assertTrue(plan.needs_retrieval)

    def test_device_and_arithmetic_stay_on_the_existing_pipeline(self):
        self.assertEqual(plan_question("这款设备怎么开机？").route, "defer")
        self.assertEqual(plan_question("1+1等于几？").route, "defer")

    def test_corpus_company_and_share_class_resolve_without_a_file_choice(self):
        company = plan_question("贵州茅台2026年一季度营业收入是多少")
        fund = plan_question("易方达智造优势混合C的风险等级是什么")

        self.assertEqual(company.entities[0].canonical_name, "贵州茅台")
        self.assertEqual(company.entities[0].entity_type, "company")
        self.assertIsNone(company.clarification)
        self.assertEqual(fund.entities[0].canonical_name, "易方达智造优势混合C")
        self.assertEqual(fund.entities[0].share_class, "C")
        self.assertIsNone(fund.clarification)

    def test_managed_fund_question_is_a_relation_lookup(self):
        plan = plan_question("华夏基金还管理哪些已收录基金？")

        self.assertEqual(plan.intent, "relation")
        self.assertEqual(plan.task_type, "manager_products")
        self.assertEqual(plan.entities[0].canonical_name, "华夏基金")
        self.assertEqual(plan.entities[0].entity_type, "institution")

    def test_company_statement_divergence_and_industry_are_research(self):
        statement = plan_question("分析平安银行年报，利润和现金流为何背离")
        industry = plan_question("分析银行行业的经营变化和主要风险")

        self.assertEqual(statement.route, "research")
        self.assertEqual(statement.task_type, "company_statement")
        self.assertEqual(industry.route, "research")
        self.assertEqual(industry.task_type, "industry")
        self.assertEqual(industry.entities, [])


class EntityResolutionTests(TestCase):
    def test_exact_alias_is_selected_below_the_old_direct_threshold(self):
        decision = resolve_catalog_hits(
            ["平安银行"],
            {"平安银行": [{
                "entity_id": "company:平安银行",
                "canonical_name": "平安银行",
                "aliases": ["平安银行"],
                "score": 0.89,
                "doc_titles": ["平安银行2026年第一季度报告"],
            }]},
        )

        self.assertEqual(decision["status"], "resolved")
        self.assertEqual(decision["entity_ids"], ["company:平安银行"])
        self.assertFalse(decision["needs_clarification"])

    def test_same_entity_documents_merge_and_near_tie_entities_clarify(self):
        merged = resolve_catalog_hits(
            ["平安银行"],
            {"平安银行": [
                {
                    "entity_id": "company:平安银行",
                    "canonical_name": "平安银行",
                    "score": 0.80,
                    "doc_titles": ["手册A"],
                },
                {
                    "entity_id": "company:平安银行",
                    "canonical_name": "平安银行",
                    "score": 0.79,
                    "doc_titles": ["手册B"],
                },
            ]},
        )
        conflict = resolve_catalog_hits(
            ["平安"],
            {"平安": [
                {"entity_id": "company:平安银行", "canonical_name": "平安银行", "score": 0.910},
                {"entity_id": "company:中国平安", "canonical_name": "中国平安", "score": 0.909},
            ]},
        )

        self.assertEqual(len(merged["candidates"]), 1)
        self.assertEqual(merged["status"], "resolved")
        self.assertEqual(conflict["status"], "clarify")
        self.assertEqual(
            [item["entity_id"] for item in conflict["candidates"]],
            ["company:平安银行", "company:中国平安"],
        )

    def test_empty_catalog_does_not_block_a_concept_lookup(self):
        decision = resolve_catalog_hits([], {}, task_type="concept")

        self.assertEqual(decision["status"], "unscoped")
        self.assertEqual(decision["entity_ids"], [])
        self.assertFalse(decision["needs_clarification"])


class EvidenceTests(TestCase):
    def test_citation_uses_page_and_section_without_inventing_missing_ones(self):
        located = build_citation({
            "content": "基金净值是份额资产价值。",
            "file_name": "基金投资者教育手册.md",
            "document_title": "基金投资者教育手册",
            "page": 4,
            "section": "定义",
            "chunk_id": "secret-id",
        })
        missing = build_citation({
            "content": "只有正文。",
            "file_name": "笔记.md",
            "chunk_id": "secret-id",
        })

        self.assertEqual(located["page"], 4)
        self.assertEqual(located["section"], "定义")
        self.assertEqual(located["document_title"], "基金投资者教育手册")
        self.assertNotIn("secret-id", str(located))
        self.assertIsNone(missing["page"])
        self.assertIsNone(missing["section"])

    def test_retrieval_failure_is_not_reported_as_no_material(self):
        plan = plan_question("什么是基金净值？")
        failed = assess_support(plan, [], retrieval_error="MilvusTimeout")
        empty = assess_support(plan, [], retrieval_error=None)

        self.assertEqual(failed["status"], "retrieval_error")
        self.assertIn("检索失败", failed["user_message"])
        self.assertNotIn("没有资料", failed["user_message"])
        self.assertEqual(empty["status"], "unsupported")
        self.assertNotEqual(empty["user_message"], failed["user_message"])

    def test_comparison_missing_one_side_is_partial_and_conflict_keeps_both_values(self):
        plan = plan_question("分别列出平安银行和招商银行2024年营业收入")
        partial = assess_support(plan, [{
            "content": "平安银行 2024年 营业收入 100亿元",
            "file_name": "平安.md",
            "page": 3,
        }], retrieval_error=None)
        conflict = assess_support(plan_question("平安银行2024年营业收入是多少"), [
            {"content": "平安银行 2024年 营业收入 100亿元", "file_name": "一季报.md", "page": 2},
            {"content": "平安银行 2024年 营业收入 180亿元", "file_name": "更正公告.md", "page": 1},
        ], retrieval_error=None)

        self.assertEqual(partial["status"], "partial")
        self.assertIn("招商银行", partial["user_message"])
        self.assertEqual(conflict["status"], "conflict")
        self.assertIn("100", conflict["user_message"])
        self.assertIn("180", conflict["user_message"])

    def test_realtime_nav_and_profit_promise_have_separate_boundaries(self):
        nav = boundary_reply(
            plan_question("今天华夏债券C的最新净值是多少？"),
            [{"content": "华夏债券C 2024-12-31 净值 1.234 元", "file_name": "净值.md", "page": 1}],
            today=date(2026, 11, 4),
        )
        promise = boundary_reply(
            plan_question("这个理财产品保证赚钱吗？"),
            [{"content": "本产品不保证本金和收益。", "file_name": "说明书.md", "page": 2}],
            today=date(2026, 11, 4),
        )

        self.assertIn("2024-12-31", nav)
        self.assertIn("没有", nav)
        self.assertNotIn("今日净值为1.234", nav.replace(" ", ""))
        self.assertIn("不承诺", promise)
        self.assertNotIn("保证赚钱", promise)

    def test_degraded_table_cannot_verify_a_financial_number(self):
        plan = plan_question("平安银行2026年一季度净利润是多少")
        assessed = assess_support(plan, [{
            "content": "净利润\n20",
            "file_name": "季报.md",
            "parse_quality": "degraded",
            "numeric_verified": False,
        }], retrieval_error=None)

        self.assertIn(assessed["status"], {"partial", "unsupported"})
        self.assertTrue(assessed["numbers_unverified"])
        self.assertNotIn("20", assessed.get("verified_values", []))


class FinancialChunkTests(TestCase):
    def test_faq_keeps_each_question_with_its_answer(self):
        text = (
            "问：什么是基金净值？\n答：基金净值是份额对应的资产价值。\n"
            "问：赎回多久到账？\n答：到账时间以产品合同为准，没有统一天数。"
        )
        self.assertEqual(detect_content_type(text, "基金FAQ.md"), "faq")
        chunks = split_by_document_type(text, "faq", title="基金FAQ")

        self.assertEqual(len(chunks), 2)
        self.assertIn("什么是基金净值", chunks[0]["content"])
        self.assertIn("份额对应的资产价值", chunks[0]["content"])
        self.assertNotIn("赎回多久", chunks[0]["content"])

    def test_financial_table_row_keeps_header_unit_and_period(self):
        text = """## 主要财务指标

单位：人民币百万元

| 指标 | 2026年一季度 |
| --- | --- |
| 营业收入 | 100 |
| 净利润 | 20 |
"""
        chunks = split_by_document_type(text, "financial_report", title="平安银行2026年第一季度报告")
        profit = next(chunk for chunk in chunks if "净利润" in chunk["content"])

        self.assertIn("2026年一季度", profit["content"])
        self.assertIn("人民币百万元", profit["content"])
        self.assertEqual(profit["parse_quality"], "ok")
        self.assertTrue(profit["parent_id"])
        self.assertTrue(profit["numeric_verified"])

    def test_flattened_table_is_marked_degraded(self):
        chunks = split_by_document_type(
            "营业收入\n净利润\n100\n20\n",
            "financial_report",
            title="平安银行报告",
        )

        self.assertEqual(chunks[0]["parse_quality"], "degraded")
        self.assertFalse(chunks[0]["numeric_verified"])

    def test_policy_keeps_issuer_and_date_and_fee_keeps_its_condition(self):
        policy = split_by_document_type(
            "中国人民银行于2024年3月1日发布。适用范围：银行业金融机构。第一条 应当建立风险限额。",
            "policy",
            title="通知",
        )
        fee = split_by_document_type(
            "## 赎回费率\nC类份额持有少于7天收取1.5%，持有满7天不收取赎回费。",
            "prospectus",
            title="华夏债券",
        )

        self.assertEqual(policy[0]["issuer"], "中国人民银行")
        self.assertIn("2024-03-01", policy[0]["publish_date"])
        self.assertIn("少于7天", fee[0]["content"])
        self.assertIn("1.5%", fee[0]["content"])

    def test_device_manual_is_not_relabelled_as_financial(self):
        text = "# 开机\n长按电源键三秒。\n# 故障\n指示灯闪烁时重启。"
        self.assertEqual(detect_content_type(text, "平板用户指南.md"), "generic")

    def test_process_markdown_uses_typed_chunks_for_faq(self):
        from upload.node_docment_split import process_markdown

        with TemporaryDirectory() as folder:
            path = Path(folder) / "基金FAQ_new.md"
            path.write_text(
                "问：什么是基金净值？\n答：基金净值是份额对应的资产价值。\n"
                "问：赎回多久到账？\n答：以产品合同为准。\n",
                encoding="utf-8",
            )
            chunks = process_markdown(path, file_title="基金FAQ")

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["content_type"], "faq")
        self.assertIn("份额对应的资产价值", chunks[0]["content"])
        self.assertNotIn("赎回多久", chunks[0]["content"])


class RelationEvidenceTests(TestCase):
    def test_cooccurrence_does_not_become_a_management_edge(self):
        edges = business_edges_from_mentions([{
            "names": ["平安银行", "招商银行"],
            "file_name": "同页.pdf",
            "chunk_id": "c1",
        }])

        self.assertEqual(edges, [])

    def test_management_query_returns_source_chunks_and_drops_incomplete_paths(self):
        edges = [
            RelationEdge("fund:华夏债券C", "MANAGED_BY", "inst:华夏基金", ("e1",), "c1"),
            RelationEdge("fund:华夏货币A", "MANAGED_BY", "inst:华夏基金", ("e2",), "c2"),
            RelationEdge("company:子公司", "SUBSIDIARY_OF", "company:平安银行", ("e3",), "c3"),
            RelationEdge("company:平安银行", "SUBSIDIARY_OF", "company:中国平安", (), "c4"),
        ]

        managed = query_relations(edges, start="inst:华夏基金", relation="MANAGED_BY", direction="in", max_hops=1)
        incomplete = query_relations(
            edges, start="company:子公司", relation="SUBSIDIARY_OF", direction="out", max_hops=2,
        )

        self.assertEqual(
            sorted(item["entity_id"] for item in managed),
            ["fund:华夏债券C", "fund:华夏货币A"],
        )
        self.assertEqual(sorted(item["chunk_id"] for item in managed), ["c1", "c2"])
        self.assertTrue(all(item["complete"] for item in managed))
        self.assertEqual([item["entity_id"] for item in incomplete], ["company:平安银行"])
        self.assertNotIn("company:中国平安", [item["entity_id"] for item in incomplete])

    def test_relation_evidence_keeps_only_chunks_on_complete_edges(self):
        from graph_relations import relation_evidence

        plan = plan_question("华夏基金还管理哪些已收录基金？")
        chunks = [
            {"chunk_id": "c1", "content": "华夏基金管理华夏债券C"},
            {"chunk_id": "c9", "content": "同页共现不能当管理关系"},
        ]
        edges = [RelationEdge("fund:华夏债券C", "MANAGED_BY", "institution:华夏基金", ("e1",), "c1")]

        kept, gap = relation_evidence(plan, chunks, edges, graph_error=None)
        failed, failed_gap = relation_evidence(plan, chunks, [], graph_error="ConnectionError")

        self.assertIsNone(gap)
        self.assertEqual([item["chunk_id"] for item in kept], ["c1"])
        self.assertEqual(failed, [])
        self.assertIn("不能", failed_gap)
        self.assertNotIn("华夏债券C", failed_gap)


class WorkflowRouteTests(TestCase):
    def test_concept_question_skips_product_gate_and_searches_current_kb(self):
        from search.workflow import run_search_workflow

        def search(state, plan):
            self.assertEqual(plan.intent, "concept")
            state["financial_hits"] = [{
                "content": "基金净值是份额对应的资产价值。",
                "file_name": "手册.md",
                "document_title": "基金投资者教育手册",
                "page": 4,
                "section": "定义",
            }]
            return state

        def answer(state):
            state["final_answer"] = "基金净值是份额对应的资产价值。"
            state["answer_message_seq"] = 2
            return state

        with (
            patch("search.node_item_name_confirm.node_item_name_confirm") as confirm,
            patch("search.financial_retrieve.search_financial_evidence", side_effect=search),
            patch("search.node_answer_output.node_answer_output", side_effect=answer),
        ):
            state = run_search_workflow({"current_question": "什么是基金净值？"})

        confirm.assert_not_called()
        self.assertEqual(state["evidence_status"], "sufficient")
        self.assertEqual(state["citations"][0]["page"], 4)
        self.assertNotEqual(state["item_name_confirm_status"], "general_chat")

    def test_greeting_does_not_call_product_confirmation(self):
        from search.workflow import run_search_workflow

        with patch("search.node_item_name_confirm.node_item_name_confirm") as confirm:
            state = run_search_workflow({"current_question": "你好"})

        confirm.assert_not_called()
        self.assertEqual(state["item_name_confirm_status"], "general_chat")

    def test_device_question_still_uses_product_confirmation(self):
        from search.workflow import run_search_workflow

        with patch(
            "search.node_item_name_confirm.node_item_name_confirm",
            side_effect=lambda state: {**state, "item_name_confirm_status": "general_chat"},
        ) as confirm:
            run_search_workflow({"current_question": "这款设备怎么开机？"})

        confirm.assert_called_once()

    def test_retrieval_exception_is_a_service_failure(self):
        from search.workflow import run_search_workflow

        with (
            patch("search.node_item_name_confirm.node_item_name_confirm") as confirm,
            patch(
                "search.financial_retrieve.search_financial_evidence",
                side_effect=ConnectionError("milvus down"),
            ),
        ):
            state = run_search_workflow({"current_question": "什么是基金净值？"})

        confirm.assert_not_called()
        self.assertEqual(state["evidence_status"], "retrieval_error")
        self.assertIn("检索失败", state["final_answer"])
        self.assertNotIn("暂无查询结果", state["final_answer"])

    def test_fast_mode_skips_the_research_runtime(self):
        from search.workflow import run_search_workflow

        def search(state, plan):
            self.assertEqual(plan.route, "program_parallel")
            state["financial_hits"] = [{"content": "已收录资料", "file_name": "年报.md"}]
            return state

        def answer(state):
            state["final_answer"] = "已按快速模式核对资料。"
            state["answer_message_seq"] = 3
            return state

        with (
            patch("search.financial_retrieve.search_financial_evidence", side_effect=search),
            patch("search.node_answer_output.node_answer_output", side_effect=answer),
            patch("research.runtime.run_research") as research,
        ):
            run_search_workflow({
                "current_question": "比较平安银行和招商银行的盈利能力、资产质量和经营风险",
                "answer_mode": "fast",
            })

        research.assert_not_called()

    def test_relation_question_does_not_invent_products_when_graph_fails(self):
        from search.workflow import run_search_workflow

        def search(state, plan):
            state["financial_hits"] = [{
                "content": "同页出现华夏基金和华夏债券C",
                "file_name": "同页.md",
                "chunk_id": "c1",
            }]
            return state

        with (
            patch("search.financial_retrieve.search_financial_evidence", side_effect=search),
            patch("graph_store.load_business_edges", side_effect=ConnectionError("neo4j down")),
            patch("search.node_answer_output.node_answer_output") as answer,
        ):
            state = run_search_workflow({"current_question": "华夏基金还管理哪些已收录基金？"})

        answer.assert_not_called()
        self.assertIn("不能", state["final_answer"])
        self.assertNotIn("华夏债券C", state["final_answer"])
        self.assertEqual(state["evidence_status"], "unsupported")


class VectorCacheTests(TestCase):
    def test_blank_vector_cache_is_initialized_before_hybrid_search(self):
        from search.financial_retrieve import search_financial_evidence

        seen = {}

        def hybrid(_client, _settings, _collection, _text, _expression, _limit, _fields, cache):
            seen["cache"] = cache
            return []

        class _Client:
            def load_collection(self, collection_name):
                return None

            def close(self):
                return None

        class _Settings:
            chunks_collection = "chunks"
            bge_model_name = "bge"
            bge_device = "cpu"
            bge_fp16 = False
            embedding_dim = 1024

        plan = plan_question("什么是基金净值？")
        state = {"question_rewritten": "什么是基金净值？", "search_query_vector_cache": {}}
        with (
            patch("search.node_search_embedding.load_search_settings", return_value=_Settings()),
            patch("search.node_search_embedding.get_milvus_client", return_value=_Client()),
            patch("search.node_search_embedding._hybrid_search", side_effect=hybrid),
        ):
            search_financial_evidence(state, plan)

        self.assertEqual(seen["cache"]["vectors"], {})
        self.assertIn("model_signature", seen["cache"])

    def test_per_entity_recall_keeps_the_lower_scoring_company(self):
        from search.financial_retrieve import assemble_evidence

        plan = plan_question("分别列出平安银行和招商银行2024年营业收入")
        hits = [
            {"content": f"平安银行 2024年 营业收入 {index}亿元", "item_name": "平安银行报告", "score": 1 - index / 100}
            for index in range(12)
        ]
        hits.append({
            "content": "招商银行 2024年 营业收入 200亿元",
            "item_name": "招商银行报告",
            "score": 0.11,
        })

        kept = assemble_evidence(hits, plan, per_subject_limit=3)
        joined = "\n".join(item["content"] for item in kept)
        self.assertIn("招商银行", joined)
        self.assertLessEqual(sum("平安银行" in item["content"] for item in kept), 3)

    def test_rerank_keeps_the_relevant_passage_ahead_of_vector_score(self) -> None:
        from search.financial_retrieve import search_financial_evidence

        class _Reranker:
            def compute_score(self, pairs, normalize=True):
                del normalize
                scores = []
                for _question, content in pairs:
                    scores.append(0.95 if "营业收入" in content else 0.2)
                return scores

        def hybrid(_client, _settings, _collection, _text, _expression, _limit, _fields, cache):
            del cache
            hits = [
                {
                    "id": f"noise-{index}",
                    "distance": 0.99 - index / 100,
                    "entity": {
                        "content": f"平安银行 风险提示 {index}",
                        "item_name": "平安银行报告",
                        "file_name": "pingan.md",
                    },
                }
                for index in range(9)
            ]
            hits.append({
                "id": "revenue",
                "distance": 0.01,
                "entity": {
                    "content": "平安银行 2024年 营业收入 100亿元",
                    "item_name": "平安银行报告",
                    "file_name": "pingan.md",
                },
            })
            return hits

        class _Client:
            def load_collection(self, collection_name):
                return None

            def close(self):
                return None

        class _Settings:
            chunks_collection = "chunks"
            bge_model_name = "bge"
            bge_device = "cpu"
            bge_fp16 = False
            embedding_dim = 1024

        plan = plan_question("平安银行2024年营业收入是多少")
        state = {"question_rewritten": plan.rewritten, "search_query_vector_cache": {}}
        with (
            patch("search.node_search_embedding.load_search_settings", return_value=_Settings()),
            patch("search.node_search_embedding.get_milvus_client", return_value=_Client()),
            patch("search.node_search_embedding._hybrid_search", side_effect=hybrid),
            patch("search.node_rerank.get_reranker", return_value=_Reranker()),
        ):
            search_financial_evidence(state, plan)

        kept = state["financial_hits"]
        self.assertEqual(kept[0]["chunk_id"], "revenue")
        self.assertEqual(kept[0]["rerank_score"], 0.95)
        self.assertNotIn("noise-8", [item["chunk_id"] for item in kept])
        self.assertEqual(state["rerank_chunks"][0]["chunk_id"], "revenue")


class SessionHandoffTests(TestCase):
    def test_follow_up_receives_the_resolved_company(self):
        from tests.test_chat_service import FakeCollection

        seen = []

        def workflow(state):
            seen.append(state.get("session_memory") or {})
            plan = plan_question(state["current_question"], state.get("session_memory"))
            state["session_memory"] = plan.session_memory
            state["final_answer"] = plan.rewritten
            state["item_name_confirm_status"] = "knowledge"
            return state

        service = ChatService(FakeCollection(), FakeCollection(), lambda history: "不应走闲聊", workflow)
        conversation = service.create("browser-a")
        service.send("browser-a", conversation["conversation_id"], "分析平安银行2026年一季度业绩")
        service.send("browser-a", conversation["conversation_id"], "它的主要风险呢？")

        self.assertEqual(seen[0], {})
        self.assertEqual(seen[1]["resolved_entities"][0]["canonical_name"], "平安银行")
        self.assertEqual(seen[1]["active_period"]["year"], 2026)

    def test_typed_business_name_selects_pending_candidate(self):
        pending = {"item_name_candidates": [
            {"itemname_question": "平安银行", "display_name": "平安银行"},
            {"itemname_question": "中国平安", "display_name": "中国平安"},
        ]}

        self.assertEqual(
            infer_pending_decision("平安银行", pending),
            {"item_name_user_selection": "平安银行"},
        )
