"""用户自己定义的技能。技能只提供做法，不在对话里执行更新。"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg import Connection


class SkillError(ValueError):
    """技能名称、做法或编号不符合保存规则。"""


_BUILTIN = [
    {
        "skill_id": "check_table",
        "name": "只按已发布的表回答",
        "summary": "先看已确认的表。表里没有的数字不从说明或文档里补。",
        "instructions": "按这个技能回答：优先使用已发布的结构化表。只引用这次查到的记录。字段说明和文档中的数字不能补表里没有的记录。没有记录就直接说表中没有记录。不要附上内容对不上的文件。",
        "code_id": "",
    },
    {
        "skill_id": "cite_evidence",
        "name": "只引用用到的资料",
        "summary": "回答里的每个资料都要能对上正文，用不到的文件不列出。",
        "instructions": "按这个技能回答：每条引用都要能在正文里对上原文。没有用到的文件不要写成参考。网页引用必须保留链接。",
        "code_id": "",
    },
    {
        "skill_id": "reload_benchmarks",
        "name": "重装业绩基准",
        "summary": "给定时任务用。按演示任务里的业绩基准表重装已发布的 mock_benchmarks。",
        "instructions": "这是定时任务技能。对话里不要执行更新，只说明会重装已发布的业绩基准表，并提醒到点不会自动跑。",
        "code_id": "reload_mock_benchmarks",
    },
]


def list_skills(conn: Connection | None = None) -> list[dict[str, str]]:
    """只返回用户保存的技能。内置技能仍可被已有任务按编号找到。"""
    own = conn is None
    if own:
        from structured.session import open_db

        conn = open_db()
    try:
        _ensure(conn)
        rows = conn.execute(
            """
            SELECT skill_id, name, summary, instructions, code_id
            FROM user_skill
            ORDER BY created_at, skill_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own:
            conn.close()


