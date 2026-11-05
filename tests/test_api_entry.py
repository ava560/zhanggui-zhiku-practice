"""FastAPI 入口仍提供原来的配置和上传校验。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app


class ApiEntryTests(TestCase):
    def test_personal_memory_can_be_saved_and_deleted(self) -> None:
        from search.long_term_memory import LongTermMemoryBook

        book = LongTermMemoryBook()
        client = TestClient(app)
        with patch("api.get_memory_book", return_value=book):
            saved = client.post("/api/memory", json={"text": "只看稳健型产品"})
            self.assertEqual(saved.status_code, 201)
            listed = client.get("/api/memory")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.json()["items"][0]["text"], "只看稳健型产品")
            self.assertEqual(listed.json()["items"][0]["source"], "settings")
            removed = client.request("DELETE", "/api/memory", json={"text": "只看稳健型产品"})
            self.assertEqual(removed.status_code, 200)
            self.assertTrue(removed.json()["removed"])
            self.assertEqual(client.get("/api/memory").json()["items"], [])

    def test_imported_source_can_be_downloaded_by_cited_name(self) -> None:
        from search.source_files import find_imported_file

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "task" / "source"
            source.mkdir(parents=True)
            original = source / "基金基础知识.pdf"
            original.write_bytes(b"%PDF")
            artifacts = root / "task" / "artifacts"
            artifacts.mkdir()
            (artifacts / "基金基础知识_new.md").write_text("markdown", encoding="utf-8")
            found = find_imported_file("基金基础知识_new.md", root)
            self.assertEqual(found, original)
            notes = root / "only" / "artifacts"
            notes.mkdir(parents=True)
            (notes / "只有笔记_new.md").write_text("markdown", encoding="utf-8")
            self.assertIsNone(find_imported_file("只有笔记_new.md", root))
    def test_config_accepts_word(self) -> None:
        response = TestClient(app).get("/api/config")
        self.assertEqual(response.status_code, 200)
        allowed = set(response.json()["allowed_extensions"])
        self.assertTrue({".pdf", ".md", ".docx", ".doc"} <= allowed)

    def test_rejects_plain_text_upload(self) -> None:
        response = TestClient(app).post(
            "/api/tasks",
            params={"filename": "notes.txt"},
            content=b"hello",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Word", response.json()["error"])

    def test_chat_page_uses_financial_examples_and_the_stream(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "web"
        html = (root / "chat.html").read_text(encoding="utf-8")
        script = (root / "chat.js").read_text(encoding="utf-8")

        self.assertIn("什么是基金净值", html)
        self.assertIn("messages/stream", script)
        self.assertIn("citations", script)
        self.assertIn("联网搜索", html)
        self.assertIn("个性化", html)
        self.assertNotIn("answerMode", html)
        self.assertNotIn("⟨", html)
        self.assertIn("M4 7h16", html)
        self.assertIn("progress-list", script)
        self.assertIn("web_search", script)
        self.assertNotIn("answer_mode", script)

    def test_message_stream_emits_status_before_the_final_event(self) -> None:
        class _Service:
            def iter_events(self, user_id, conversation_id, content, **kwargs):
                self.seen = (user_id, conversation_id, content, kwargs.get("use_web_search"))
                yield {"event": "status", "data": {"text": "已收到问题，正在判断需要哪类资料。"}}
                yield {"event": "answer_delta", "data": {"text": "基金净值是份额对应的资产价值。"}}
                yield {"event": "citations", "data": {"citations": [{"document_title": "手册", "page": 4}]}}
                yield {"event": "done", "data": {"message": {"role": "assistant", "content": "基金净值是份额对应的资产价值。"}}}

        service = _Service()
        with patch("api.get_chat_service", return_value=service):
            response = TestClient(app).post(
                "/api/conversations/" + ("a" * 32) + "/messages/stream",
                json={"content": "什么是基金净值？", "web_search": False},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        body = response.text
        self.assertLess(body.index("event: status"), body.index("event: answer_delta"))
        self.assertLess(body.index("event: answer_delta"), body.index("event: done"))
        self.assertEqual(service.seen[2], "什么是基金净值？")
        self.assertIs(service.seen[3], False)
