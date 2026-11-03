"""将文档标题中的商品名称与“用户指南”等文件描述分开。"""

from __future__ import annotations

import re

_DOCUMENT_LABEL = re.compile(
    r"用户指南|用户手册|使用手册|产品说明书|使用说明书|快速入门指南|操作手册|安装指南|维修手册|说明书"
)


def canonical_product_name(value: str) -> str:
    """只裁掉明确的文档类型及其尾部版本标记，不猜测品牌或型号。"""
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    marker = _DOCUMENT_LABEL.search(name)
    if marker:
        product = name[: marker.start()].rstrip(" -_—:：([（")
        if product:
            return product
    return name
