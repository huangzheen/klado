"""
Alibaba Cloud OSS storage service.

All three former MinIO buckets (datacenter-raw, product-images, competitor-images)
are stored in a single OSS bucket (OSS_BUCKET_NAME = "klado").
The old bucket name becomes the first segment of the object key:
  datacenter-raw/2026-01/20260101_120000_file.xlsx
  product-images/U1234.jpg
  competitor-images/acme/acme-model-123.jpg
"""
from __future__ import annotations

import io
import logging
import os
import time
from collections.abc import Callable
from typing import Generator

import oss2

# ── Configuration ─────────────────────────────────────────────────────────────

_OSS_ACCESS_KEY_ID     = os.environ.get("OSS_ACCESS_KEY_ID", "")
_OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
_OSS_ENDPOINT          = os.environ.get("OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
_OSS_BUCKET_NAME       = os.environ.get("OSS_BUCKET_NAME", "klado")
_OSS_TIMEOUT_SECS      = int(os.environ.get("OSS_TIMEOUT_SECS", "60"))
_OSS_RETRIES           = int(os.environ.get("OSS_RETRIES", "2"))

# Opt-in S3-compatible backend (e.g. a self-hosted MinIO). When OSS_S3_COMPAT
# is enabled, every operation goes through boto3's S3 client (AWS SigV4)
# against OSS_ENDPOINT instead of the Alibaba OSS SDK — aliyun's OSS signing
# scheme is rejected by plain S3 servers ("Please use AWS4-HMAC-SHA256").
# Left unset (the default), the exact original oss2/Alibaba Cloud path runs.
_S3_COMPAT = os.environ.get("OSS_S3_COMPAT", "").strip().lower() in ("1", "true", "yes", "on")
_OSS_REGION = os.environ.get("OSS_REGION", "us-east-1")

_S3_CLIENT = None  # lazily created boto3 client (S3-compatible mode only)

_LOG = logging.getLogger("klado.oss")
_LOG.setLevel(logging.INFO)

_CT_MAP = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
    "xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    "csv":  "text/csv",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "png":  "image/png",
    "webp": "image/webp",
    "gif":  "image/gif",
    "pdf":  "application/pdf",
}


def _bucket() -> oss2.Bucket:
    """Return an oss2.Bucket instance (stateless; cheap to construct)."""
    auth = oss2.Auth(_OSS_ACCESS_KEY_ID, _OSS_ACCESS_KEY_SECRET)
    bucket = oss2.Bucket(auth, _OSS_ENDPOINT, _OSS_BUCKET_NAME, connect_timeout=5)
    # oss2 silently defaults the whole-request (read/response) timeout to 3600s;
    # connect_timeout only covers the TCP handshake. A hung cross-region link
    # would otherwise freeze snapshot upload/download workers for a full hour.
    bucket.timeout = _OSS_TIMEOUT_SECS
    return bucket


def _s3():
    """Return a cached boto3 S3 client for an S3-compatible endpoint (MinIO).

    Only used when OSS_S3_COMPAT is enabled; importing boto3 lazily keeps the
    default Alibaba Cloud path free of any boto3 dependency.
    """
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3
        from botocore.config import Config
        _S3_CLIENT = boto3.client(
            "s3",
            endpoint_url=_OSS_ENDPOINT,
            aws_access_key_id=_OSS_ACCESS_KEY_ID,
            aws_secret_access_key=_OSS_ACCESS_KEY_SECRET,
            region_name=_OSS_REGION,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},  # localhost/IP endpoints are not virtual-hostable
                connect_timeout=5,
                # Mirrors _bucket()'s timeout: a hung endpoint must not wedge a
                # snapshot worker for an hour.
                read_timeout=_OSS_TIMEOUT_SECS,
                # _run() owns retries; keep botocore's own layer at a single try
                # so a failure is not retried twice.
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
    return _S3_CLIENT


def _s3_is_not_found(exc: Exception) -> bool:
    """True when a boto3 error means the S3 object does not exist."""
    try:
        from botocore.exceptions import ClientError
    except Exception:  # boto3 absent — not an S3-compatible deployment
        return False
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        return status == 404 or code in ("404", "NoSuchKey", "NotFound")
    return False


