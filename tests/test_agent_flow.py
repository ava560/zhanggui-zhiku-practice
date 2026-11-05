"""主 Agent、两个子 Agent 和共用私有库查询。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from knowledge.registry import EntityRegistry
from search.agent_workflow import run_agent_workflow
from search.citations import compose_sourced_text, enforce_sourced_answer
from search.intent_classify import IntentClassificationError, classify_intent
from search.long_term_memory import LongTermMemoryBook, extract_explicit_memory
from search.node_item_name_confirm import classify_matches
from search.node_rrf import fuse_chunks
from search.react_agent import run_react
from search.self_ask import run_private_self_ask, run_web_self_ask
from search.shared_query import run_shared_query


def _ports(**overrides):
    calls = []

    def prepare(state):
        calls.append("prepare")
        return {
            "question_rewritten": state.get("question_rewritten") or state["current_question"],
            "extracted_names": list(state.get("extracted_names") or []),
        }

    def match_names(names, question):
        calls.append(("match", tuple(names), question))
        return state_match

    def resolve_registry(question, unresolved):
        calls.append(("registry", question, tuple(unresolved)))
        return {"confirmed": [], "ambiguous": False, "candidates": [], "message": ""}

    def search_named(question, names):
        calls.append(("named", question, tuple(names)))
        return [{
            "chunk_id": "kb-1",
            "item_name": names[0],
            "file_name": "星海说明书.md",
            "content": "星海债券A 风险等级 R2",
            "score": 0.8,
        }]

    def search_direct(question):
        calls.append(("direct", question))
        return [{
            "chunk_id": "kb-direct",
            "file_name": "术语.md",
            "content": "净值是份额对应的资产价值。",
            "score": 0.7,
        }]

    def search_hyde(question, names):
        calls.append(("hyde", question, None if names is None else tuple(names)))
        return [{
            "chunk_id": "kb-hyde",
            "item_name": "" if not names else names[0],
            "file_name": "术语.md",
            "content": "假设文本里的 999 亿元不能当事实。",
            "score": 0.6,
        }]

    def search_web(question):
        calls.append(("web", question))
        return [{
            "title": "星海债券说明",
            "url": "https://example.com/xinghai",
            "content": "星海债券A 的公开摘要",
        }]

    state_match = {"item_name_confirm_status": "matched", "itemname_question": ["星海债券A"], "item_name_candidates": [], "item_name_confirmation_message": ""}
    ports = {
        "prepare": prepare,
        "match_names": match_names,
        "resolve_registry": resolve_registry,
        "search_named": search_named,
        "search_direct": search_direct,
        "search_hyde": search_hyde,
        "search_web": search_web,
        "calls": calls,
    }
    ports.update(overrides)
    return ports


class ThreeWayRrfTests(TestCase):
    def test_web_summaries_join_hyde_and_normal_retrieval(self):
        fused = fuse_chunks(
            [{"chunk_id": "a", "content": "库内", "file_name": "a.md", "score": 0.9}],
            [{"chunk_id": "b", "content": "假设检索", "file_name": "b.md", "score": 0.8}],
            [{
                "title": "网页",
                "url": "https://example.com/a",
                "content": "摘要",
            }],
        )
        urls = [item.get("url") for item in fused]
        self.assertIn("https://example.com/a", urls)
        web = next(item for item in fused if item.get("url") == "https://example.com/a")
        self.assertEqual(web["source_ranks"]["web"], 1)
        self.assertAlmostEqual(web["rrf_score"], 1 / 61)

    def test_web_summary_without_a_link_is_dropped(self):
        fused = fuse_chunks(
            [{"chunk_id": "a", "content": "库内", "file_name": "a.md", "score": 0.9}],
            [{"chunk_id": "a", "content": "库内", "file_name": "a.md", "score": 0.8}],
            [{"title": "没有链接", "content": "不能用", "url": ""}],
        )
        self.assertEqual([item["chunk_id"] for item in fused], ["a"])
        self.assertNotIn("web", fused[0]["source_ranks"])


class SharedQueryTests(TestCase):
    def test_no_name_skips_the_name_index_and_searches_chunks(self):
        ports = _ports()
        result = run_shared_query({"current_question": "什么是份额净值？", "extracted_names": []}, ports)

        self.assertNotIn("match", [item[0] for item in ports["calls"] if isinstance(item, tuple)])
        self.assertIn(("direct", "什么是份额净值？"), ports["calls"])
        self.assertEqual(result["status"], "supported")
        self.assertNotIn("final_answer", result)

    def test_score_equal_to_0_9_is_adopted_without_asking(self):
        def match_names(names, question):
            return classify_matches(list(names), {names[0]: [{
                "itemname_question": "星海债券A",
                "score": 0.9,
            }]})

        ports = _ports(match_names=match_names)
        result = run_shared_query({
            "current_question": "星海债券A 的风险等级",
            "extracted_names": ["星海债券A"],
        }, ports)

        self.assertFalse(result["awaiting_user_confirmation"])
        self.assertIn(("named", "星海债券A 的风险等级", ("星海债券A",)), ports["calls"])

    def test_middle_score_pauses_before_chunk_search(self):
        def match_names(names, question):
            return classify_matches(list(names), {names[0]: [
                {"itemname_question": "星海债券A", "score": 0.8},
                {"itemname_question": "星海债券B", "score": 0.7},
            ]})

        ports = _ports(match_names=match_names)
        result = run_shared_query({
            "current_question": "星海债券怎么样",
            "extracted_names": ["星海债券"],
        }, ports)

        self.assertTrue(result["awaiting_user_confirmation"])
        self.assertGreaterEqual(len(result["item_name_candidates"]), 2)
        self.assertNotIn("named", [item[0] for item in ports["calls"] if isinstance(item, tuple)])

    def test_vector_miss_uses_registry_and_asks_about_an_unknown_alias(self):
        registry = EntityRegistry(seeded=False)
        registry.provisional("organization", "星海银行")
        registry.provisional("organization", "星海保险")
        registry.share_alias("星海", ["organization:星海银行", "organization:星海保险"])

        def match_names(names, question):
            return classify_matches(list(names), {names[0]: [{
                "itemname_question": "其他机构",
                "score": 0.2,
            }]})

        def resolve_registry(question, unresolved):
            ambiguous = registry.ambiguous_aliases(question)
            if ambiguous:
                alias, records = ambiguous[0]
                return {
                    "confirmed": [],
                    "ambiguous": True,
                    "message": f"请确认“{alias}”指哪一个",
                    "candidates": [{"itemname_question": item.canonical_name, "display_name": item.canonical_name, "score": 0.0} for item in records],
                }
            return {"confirmed": [item.canonical_name for item in registry.find_in_text(question)], "ambiguous": False, "candidates": [], "message": ""}

        ports = _ports(match_names=match_names, resolve_registry=resolve_registry)
        result = run_shared_query({
            "current_question": "星海的业绩怎么样？",
            "extracted_names": ["星海"],
        }, ports)

        self.assertTrue(result["awaiting_user_confirmation"])
        self.assertEqual(
            {item["display_name"] for item in result["item_name_candidates"]},
            {"星海银行", "星海保险"},
        )
        self.assertNotIn("named", [item[0] for item in ports["calls"] if isinstance(item, tuple)])

    def test_web_failure_keeps_private_chunks(self):
        def search_web(question):
            raise ConnectionError("mcp down")

        ports = _ports(search_web=search_web)
        result = run_shared_query({"current_question": "什么是份额净值？", "extracted_names": []}, ports)

        self.assertEqual(result["status"], "supported")
        self.assertIn("联网", result["web_search_error"])
        self.assertTrue(result["chunks"])

    def test_private_search_failure_is_not_an_empty_library(self):
        def search_direct(question):
            raise ConnectionError("milvus down")

        ports = _ports(search_direct=search_direct)
        result = run_shared_query({"current_question": "什么是份额净值？", "extracted_names": []}, ports)

        self.assertEqual(result["status"], "retrieval_error")
        self.assertIn("检索失败", result["user_message"])
        self.assertNotIn("未检索到足够信息", result["user_message"])


class SelfAskAndReactTests(TestCase):
    def test_private_sub_agent_uses_the_same_query_and_does_not_answer(self):
        ports = _ports()
        state = {"current_question": "星海债券A 的风险等级", "extracted_names": ["星海债券A"]}
        direct = run_shared_query(state, ports)
        ports["calls"].clear()
        pack = run_private_self_ask(
            {"task_id": "t1", "question": state["current_question"]},
            state,
            ports,
            decide_followup=lambda *_: None,
        )

        self.assertEqual(pack["chunks"][0]["chunk_id"], direct["chunks"][0]["chunk_id"])
        self.assertIn(("hyde", state["current_question"], ("星海债券A",)), ports["calls"])
        self.assertIn(("web", state["current_question"]), ports["calls"])
        self.assertNotIn("final_answer", pack)
        self.assertEqual(pack["task_type"], "private_kb")

    def test_third_followup_is_not_executed(self):
        ports = _ports()
        pack = run_private_self_ask(
            {"task_id": "t1", "question": "星海债券A 的风险等级"},
            {"current_question": "星海债券A 的风险等级", "extracted_names": ["星海债券A"]},
            ports,
            decide_followup=lambda *_: "再查一次星海债券A",
        )
        hyde_calls = [item for item in ports["calls"] if isinstance(item, tuple) and item[0] == "hyde"]
        self.assertEqual(len(hyde_calls), 3)
        self.assertEqual(len(pack["followups"]), 2)

    def test_web_agent_drops_results_without_links(self):
        seen = []

        def search_web(question):
            seen.append(question)
            return [
                {"title": "有链接", "url": "https://example.com/ok", "content": "可用"},
                {"title": "无链接", "content": "不能用"},
            ]

        pack = run_web_self_ask(
            {"task_id": "w1", "question": "星海债券最新公告"},
            search_web,
            decide_followup=lambda *_: None,
        )
        self.assertEqual([item["url"] for item in pack["chunks"]], ["https://example.com/ok"])
        self.assertNotIn("final_answer", pack)
        self.assertEqual(seen, ["星海债券最新公告"])

    def test_react_rejects_the_fifth_task_and_unknown_actions(self):
        questions = []

        def planner(context):
            if context["dispatches"]:
                return [{"action": "finish", "thought": "停止"}]
            return [
                {"action": "private_kb", "question": f"子任务{index}", "thought": "拆开"}
                for index in range(1, 6)
            ]

        def private_runner(action):
            questions.append(action["question"])
            return {"task_id": action["question"], "task_type": "private_kb", "status": "unsupported", "chunks": [], "gaps": ["缺资料"], "followups": []}

        result = run_react("比较星海和北辰", planner=planner, private_runner=private_runner, web_runner=lambda action: None)
        self.assertEqual(questions, ["子任务1", "子任务2", "子任务3", "子任务4"])
        self.assertNotIn("子任务5", questions)
        self.assertTrue(result["stopped_for_limit"])

        def bad_planner(_context):
            return [{"action": "delete_database", "thought": "不行"}]

        with self.assertRaises(PermissionError):
            run_react("比较星海和北辰", planner=bad_planner, private_runner=private_runner, web_runner=lambda action: None)

    def test_third_supplement_round_does_not_start(self):
        plans = []

        def planner(context):
            plans.append(context["supplements_done"])
            return [{"action": "private_kb", "question": f"轮次{context['supplements_done']}", "thought": "仍有缺口"}]

        def private_runner(action):
            return {"task_id": action["question"], "task_type": "private_kb", "status": "unsupported", "chunks": [], "gaps": ["缺"], "followups": []}

        run_react("比较星海和北辰", planner=planner, private_runner=private_runner, web_runner=lambda action: None)
        self.assertEqual(plans, [0, 1, 2])

    def test_react_trace_records_thought_action_and_observation(self):
        def planner(context):
            if context["dispatches"]:
                return [{"action": "finish", "thought": "够了"}]
            return [{"action": "web_search", "question": "星海公告", "thought": "需要网页"}]

        def web_runner(action):
            return {"task_id": "w", "task_type": "web_search", "status": "supported", "chunks": [{"url": "https://example.com/a", "title": "公告", "content": "正文"}], "gaps": [], "followups": []}

        result = run_react("星海公告", planner=planner, private_runner=lambda action: None, web_runner=web_runner)
        self.assertEqual(result["trace"][0]["action"], "web_search")
        self.assertEqual(result["trace"][0]["thought"], "需要网页")
        self.assertEqual(result["trace"][0]["observation"]["status"], "supported")


class IntentAndMemoryTests(TestCase):
    def test_intent_follows_the_llm_and_retries_once(self):
        calls = []

        def llm(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return "不是 JSON"
            return '{"intent": "simple"}'

        self.assertEqual(classify_intent("比较星海和北辰的风险", llm), "simple")
        self.assertIn("复杂任务", calls[0])
        self.assertEqual(len(calls), 2)

        def always_bad(_prompt):
            return "仍然不行"

        with self.assertRaises(IntentClassificationError):
            classify_intent("你好", always_bad)

    def test_explicit_memory_is_saved_and_ordinary_questions_are_not(self):
        with TemporaryDirectory() as directory:
            book = LongTermMemoryBook(Path(directory) / "memory.json")
            self.assertEqual(extract_explicit_memory("记住我只看稳健型产品"), "我只看稳健型产品")
            self.assertIsNone(extract_explicit_memory("星海债券A 的风险等级是什么"))
            book.add("user-1", "我只看稳健型产品", "user_explicit")
            self.assertEqual(book.texts("user-1"), ["我只看稳健型产品"])
            book.delete("user-1", "我只看稳健型产品")
            self.assertEqual(book.texts("user-1"), [])
            with self.assertRaises(ValueError):
                book.add("user-1", "自动学来的", "inferred")


class AgentWorkflowTests(TestCase):
    def test_chat_intent_does_not_search_and_only_explicit_memory_is_saved(self):
        ports = _ports()
        book = LongTermMemoryBook()
        state = run_agent_workflow(
            {"current_question": "记住我只看稳健型产品", "user_id": "u1"},
            llm=lambda _prompt: '{"intent":"chat"}',
            ports=ports,
            memory=book,
        )
        self.assertEqual(state["item_name_confirm_status"], "general_chat")
        self.assertNotIn("direct", [item[0] for item in ports["calls"] if isinstance(item, tuple)])
        self.assertEqual(book.texts("u1"), ["我只看稳健型产品"])

        ports = _ports()
        book = LongTermMemoryBook()
        answered = run_agent_workflow(
            {"current_question": "什么是份额净值？", "user_id": "u2", "extracted_names": []},
            llm=lambda _prompt: '{"intent":"simple"}',
            ports=ports,
            memory=book,
            answerer=lambda _state, chunks, gaps, web_error: compose_sourced_text(chunks, gaps, web_error),
        )
        self.assertIn("术语.md", answered["final_answer"])
        self.assertTrue(answered["citations"])
        self.assertEqual(book.texts("u2"), [])

        failed = run_agent_workflow(
            {"current_question": "什么是份额净值？", "user_id": "u3"},
            llm=lambda _prompt: "不是分类",
            ports=_ports(),
            memory=LongTermMemoryBook(),
        )
        self.assertIn("分类失败", failed["final_answer"])
        self.assertEqual(failed["evidence_status"], "retrieval_error")


class WebSearchSwitchTests(TestCase):
    def test_shared_query_skips_web_when_the_user_turns_it_off(self):
        ports = _ports()
        result = run_shared_query(
            {"current_question": "什么是份额净值？", "extracted_names": [], "use_web_search": False},
            ports,
        )
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "web" for call in ports["calls"]))
        self.assertTrue(result["chunks"])
        self.assertFalse(any(item.get("url") for item in result["chunks"]))

    def test_answer_uses_citation_marks_and_keeps_file_and_web_addresses(self):
        from search.citations import annotate_answer

        answer, citations = annotate_answer(
            "净值是份额对应的资产价值。公开资料也这样说明。",
            [
                {"file_name": "术语.md", "item_name": "手册", "content": "净值是份额对应的资产价值。"},
                {"title": "净值说明", "url": "https://example.com/nav", "content": "公开资料也这样说明。"},
            ],
            download_for=lambda name: "/api/sources/download?name=术语.md" if name == "术语.md" else "",
        )
        self.assertIn("[1]", answer)
        self.assertIn("[2]", answer)
        self.assertNotIn("来源：", answer)
        self.assertEqual(citations[0]["index"], 1)
        self.assertEqual(citations[0]["source_file"], "术语.md")
        self.assertEqual(citations[0]["download_url"], "/api/sources/download?name=术语.md")
        self.assertEqual(citations[1]["url"], "https://example.com/nav")
        self.assertIn("净值说明", citations[1]["title"])

    def test_progress_names_each_stage_and_skips_web_when_turned_off(self):
        notes = []
        ports = _ports()
        run_agent_workflow(
            {
                "current_question": "什么是份额净值？",
                "user_id": "u-progress",
                "extracted_names": [],
                "use_web_search": False,
                "on_status": notes.append,
            },
            llm=lambda _prompt: '{"intent":"simple"}',
            ports=ports,
            memory=LongTermMemoryBook(),
            answerer=lambda *_args: "净值是份额对应的资产价值。",
        )
        self.assertIn("正在检索知识库", notes)
        self.assertIn("正在组织回答", notes)
        self.assertNotIn("正在联网搜索", notes)


class CitationTests(TestCase):
    def test_web_fact_without_a_link_is_removed(self):
        chunks = [
            {"title": "星海公告", "url": "https://example.com/a", "content": "星海债券A 风险等级 R2"},
            {"file_name": "星海说明书.md", "content": "赎回 T+3", "item_name": "星海债券A"},
        ]
        kept = enforce_sourced_answer(
            "根据星海说明书.md，赎回 T+3。星海公告称风险等级 R2。这是没有来源的句子。",
            chunks,
        )
        self.assertIn("星海说明书.md", kept)
        self.assertNotIn("没有来源", kept)
        self.assertNotIn("星海公告称", kept)

        linked = enforce_sourced_answer(
            "星海公告：风险等级 R2 https://example.com/a",
            chunks,
        )
        self.assertIn("https://example.com/a", linked)