def skill_by_id(skill_id: str) -> dict[str, str] | None:
    text = str(skill_id or "").strip()
    if not text:
        return None
    from structured.session import open_db

    conn = open_db()
    try:
        _ensure(conn)
        row = conn.execute(
            """
            SELECT skill_id, name, summary, instructions, code_id
            FROM user_skill
            WHERE skill_id = %s
            """,
            (text,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return dict(row)
    for item in _BUILTIN:
        if item["skill_id"] == text:
            return item
    return None


def save_skill(
    conn: Connection,
    *,
    name: str,
    instructions: str,
    summary: str = "",
    code_id: str = "",
    skill_id: str | None = None,
) -> dict[str, str]:
    _ensure(conn)
    title = _limited(name, 80, "技能需要名称")
    steps = _limited(instructions, 4000, "技能需要写下做法")
    brief = str(summary or "").strip()
    if len(brief) > 200:
        raise SkillError("技能说明不能超过 200 字")
    bound = _code_id(code_id)
    current = str(skill_id or "").strip()
    if current:
        updated = conn.execute(
            """
            UPDATE user_skill
            SET name = %s, summary = %s, instructions = %s, code_id = %s
            WHERE skill_id = %s
            RETURNING skill_id, name, summary, instructions, code_id
            """,
            (title, brief, steps, bound, current),
        ).fetchone()
        if updated is None:
            conn.rollback()
            raise SkillError("技能不存在")
        conn.commit()
        return dict(updated)
    created = "sk_" + uuid.uuid4().hex
    row = conn.execute(
        """
        INSERT INTO user_skill (skill_id, name, summary, instructions, code_id)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING skill_id, name, summary, instructions, code_id
        """,
        (created, title, brief, steps, bound),
    ).fetchone()
    conn.commit()
    return dict(row)


def delete_skill(conn: Connection, skill_id: str) -> None:
    _ensure(conn)
    text = str(skill_id or "").strip()
    removed = conn.execute("DELETE FROM user_skill WHERE skill_id = %s", (text,)).rowcount
    if not removed:
        conn.rollback()
        raise SkillError("技能不存在")
    conn.commit()


def apply_skill(skill_id: str, content: str) -> dict[str, str | bool]:
    """技能步骤与用户原话分开返回，不把做法拼进问题。"""
    return resolve_turn(content, skill_id=skill_id)


def resolve_turn(
    question: str,
    skill_id: str = "",
    job_skill_id: str = "",
    scorer=None,
) -> dict[str, str | bool]:
    """按 @、请求编号、任务绑定或相似度决定本轮技能。原问题保持不动。"""
    text = str(question or "")
    skills = list_skills()
    picked = _mentions(text, skills)
    if picked["missing"]:
        return _stopped(text, "未找到这个技能，本轮不继续检索。")
    if picked["ambiguous"] or len(picked["skills"]) > 1:
        return _stopped(text, "点名了多个技能，请先选择一个。")
    named = picked["skills"][0] if picked["skills"] else None
    requested = _requested(skill_id)
    if named and requested and named["skill_id"] != requested["skill_id"]:
        return _stopped(text, "点名了多个技能，请先选择一个。")
    chosen = named or requested
    if chosen is None and str(job_skill_id or "").strip():
        chosen = skill_by_id(job_skill_id)
    if chosen is None and scorer is not None:
        chosen = _by_score(text, skills, scorer)
        if chosen == "choose":
            return _stopped(text, "有多个相近技能，请先选择一个。")
    if chosen is None:
        return _open(text, "", "")
    return _open(text, chosen["skill_id"], chosen["instructions"])


def _requested(skill_id: str) -> dict[str, str] | None:
    text = str(skill_id or "").strip()
    if not text:
        return None
    found = skill_by_id(text)
    if found is None:
        raise ValueError("技能不存在")
    return found


def _mentions(text: str, skills: list[dict[str, str]]) -> dict[str, object]:
    names = sorted({item["name"] for item in skills}, key=len, reverse=True)
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in skills:
        grouped.setdefault(item["name"], []).append(item)
    found: list[dict[str, str]] = []
    index = 0
    while index < len(text):
        if text[index] not in "@＠":
            index += 1
            continue
        rest = text[index + 1 :]
        match = next((name for name in names if rest.startswith(name)), "")
        if not match:
            return {"missing": True, "ambiguous": False, "skills": []}
        group = grouped[match]
        if len(group) != 1:
            return {"missing": False, "ambiguous": True, "skills": []}
        found.append(group[0])
        index += 1 + len(match)
    return {"missing": False, "ambiguous": False, "skills": found}


def _by_score(question: str, skills: list[dict[str, str]], scorer):
    scored = []
    for item in skills:
        try:
            score = float(scorer(question, item))
        except (TypeError, ValueError):
            continue
        if score >= 0.6:
            scored.append((score, item))
    high = [item for score, item in scored if score >= 0.9]
    if len(high) == 1:
        return high[0]
    if scored:
        return "choose"
    return None


def _open(question: str, skill_id: str, instructions: str) -> dict[str, str | bool]:
    return {
        "question": question,
        "skill_id": skill_id,
        "instructions": instructions,
        "stop": False,
        "message": "",
    }


def _stopped(question: str, message: str) -> dict[str, str | bool]:
    return {"question": question, "skill_id": "", "instructions": "", "stop": True, "message": message}


def _ensure(conn: Connection) -> None:
    from structured.schema import ensure_schema

    ensure_schema(conn)


def _limited(value: str, limit: int, empty_message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SkillError(empty_message)
    if len(text) > limit:
        raise SkillError(f"不能超过 {limit} 字")
    return text


def bge_skill_score(question: str, skill: dict[str, Any]) -> float:
    """用已有 BGE-M3 比较问题和技能名称、何时使用。模型不可用时记 0，不套用。"""
    left = str(question or "").strip()
    right = f"{skill.get('name') or ''}\n{skill.get('summary') or ''}".strip()
    if not left or not right:
        return 0.0
    try:
        from upload.node_bge_embedding import encode_batch, get_bge_model, load_settings

        vectors, _sparse = encode_batch(get_bge_model(load_settings()), [left, right])
        return float(sum(a * b for a, b in zip(vectors[0], vectors[1])))
    except Exception:
        return 0.0


def _code_id(value: Any) -> str:
    text = str(value or "").strip()
    if any(token in text for token in ("\n", "\r", "/", "\\", ".py")):
        raise SkillError("技能不能附带代码正文或路径")
    if len(text) > 80:
        raise SkillError("代码标识过长")
    return text
