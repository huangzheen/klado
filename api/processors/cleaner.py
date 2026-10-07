"""
Data cleaning engine — applies user-defined rules to a pandas DataFrame.

Rules schema:
{
  "skip_rows": 0,           # skip N rows from top before treating as data
  "use_row_as_header": -1,  # -1 = no change; N = use row N as new header then skip above
  "remove_empty_rows": true,
  "deduplicate": false,
  "columns": [
    {
      "original": "原始列名",
      "include":  true,
      "rename":   "",        # empty = keep original
      "dtype":    "text|number|date|boolean",
      "trim":     true
    }
  ],
  "filters": [
    { "column": "col", "op": "eq|ne|contains|not_contains|gt|lt|gte|lte|is_empty|is_not_empty|starts_with|ends_with", "value": "val" }
  ],
  "replacements": [
    { "column": "col", "all_columns": false, "find": "old", "replace": "new", "regex": false }
  ],
  "derived_columns": [
    # constant column derived from the import context, e.g. stamp the source
    # month captured from the file name: 客户库存_202608.xlsx -> "202608"
    { "column": "数据月份", "from": "filename", "regex": "(\\d{6})", "default": "" }
  ]
}
"""

from __future__ import annotations

import re
import pandas as pd


def apply_cleaning_rules(
    df: pd.DataFrame,
    rules: dict,
    source_filename: str | None = None,
) -> pd.DataFrame:
    df = df.copy()

    # ── 1. Skip top rows ──────────────────────────────────────────────────────
    skip = int(rules.get("skip_rows", 0))
    if skip > 0:
        use_as_header = int(rules.get("use_row_as_header", -1))
        if use_as_header >= 0 and use_as_header < len(df):
            # Re-header: treat row[use_as_header] as new column names
            new_header = [str(v) if not pd.isna(v) else f"col_{i}"
                          for i, v in enumerate(df.iloc[use_as_header])]
            df = df.iloc[use_as_header + 1:].copy()
            df.columns = new_header
        else:
            df = df.iloc[skip:].copy()
        df = df.reset_index(drop=True)

    # ── 2. Column management ──────────────────────────────────────────────────
    col_rules = rules.get("columns", [])
    if col_rules:
        included = [r for r in col_rules
                    if r.get("include", True) and r.get("original") in df.columns]
        if included:
            df = df[[r["original"] for r in included]].copy()

            # Build rename map
            rename_map: dict[str, str] = {}
            for r in included:
                new_name = (r.get("rename") or "").strip()
                if new_name and new_name != r["original"]:
                    rename_map[r["original"]] = new_name
            if rename_map:
                df = df.rename(columns=rename_map)

            # Trim & dtype per column
            for r in included:
                col = rename_map.get(r["original"], r["original"])
                if col not in df.columns:
                    continue
                if r.get("trim", False):
                    df[col] = df[col].apply(
                        lambda x: x.strip() if isinstance(x, str) else x
                    )
                _cast_column(df, col, r.get("dtype", "text"))
                if r.get("fill_na_zero") and col in df.columns:
                    df[col] = df[col].fillna(0)

    # ── 3. Remove fully-empty rows ────────────────────────────────────────────
    if rules.get("remove_empty_rows", False):
        df = df.dropna(how="all")
        df = df[~df.apply(
            lambda row: all(str(v).strip() == "" for v in row), axis=1
        )]
        df = df.reset_index(drop=True)

    # ── 4. Deduplicate ────────────────────────────────────────────────────────
    if rules.get("deduplicate", False):
        df = df.drop_duplicates().reset_index(drop=True)

    # ── 5. Row filters ────────────────────────────────────────────────────────
    for flt in rules.get("filters", []):
        col = flt.get("column")
        if not col or col not in df.columns:
            continue
        op  = flt.get("op", "eq")
        try:
            if op == "in":
                values = [str(v) for v in flt.get("values", [])]
                if values:
                    df = df[df[col].astype(str).isin(values)].reset_index(drop=True)
            else:
                val = flt.get("value", "")
                df = _apply_filter(df, col, op, val)
        except Exception:
            pass

    # ── 6. Replace values ─────────────────────────────────────────────────────
    for rep in rules.get("replacements", []):
        find    = str(rep.get("find", ""))
        replace = str(rep.get("replace", ""))
        use_re  = bool(rep.get("regex", False))
        cols    = list(df.columns) if rep.get("all_columns") else [rep.get("column")]
        for col in cols:
            if col and col in df.columns:
                try:
                    df[col] = df[col].astype(str).str.replace(
                        find, replace, regex=use_re
                    )
                except Exception:
                    pass

    # ── 7. Derived columns (constant value from the import context) ──────────
    # Currently supports from="filename": first match of `regex` against the
    # source file name. Missing match / missing filename -> `default`.
    for der in rules.get("derived_columns", []):
        col = str(der.get("column") or "").strip()
        if not col:
            continue
        value = str(der.get("default", ""))
        if str(der.get("from", "filename")) == "filename" and source_filename:
            pattern = str(der.get("regex", "")).strip()
            if pattern:
                try:
                    m = re.search(pattern, source_filename)
                except Exception:
                    m = None
                if m:
                    value = m.group(1) if m.groups() else m.group(0)
        df[col] = value

    return df


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cast_column(df: pd.DataFrame, col: str, dtype: str) -> None:
    from processors.column_types import infer_column
    if dtype == "auto":
        return
    if dtype == "number":
        kind, values, _ = infer_column(df[col], protect_identifiers=False)
        if kind not in ("BIGINT", "NUMERIC", "DOUBLE PRECISION"):
            raise ValueError(f"列 {col} 包含不能转换的数值 / column {col} has invalid numbers")
        df[col] = values
    elif dtype == "date":
        df[col] = pd.to_datetime(df[col], format="mixed", errors="raise")
    elif dtype == "boolean":
        truth = {"1": True, "true": True, "yes": True, "y": True, "是": True, "✓": True,
                 "0": False, "false": False, "no": False, "n": False, "否": False}
        def convert(value):
            if pd.isna(value) or str(value).strip() == '':
                return None
            text = str(value).strip().lower()
            if text not in truth:
                raise ValueError(f"列 {col} 包含不能转换的布尔值 / column {col} has invalid booleans")
            return truth[text]
        df[col] = df[col].map(convert)
    else:
        df[col] = df[col].map(lambda value: None if pd.isna(value) else str(value))