def _s3_is_retryable(exc: Exception) -> bool:
    """Network failures and S3 5xx are retryable; 4xx (incl. 404) are not."""
    try:
        from botocore.exceptions import (
            ClientError, ConnectionClosedError, ConnectTimeoutError,
            EndpointConnectionError, ReadTimeoutError,
        )
    except Exception:
        return False
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError,
                        ReadTimeoutError, ConnectionClosedError)):
        return True
    if isinstance(exc, ClientError):
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        return status >= 500
    return False


class _S3NotFound(Exception):
    """Internal marker: boto3 reported the object as missing.

    Lets callers reuse `_run(..., quiet_exceptions=...)` exactly like the oss2
    path does with `oss2.exceptions.NoSuchKey`, so a normal existence probe
    neither logs an error nor gets retried.
    """


def _s3_head_object(full_key: str):
    """head_object that raises _S3NotFound for a missing key; other errors pass through."""
    try:
        return _s3().head_object(Bucket=_OSS_BUCKET_NAME, Key=full_key)
    except Exception as exc:
        if _s3_is_not_found(exc):
            raise _S3NotFound(full_key) from exc
        raise


def _s3_delete_object(full_key: str):
    """delete_object that raises _S3NotFound for a missing key; other errors pass through."""
    try:
        return _s3().delete_object(Bucket=_OSS_BUCKET_NAME, Key=full_key)
    except Exception as exc:
        if _s3_is_not_found(exc):
            raise _S3NotFound(full_key) from exc
        raise


def _error_details(exc: Exception) -> str:
    """Useful OSS error identifiers, without ever exposing credentials."""
    parts = [type(exc).__name__, str(exc)]
    for name in ("status", "code", "request_id"):
        value = getattr(exc, name, None)
        if value:
            parts.append(f"{name}={value}")
    return " ".join(parts)


def _is_retryable(exc: Exception) -> bool:
    """Network-level failures and OSS 5xx are worth retrying; 4xx is not."""
    if _S3_COMPAT:
        return _s3_is_retryable(exc)
    if isinstance(exc, oss2.exceptions.RequestError):
        return True  # timeouts / connection errors
    if isinstance(exc, oss2.exceptions.OssError):
        return int(getattr(exc, "status", 0) or 0) >= 500
    return False


def _run(
    operation: str,
    full_key: str,
    request: Callable[[], object],
    quiet_exceptions: tuple[type[Exception], ...] = (),
) -> object:
    last_exc: Exception | None = None
    for attempt in range(_OSS_RETRIES + 1):
        started = time.monotonic()
        _LOG.info("OSS %s start endpoint=%s bucket=%s key=%s attempt=%d",
                  operation, _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key, attempt + 1)
        try:
            result = request()
        except quiet_exceptions:
            raise
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if _is_retryable(exc) and attempt < _OSS_RETRIES:
                _LOG.warning(
                    "OSS %s retryable failure endpoint=%s bucket=%s key=%s elapsed_ms=%d "
                    "attempt=%d error=%s — retrying",
                    operation, _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key,
                    elapsed_ms, attempt + 1, _error_details(exc),
                )
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue
            _LOG.error(
                "OSS %s failed endpoint=%s bucket=%s key=%s elapsed_ms=%d error=%s",
                operation, _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key,
                elapsed_ms, _error_details(exc),
            )
            raise
        _LOG.info(
            "OSS %s succeeded endpoint=%s bucket=%s key=%s elapsed_ms=%d",
            operation, _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key,
            int((time.monotonic() - started) * 1000),
        )
        return result
    raise last_exc  # unreachable, keeps type-checkers happy


