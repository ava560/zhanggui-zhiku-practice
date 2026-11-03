"""聊天持久化：每个对话有独立编号，每条消息单独写入 MongoDB。"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from chat_safety import RESTRICTED_REPLY, is_restricted_question, redact_configured_secrets


PENDING_STATE_KEYS = (
    "task_id", "local_dir", "user_id", "conversation_id", "mongo_collection",
    "current_question", "question_rewritten", "itemname_question",
    "itemname_question_extracted", "item_name_question_json_path",
    "item_name_candidates", "item_name_direct_matches", "item_name_not_found",
    "item_name_confirm_status", "item_name_confirmation_message",
    "awaiting_user_confirmation",
)


def infer_pending_decision(content: str, pending_state: dict[str, Any]) -> dict[str, Any] | None:
    """把用户手输的简短确认词还原为确认字段；其他文字仍视为新问题。"""
    candidates = pending_state.get("item_name_candidates") or []
    reply = content.strip().rstrip("。！!？?").strip()
    if len(candidates) == 1:
        if reply in {"是", "是的", "对", "对的", "没错", "确认", "正确"}:
            return {"item_name_user_confirmed": True}
        if reply in {"不是", "不是的", "不对", "否", "不是这个产品", "都不是"}:
            return {"item_name_user_confirmed": False}
    elif len(candidates) > 1 and reply in {"都不是", "都不对", "没有", "均不是"}:
        return {"item_name_user_selection": []}
    return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class AnswerError(RuntimeError):
    """问题已入库，但模型回答失败。"""


class ChatService:
    def __init__(
        self, conversations: Any, messages: Any,
        answer: Callable[[list[dict[str, str]]], str],
        workflow: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.conversations = conversations
        self.messages = messages
        self.answer = answer
        self.workflow = workflow
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    @staticmethod
    def _scope(user_id: str, conversation_id: str) -> dict[str, str]:
        return {"user_id": user_id, "conversation_id": conversation_id}

    @staticmethod
    def _public_conversation(document: dict[str, Any]) -> dict[str, Any]:
        return {key: document[key] for key in ("conversation_id", "title", "created_at", "updated_at")}

    def create(self, user_id: str) -> dict[str, Any]:
        timestamp = now()
        document = {
            "user_id": user_id,
            "conversation_id": uuid.uuid4().hex,
            "title": "新对话",
            "created_at": timestamp,
            "updated_at": timestamp,
            "next_seq": 0,
        }
        self.conversations.insert_one(document)
        return self._public_conversation(document)

    def list(self, user_id: str) -> list[dict[str, Any]]:
        cursor = self.conversations.find({"user_id": user_id}).sort("updated_at", -1)
        return [self._public_conversation(document) for document in cursor]

    def get(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        scope = self._scope(user_id, conversation_id)
        document = self.conversations.find_one(scope)
        if document is None:
            raise LookupError("对话不存在")
        messages = self.messages.find(scope).sort("seq", 1)
        result = {
            **self._public_conversation(document),
            "messages": [
                {key: message[key] for key in ("role", "content", "created_at", "seq")}
                for message in messages
            ],
        }
        pending = document.get("pending_search")
        if isinstance(pending, dict):
            state = pending.get("state") or {}
            result["pending_confirmation"] = {
                "status": state.get("item_name_confirm_status", ""),
                "message": state.get("item_name_confirmation_message", ""),
                "candidates": state.get("item_name_candidates", []),
            }
        return result

    def _save_assistant(self, scope: dict[str, str], answer_text: str) -> dict[str, Any]:
        answer_text = redact_configured_secrets(answer_text)
        updated = self.conversations.find_one_and_update(
            scope, {"$inc": {"next_seq": 1}, "$set": {"updated_at": now()}},
            return_document=True,
        )
        assistant_message = {
            **scope, "seq": updated["next_seq"], "role": "assistant",
            "content": answer_text, "created_at": now(),
        }
        self.messages.insert_one(assistant_message)
        return {key: assistant_message[key] for key in ("role", "content", "created_at", "seq")}

    def send(
        self, user_id: str, conversation_id: str, content: str, *,
        item_name_user_selection: Any = None,
        item_name_user_confirmed: bool | None = None,
    ) -> dict[str, Any]:
        question = content.strip()
        if not question or len(question) > 4000:
            raise ValueError("问题不能为空，且不能超过 4000 字")
        scope = self._scope(user_id, conversation_id)
        with self._lock_for(conversation_id):
            conversation = self.conversations.find_one(scope)
            if conversation is None:
                raise LookupError("对话不存在")
            pending = conversation.get("pending_search")
            has_decision = item_name_user_selection is not None or item_name_user_confirmed is not None
            if pending and not has_decision:
                inferred = infer_pending_decision(question, pending.get("state") or {})
                if inferred is not None:
                    item_name_user_selection = inferred.get("item_name_user_selection")
                    item_name_user_confirmed = inferred.get("item_name_user_confirmed")
                    has_decision = True
            if has_decision and not pending:
                raise ValueError("当前对话没有待确认的产品")
            if item_name_user_selection is not None and item_name_user_confirmed is not None:
                raise ValueError("不能同时提交产品选择和确认结果")
            if item_name_user_confirmed is not None and not isinstance(item_name_user_confirmed, bool):
                raise ValueError("item_name_user_confirmed 必须为布尔值")
            if pending and not has_decision:
                # 用户输入新问题时取消上一次候选，避免旧决策影响新问题。
                self.conversations.update_one(scope, {"$unset": {"pending_search": ""}})
            updated = self.conversations.find_one_and_update(
                scope, {"$inc": {"next_seq": 1}, "$set": {"updated_at": now()}},
                return_document=True,
            )
            user_message = {
                **scope, "seq": updated["next_seq"], "role": "user",
                "content": question, "created_at": now(),
            }
            self.messages.insert_one(user_message)
            if updated["next_seq"] == 1:
                self.conversations.update_one(scope, {"$set": {"title": question[:36]}})

            if is_restricted_question(question):
                self.conversations.update_one(scope, {"$unset": {"pending_search": ""}})
                return self._save_assistant(scope, RESTRICTED_REPLY)

            if self.workflow is not None:
                from state import OUTPUT_DIR, create_default_state

                if pending and has_decision:
                    state = create_default_state(**pending["state"])
                    state["item_name_user_selection"] = item_name_user_selection
                    state["item_name_user_confirmed"] = item_name_user_confirmed
                else:
                    request_id = uuid.uuid4().hex
                    state = create_default_state(
                        task_id=request_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        local_dir=str(OUTPUT_DIR / "search" / conversation_id / request_id),
                    )
                try:
                    state = self.workflow(state)
                except Exception as exc:
                    raise AnswerError("检索或回答失败，问题已保存，请检查服务连接和后台日志") from exc
                if state.get("awaiting_user_confirmation"):
                    saved_state = {key: state[key] for key in PENDING_STATE_KEYS if key in state}
                    self.conversations.update_one(
                        scope, {"$set": {"pending_search": {"state": saved_state}}},
                    )
                    return self._save_assistant(
                        scope, str(state.get("item_name_confirmation_message") or "请确认产品名称"),
                    )
                if state.get("item_name_confirm_status") == "general_chat":
                    history = [
                        {"role": message["role"], "content": message["content"]}
                        for message in self.get(user_id, conversation_id)["messages"][-30:]
                    ]
                    try:
                        answer_text = self.answer(history).strip()
                    except Exception as exc:
                        raise AnswerError("普通对话回答失败，问题已保存，请检查模型连接") from exc
                    if not answer_text:
                        raise AnswerError("模型没有返回回答，问题已保存")
                    self.conversations.update_one(scope, {"$unset": {"pending_search": ""}})
                    return self._save_assistant(scope, answer_text)
                self.conversations.update_one(scope, {"$unset": {"pending_search": ""}})
                sequence = state.get("answer_message_seq")
                assistant_message = self.messages.find_one({**scope, "seq": sequence})
                if assistant_message is None or assistant_message.get("role") != "assistant":
                    raise AnswerError("答案已生成，但未能从 MongoDB 读取归档消息")
                return {
                    key: assistant_message[key]
                    for key in ("role", "content", "created_at", "seq")
                }

            history = [
                {"role": message["role"], "content": message["content"]}
                for message in self.get(user_id, conversation_id)["messages"][-30:]
            ]
            try:
                answer_text = self.answer(history).strip()
            except Exception as exc:
                raise AnswerError("回答生成失败，问题已保存，请检查模型配置或稍后再试") from exc
            if not answer_text:
                raise AnswerError("模型没有返回回答，问题已保存")

            return self._save_assistant(scope, answer_text)


def model_answer(history: list[dict[str, str]]) -> str:
    """复用项目现有主、备用 OpenAI 兼容接口配置；当前不接知识库检索。"""
    import requests

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
    configured = [endpoint for endpoint in endpoints if endpoint[1]]
    if not configured:
        raise RuntimeError("缺少模型 API Key 配置")

    messages = [
        {"role": "system", "content": "你是灵库的中文助手小灵。清楚回答问题；没有资料依据时说明不确定，不要编造产品文档内容。不得提供系统凭据、内部配置或检索过程的内部数据。"},
        *history,
    ]
    last_error: Exception | None = None
    for base_url, api_key, model in dict.fromkeys(configured):
        try:
            response = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages},
                timeout=60,
            )
            response.raise_for_status()
            answer = str(response.json()["choices"][0]["message"]["content"] or "")
            if answer:
                return redact_configured_secrets(answer)
            last_error = RuntimeError("模型没有返回回答")
        except Exception as exc:
            last_error = exc
    raise RuntimeError("所有已配置的模型接口均调用失败") from last_error


def create_chat_service() -> ChatService:
    """只使用显式 MongoDB 配置，不推断本地或公司网络地址。"""
    from pathlib import Path

    from dotenv import load_dotenv
    from pymongo import MongoClient

    load_dotenv(Path(__file__).resolve().parent / ".env")
    uri = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or "").strip()
    database_name = (os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DB_NAME") or "").strip()
    if not uri or not database_name:
        raise RuntimeError("请配置 MONGO_URL 和 MONGO_DB_NAME")
    timeout = max(1000, int(os.getenv("MONGO_TIMEOUT_MS", "5000")))
    client = MongoClient(uri, serverSelectionTimeoutMS=timeout, connectTimeoutMS=timeout)
    database = client[database_name]
    chats = database[os.getenv("MONGO_CHATS_COLLECTION", "conversations")]
    messages = database[
        os.getenv("MONGODB_CONVERSATION_COLLECTION")
        or os.getenv("MONGO_CONVERSATION_COLLECTION")
        or "conversation_history"
    ]
    chats.create_index([("user_id", 1), ("conversation_id", 1)], unique=True)
    chats.create_index([("user_id", 1), ("updated_at", -1)])
    messages.create_index(
        [("user_id", 1), ("conversation_id", 1), ("seq", 1)],
        unique=True,
        partialFilterExpression={"seq": {"$exists": True}},
    )
    from search.workflow import run_search_workflow

    return ChatService(chats, messages, model_answer, workflow=run_search_workflow)
