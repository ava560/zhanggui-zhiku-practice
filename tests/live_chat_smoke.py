"""显式运行的本机真实服务冒烟测试；不使用替身或模拟数据。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from minio import Minio
from pymilvus import MilvusClient
from pymongo import MongoClient

from chat_service import create_chat_service
from search.workflow import run_search_workflow  # noqa: F401 -- 入口必须可导入


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=5000)
    mongo.admin.command("ping")
    bucket = os.environ["MINIO_BUCKET_NAME"]
    minio = Minio(
        os.environ["MINIO_ENDPOINT"],
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=os.getenv("MINIO_SECURE", "false").lower() in {"1", "true", "yes"},
    )
    assert minio.bucket_exists(bucket), f"MinIO bucket 不存在: {bucket}"
    kwargs = {
        "uri": os.environ["MILVUS_URL"],
        "timeout": 10,
        "grpc_options": {"grpc.enable_http_proxy": 0},
    }
    if os.getenv("MILVUS_TOKEN"):
        kwargs["token"] = os.environ["MILVUS_TOKEN"]
    milvus = MilvusClient(**kwargs)
    try:
        assert {os.environ["ITEM_NAME_COLLECTION"], os.environ["CHUNKS_COLLECTION"]} <= set(milvus.list_collections())
        items = milvus.query(
            os.environ["ITEM_NAME_COLLECTION"], filter="", output_fields=["item_name"], limit=1,
        )
        assert items and items[0].get("item_name"), "kb_item_names 没有可测试的商品"
        product = items[0]["item_name"]
    finally:
        milvus.close()

    service = create_chat_service()
    user_id = uuid.uuid4().hex
    chat = service.create(user_id)
    conversation_id = chat["conversation_id"]
    question = f"{product}的连接方式是什么？"
    result = service.send(user_id, conversation_id, question)
    current = service.get(user_id, conversation_id)
    confirmation = current.get("pending_confirmation")
    if confirmation:
        candidates = confirmation["candidates"]
        assert candidates
        if len(candidates) == 1:
            result = service.send(user_id, conversation_id, "确认", item_name_user_confirmed=True)
        else:
            result = service.send(
                user_id, conversation_id, "选择第一个候选产品",
                item_name_user_selection=[candidates[0]["itemname_question"]],
            )
        current = service.get(user_id, conversation_id)

    messages = current["messages"]
    assert result["role"] == "assistant"
    assert messages[-1]["content"] == result["content"]
    assert len({message["seq"] for message in messages}) == len(messages)
    assert not current.get("pending_confirmation")
    print(json.dumps({
        "mongo": "ok", "minio": "ok", "milvus": "ok",
        "product": product, "conversation_id": conversation_id,
        "messages": len(messages), "answer_chars": len(result["content"]),
    }, ensure_ascii=True))
    mongo.close()


if __name__ == "__main__":
    main()