def log_startup_diagnostics() -> bool:
    """Log safe configuration and test bucket access without blocking startup."""
    configured = bool(_OSS_ACCESS_KEY_ID and _OSS_ACCESS_KEY_SECRET and _OSS_ENDPOINT and _OSS_BUCKET_NAME)
    _LOG.info(
        "OSS startup configuration endpoint=%s bucket=%s s3_compat=%s access_key_id_set=%s access_key_secret_set=%s",
        _OSS_ENDPOINT, _OSS_BUCKET_NAME, _S3_COMPAT,
        bool(_OSS_ACCESS_KEY_ID), bool(_OSS_ACCESS_KEY_SECRET),
    )
    if not configured:
        _LOG.warning("OSS startup check skipped: credentials or bucket configuration is missing")
        return False
    try:
        if _S3_COMPAT:
            _run(
                "startup_check",
                "datacenter-raw/",
                lambda: _s3().list_objects_v2(
                    Bucket=_OSS_BUCKET_NAME, Prefix="datacenter-raw/", MaxKeys=1),
            )
        else:
            _run(
                "startup_check",
                "datacenter-raw/",
                lambda: _bucket().list_objects(prefix="datacenter-raw/", max_keys=1),
            )
        return True
    except Exception:
        # _run already emitted status/code/request-id.  An OSS outage must not
        # prevent the rest of the application from serving non-file features.
        try:
            if _S3_COMPAT:
                bucket_names = [b["Name"] for b in _s3().list_buckets().get("Buckets", [])]
            else:
                service = oss2.Service(
                    oss2.Auth(_OSS_ACCESS_KEY_ID, _OSS_ACCESS_KEY_SECRET),
                    _OSS_ENDPOINT,
                    connect_timeout=5,
                )
                bucket_names = [item.name for item in service.list_buckets().buckets]
            _LOG.warning(
                "OSS credential discovery endpoint=%s accessible_buckets=%s configured_bucket=%s",
                _OSS_ENDPOINT, bucket_names, _OSS_BUCKET_NAME,
            )
        except Exception as discovery_exc:
            _LOG.error(
                "OSS credential discovery failed endpoint=%s error=%s",
                _OSS_ENDPOINT, _error_details(discovery_exc),
            )
        return False


def _full_key(bucket_prefix: str, key: str) -> str:
    """Compose the full OSS object key: '<bucket_prefix>/<key>'."""
    bucket_prefix = bucket_prefix.strip("/")
    key = key.lstrip("/")
    return f"{bucket_prefix}/{key}"


# ── Public API ────────────────────────────────────────────────────────────────

def put_object(bucket_prefix: str, key: str, data: bytes,
               content_type: str | None = None) -> str:
    """Upload bytes to OSS. Returns the full OSS key."""
    full_key = _full_key(bucket_prefix, key)
    if content_type is None:
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        content_type = _CT_MAP.get(ext, "application/octet-stream")
    if _S3_COMPAT:
        _run("put_object", full_key,
             lambda: _s3().put_object(Bucket=_OSS_BUCKET_NAME, Key=full_key,
                                      Body=data, ContentType=content_type))
        return full_key
    headers = {"Content-Type": content_type}
    _run("put_object", full_key, lambda: _bucket().put_object(full_key, data, headers=headers))
    return full_key


def put_object_from_file(bucket_prefix: str, key: str, file_path: str,
                         content_type: str | None = None) -> str:
    """Upload a local file to OSS without loading it all into memory."""
    full_key = _full_key(bucket_prefix, key)
    if content_type is None:
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        content_type = _CT_MAP.get(ext, "application/octet-stream")
    if _S3_COMPAT:
        def _upload():
            # Reopen per attempt so a retry restarts from byte 0.
            with open(file_path, "rb") as fh:
                return _s3().put_object(Bucket=_OSS_BUCKET_NAME, Key=full_key,
                                        Body=fh, ContentType=content_type)
        _run("put_object_from_file", full_key, _upload)
        return full_key
    headers = {"Content-Type": content_type}
    _run("put_object_from_file", full_key, lambda: _bucket().put_object_from_file(full_key, file_path, headers=headers))
    return full_key


def get_object(bucket_prefix: str, key: str) -> bytes:
    """Download an object from OSS and return its bytes."""
    full_key = _full_key(bucket_prefix, key)
    if _S3_COMPAT:
        result = _run("get_object", full_key,
                      lambda: _s3().get_object(Bucket=_OSS_BUCKET_NAME, Key=full_key))
        body = result["Body"]
        try:
            return body.read()
        finally:
            body.close()
    result = _run("get_object", full_key, lambda: _bucket().get_object(full_key))
    return result.read()