def _apply_filter(df: pd.DataFrame, col: str, op: str, val: str) -> pd.DataFrame:
    s = df[col]
    if op == "eq":
        mask = s.astype(str).str.strip() == val.strip()
    elif op == "ne":
        mask = s.astype(str).str.strip() != val.strip()
    elif op == "contains":
        mask = s.astype(str).str.contains(val, na=False, case=False)
    elif op == "not_contains":
        mask = ~s.astype(str).str.contains(val, na=False, case=False)
    elif op == "starts_with":
        mask = s.astype(str).str.startswith(val)
    elif op == "ends_with":
        mask = s.astype(str).str.endswith(val)
    elif op == "gt":
        mask = pd.to_numeric(s, errors="coerce") > float(val)
    elif op == "lt":
        mask = pd.to_numeric(s, errors="coerce") < float(val)
    elif op == "gte":
        mask = pd.to_numeric(s, errors="coerce") >= float(val)
    elif op == "lte":
        mask = pd.to_numeric(s, errors="coerce") <= float(val)
    elif op == "is_empty":
        mask = s.isna() | (s.astype(str).str.strip() == "")
    elif op == "is_not_empty":
        mask = ~(s.isna() | (s.astype(str).str.strip() == ""))
    else:
        return df
    return df[mask].reset_index(drop=True)


def df_to_preview(df: pd.DataFrame, limit: int = 50) -> dict:
    """Convert DataFrame to JSON-serialisable preview dict."""
    preview = df.head(limit).copy()
    # Safely convert every cell — NaN/Inf → None, everything else → str
    def _safe(x):
        try:
            if x is None:
                return None
            if isinstance(x, float) and (x != x or x == float('inf') or x == float('-inf')):
                return None
            if hasattr(pd, 'isna') and pd.isna(x):
                return None
        except Exception:
            pass
        return str(x) if not isinstance(x, (int, bool)) else x

    rows = []
    for _, row in preview.iterrows():
        rows.append([_safe(v) for v in row])

    distinct_values = {}
    for i, col in enumerate(df.columns):
        series = df.iloc[:, i]  # positional access avoids duplicate-col name issue
        distinct_values[col] = sorted(
            (str(v) if not (isinstance(v, float) and v != v) else ''
             for v in series.dropna().unique()),
            key=str
        )

    return {
        "columns":    list(df.columns),
        "rows":       rows,
        "total_rows": len(df),
        "distinct_values": distinct_values,
    }
