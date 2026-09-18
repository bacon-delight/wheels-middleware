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
            from botocore.config import Config

            # ap-south-2 (and other newer regions) do NOT serve the global s3.amazonaws.com
            # endpoint, so presigned URLs must target the REGIONAL endpoint with SigV4 —
            # otherwise a browser PUT to the presigned URL is rejected with 400.
            region = settings.core_region
            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=f"https://s3.{region}.amazonaws.com",
                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            )
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

    def delete_prefix(self, prefix: str) -> int:
        """Remove every object under a prefix. Used when a document is deleted outright."""
        deleted, token = 0, None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            r = self.client.list_objects_v2(**kwargs)
            objects = [{"Key": o["Key"]} for o in r.get("Contents", [])]
            if objects:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})
                deleted += len(objects)
            token = r.get("NextContinuationToken")
            if not r.get("IsTruncated"):
                return deleted
