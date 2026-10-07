"""
Excel 处理引擎
- 自动解析多 Sheet
- 智能推断列类型
- 生成 schema 指纹（用于识别"同一报表再次上传"）
- 增量更新 vs 新建表
"""
from __future__ import annotations

import hashlib
import io
import re
import openpyxl
import pandas as pd
from typing import Any, Collection
from processors.column_types import infer_column, infer_dataframe


# 列类型映射：pandas dtype → PostgreSQL type
DTYPE_MAP = {
    "int64": "BIGINT",
    "int32": "INTEGER",
    "float64": "DOUBLE PRECISION",
    "float32": "REAL",
    "bool": "BOOLEAN",
    "datetime64[ns]": "TIMESTAMP",
    "object": "TEXT",
}


def _safe_col_name(col: str) -> str:
    """把列名转成合法的 SQL 标识符"""
    col = str(col).strip()
    col = re.sub(r"[^\w\u4e00-\u9fff]", "_", col)
    col = re.sub(r"_+", "_", col).strip("_")
    if col and col[0].isdigit():
        col = "col_" + col
    return col.lower() or "unnamed"


def _infer_pg_type(series: pd.Series) -> str:
    return infer_column(series, str(series.name or ''))[0]


def _make_fingerprint(columns: list[tuple[str, str]]) -> str:
    """
    根据（列名, 类型）列表生成 schema 指纹 (MD5)
    用于识别"这是同一张报表的更新版"
    """
    sig = "|".join(f"{c}:{t}" for c, t in sorted(columns))
    return hashlib.md5(sig.encode()).hexdigest()


def parse_excel(
    file_bytes: bytes,
    filename: str,
    sheet_names: Collection[str] | None = None,
) -> list[dict]:
    """
    解析 Excel 文件，返回每个 Sheet 的解析结果列表：
    [
      {
        "sheet_name": "Sheet1",
        "columns": [("col_name", "PG_TYPE"), ...],
        "fingerprint": "abc123...",
        "df": DataFrame,
        "row_count": 1000,
        "suggested_table_name": "report_sheet1",
      },
      ...
    ]
    """
    results = []

    base_name = re.sub(r"\.[^.]+$", "", filename)
    base_name = re.sub(r"[^\w\u4e00-\u9fff]", "_", base_name).strip("_").lower()

    if filename.lower().endswith(".csv"):
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=object)
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8-sig", dtype=object)
        except Exception as e:
            raise ValueError(f"无法解析 CSV 文件: {e}")
        return [_parse_dataframe(df, "Sheet1", base_name, base_name, 1)]

    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
    except Exception as e:
        raise ValueError(f"无法解析 Excel 文件: {e}")

    selected_sheets = set(sheet_names) if sheet_names is not None else None
    # ⚠️ The same hidden technical sheet that broke the preview breaks THIS, and
    # worse: `/files/stage` swallows parser errors (`except Exception: pass`), so the
    # upload still succeeds — with EMPTY `sheets_meta` — and the file then appears in
    # every "pick a source file" dropdown with nothing selectable. An SAP export's
    # first sheet is exactly that (`com.sap.ip.bi.xls.hiddensheet`).
    # ⚠️ NOT `with openpyxl.load_workbook(...) as probe:` — that raises
    # `TypeError: 'Workbook' object does not support the context manager protocol`
    # (openpyxl 3.1.5), and this `except` swallowed it into `readable = None`,
    # which silently turns the skip below OFF. It looked fixed because an SAP
    # hidden sheet is empty, and an empty sheet is caught by the `ValueError`
    # guard further down — so a hidden sheet WITH DATA was still imported as a
    # dataset, and the "skip hidden" logic had never run once.
    readable = None
    probe = None
    try:
        probe = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        readable = set(_ordered_sheet_titles(probe))
    except Exception:
        readable = None
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass

    skipped: list[str] = []
    for sheet_name in xl.sheet_names:
        # A Smart Import job is normally mapped to one or a few sheets.  Do
        # not parse every workbook sheet just to import those selected ones.
        if selected_sheets is not None and sheet_name not in selected_sheets:
            continue
        # A sheet the reader cannot SEE is not a dataset they asked for.
        if readable is not None and sheet_name not in readable:
            skipped.append(sheet_name)
            continue
        try:
            df = xl.parse(sheet_name, header=0, dtype=object)
        except Exception:
            continue

        safe_sheet = re.sub(r"[^\w\u4e00-\u9fff]", "_", sheet_name).strip("_").lower()
        if len(xl.sheet_names) == 1:
            suggested = base_name[:50]
        else:
            suggested = f"{base_name}_{safe_sheet}"[:50]
        try:
            results.append(_parse_dataframe(df, sheet_name, safe_sheet, suggested, len(xl.sheet_names)))
        except ValueError:
            # One unreadable sheet must not cost the reader every OTHER sheet in
            # the workbook. Before this it propagated and killed the whole parse.
            skipped.append(sheet_name)

    if not results and skipped:
        raise ValueError(
            f"\u8fd9\u4e2a\u6587\u4ef6\u7684 {len(skipped)} \u4e2a\u5de5\u4f5c\u8868\u91cc\u6ca1\u6709\u53ef\u5bfc\u5165\u7684\u6570\u636e\uff1a"
            f"{'\u3001'.join(skipped)}\u3002\u5982\u679c\u6570\u636e\u5728\u8bfb\u4e0d\u4e86\u7684\u5de5\u4f5c\u8868\u4e0a\uff0c\u8bf7\u5bfc\u51fa\u4e3a CSV\u3002 / "
            f"No importable sheet in this file. If the data is on a sheet this app "
            f"cannot read, export it as CSV."
        )

    return results


