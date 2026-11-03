"""通过本机 HTTP 对话接口运行真实检索链；需要先启动 web_server.py。"""

from __future__ import annotations

import http.cookiejar
import json
import os
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener

from dotenv import load_dotenv
from pymilvus import MilvusClient


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    base = f"http://127.0.0.1:{os.getenv('WEB_PORT', '8000')}"
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path: str, payload: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        with opener.open(Request(base + path, data=data, headers=headers), timeout=300) as response:
            return response.status, response.read()

    status, page = request("/chat")
    assert status == 200 and b"messageForm" in page
    status, script = request("/chat.js")
    assert status == 200 and b"pendingConfirmation" in script

    kwargs = {"uri": os.environ["MILVUS_URL"], "timeout": 10,
              "grpc_options": {"grpc.enable_http_proxy": 0}}
    if os.getenv("MILVUS_TOKEN"):
        kwargs["token"] = os.environ["MILVUS_TOKEN"]
    milvus = MilvusClient(**kwargs)
    try:
        item = milvus.query(os.environ["ITEM_NAME_COLLECTION"], filter="", output_fields=["item_name"], limit=1)[0]["item_name"]
    finally:
        milvus.close()

    status, body = request("/api/conversations", {})
    assert status == 201
    conversation_id = json.loads(body)["conversation_id"]
    path = f"/api/conversations/{conversation_id}"
    status, body = request(path + "/messages", {"content": f"{item}的连接方式是什么？"})
    assert status == 201, body
    status, body = request(path)
    assert status == 200
    conversation = json.loads(body)
    confirmation = conversation.get("pending_confirmation")
    if confirmation:
        candidates = confirmation["candidates"]
        assert candidates
        if len(candidates) == 1:
            decision = {"content": "确认", "item_name_user_confirmed": True}
        else:
            decision = {"content": "选择第一个候选产品", "item_name_user_selection": [candidates[0]["itemname_question"]]}
        status, body = request(path + "/messages", decision)
        assert status == 201, body
        status, body = request(path)
        conversation = json.loads(body)

    messages = conversation["messages"]
    assert not conversation.get("pending_confirmation")
    assert messages[-1]["role"] == "assistant" and messages[-1]["content"]
    assert len({message["seq"] for message in messages}) == len(messages)
    print(json.dumps({"http": "ok", "conversation_id": conversation_id,
                      "messages": len(messages), "answer_chars": len(messages[-1]["content"])}, ensure_ascii=True))


if __name__ == "__main__":
    main()
