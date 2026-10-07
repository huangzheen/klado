"""
MinerU API 客户端（Klado）
- Batch file upload for PDF/PPT/DOC/DOCX/images
- 轮询解析状态
- 下载并提取 markdown 结果
"""
import io
import time
import zipfile
from typing import Any

import httpx

MINERU_BASE = "https://mineru.net/api/v4"


class MinerUError(Exception):
    pass


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def submit_file(file_bytes: bytes, filename: str, token: str) -> str:
    """
    上传文件到 MinerU，返回 batch_id。
    流程：
      1. POST /file-urls/batch → 获取预签名上传 URL
      2. PUT 文件数据到预签名 URL
    返回 batch_id（用于轮询）
    """
    resp = httpx.post(
        f"{MINERU_BASE}/file-urls/batch",
        headers=_auth_header(token),
        json={
            "files": [{"name": filename, "is_ocr": True}],
            "model_version": "vlm",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise MinerUError(f"申请上传 URL 失败: {data}")

    batch_id: str = data["data"]["batch_id"]
    upload_urls: list[str] = data["data"]["file_urls"]

    put_resp = httpx.put(upload_urls[0], content=file_bytes, timeout=120)
    if put_resp.status_code != 200:
        raise MinerUError(f"文件上传失败: HTTP {put_resp.status_code}")

    return batch_id


def poll_result(batch_id: str, token: str) -> dict[str, Any]:
    """
    查询单个 batch 的解析进度。
    返回 data 部分，包含 extract_result 列表。
    state: pending / running / done / failed / converting / waiting-file
    """
    resp = httpx.get(
        f"{MINERU_BASE}/extract-results/batch/{batch_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise MinerUError(f"查询结果失败: {data}")
    return data["data"]


def download_markdown(zip_url: str) -> str:
    """
    下载结果 ZIP，提取 .md 文件内容并返回字符串。
    """
    resp = httpx.get(zip_url, timeout=120, follow_redirects=True)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        names = z.namelist()
        md_files = [n for n in names if n.endswith(".md")]
        if md_files:
            return z.read(md_files[0]).decode("utf-8", errors="replace")
        txt_files = [n for n in names if n.endswith(".txt")]
        if txt_files:
            return z.read(txt_files[0]).decode("utf-8", errors="replace")
        return f"[ZIP 内无 .md 文件, 包含: {names}]"
