"""导入工作流的共享状态。每个节点读取所需字段，并返回要合并回状态的更新。"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parent
INPUT_DIR = PROJECT_ROOT / "input"
OUTPUT_DIR = PROJECT_ROOT / "output"


class ImportGraphState(TypedDict, total=False):
    """知识库导入图的数据契约，字段由各节点生产或消费。"""

    task_id: str

    is_md_read_enabled: bool
    is_pdf_read_enabled: bool

    is_normal_split_enabled: bool
    is_silicon_flow_api_enabled: bool
    is_advanced_split_enabled: bool
    is_vllm_enabled: bool

    local_dir: str
    local_file_path: str
    file_title: str
    pdf_path: str
    md_path: str
    split_path: str
    embeddings_path: str
    minio_source_object: str
    minio_source_url: str

    md_content: str
    chunks: list
    item_name: str
    embeddings_content: list
    import_skip_reason: str
    graph_status: str
    import_skip_reason: str
    graph_status: str

    # 问答链路：商品名提取、改写与 Milvus 商品名确认。
    user_id: str
    conversation_id: str
    session_id: str
    mongodb_filter: dict[str, Any]
    mongo_collection: str
    conversation_history: list[dict[str, str]]
    current_question: str
    itemname_question: list[str]
    itemname_question_extracted: list[str]
    question_rewritten: str
    is_product_question: bool
    item_name_question_json_path: str
    item_name_candidates: list[dict[str, Any]]
    item_name_match_results: list[dict[str, Any]]
    item_name_direct_matches: list[str]
    item_name_not_found: list[str]
    item_name_confirm_status: str
    item_name_confirmation_message: str
    awaiting_user_confirmation: bool
    item_name_user_selection: Any
    item_name_user_confirmed: bool | None
    search_matched_item_names: list[str]
    search_missing_item_names: list[str]
    search_query_vector_cache: dict[str, Any]
    search_top5: list[dict[str, Any]]
    search_top5_output_path: str
    search_top5_json_path: str
    search_embedding_top5_json_path: str
    hyde_answer: str
    question_answer: str
    search_hyde_top5_output_path: str
    search_hyde_top5_json_path: str
    rrf_chunks: list[dict[str, Any]]
    rrf_output_path: str
    rrf_json_path: str
    rerank_chunks: list[dict[str, Any]]
    rerank_output_path: str
    rerank_json_path: str
    web_search_top5: list[dict[str, str]]
    web_search_output_path: str
    web_search_json_path: str
    web_search_error: str
    related_file_names: list[str]
    graph_expanded_names: list[str]
    final_answer: str
    answer_image_urls: list[str]
    answer_message_seq: int
    query_plan: dict[str, Any]
    session_memory: dict[str, Any]
    evidence_status: str
    citations: list[dict[str, Any]]
    search_error: str
    answer_mode: str
    defer_answer_generation: bool
    graph_status: str
    status_text: str


graph_default_state: ImportGraphState = {
    "task_id": "",
    "is_pdf_read_enabled": False,
    "is_md_read_enabled": False,
    "is_normal_split_enabled": True,
    "is_silicon_flow_api_enabled": True,
    "is_advanced_split_enabled": False,
    "is_vllm_enabled": False,
    "local_dir": str(OUTPUT_DIR),
    "local_file_path": "",
    "pdf_path": "",
    "md_path": "",
    "file_title": "",
    "split_path": "",
    "embeddings_path": "",
    "minio_source_object": "",
    "minio_source_url": "",
    "md_content": "",
    "chunks": [],
    "item_name": "",
    "embeddings_content": [],
    "conversation_history": [],
    "current_question": "",
    "itemname_question": [],
    "itemname_question_extracted": [],
    "question_rewritten": "",
    "is_product_question": True,
    "item_name_question_json_path": "",
    "item_name_candidates": [],
    "item_name_match_results": [],
    "item_name_direct_matches": [],
    "item_name_not_found": [],
    "item_name_confirm_status": "",
    "item_name_confirmation_message": "",
    "awaiting_user_confirmation": False,
    "item_name_user_selection": None,
    "item_name_user_confirmed": None,
    "search_matched_item_names": [],
    "search_missing_item_names": [],
    "search_query_vector_cache": {},
    "search_top5": [],
    "search_top5_output_path": "",
    "search_top5_json_path": "",
    "search_embedding_top5_json_path": "",
    "hyde_answer": "",
    "question_answer": "",
    "search_hyde_top5_output_path": "",
    "search_hyde_top5_json_path": "",
    "rrf_chunks": [],
    "rrf_output_path": "",
    "rrf_json_path": "",
    "rerank_chunks": [],
    "rerank_output_path": "",
    "rerank_json_path": "",
    "web_search_top5": [],
    "web_search_output_path": "",
    "web_search_json_path": "",
    "web_search_error": "",
    "related_file_names": [],
    "graph_expanded_names": [],
    "final_answer": "",
    "answer_image_urls": [],
    "answer_message_seq": 0,
}


def create_default_state(**overrides: Any) -> ImportGraphState:
    state = copy.deepcopy(graph_default_state)
    state.update(overrides)
    return state


def get_default_state() -> ImportGraphState:
    return copy.deepcopy(graph_default_state)


def format_state(state: dict[str, Any]) -> dict[str, Any]:
    """打印状态时避免把全文和向量全部倒出来。"""
    preview: dict[str, Any] = {}
    for key, value in state.items():
        if key in {"md_content", "hyde_answer", "question_answer"} and isinstance(value, str):
            preview[key] = f"<str len={len(value)}>"
        elif key in {"chunks", "embeddings_content", "conversation_history", "search_top5", "rrf_chunks", "rerank_chunks", "web_search_top5"} and isinstance(value, list):
            preview[key] = f"<list len={len(value)}>"
        elif key == "search_query_vector_cache" and isinstance(value, dict):
            preview[key] = f"<dict len={len(value.get('vectors', {}))}>"
        else:
            preview[key] = value
    return preview


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger(__name__)
    sample = create_default_state(task_id="task_001", local_file_path="万用表RS-12的使用.pdf")
    logger.info("默认状态: %s", format_state(sample))
