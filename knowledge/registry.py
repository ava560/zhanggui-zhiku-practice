"""导入和查询共用的实体注册表。名单只提供已审核名称，不限制谁可以入库。"""

from __future__ import annotations

from knowledge.contracts import EntityRecord


_SEED = (
    ("易方达智造优势混合C", "fund", "C", "fund:易方达智造优势混合C"),
    ("易方达智造优势混合", "fund", None, "fund:易方达智造优势混合"),
    ("华夏债券C", "fund", "C", "fund:华夏债券C"),
    ("华夏债券", "fund", None, "fund:华夏债券"),
    ("平安银行", "company", None, "company:平安银行"),
    ("招商银行", "company", None, "company:招商银行"),
    ("中国平安", "company", None, "company:中国平安"),
    ("贵州茅台", "company", None, "company:贵州茅台"),
    ("华夏基金", "institution", None, "institution:华夏基金"),
)


class EntityRegistry:
    def __init__(self, *, seeded: bool = True) -> None:
        self._by_id: dict[str, EntityRecord] = {}
        self._by_name: dict[str, str] = {}
        self._shared_aliases: dict[str, tuple[str, ...]] = {}
        if seeded:
            for name, entity_type, share_class, entity_id in _SEED:
                self.register(EntityRecord(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    canonical_name=name,
                    share_class=share_class,
                    status="reviewed",
                ))
            self.share_alias("平安", ["company:平安银行", "company:中国平安"])

    def register(self, entity: EntityRecord) -> EntityRecord:
        existing = self._by_name.get(_key(entity.entity_type, entity.canonical_name))
        if existing:
            return self._by_id[existing]
        for alias in entity.aliases:
            occupied = self._by_name.get(_key(entity.entity_type, alias))
            if occupied:
                return self._by_id[occupied]
        self._by_id[entity.entity_id] = entity
        self._by_name[_key(entity.entity_type, entity.canonical_name)] = entity.entity_id
        self._by_name[_key("", entity.canonical_name)] = entity.entity_id
        for alias in entity.aliases:
            self._by_name.setdefault(_key("", alias), entity.entity_id)
        return entity

    def share_alias(self, alias: str, entity_ids: list[str]) -> None:
        """一个简称对应多个已登记主体时，问题里出现该简称就要澄清。"""
        name = (alias or "").strip()
        ids = tuple(dict.fromkeys(entity_id for entity_id in entity_ids if entity_id in self._by_id))
        if name and len(ids) >= 2:
            self._shared_aliases[name] = ids

    def get(self, entity_id: str) -> EntityRecord | None:
        return self._by_id.get(entity_id)

    def find_in_text(self, text: str) -> list[EntityRecord]:
        """最长名称优先，避免短名截走长名。"""
        names = sorted(
            ((name.split("\n", 1)[1], entity_id) for name, entity_id in self._by_name.items() if name.startswith("\n")),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        occupied = [False] * len(text or "")
        found: list[EntityRecord] = []
        seen: set[str] = set()
        for name, entity_id in names:
            start = 0
            while True:
                index = text.find(name, start)
                if index < 0:
                    break
                end = index + len(name)
                if not any(occupied[index:end]) and entity_id not in seen:
                    for cursor in range(index, end):
                        occupied[cursor] = True
                    seen.add(entity_id)
                    found.append(self._by_id[entity_id])
                start = end
        return found

    def ambiguous_aliases(self, text: str) -> list[tuple[str, list[EntityRecord]]]:
        """简称落在尚未被更长规范名占用的位置，且对应至少两个主体。"""
        text = text or ""
        occupied = [False] * len(text)
        for record in self.find_in_text(text):
            start = 0
            while True:
                index = text.find(record.canonical_name, start)
                if index < 0:
                    break
                end = index + len(record.canonical_name)
                if not any(occupied[index:end]):
                    for cursor in range(index, end):
                        occupied[cursor] = True
                start = end
        found: list[tuple[str, list[EntityRecord]]] = []
        for alias, entity_ids in sorted(self._shared_aliases.items(), key=lambda item: len(item[0]), reverse=True):
            start = 0
            while True:
                index = text.find(alias, start)
                if index < 0:
                    break
                end = index + len(alias)
                if not any(occupied[index:end]):
                    for cursor in range(index, end):
                        occupied[cursor] = True
                    found.append((alias, [self._by_id[entity_id] for entity_id in entity_ids]))
                    break
                start = end
        return found

    def provisional(self, entity_type: str, canonical_name: str, *, code: str = "", share_class: str | None = None) -> EntityRecord:
        name = " ".join((canonical_name or "").split())
        if not name:
            raise ValueError("实体名称不能为空")
        entity_id = f"{entity_type}:{code}" if code else f"{entity_type}:{name}"
        return self.register(EntityRecord(
            entity_id=entity_id,
            entity_type=entity_type,
            canonical_name=name,
            status="provisional",
            share_class=share_class,
            code=code,
        ))


def _key(entity_type: str, name: str) -> str:
    return f"{entity_type}\n{name}"


_DEFAULT: EntityRegistry | None = None


def default_registry() -> EntityRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = EntityRegistry(seeded=True)
    return _DEFAULT
