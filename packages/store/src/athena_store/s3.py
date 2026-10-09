"""Upload a local snapshot to the S3-compatible endpoint in the environment."""

from __future__ import annotations

import os
from pathlib import Path


def sync_snapshot(path: Path) -> str | None:
    """Upload one file. Returns the object key, or None when sync is off."""
    enabled = os.environ.get("ATHENA_S3_SYNC_ENABLED", "").lower()
    if enabled not in {"1", "true", "yes"}:
        return None
    import boto3
    from botocore.config import Config

    bucket = os.environ["ATHENA_S3_BUCKET"]
    prefix = os.environ.get("ATHENA_S3_PREFIX", "")
    key = f"{prefix}{path.name}"
    addressing = "path"
    if os.environ.get("ATHENA_S3_FORCE_PATH_STYLE", "true").lower() == "false":
        addressing = "virtual"
    verify = os.environ.get("ATHENA_S3_TLS_VERIFY", "true").lower() != "false"
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["ATHENA_S3_ENDPOINT"],
        region_name=os.environ.get("ATHENA_S3_REGION", "us-east-1"),
        aws_access_key_id=os.environ["ATHENA_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["ATHENA_S3_SECRET_ACCESS_KEY"],
        config=Config(s3={"addressing_style": addressing}),
        verify=verify,
    )
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        client.create_bucket(Bucket=bucket)
    client.upload_file(str(path), bucket, key)
    return key
