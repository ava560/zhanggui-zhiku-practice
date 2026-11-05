"""知识库导入 LangGraph：按文件类型分流，再走统一的切分、识别、向量化和入库。"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from upload.node_bge_embedding import node_bge_embedding
from upload.node_docment_split import node_docment_split
from upload.node_entry import node_entry
from upload.corpus_scope import is_non_financial
from upload.knowledge_graph import node_build_knowledge_graph
from upload.node_import_milvus import node_import_milvus
from upload.node_item_name_recognition import node_item_name_recognition
from upload.node_md_to_img import node_md_to_img
from upload.node_pdf_to_md import node_pdf_to_md
from state import (
    INPUT_DIR,
    OUTPUT_DIR,
    ImportGraphState,
    create_default_state,
    format_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv(ROOT / ".env")

workflow = StateGraph(ImportGraphState)
workflow.add_node("node_entry", node_entry)
workflow.add_node("node_pdf_to_md", node_pdf_to_md)
workflow.add_node("node_md_to_img", node_md_to_img)
workflow.add_node("node_docment_split", node_docment_split)
workflow.add_node("node_item_name_recognition", node_item_name_recognition)
workflow.add_node("node_bge_embedding", node_bge_embedding)
workflow.add_node("node_import_milvus", node_import_milvus)
workflow.add_node("node_build_knowledge_graph", node_build_knowledge_graph)

workflow.add_edge(START, "node_entry")


def route_after_entry(state: ImportGraphState) -> str:
    """入口只更新开关，由这里决定走 PDF 转换、直接处理 MD，还是结束。"""
    if state.get("is_md_read_enabled"):
        return "node_md_to_img"
    if state.get("is_pdf_read_enabled"):
        return "node_pdf_to_md"
    return END


workflow.add_conditional_edges(
    "node_entry",
    route_after_entry,
    {
        "node_md_to_img": "node_md_to_img",
        "node_pdf_to_md": "node_pdf_to_md",
        END: END,
    },
)
workflow.add_edge("node_pdf_to_md", "node_md_to_img")
workflow.add_edge("node_md_to_img", "node_docment_split")
workflow.add_edge("node_docment_split", "node_item_name_recognition")
workflow.add_edge("node_item_name_recognition", "node_bge_embedding")
workflow.add_edge("node_bge_embedding", "node_import_milvus")
workflow.add_edge("node_import_milvus", "node_build_knowledge_graph")
workflow.add_edge("node_build_knowledge_graph", END)

kb_import_app = workflow.compile()


def list_input_files(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    files = [
        path
        for path in sorted(input_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in {".pdf", ".md", ".docx", ".doc"}
    ]
    logger.info("在 %s 中找到 %d 个可导入文件", input_dir, len(files))
    return files


def run_import(local_file_path: str, task_id: str = "") -> ImportGraphState:
    initial = create_default_state(
        task_id=task_id or uuid.uuid4().hex[:12],
        local_file_path=str(Path(local_file_path).expanduser()),
        local_dir=str(OUTPUT_DIR),
    )
    logger.info("开始导入: %s", initial["local_file_path"])
    result = kb_import_app.invoke(initial)
    logger.info("导入完成: %s", format_state(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库导入工作流")
    parser.add_argument(
        "files",
        nargs="*",
        help="要导入的 PDF 或 Markdown 路径；省略时扫描 input 目录",
    )
    args = parser.parse_args()

    files = [Path(item) for item in args.files] if args.files else list_input_files(INPUT_DIR)
    if not files:
        raise SystemExit("没有可导入的文件。请传入路径，或把 PDF/Markdown 放到 input 目录。")

    failed: list[str] = []
    for path in files:
        if is_non_financial(path.name):
            logger.info("跳过与金融无关的文件: %s", path.name)
            continue
        try:
            run_import(str(path))
        except Exception:
            logger.exception("导入失败: %s", path)
            failed.append(str(path))

    if failed:
        raise SystemExit(f"有 {len(failed)} 个文件导入失败: {failed}")
    logger.info("全部导入完成，共 %d 个文件", len(files))


if __name__ == "__main__":
    main()
