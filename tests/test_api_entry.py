"""FastAPI 入口仍提供原来的配置和上传校验。"""

from unittest import TestCase

from fastapi.testclient import TestClient

from api import app


class ApiEntryTests(TestCase):
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
