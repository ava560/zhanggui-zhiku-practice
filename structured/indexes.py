"""值索引和元数据向量只用于召回。写失败时不切换可查询版本。"""

from __future__ import annotations

import os
from typing import Any

import requests


class RecordingIndex:
    """测试里记下写入顺序。fail=True 时模拟索引失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def write_values(self, data_version_id: str, records: list[dict[str, Any]]) -> None:
        if self.fail:
            raise RuntimeError("值索引失败")
        self.calls.append({"kind": "values", "data_version_id": data_version_id, "records": records})

    def write_metadata(self, import_version_id: str, documents: list[dict[str, Any]]) -> None:
        if self.fail:
            raise RuntimeError("元数据向量失败")
        self.calls.append({"kind": "metadata", "import_version_id": import_version_id, "documents": documents})

    def delete_values(self, data_version_id: str) -> None:
        self.calls.append({"kind": "delete_values", "data_version_id": data_version_id})

    def delete_metadata(self, import_version_id: str) -> None:
        self.calls.append({"kind": "delete_metadata", "import_version_id": import_version_id})

    def search_values(self, tokens: list[str], data_version_ids: list[str]) -> list[dict[str, str]]:
        found = []
        visible = set(data_version_ids)
        for call in self.calls:
            if call["kind"] != "values" or call["data_version_id"] not in visible:
                continue
            for record in call["records"]:
                if str(record["value"]) in tokens:
                    found.append(record)
        return found


class ElasticsearchValueIndex:
    def __init__(self) -> None:
        self.url = os.getenv("ELASTICSEARCH_URL", "").strip().rstrip("/")
        self.index = os.getenv("ELASTICSEARCH_VALUE_INDEX", "table_values").strip() or "table_values"
        self.user = os.getenv("ELASTICSEARCH_USER", "").strip()
        self.password = os.getenv("ELASTICSEARCH_PASSWORD", "")
        if not self.url:
            raise RuntimeError("缺少 ELASTICSEARCH_URL")

    def write_values(self, data_version_id: str, records: list[dict[str, Any]]) -> None:
        self._ensure_index()
        self._delete_version(data_version_id)
        if not records:
            self._refresh()
            return
        lines: list[str] = []
        for record in records:
            identity = "|".join(
                str(record[key])
                for key in ("column_id", "table_id", "import_version_id", "data_version_id", "value")
            )
            lines.append(__import__("json").dumps({"index": {"_id": identity}}, ensure_ascii=False))
            lines.append(__import__("json").dumps(record, ensure_ascii=False))
        response = self._request(
            "POST",
            f"/{self.index}/_bulk",
            data=("\n".join(lines) + "\n").encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )
        if response.status_code >= 300 or response.json().get("errors"):
            raise RuntimeError("值索引写入失败")
        self._refresh()
        counted = self._count(data_version_id)
        if counted != len({self._identity(item) for item in records}):
            raise RuntimeError("值索引核对失败")

    def delete_values(self, data_version_id: str) -> None:
        self._delete_version(data_version_id)

    def search_values(self, tokens: list[str], data_version_ids: list[str]) -> list[dict[str, str]]:
        if not tokens or not data_version_ids:
            return []
        body = {
            "size": 50,
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"value": tokens}},
                        {"terms": {"data_version_id": data_version_ids}},
                    ]
                }
            },
        }
        response = self._request("POST", f"/{self.index}/_search", json=body)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        hits = response.json().get("hits", {}).get("hits", [])
        return [hit.get("_source") or {} for hit in hits]

    def write_metadata(self, import_version_id: str, documents: list[dict[str, Any]]) -> None:
        del import_version_id, documents

    def delete_metadata(self, import_version_id: str) -> None:
        del import_version_id

    def _ensure_index(self) -> None:
        mapping = {"mappings": {"properties": {
            "column_id": {"type": "keyword"},
            "table_id": {"type": "keyword"},
            "import_version_id": {"type": "keyword"},
            "data_version_id": {"type": "keyword"},
            "value": {"type": "keyword"},
        }}}
        response = self._request("PUT", f"/{self.index}", json=mapping)
        if response.status_code not in {200, 400}:
            response.raise_for_status()

    def _delete_version(self, data_version_id: str) -> None:
        response = self._request(
            "POST",
            f"/{self.index}/_delete_by_query?refresh=true",
            json={"query": {"term": {"data_version_id": data_version_id}}},
        )
        if response.status_code == 404:
            return
        response.raise_for_status()

    def _refresh(self) -> None:
        self._request("POST", f"/{self.index}/_refresh").raise_for_status()

    def _count(self, data_version_id: str) -> int:
        response = self._request(
            "POST",
            f"/{self.index}/_count",
            json={"query": {"term": {"data_version_id": data_version_id}}},
        )
        response.raise_for_status()
        return int(response.json().get("count") or 0)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        auth = (self.user, self.password) if self.user else None
        return requests.request(method, self.url + path, auth=auth, timeout=30, **kwargs)

    @staticmethod
    def _identity(record: dict[str, Any]) -> str:
        return "|".join(str(record[key]) for key in ("column_id", "table_id", "data_version_id", "value"))


class MetadataVectorIndex:
    """同一 Milvus 实例上的独立集合，不写入文档切片集合。"""

    def write_values(self, data_version_id: str, records: list[dict[str, Any]]) -> None:
        del data_version_id, records

    def delete_values(self, data_version_id: str) -> None:
        del data_version_id

    def search_values(self, tokens: list[str], data_version_ids: list[str]) -> list[dict[str, str]]:
        del tokens, data_version_ids
        return []

    def write_metadata(self, import_version_id: str, documents: list[dict[str, Any]]) -> None:
        client, collection = _metadata_collection()
        try:
            self.delete_metadata(import_version_id)
            if not documents:
                return
            from search.node_item_name_confirm import generate_query_vectors, load_settings

            settings = load_settings()
            rows = []
            for document in documents:
                dense, _sparse = generate_query_vectors(settings, document["text"])
                rows.append({
                    "id": document["id"],
                    "vector": dense,
                    "text": document["text"][:2000],
                    "import_version_id": import_version_id,
                    "table_id": document.get("table_id") or "",
                    "column_id": document.get("column_id") or "",
                    "metric_id": document.get("metric_id") or "",
                    "kind": document.get("kind") or "",
                })
            client.insert(collection_name=collection, data=rows)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def delete_metadata(self, import_version_id: str) -> None:
        client, collection = _metadata_collection()
        try:
            if client.has_collection(collection):
                client.delete(collection_name=collection, filter=f'import_version_id == "{import_version_id}"')
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()


class CombinedIndex:
    def __init__(self, values: Any, metadata: Any) -> None:
        self.values = values
        self.metadata = metadata

    def write_values(self, data_version_id: str, records: list[dict[str, Any]]) -> None:
        self.values.write_values(data_version_id, records)

    def write_metadata(self, import_version_id: str, documents: list[dict[str, Any]]) -> None:
        self.metadata.write_metadata(import_version_id, documents)

    def delete_values(self, data_version_id: str) -> None:
        self.values.delete_values(data_version_id)

    def delete_metadata(self, import_version_id: str) -> None:
        self.metadata.delete_metadata(import_version_id)

    def search_values(self, tokens: list[str], data_version_ids: list[str]) -> list[dict[str, str]]:
        return self.values.search_values(tokens, data_version_ids)


def live_index() -> CombinedIndex:
    return CombinedIndex(ElasticsearchValueIndex(), MetadataVectorIndex())


def _metadata_collection() -> tuple[Any, str]:
    from pymilvus import DataType, MilvusClient

    from search.node_item_name_confirm import get_milvus_client, load_settings

    settings = load_settings()
    collection = os.getenv("METADATA_COLLECTION", "kb_table_metadata").strip() or "kb_table_metadata"
    client = get_milvus_client(settings)
    if not client.has_collection(collection):
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=160)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=int(settings.embedding_dim or 1024))
        schema.add_field("text", DataType.VARCHAR, max_length=2048)
        schema.add_field("import_version_id", DataType.VARCHAR, max_length=80)
        schema.add_field("table_id", DataType.VARCHAR, max_length=80)
        schema.add_field("column_id", DataType.VARCHAR, max_length=80)
        schema.add_field("metric_id", DataType.VARCHAR, max_length=80)
        schema.add_field("kind", DataType.VARCHAR, max_length=32)
        index = client.prepare_index_params()
        index.add_index(field_name="vector", metric_type="COSINE", index_type="AUTOINDEX")
        client.create_collection(collection_name=collection, schema=schema, index_params=index)
    return client, collection
