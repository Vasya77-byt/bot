import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("financial-architect")


def save_file_bytes(content: bytes, filename: str) -> Optional[str]:
    """
    Save file to local storage dir. If S3 bucket configured and boto3 available,
    try to upload as well. Returns local path or None.
    """
    storage_dir = Path(os.getenv("STORAGE_DIR", "storage"))
    storage_dir.mkdir(parents=True, exist_ok=True)
    local_path = storage_dir / filename
    try:
        local_path.write_bytes(content)
    except Exception as exc:
        logger.warning("Failed to write file %s: %s", local_path, exc)
        return None

    _maybe_upload_s3(content, filename)
    return str(local_path)


def _maybe_upload_s3(content: bytes, filename: str) -> None:
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        return
    key_prefix = os.getenv("S3_PREFIX", "").rstrip("/")
    key = f"{key_prefix}/{filename}" if key_prefix else filename
    try:
        import boto3  # type: ignore
    except Exception:
        logger.warning("boto3 not installed; skip S3 upload")
        return

    extra_args = {"ContentType": _content_type(filename)}
    try:
        session = boto3.session.Session(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_DEFAULT_REGION"),
        )
        s3 = session.client("s3", endpoint_url=os.getenv("AWS_ENDPOINT_URL"))
        s3.put_object(Bucket=bucket, Key=key, Body=content, **extra_args)
        logger.info("Uploaded to S3 bucket=%s key=%s", bucket, key)
    except Exception as exc:
        logger.warning("Failed to upload to S3 bucket=%s key=%s: %s", bucket, key, exc)


def _content_type(filename: str) -> str:
    if filename.endswith(".pdf"):
        return "application/pdf"
    if filename.endswith(".png"):
        return "image/png"
    return "application/octet-stream"

