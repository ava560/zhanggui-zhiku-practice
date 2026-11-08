"""原件进 MinIO。测试可以换成本地存储，避免碰真实对象。"""

from __future__ import annotations

import uuid
from io import BytesIO


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, filename: str, data: bytes) -> str:
        key = f"tables/{uuid.uuid4().hex}/{filename}"
        self.objects[key] = data
        return key

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class MinioStore:
    def put(self, filename: str, data: bytes) -> str:
        from minio import Minio

        from node_upload_minio import _safe_object_filename, load_settings

        settings = load_settings()
        client = _client(settings, Minio)
        key = f"{settings.source_dir}/tables/{uuid.uuid4().hex}/{_safe_object_filename(filename)}"
        client.put_object(settings.bucket, key, BytesIO(data), length=len(data))
        return key

    def get(self, key: str) -> bytes:
        from minio import Minio

        from node_upload_minio import load_settings

        settings = load_settings()
        client = _client(settings, Minio)
        response = client.get_object(settings.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete(self, key: str) -> None:
        from minio import Minio

        from node_upload_minio import load_settings

        settings = load_settings()
        _client(settings, Minio).remove_object(settings.bucket, key)


def _client(settings, minio_cls):
    client = minio_cls(
        settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        secure=settings.secure,
    )
    if not client.bucket_exists(settings.bucket):
        client.make_bucket(settings.bucket)
    return client
