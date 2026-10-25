"""Object storage: a local key/value directory and an S3-compatible backend.

Both expose the same immutable-object contract: a key is written once, and ``sha256``
is computed from the actual bytes (never from a multipart upload's ETag, doc §7.1).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from app.adapters.base import AdapterError, StoredObject
from app.services.textproc import sha256_bytes

_CHUNK = 256 * 1024


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise AdapterError("storage_path_escape", key)
        return path

    def _meta_path(self, key: str) -> Path:
        return self._path(key).with_suffix(self._path(key).suffix + ".meta.json")

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._path(key)
        if path.exists():
            meta = await self.stat(key)
            if meta.sha256 == sha256_bytes(data):
                return meta
            raise AdapterError("storage_key_immutable", key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        digest = sha256_bytes(data)
        await asyncio.to_thread(
            self._meta_path(key).write_text,
            json.dumps({"content_type": content_type, "sha256": digest, "size": len(data)}),
            "utf-8",
        )
        return StoredObject(key=key, size=len(data), sha256=digest, content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise AdapterError("storage_missing", key)
        return await asyncio.to_thread(path.read_bytes)

    async def stat(self, key: str) -> StoredObject:
        path = self._path(key)
        if not path.exists():
            raise AdapterError("storage_missing", key)
        meta = json.loads(await asyncio.to_thread(self._meta_path(key).read_text, "utf-8"))
        return StoredObject(
            key=key,
            size=int(meta["size"]),
            sha256=meta["sha256"],
            content_type=meta.get("content_type", "application/octet-stream"),
        )

    async def read_range(self, key: str, start: int, end_inclusive: int) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise AdapterError("storage_missing", key)
        size = path.stat().st_size
        if start >= size or end_inclusive < start:
            raise AdapterError("storage_range_unsatisfiable", f"{start}-{end_inclusive}/{size}")
        end_inclusive = min(end_inclusive, size - 1)

        def _read() -> bytes:
            with path.open("rb") as handle:
                handle.seek(start)
                return handle.read(end_inclusive - start + 1)

        return await asyncio.to_thread(_read)

    async def stream(self, key: str, start: int, length: int) -> AsyncIterator[bytes]:
        path = self._path(key)
        remaining = length

        def _open():
            handle = path.open("rb")
            handle.seek(start)
            return handle

        handle = await asyncio.to_thread(_open)
        try:
            while remaining > 0:
                chunk = await asyncio.to_thread(handle.read, min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)


class S3Storage:
    """S3-compatible storage; ``boto3`` is an optional dependency."""

    def __init__(
        self, *, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str
    ) -> None:
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional extra
            raise AdapterError("s3_dependency_missing", "pip install '.[s3]'") from exc
        self.bucket = bucket
        self.client = boto3.client(  # pragma: no cover - requires AWS
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> StoredObject:  # pragma: no cover
        digest = sha256_bytes(data)
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return StoredObject(key=key, size=len(data), sha256=digest, content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:  # pragma: no cover
        response = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def stat(self, key: str) -> StoredObject:  # pragma: no cover
        response = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        return StoredObject(
            key=key,
            size=int(response["ContentLength"]),
            sha256=(response.get("Metadata") or {}).get("sha256", ""),
            content_type=response.get("ContentType", "application/octet-stream"),
        )

    async def read_range(self, key: str, start: int, end_inclusive: int) -> bytes:  # pragma: no cover
        response = await asyncio.to_thread(
            self.client.get_object,
            Bucket=self.bucket,
            Key=key,
            Range=f"bytes={start}-{end_inclusive}",
        )
        return await asyncio.to_thread(response["Body"].read)

    async def stream(self, key: str, start: int, length: int) -> AsyncIterator[bytes]:  # pragma: no cover
        data = await self.read_range(key, start, start + length - 1)
        yield data


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()