def get_object_stream(bucket_prefix: str, key: str,
                      process: str | None = None) -> tuple[Generator, str]:
    """
    Return (response_iterator, content_type) for streaming.
    Caller must iterate the generator to consume the stream.

    `process` is an optional OSS image-processing instruction (e.g.
    ``image/resize,w_120``). The bucket must have the image service enabled;
    callers that pass it are expected to fall back to a plain fetch on error.
    """
    full_key = _full_key(bucket_prefix, key)
    if _S3_COMPAT:
        # S3/MinIO has no OSS image-processing (`process` is an Alibaba feature).
        # Callers that pass it already fall back to the original object, so we
        # simply ignore it and stream the plain bytes.
        resp = _run("get_object_stream", full_key,
                    lambda: _s3().get_object(Bucket=_OSS_BUCKET_NAME, Key=full_key))
        body = resp["Body"]
        content_type = resp.get("ContentType") or "application/octet-stream"
        return body.iter_chunks(), content_type
    if process:
        resp = _run("get_object_stream", full_key,
                    lambda: _bucket().get_object(full_key, params={"x-oss-process": process}))
    else:
        resp = _run("get_object_stream", full_key, lambda: _bucket().get_object(full_key))
    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    return resp, content_type


def object_exists(bucket_prefix: str, key: str) -> bool:
    """Return True if the object exists in OSS."""
    full_key = _full_key(bucket_prefix, key)
    if _S3_COMPAT:
        try:
            _run("head_object", full_key, lambda: _s3_head_object(full_key),
                 quiet_exceptions=(_S3NotFound,))
            return True
        except _S3NotFound:
            _LOG.info("OSS head_object not_found endpoint=%s bucket=%s key=%s",
                      _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key)
            return False
        except Exception:
            return False
    try:
        _run(
            "head_object", full_key, lambda: _bucket().head_object(full_key),
            quiet_exceptions=(oss2.exceptions.NoSuchKey,),
        )
        return True
    except oss2.exceptions.NoSuchKey:
        _LOG.info("OSS head_object not_found endpoint=%s bucket=%s key=%s", _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key)
        return False
    except Exception:
        return False


def object_size(bucket_prefix: str, key: str) -> int:
    """Return the object size in bytes. Raises if the object cannot be stat'ed."""
    full_key = _full_key(bucket_prefix, key)
    if _S3_COMPAT:
        resp = _run("head_object", full_key,
                    lambda: _s3().head_object(Bucket=_OSS_BUCKET_NAME, Key=full_key))
        return int(resp["ContentLength"])
    return int(_run("head_object", full_key, lambda: _bucket().head_object(full_key)).content_length)


def remove_object(bucket_prefix: str, key: str) -> None:
    """Delete an object from OSS. Silently ignores missing objects."""
    full_key = _full_key(bucket_prefix, key)
    if _S3_COMPAT:
        try:
            _run("delete_object", full_key, lambda: _s3_delete_object(full_key),
                 quiet_exceptions=(_S3NotFound,))
        except _S3NotFound:
            _LOG.info("OSS delete_object not_found endpoint=%s bucket=%s key=%s",
                      _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key)
        return
    try:
        _run(
            "delete_object", full_key, lambda: _bucket().delete_object(full_key),
            quiet_exceptions=(oss2.exceptions.NoSuchKey,),
        )
    except oss2.exceptions.NoSuchKey:
        _LOG.info("OSS delete_object not_found endpoint=%s bucket=%s key=%s", _OSS_ENDPOINT, _OSS_BUCKET_NAME, full_key)


def copy_object(bucket_prefix: str, dest_key: str, src_key: str) -> None:
    """Copy src_key to dest_key within the same bucket prefix."""
    src_full  = _full_key(bucket_prefix, src_key)
    dest_full = _full_key(bucket_prefix, dest_key)
    if _S3_COMPAT:
        _run("copy_object", f"{src_full} -> {dest_full}",
             lambda: _s3().copy_object(
                 Bucket=_OSS_BUCKET_NAME, Key=dest_full,
                 CopySource={"Bucket": _OSS_BUCKET_NAME, "Key": src_full}))
        return
    _run("copy_object", f"{src_full} -> {dest_full}", lambda: _bucket().copy_object(_OSS_BUCKET_NAME, src_full, dest_full))


