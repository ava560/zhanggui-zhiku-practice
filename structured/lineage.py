"""加工版本的血缘。测试使用内存实现，配置了 MongoDB 时写入 data_lineage。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


class MemoryLineage:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def upsert(self, record: dict[str, Any]) -> None:
        self.docs[str(record["lineage_id"])] = dict(record)

    def get(self, lineage_id: str) -> dict[str, Any] | None:
        found = self.docs.get(str(lineage_id))
        return dict(found) if found else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = sorted(self.docs.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return [dict(item) for item in rows[:limit]]


def build_lineage(
    *,
    output: dict[str, str],
    inputs: list[dict[str, str]],
    code_id: str = "",
    steps: list[dict[str, Any]] | None = None,
    unresolved: list[dict[str, Any]] | None = None,
    producer: str = "scheduled_job",
    job_id: str = "",
    skill_id: str = "",
    code_version_id: str = "",
    description: str = "",
) -> dict[str, Any]:
    """根据这次实际读到的输入组装一条血缘。对不上的输入照实保留。"""
    version = str(output["data_version_id"])
    who = producer if producer in {"llm", "scheduled_job"} else "scheduled_job"
    return {
        "lineage_id": version,
        "producer": who,
        "output": {
            "doc_id": output["doc_id"],
            "table_id": output.get("table_id") or "",
            "import_version_id": output["import_version_id"],
            "data_version_id": version,
        },
        "inputs": list(inputs),
        "unresolved": list(unresolved or []),
        "steps": steps or [{"order": 1, "action": "加工步骤尚未生成"}],
        "description": description or "加工描述尚未生成",
        "code_id": code_id or "",
        "code_version_id": code_version_id or "",
        "job_id": job_id or "",
        "skill_id": skill_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


_mongo_lineage = None


def lookup_lineage(data_version_id: str, store: Any = None) -> dict[str, Any]:
    """按数据版本倒查。没有记录和读取失败用不同说明，不补内容。"""
    try:
        target = store if store is not None else mongo_lineage()
    except Exception:
        return {"found": False, "message": "血缘读取失败", "record": None}
    if target is None:
        return {"found": False, "message": "血缘读取失败", "record": None}
    try:
        record = target.get(str(data_version_id or ""))
    except Exception:
        return {"found": False, "message": "血缘读取失败", "record": None}
    if not record:
        return {"found": False, "message": "没有血缘", "record": None}
    return {"found": True, "message": "", "record": record}


def present_lineage(
    records: list[dict[str, Any]],
    *,
    files: dict[str, str] | None = None,
    jobs: dict[str, str] | None = None,
    codes: dict[str, str] | None = None,
    previews: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把血缘里的编号换成文件名、任务名和这次写出的行。读不到名称时不编造编号。"""
    files = files or {}
    jobs = jobs or {}
    codes = codes or {}
    previews = previews or {}
    shown = []
    for record in records:
        output = record.get("output") or {}
        doc_id = str(output.get("doc_id") or "")
        title = files.get(doc_id) or "文件已不在"
        job_name = jobs.get(str(record.get("job_id") or "")) or ""
        code_name = codes.get(str(record.get("code_id") or "")) or ""
        parts = ["模型写回" if record.get("producer") == "llm" else "定时任务"]
        if job_name:
            parts.append(job_name)
        if code_name:
            parts.append(f"代码 {code_name}")
        elif record.get("producer") != "llm":
            parts.append("没有登记代码")
        when = _beijing(str(record.get("created_at") or ""))
        if when:
            parts.append(when)
        inputs = []
        for item in record.get("inputs") or []:
            name = str(item.get("source_file") or "") or files.get(str(item.get("doc_id") or ""))
            if not name:
                label = "未对应到已登记文件"
            elif not item.get("data_version_id"):
                label = f"{name}（未对应）"
            else:
                label = name
            inputs.append(label)
        for item in record.get("unresolved") or []:
            text = str(item.get("source_file") or item.get("name") or "").strip()
            inputs.append(f"{text or '未对应'}（未对应）")
        shown.append({
            "title": title,
            "subtitle": " · ".join(parts),
            "steps": list(record.get("steps") or []),
            "inputs": inputs,
            "code_name": code_name or ("模型步骤" if record.get("producer") == "llm" else "没有登记代码"),
            "job_name": job_name,
            "description": str(record.get("description") or "加工描述尚未生成"),
        })
    return shown


def _beijing(value: str) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


