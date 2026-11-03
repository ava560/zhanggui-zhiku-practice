"""名称与文件的关系图。只在已有确认名称、且问题在问关系时查询。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

RELATION_TERMS = (
    "管理人",
    "所属",
    "发行",
    "旗下",
    "对应",
    "披露",
    "适用于",
    "哪家",
    "哪些文件",
    "哪个文件",
    "哪份",
    "关系",
)


def is_relation_question(text: str) -> bool:
    """内容问答不查图；只有问到归属、对应或相关文件时才查。"""
    question = text or ""
    return any(term in question for term in RELATION_TERMS)


def _database_name() -> str:
    raw = os.getenv("NEO4J_DATABASE", "neo4j") or "neo4j"
    return raw.split("#", 1)[0].strip() or "neo4j"


def _driver():
    from neo4j import GraphDatabase

    uri = (os.getenv("NEO4J_URI") or "bolt://127.0.0.1:7687").strip()
    user = (os.getenv("NEO4J_USERNAME") or "neo4j").strip()
    password = (os.getenv("NEO4J_PASSWORD") or "").strip()
    return GraphDatabase.driver(uri, auth=(user, password))


def link_name_to_file(item_name: str, file_name: str) -> None:
    """把已识别名称连到当前文件。连不上时只记录，不打断入库。"""
    name = (item_name or "").strip()
    document = (file_name or "").strip()
    if not name or not document:
        return
    driver = _driver()
    try:
        driver.verify_connectivity()
        with driver.session(database=_database_name()) as session:
            session.run(
                """
                MERGE (item:Item {name: $name})
                MERGE (file:SourceFile {name: $document})
                MERGE (item)-[:DESCRIBED_IN]->(file)
                """,
                name=name,
                document=document,
            )
        logger.info("已写入名称关系: %s -> %s", name, document)
    finally:
        driver.close()


def related_records(names: list[str]) -> tuple[list[str], list[str]]:
    """返回与这些名称通过同一文件相连的其他名称，以及相关文件名。"""
    cleaned = list(dict.fromkeys(name.strip() for name in names if name and name.strip()))
    if not cleaned:
        return [], []
    driver = _driver()
    try:
        driver.verify_connectivity()
        with driver.session(database=_database_name()) as session:
            rows = session.run(
                """
                MATCH (item:Item)-[:DESCRIBED_IN]->(file:SourceFile)
                WHERE item.name IN $names
                OPTIONAL MATCH (file)<-[:DESCRIBED_IN]-(other:Item)
                RETURN item.name AS name, file.name AS file_name, other.name AS other_name
                """,
                names=cleaned,
            )
            extra_names: list[str] = []
            file_names: list[str] = []
            known = set(cleaned)
            for row in rows:
                file_name = str(row.get("file_name") or "").strip()
                other_name = str(row.get("other_name") or "").strip()
                if file_name:
                    file_names.append(file_name)
                if other_name and other_name not in known:
                    extra_names.append(other_name)
        return list(dict.fromkeys(extra_names)), list(dict.fromkeys(file_names))
    finally:
        driver.close()


def expand_relation_scope(state: dict) -> None:
    """在现有确认结果上补相关名称。不满足条件或图不可用时保持原检索范围。"""
    if state.get("awaiting_user_confirmation"):
        return
    if state.get("item_name_confirm_status") == "general_chat":
        return
    names = state.get("itemname_question") or []
    if not isinstance(names, list) or not names:
        return
    question = str(state.get("question_rewritten") or state.get("current_question") or "")
    if not is_relation_question(question):
        return
    try:
        extra_names, file_names = related_records([str(name) for name in names])
    except Exception as exc:
        logger.warning("Neo4j 关系查询未完成，继续使用已确认名称：%s", type(exc).__name__)
        state["related_file_names"] = []
        state["graph_expanded_names"] = []
        return
    state["related_file_names"] = file_names
    state["graph_expanded_names"] = extra_names
    if file_names or extra_names:
        logger.info("关系查询命中文件 %s，补充名称 %s", file_names, extra_names)
    if extra_names:
        state["itemname_question"] = list(dict.fromkeys([*names, *extra_names]))
        logger.info("关系查询补充名称: %s", extra_names)