class _OSSObject:
    """Minimal stand-in for minio Object to keep callers compatible."""
    __slots__ = ("key", "size", "last_modified", "is_dir")

    def __init__(self, key: str, size: int, last_modified, is_dir: bool = False):
        import datetime as _dt
        self.key  = key
        self.size = size
        # oss2 returns last_modified as a Unix int timestamp; normalise to datetime
        if isinstance(last_modified, (int, float)):
            self.last_modified = _dt.datetime.fromtimestamp(last_modified, tz=_dt.timezone.utc)
        else:
            self.last_modified = last_modified
        self.is_dir = is_dir


def get_presigned_url(bucket_prefix: str, key: str, expires: int = 3600) -> str:
    """Return a pre-signed GET URL valid for `expires` seconds."""
    full_key = _full_key(bucket_prefix, key)
    if _S3_COMPAT:
        return _run("sign_url", full_key,
                    lambda: _s3().generate_presigned_url(
                        "get_object",
                        Params={"Bucket": _OSS_BUCKET_NAME, "Key": full_key},
                        ExpiresIn=expires))
    return _run("sign_url", full_key, lambda: _bucket().sign_url("GET", full_key, expires))


def list_top_level_prefixes() -> list[str]:
    """Return top-level prefixes in the configured OSS bucket with diagnostics."""
    def _list() -> list[str]:
        if _S3_COMPAT:
            prefixes: set[str] = set()
            paginator = _s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=_OSS_BUCKET_NAME, Delimiter="/"):
                for cp in page.get("CommonPrefixes", []):
                    prefixes.add(cp["Prefix"].rstrip("/"))
            return sorted(prefixes)
        return sorted({obj.key.rstrip("/") for obj in oss2.ObjectIterator(_bucket(), delimiter="/") if obj.key.endswith("/")})
    return _run("list_top_level_prefixes", "<root>", _list)


def list_objects(bucket_prefix: str, prefix: str = "",
                 recursive: bool = False) -> list[_OSSObject]:
    """
    List objects under bucket_prefix/prefix.
    When recursive=False uses delimiter='/' to emulate directory listing.
    Returns list of _OSSObject with .key relative to bucket_prefix (no leading slash).
    """
    full_prefix = _full_key(bucket_prefix, prefix) if prefix else bucket_prefix + "/"
    delimiter   = "" if recursive else "/"
    results: list[_OSSObject] = []

    def _list() -> list[_OSSObject]:
        if _S3_COMPAT:
            kwargs = {"Bucket": _OSS_BUCKET_NAME, "Prefix": full_prefix}
            if delimiter:
                kwargs["Delimiter"] = delimiter
            paginator = _s3().get_paginator("list_objects_v2")
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    rel_key = obj["Key"][len(bucket_prefix) + 1:]  # remove "bucket_prefix/"
                    results.append(_OSSObject(key=rel_key, size=obj["Size"],
                                              last_modified=obj["LastModified"], is_dir=False))
                for cp in page.get("CommonPrefixes", []):
                    rel_key = cp["Prefix"][len(bucket_prefix) + 1:]
                    results.append(_OSSObject(key=rel_key, size=0, last_modified=None, is_dir=True))
            return results
        bucket = _bucket()
        for obj in oss2.ObjectIterator(bucket, prefix=full_prefix, delimiter=delimiter):
            # Strip the bucket_prefix/ from the front so callers see the same
            # relative keys as they did with MinIO
            rel_key = obj.key[len(bucket_prefix) + 1:]  # remove "bucket_prefix/"
            if obj.is_prefix():
                results.append(_OSSObject(key=rel_key, size=0, last_modified=None, is_dir=True))
            else:
                results.append(_OSSObject(key=rel_key, size=obj.size, last_modified=obj.last_modified, is_dir=False))
        return results

    return _run("list_objects", full_prefix, _list)
