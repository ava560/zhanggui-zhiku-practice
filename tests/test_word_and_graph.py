"""Word 转 Markdown，以及关系问题才查图。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from docx import Document

from graph_store import expand_relation_scope, is_relation_question
from upload.word_to_md import docx_to_markdown, stage_word_as_markdown


class WordMarkdownTests(TestCase):
    def test_docx_keeps_paragraphs_and_table(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "基金问答.docx"
            document = Document()
            document.add_paragraph("易方达智造优势混合")
            table = document.add_table(rows=2, cols=2)
            table.rows[0].cells[0].text = "管理人"
            table.rows[0].cells[1].text = "易方达"
            table.rows[1].cells[0].text = "类型"
            table.rows[1].cells[1].text = "混合"
            document.save(path)
            markdown = docx_to_markdown(path)
            staged = stage_word_as_markdown(path, Path(folder) / "out")
            staged_text = staged.read_text(encoding="utf-8")
            staged_name = staged.name

        self.assertIn("易方达智造优势混合", markdown)
        self.assertIn("| 管理人 | 易方达 |", markdown)
        self.assertEqual(staged_name, "基金问答.md")
        self.assertIn("混合", staged_text)


class RelationScopeTests(TestCase):
    def test_content_question_does_not_query_graph(self) -> None:
        state = {
            "itemname_question": ["招商银行"],
            "question_rewritten": "招商银行一季度利润是多少",
            "item_name_confirm_status": "confirmed",
        }
        with patch("graph_store.related_records") as query:
            expand_relation_scope(state)
        query.assert_not_called()
        self.assertEqual(state["itemname_question"], ["招商银行"])

    def test_relation_question_adds_related_names(self) -> None:
        state = {
            "itemname_question": ["招商银行"],
            "question_rewritten": "招商银行对应哪些文件",
            "item_name_confirm_status": "confirmed",
        }
        with patch(
            "graph_store.related_records",
            return_value=(["招商银行风险揭示"], ["招商银行季报.pdf"]),
        ):
            expand_relation_scope(state)
        self.assertEqual(state["itemname_question"], ["招商银行", "招商银行风险揭示"])
        self.assertEqual(state["related_file_names"], ["招商银行季报.pdf"])

    def test_general_chat_skips_graph(self) -> None:
        self.assertFalse(is_relation_question("你好"))
        state = {
            "itemname_question": ["招商银行"],
            "question_rewritten": "招商银行对应哪些文件",
            "item_name_confirm_status": "general_chat",
        }
        with patch("graph_store.related_records") as query:
            expand_relation_scope(state)
        query.assert_not_called()
