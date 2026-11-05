"""流式事件先给状态，再给已核对答案，并且只归档一次。"""

from unittest import TestCase

from chat_service import ChatService
from tests.test_chat_service import FakeCollection


class ChatStreamTests(TestCase):
    def _service(self, workflow, stream_answer=None):
        return ChatService(
            FakeCollection(),
            FakeCollection(),
            lambda history: "不应整段等待",
            workflow,
            stream_answer=stream_answer,
        )

    def test_status_precedes_answer_and_citations_are_archived_once(self):
        def workflow(state):
            state["final_answer"] = "基金净值是份额对应的资产价值。"
            state["citations"] = [{
                "document_title": "基金投资者教育手册",
                "file_name": "手册.md",
                "page": 4,
                "section": "定义",
                "source_span": "份额对应的资产价值",
            }]
            state["evidence_status"] = "sufficient"
            state["item_name_confirm_status"] = "knowledge"
            return state

        service = self._service(workflow)
        conversation = service.create("browser-a")
        events = list(service.iter_events(
            "browser-a",
            conversation["conversation_id"],
            "什么是基金净值？",
        ))
        names = [event["event"] for event in events]

        self.assertLess(names.index("status"), names.index("answer_delta"))
        self.assertEqual(names[-2:], ["citations", "done"])
        self.assertEqual(events[names.index("citations")]["data"]["citations"][0]["page"], 4)
        archived = [
            message for message in service.get("browser-a", conversation["conversation_id"])["messages"]
            if message["role"] == "assistant"
        ]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["content"], "基金净值是份额对应的资产价值。")
        self.assertEqual(archived[0]["citations"][0]["page"], 4)
        self.assertEqual(archived[0]["evidence_status"], "sufficient")

    def test_general_chat_stream_replaces_a_return_promise(self):
        def workflow(state):
            state["item_name_confirm_status"] = "general_chat"
            return state

        def stream_answer(_history):
            yield "可以继续了解资料。这只产品保证收益。"

        service = self._service(workflow, stream_answer)
        conversation = service.create("browser-a")
        events = list(service.iter_events("browser-a", conversation["conversation_id"], "你好"))
        text = "".join(event["data"]["text"] for event in events if event["event"] == "answer_delta")

        self.assertNotIn("保证收益", text)
        self.assertIn("不承诺收益", text)
        archived = [
            message for message in service.get("browser-a", conversation["conversation_id"])["messages"]
            if message["role"] == "assistant"
        ]
        self.assertEqual(len(archived), 1)

    def test_fast_mode_is_visible_to_the_workflow(self):
        seen = {}

        def workflow(state):
            seen["mode"] = state.get("answer_mode")
            state["item_name_confirm_status"] = "general_chat"
            return state

        service = self._service(workflow, lambda _history: iter(["好的。"]))
        conversation = service.create("browser-a")
        list(service.iter_events(
            "browser-a",
            conversation["conversation_id"],
            "你好",
            answer_mode="fast",
        ))

        self.assertEqual(seen["mode"], "fast")
