"""Generic styled-xlsx renderer.

Client-side pages that already hold the data (dynamic channel columns, DOM
scraped tables, SheetJS callers) post it here instead of building the workbook
in the browser: SheetJS cannot hide gridlines, so the browser cannot reproduce
the export style contract. See services/xlsx_export.py for the rules.
"""

from __future__ import annotations

from typing import Any, List, Optional

import io

import openpyxl
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.i18n import pick, request_lang
from services import xlsx_export

router = APIRouter()

MAX_COLUMNS = 40
MAX_ROWS = 20000
MAX_CELL_CHARS = 500


class ExportColumn(BaseModel):
    label: str = ""
    kind: str = "text"                      # text | money | percent | date
    align: str = "center"                   # center | left
    width_min: Optional[int] = None


class ExportRequest(BaseModel):
    sheet_name: str = "Export"
    columns: List[ExportColumn] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    file_name: Optional[str] = None


@router.post("/xlsx")
def export_xlsx(body: ExportRequest, request: Request):
    """Render posted rows as a workbook that follows the shared style contract."""
    if not body.columns:
        raise HTTPException(status_code=400, detail=pick("没有可导出的列 / No columns to export", request_lang(request) if request else None))
    if len(body.columns) > MAX_COLUMNS:
        raise HTTPException(status_code=400, detail=pick(f"列数过多（上限 {MAX_COLUMNS}） / Too many columns (max {MAX_COLUMNS})", request_lang(request) if request else None))
    if len(body.rows) > MAX_ROWS:
        raise HTTPException(status_code=400, detail=pick(f"行数过多（上限 {MAX_ROWS}） / Too many rows (max {MAX_ROWS})", request_lang(request) if request else None))

    columns = [
        {"label": c.label[:120], "kind": c.kind, "align": c.align, "width_min": c.width_min}
        for c in body.columns
    ]
    rows = []
    for row in body.rows:
        rows.append([
            (v[:MAX_CELL_CHARS] if isinstance(v, str) else v)
            for v in (row or [])[:len(columns)]
        ])

    buf = xlsx_export.build_workbook(body.sheet_name, columns, rows)
    filename = (body.file_name or (body.sheet_name or "export") + ".xlsx")
    filename = "".join(ch for ch in filename if ch not in '\\/:*?"<>|').strip() or "export.xlsx"
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    return StreamingResponse(
        buf,
        media_type=xlsx_export.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/normalise")
async def normalise_xlsx(request: Request, file: UploadFile = File(...), row_height_floor: int | None = None):
    """Bring a client-built workbook onto the export style contract.

    The browser cannot do this itself: SheetJS cannot write the gridline setting.
    Complex reports that must keep their own layout (merged headers, section
    colours, pivots) build the file client-side and post it here so it still ends
    up gridline-free and in the house typeface.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail=pick("文件是空的 / Empty file", request_lang(request) if request else None))
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=pick(f"不是可读的工作簿: {e} / Not a readable workbook: {e}", request_lang(request) if request else None))
    changed = xlsx_export.normalise_workbook(wb, row_height_floor=row_height_floor)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = (file.filename or "export.xlsx").rsplit("/", 1)[-1]
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    return StreamingResponse(
        buf,
        media_type=xlsx_export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Cells-Restyled": str(changed),
        },
    )