def _ordered_sheet_titles(wb) -> list[str]:
    """Worksheet titles a person could actually mean: VISIBLE ones, in workbook order.

    ⚠️ Why this exists, and why it is not a cosmetic filter. An SAP Analysis Office
    export carries a hidden technical sheet — its `sheetId` is literally
    `com.sap.ip.bi.xls.hiddensheet` — and it is usually the FIRST sheet. Both readers
    used to take `sheet_names[0]` / iterate everything, so they landed on that
    empty technical sheet and raised `Sheet 'Sheet1' 没有可导入的数据` while the real
    data sat in the very next sheet. Reported verbatim from a 2026-10-06 upload of
    `06_2025 Basic Audit Schedule - 审核排期表.xlsx`.

    It had TWO faces, and only one was visible:
      · the preview pane showed that error;
      · `parse_excel` raised too, and `/files/stage` swallows parser errors
        (`except Exception: pass`) so the upload still "succeeded" — with EMPTY
        `sheets_meta`. The file then appeared in every dropdown and could not be
        imported from any of them.

    Hidden sheets are kept as a fallback, not dropped: a workbook saved with
    everything hidden still has to open and say something useful.
    """
    visible, hidden = [], []
    for ws in getattr(wb, "worksheets", []):
        # `sheetnames` also contains chartsheets, which have no cells; using
        # `worksheets` keeps them out of the list we would try to read.
        (visible if (getattr(ws, "sheet_state", "visible") or "visible") == "visible"
         else hidden).append(ws.title)
    return visible or hidden


