"""S3 helpers: presigned upload/download URLs + object put/get for docs, page renders, JSON."""

from __future__ import annotations

import json
from typing import Any

from ..config import get_settings


class S3Store:
    def __init__(self, bucket: str | None = None, client=None):
        settings = get_settings()
        self.bucket = bucket or settings.docs_bucket
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=settings.core_region)
        self.client = client

    def presign_put(
        self, key: str, content_type: str = "application/pdf", expires: int = 900
    ) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )

    def presign_get(self, key: str, expires: int = 900) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put_json(self, key: str, obj: Any) -> None:
        self.put_bytes(key, json.dumps(obj).encode(), "application/json")
