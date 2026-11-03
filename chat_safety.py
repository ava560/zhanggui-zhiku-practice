"""对话入口的内部信息请求拦截与已配置凭据脱敏。"""

from __future__ import annotations

import os
import re
import unicodedata

RESTRICTED_REPLY = "小灵无法提供系统凭据、内部配置或检索过程的内部数据。可以继续询问产品资料、使用方法或产品对比。"

_SECRET_REQUEST = re.compile(
    r"api[\s_-]*(?:key|密钥|秘钥)|access[\s_-]*(?:key|token)|"
    r"(?:服务|系统|后台|数据库|mongo(?:db)?|milvus|minio|模型)[^\n]{0,12}(?:密码|密钥|秘钥|token|令牌|连接串)|"
    r"(?:系统|开发者|内部)[^\n]{0,8}(?:提示词|prompt|指令)|"
    r"(?:环境变量|\.env\b|配置文件|访问令牌)",
    re.IGNORECASE,
)
_INTERNAL_RESULTS = re.compile(r"chunks?|chunk[_ -]?id|知识块|检索片段|召回结果|检索结果|切片", re.IGNORECASE)
_INTERNAL_DETAILS = re.compile(r"多少|几个|几条|数量|个数|返回|列出|展示|输出|原文|详情|列表|分数|score|编号|id\b", re.IGNORECASE)
_CREDENTIAL_ENV = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)$", re.IGNORECASE)


def is_restricted_question(question: str) -> bool:
    text = unicodedata.normalize("NFKC", question).replace("\u200b", "").replace("\ufeff", "")
    if _SECRET_REQUEST.search(text):
        return True
    return bool(_INTERNAL_RESULTS.search(text) and _INTERNAL_DETAILS.search(text))


def redact_configured_secrets(answer: str) -> str:
    values = {
        value for key, value in os.environ.items()
        if (_CREDENTIAL_ENV.search(key) or key in {"MONGO_URL", "MONGODB_URI"})
        and len(value) >= 8
    }
    for value in sorted(values, key=len, reverse=True):
        answer = answer.replace(value, "[已隐藏]")
    return answer
