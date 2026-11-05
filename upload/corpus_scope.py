"""区分金融资料和设备手册。删除与后续导入共用同一套判断。"""

from __future__ import annotations

_DEVICE_MARKERS = (
    "用户手册",
    "用户指南",
    "产品安全手册",
    "网桥",
    "路由器",
    "显示器",
    "万用表",
    "烫金",
    "Cable",
    "Aolynk",
    "H3C",
    "M3070",
)

_FINANCIAL_MARKERS = (
    "基金",
    "理财",
    "银行",
    "保险",
    "债券",
    "净值",
    "财报",
    "年报",
    "季报",
    "季度报告",
    "中期报告",
    "年度报告",
    "货币政策",
    "投资者",
    "金融",
    "公告",
    "招募说明书",
    "基金合同",
    "统计局",
    "茅台",
    "ETF",
    "私募",
    "央行",
    "风险揭示",
    "证券",
)


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def is_non_financial(file_name: str, item_name: str = "") -> bool:
    """文件名已经表明是金融资料时保留。设备手册即使被标成商品名也删除。"""
    file_text = file_name or ""
    item_text = item_name or ""
    if _contains_any(file_text, _FINANCIAL_MARKERS):
        return False
    if _contains_any(file_text, _DEVICE_MARKERS):
        return True
    if _contains_any(item_text, _FINANCIAL_MARKERS):
        return False
    return _contains_any(item_text, _DEVICE_MARKERS)
