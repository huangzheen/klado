"""
Storage proxy router — serves private OSS objects via the API.

Endpoint:
  GET /api/storage/serve?path=<oss_key>
  GET /api/storage/serve?path=<oss_key>&w=<px>   # downscaled (thumbnails)

The path parameter is the full OSS object key, which encodes both the
"virtual bucket" (first segment) and the file key:
  product-images/U1234.jpg
  competitor-images/acme/acme-model-123.jpg
  datacenter-raw/2026-01/20260101_120000_file.xlsx

Only keys starting with one of the known bucket prefixes are accepted.
"""
import logging
import mimetypes
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.i18n import pick, request_lang
from services import oss_storage

_LOG = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_BUCKETS = {"datacenter-raw", "product-images", "competitor-images",
                   "knowledge-assets"}

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}


def _is_image_key(key: str) -> bool:
    return os.path.splitext(key)[1].lower() in _IMAGE_EXTS


def _parse_path(path: str) -> tuple[str, str]:
    """
    Split a full OSS key like 'product-images/U1234.jpg' into
    (bucket_prefix, key). Raises ValueError for invalid paths.
    """
    if not path or ".." in path or path.startswith("/") or "\x00" in path:
        raise ValueError("invalid path")
    parts = path.split("/", 1)
    if len(parts) < 2 or not parts[1]:
        raise ValueError("path must include bucket prefix and key")
    bucket, key = parts[0], parts[1]
    if bucket not in _ALLOWED_BUCKETS:
        raise ValueError(f"unknown bucket prefix: {bucket!r}")
    return bucket, key


@router.get("/serve")
async def serve_file(
    path: str = Query(..., description="Full OSS object key, e.g. product-images/U1234.jpg"),
    download: bool = Query(False, description="Force download as attachment"),
    w: int = Query(None, ge=16, le=2048,
                   description="Downscale images to this pixel width via OSS "
                               "image processing (thumbnails). Ignored for "
                               "non-image objects and if processing fails."),
    request: Request = None,
):
    """Stream a private OSS object to the client.

    Thumbnails pass `w` to get a downscaled copy so a 1MB / 1200px original is
    not pushed into a 42px box. The target is decided from the key extension so
    the common path costs a single OSS round-trip; the plain object is the
    fallback whenever processing is not applicable.
    """
    try:
        bucket, key = _parse_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=pick(str(exc), request_lang(request) if request else None))

    # ⚠️ oss_storage 是同步阻塞调用（requests/oss2），直接在 async 处理器里调用
    # 会占住事件循环 —— 实测这个端点完全串行：单请求 0.37s，16 并发 3.0s、
    # 61 并发 11.3s，都是 ~185ms/请求。一屏 150 张缩略图因此要十几秒才出图。
    # 走线程池后并发才真的并发。
    def _fetch(process=None):
        return oss_storage.get_object_stream(bucket, key, process=process)

    want_w = w if (w and _is_image_key(key)) else None
    if want_w:
        try:
            stream, content_type = await run_in_threadpool(_fetch, f"image/resize,w_{want_w}")
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("oss resize failed, falling back to original: key=%s w=%s err=%s",
                         key, want_w, exc)
            want_w = None

    if not want_w:
        try:
            stream, content_type = await run_in_threadpool(_fetch)
        except Exception as exc:
            err_str = str(exc).lower()
            if "nosuchkey" in err_str or "404" in err_str or "not exist" in err_str:
                raise HTTPException(status_code=404, detail=pick("文件不存在 / file not found", request_lang(request) if request else None))
            raise HTTPException(status_code=502, detail=pick("对象存储错误 / storage error", request_lang(request) if request else None))

    # Guess content type from extension if OSS didn't set a useful one
    if content_type in ("application/octet-stream", "binary/octet-stream", ""):
        guessed, _ = mimetypes.guess_type(key)
        if guessed:
            content_type = guessed

    filename = os.path.basename(key)
    disposition = "attachment" if download else "inline"
    # Object names are reused per entity (e.g. product-images/<VIB>.png), so a
    # re-upload replaces the bytes under the same URL. Keep max-age short so a
    # re-upload becomes visible after reload; within a session the frontend
    # cache-busts the URL it renders after a successful upload.
    headers = {
        "Cache-Control": "public, max-age=300",
        "Content-Disposition": f'{disposition}; filename="{filename}"',
    }

    def _iter():
        for chunk in stream:
            yield chunk

    return StreamingResponse(_iter(), media_type=content_type, headers=headers)