def preview_excel(
    file_bytes: bytes,
    filename: str,
    sheet: str | None = None,
    limit: int = 200,
    max_cols: int = 256,
) -> dict:
    """
    Fast preview of a single sheet for the file-browser preview window.

    Unlike parse_excel (which parses every sheet and infers a PostgreSQL type
    per column), this reads at most `limit` data rows and `max_cols` columns
    directly with openpyxl read-only mode.  Workbooks whose sheets declare
    huge mostly-empty dimensions (e.g. a 16k-column One Sheet) would take
    minutes through the full pipeline; the preview path stays in seconds.
    Returns {"sheet_name", "columns", "rows", "total_rows"}; values keep
    their native Excel types (no dtype coercion) — preview only.
    """
    if filename.lower().endswith(".csv"):
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), nrows=limit)
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(file_bytes), nrows=limit, encoding="utf-8-sig")
        except Exception as e:
            raise ValueError(f"无法解析 CSV 文件: {e}")
        df = df.dropna(how="all")
        cols = [_safe_col_name(str(c)) for c in df.columns]
        df.columns = cols
        return {
            "sheet_name": "Sheet1",
            "columns": cols,
            "rows": dataframe_to_records(df, [(c, "TEXT") for c in cols]),
            "total_rows": len(df),
        }


    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"无法解析 Excel 文件: {e}")

    def _read(name: str):
        ws = wb[name]
        rows: list[list] = []
        for row in ws.iter_rows(min_row=1, max_row=limit + 1, max_col=max_cols, values_only=True):
            rows.append(list(row))
        return rows

    def _split(rows: list[list]):
        """`(header, data)`, or None when there is nothing to read at all."""
        if not rows or all(not str(c).strip() for c in rows[0] if c is not None):
            # First row fully empty (common banner row): treat row 2 as header.
            if len(rows) > 1:
                return rows[1], rows[2:]
            return None
        return rows[0], rows[1:]

    try:
        candidates = _ordered_sheet_titles(wb)
        # ⚠️ Fall back to `sheetnames` only if `worksheets` came back empty, so a
        # workbook made entirely of chartsheets still names something.
        order = [sheet] if (sheet and sheet in wb.sheetnames) else (candidates or list(wb.sheetnames))

        # ⚠️ Walk the candidates rather than committing to the first. A workbook
        # whose first VISIBLE sheet is an empty "说明"/cover sheet is common, and
        # the reader clicked the FILE — not that sheet. The response carries
        # `sheet_name` and the UI has a sheet switcher, so choosing is not hiding.
        header_row = data_rows = None
        sheet_name = order[0] if order else ""
        for name in order:
            try:
                raw = _read(name)
            except Exception:
                continue
            split = _split(raw)
            if split is not None:
                sheet_name, raw = name, raw
                header_row, data_rows = split
                break
        total_rows = max((wb[sheet_name].max_row or 1) - 1, 0) if sheet_name else 0
    finally:
        wb.close()

    if header_row is None:
        # Name the WORKBOOK, not one arbitrary sheet. "Sheet 'Sheet1' 没有可导入的数据"
        # sent the reader looking inside the wrong tab, and a hidden SAP sheet is
        # not even a tab they can see.
        found = "、".join(candidates) if candidates else "（无）"
        raise ValueError(
            f"这个文件里没有可导入的数据 / No importable data. "
            f"它有 {len(candidates)} 个工作表：{found}。"
            f"If the data is on a sheet this app cannot read, export it as CSV."
        )

    seen: dict[str, int] = {}
    header = []
    for i, c in enumerate(header_row[:max_cols]):
        safe = _safe_col_name(str(c)) if c is not None and str(c).strip() else f"col_{i+1}"
        if safe in seen:
            seen[safe] += 1
            safe = f"{safe}_{seen[safe]}"
        else:
            seen[safe] = 0
        header.append(safe)

    df = pd.DataFrame(data_rows, columns=header)
    df = df.dropna(how="all")
    if not df.empty:
        df = df.dropna(axis=1, how="all")
    rows = dataframe_to_records(df.head(limit), [(c, "TEXT") for c in df.columns])
    return {
        "sheet_name": sheet_name,
        "columns": list(df.columns),
        "rows": rows,
        "total_rows": max(total_rows, len(df)),
    }


def _parse_dataframe(
    df: pd.DataFrame,
    sheet_name: str,
    safe_sheet_name: str,
    suggested_table_name: str,
    sheet_count: int,
) -> dict:
    # 删掉全空行/列；header-only CSV 需要保留列，用于创建空表。
    df = df.dropna(how="all")
    if not df.empty:
        df = df.dropna(axis=1, how="all")
    if len(df.columns) == 0:
        raise ValueError(f"Sheet '{sheet_name}' 没有可导入的数据")

    # 规范化列名（处理重复列名）
    seen: dict[str, int] = {}
    new_cols = []
    for col in df.columns:
        safe = _safe_col_name(str(col))
        if safe in seen:
            seen[safe] += 1
            safe = f"{safe}_{seen[safe]}"
        else:
            seen[safe] = 0
        new_cols.append(safe)
    df.columns = new_cols

    df, columns, type_hints = infer_dataframe(df)

    fingerprint = _make_fingerprint(columns)

    return {
        "sheet_name": sheet_name,
        "columns": columns,
        "type_hints": type_hints,
        "fingerprint": fingerprint,
        "df": df,
        "row_count": len(df),
        "suggested_table_name": suggested_table_name,
    }


def dataframe_to_records(df: pd.DataFrame, columns: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Convert a DataFrame to PostgreSQL-ready records without ``iterrows``.

    ``iterrows`` creates a Series for every input row and was a material part
    of Smart Import latency on large Excel sheets.  Converting nulls once and
    iterating native tuples keeps the same output contract for all callers.
    """
    column_names = [col for col, _ in columns]
    if not column_names or df.empty:
        return []

    # The transform may have reordered columns; retain the schema order and
    # represent pandas NaN/NaT as database NULL values before iterating.
    prepared = df.reindex(columns=column_names).astype(object)
    prepared = prepared.where(pd.notna(prepared), None)

    records: list[dict[str, Any]] = []
    for row in prepared.itertuples(index=False, name=None):
        record: dict[str, Any] = {}
        for (col, pg_type), value in zip(columns, row):
            if hasattr(value, "item") and not isinstance(value, (str, bytes)):
                value = value.item()
            if pg_type == "TIMESTAMP" and hasattr(value, "isoformat"):
                value = value.isoformat()
            record[col] = value
        records.append(record)
    return records