def list_lineage(store: Any = None, limit: int = 50) -> dict[str, Any]:
    """列出已保存的血缘。读取失败不补记录。"""
    try:
        target = store if store is not None else mongo_lineage()
    except Exception:
        return {"records": [], "message": "血缘读取失败"}
    if target is None:
        return {"records": [], "message": "血缘读取失败"}
    try:
        return {"records": target.list(max(1, min(int(limit), 100))), "message": ""}
    except Exception:
        return {"records": [], "message": "血缘读取失败"}


def mongo_lineage():
    """只在显式配置了 MongoDB 时连接。没有配置就返回空。同一进程复用一个客户端。"""
    global _mongo_lineage
    if _mongo_lineage is not None:
        return _mongo_lineage
    uri = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or "").strip()
    database_name = (os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DB_NAME") or "").strip()
    if not uri or not database_name:
        return None
    from pymongo import MongoClient

    timeout = max(1000, int(os.getenv("MONGO_TIMEOUT_MS", "5000")))
    client = MongoClient(uri, serverSelectionTimeoutMS=timeout, connectTimeoutMS=timeout)
    name = os.getenv("MONGO_LINEAGE_COLLECTION", "data_lineage").strip() or "data_lineage"
    collection = client[database_name][name]
    collection.create_index("lineage_id", unique=True)
    _mongo_lineage = _MongoLineage(collection)
    return _mongo_lineage


class _MongoLineage:
    def __init__(self, collection: Any) -> None:
        self.collection = collection

    def upsert(self, record: dict[str, Any]) -> None:
        self.collection.replace_one({"lineage_id": record["lineage_id"]}, record, upsert=True)

    def get(self, lineage_id: str) -> dict[str, Any] | None:
        found = self.collection.find_one({"lineage_id": lineage_id}, {"_id": 0})
        return dict(found) if found else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        found = self.collection.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
        return [dict(item) for item in found]


def parse_narration(text: str) -> dict[str, Any]:
    """从模型返回的文字里取出描述和步骤。解析不了就当没补上。"""
    import json

    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    description = str(payload.get("description") or "").strip()
    steps = []
    for index, item in enumerate(payload.get("steps") or [], start=1):
        if isinstance(item, str):
            action = item.strip()
            order = index
        elif isinstance(item, dict):
            action = str(item.get("action") or "").strip()
            try:
                order = int(item.get("order") or index)
            except (TypeError, ValueError):
                order = index
        else:
            continue
        if action:
            steps.append({"order": order, "action": action[:500]})
    if not description and not steps:
        return {}
    return {"description": description[:1000], "steps": steps}


def describe_processing(source: str, files: list[str], output_name: str, code_name: str) -> dict[str, Any]:
    """任务成功后，让模型根据实际读到的文件和代码补加工步骤与描述。"""
    endpoints = [
        (
            os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            os.getenv("OPENAI_API_KEY", "").strip(),
            os.getenv("LLM_DEFAULT_MODEL", "qwen-flash").strip() or "qwen-flash",
        ),
        (
            os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            os.getenv("DEEPSEEK_API_KEY", "").strip(),
            os.getenv("LLM_FALLBACK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
        ),
    ]
    configured = [item for item in endpoints if item[1]]
    if not configured or not str(source or "").strip():
        return {}
    names = "、".join(files) if files else "没有读到文件"
    prompt = (
        "一次定时任务已经跑完。请只根据下面实际读到的文件和代码，补一段加工描述和有顺序的加工步骤。"
        "只输出 JSON，不要解释：{\"description\":\"一段中文描述\",\"steps\":[{\"order\":1,\"action\":\"做了什么\"}]}。"
        "不要写代码正文、密钥、整表数据，也不要编造没有读到的文件。\n"
        f"写出的表：{output_name or '未命名'}\n"
        f"代码名称：{code_name or '未命名'}\n"
        f"实际读到的文件：{names}\n"
        f"代码：\n{source[:8000]}"
    )
    try:
        import requests
    except Exception:
        return {}
    for base_url, api_key, model in configured:
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            response.raise_for_status()
            text = str(response.json()["choices"][0]["message"]["content"] or "")
        except Exception:
            continue
        parsed = parse_narration(text)
        if parsed:
            return parsed
    return {}


def supplement_lineage(version_id: str, narration: dict[str, Any], store: Any = None) -> None:
    """把模型补上的描述和步骤写回同一条血缘。没有记录时不新建。"""
    target = store if store is not None else mongo_lineage()
    if target is None or not version_id:
        return
    record = target.get(str(version_id))
    if not record:
        return
    description = str(narration.get("description") or "").strip()
    steps = narration.get("steps") or []
    if description:
        record["description"] = description
    if steps:
        record["steps"] = steps
    if description or steps:
        target.upsert(record)
