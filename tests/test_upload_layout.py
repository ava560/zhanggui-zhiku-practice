"""搬迁到 upload/ 后，网站入口和节点仍使用同一项目根目录。"""

from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import web_server


class UploadLayoutTests(TestCase):
    def test_web_task_loads_nodes_from_upload(self) -> None:
        def accept_markdown(state):
            state["is_md_read_enabled"] = True
            return state

        with ExitStack() as stack:
            source = Path("sample.md")
            task_id = "relocated_upload_test"
            web_server.task_store.create(task_id, source.name, 1)

            for module, function in (
                ("upload.node_bge_embedding", "node_bge_embedding"),
                ("upload.node_docment_split", "node_docment_split"),
                ("upload.node_entry", "node_entry"),
                ("upload.node_import_milvus", "node_import_milvus"),
                ("upload.node_item_name_recognition", "node_item_name_recognition"),
                ("upload.node_md_to_img", "node_md_to_img"),
                ("upload.node_pdf_to_md", "node_pdf_to_md"),
            ):
                action = accept_markdown if function == "node_entry" else lambda state: state
                stack.enter_context(patch(f"{module}.{function}", side_effect=action))
            stack.enter_context(
                patch("node_upload_minio.node_upload_minio", side_effect=lambda state: state)
            )

            web_server.process_task(task_id, source, Path("output"))

        task = web_server.task_store.get(task_id)
        self.assertEqual(task["status"], "completed", task["error"])

    def test_relocated_nodes_keep_project_storage_paths(self) -> None:
        from upload import (
            node_bge_embedding,
            node_docment_split,
            node_import_milvus,
            node_item_name_recognition,
            node_md_to_img,
            node_pdf_to_md,
        )

        root = Path(__file__).resolve().parents[1]
        for module in (
            node_bge_embedding,
            node_docment_split,
            node_import_milvus,
            node_item_name_recognition,
            node_md_to_img,
            node_pdf_to_md,
        ):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.OUTPUT_DIR, root / "output")
                self.assertEqual(module.BASE_DIR, root)
        self.assertEqual(node_pdf_to_md.INPUT_DIR, root / "input")
