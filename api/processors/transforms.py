"""
processors/transforms.py
表名匹配的导入前数据清洗策略注册表。

使用方式：
  from processors.transforms import apply_transform
  df, columns = apply_transform(table_name, df, columns)

新增策略：在 TRANSFORM_REGISTRY 里加 { "前缀或全名": transform_fn }，
  transform_fn(df, columns) -> (df, columns)
"""
from __future__ import annotations

import datetime
import re
import pandas as pd
from typing import Callable

# ──────────────────────────────────────────────────────────────────
# 通用工具
# ──────────────────────────────────────────────────────────────────

def _clean_excel_date(v) -> str | None:
    """Normalise an Excel date cell to YYYY-MM-DD string, or None if blank/invalid."""
    if pd.isna(v) if not isinstance(v, (list, dict)) else False:
        return None
    if isinstance(v, datetime.datetime):
        if v.year < 1900 or v == datetime.datetime(1899, 12, 30):
            return None
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime.time):
        return None  # bare time = Excel blank date cell
    s = str(v).strip()
    if not s or s in ("NaT", "None", "nan"):
        return None
    if len(s) > 10 and s[10] in ("T", " "):
        s = s[:10]
    if len(s) <= 8 and ":" in s and "-" not in s:
        return None  # pure time string like "00:00:00"
    return s


def _safe_col(s: str) -> str:
    """列名 → 合法 SQL 列名（小写 + 下划线）"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(s)).strip("_").lower()


def _infer_pg_type(series: pd.Series) -> str:
    """简单类型推断，与 excel.py 保持一致"""
    clean = series.dropna()
    if clean.empty:
        return "TEXT"
    # 先试 numeric
    try:
        pd.to_numeric(clean)
        if clean.astype(str).str.contains(r"\.").any():
            return "FLOAT"
        return "BIGINT"
    except (ValueError, TypeError):
        pass
    # 再试 datetime
    try:
        pd.to_datetime(clean, format="mixed", dayfirst=False)
        return "TIMESTAMP"
    except Exception:
        pass
    return "TEXT"


def _rebuild_columns(df: pd.DataFrame) -> list[tuple[str, str]]:
    """根据当前 DataFrame 重新推断 (col_name, pg_type) 列表"""
    seen: dict[str, int] = {}
    result = []
    for col in df.columns:
        safe = _safe_col(str(col))
        if safe in seen:
            seen[safe] += 1
            safe = f"{safe}_{seen[safe]}"
        else:
            seen[safe] = 0
        pg_type = _infer_pg_type(df[col])
        result.append((safe, pg_type))
    return result


# ──────────────────────────────────────────────────────────────────
# Products 策略
# 适用表名: products
#
# '0.Master' 工作表第一行为空白行，第二行（Excel row 1）才是列名。
# pandas 用 header=0 读取后，真正的列名成为第一条数据行，列名变成
# unnamed_1..unnamed_18。本 transform 检测并修正，同时把 Excel 列名
# 映射到 products 表的 DB 列名，去掉无对应列（如 Stutas）。
# ──────────────────────────────────────────────────────────────────

# Excel 列名（去空格后小写）→ products DB 列名
_PRODUCTS_COL_MAP: dict[str, str] = {
    "vib":           "vib",
    "brand":         "brand",
    "bf":            "bf",
    "platform":      "platform",
    "value class":   "value_class",
    "is_vavo":       "is_vavo",
    "is vavo":       "is_vavo",
    "vavo":          "is_vavo",
    "launch_year_qty": "launch_year_qty",
    "launch year qty": "launch_year_qty",
    "first_full_year_quantity": "first_full_year_quantity",
    "first full year quantity": "first_full_year_quantity",
    "eec class":     "eec_class",
    "color":         "color",
    "door material": "door_material",
    "price class":   "price_class",
    "irop":          "irop",
    "rrp":           "rrp",
    "sos":           "sos",
    "eos":           "eos",
    "lp":            "lp",
    "channel":       "channel",
    "photo_link":    "photo_link",
}


def _transform_products(df: pd.DataFrame, _columns: list) -> tuple[pd.DataFrame, list]:
    if df.empty:
        return df, _columns

    # 检测第一数据行是否为真正的列名（包含 'vib'）
    first_row_vals = [str(v).strip().lower() for v in df.iloc[0].values if pd.notna(v)]
    if "vib" not in first_row_vals:
        return df, _columns  # 已经是正确格式

    # 把第一数据行当作列名，截取后续行作为数据
    new_cols = [str(v).strip() if pd.notna(v) else "" for v in df.iloc[0].values]
    df = df.iloc[1:].copy()
    df.columns = new_cols
    df.reset_index(drop=True, inplace=True)

    # 去掉空列名列
    df = df[[c for c in df.columns if c.strip() != ""]]

    # 映射列名，去掉 DB 中不存在的列（如 Stutas）
    rename_map: dict[str, str] = {}
    drop_cols: list[str] = []
    for col in df.columns:
        key = col.strip().lower()
        if key in _PRODUCTS_COL_MAP:
            rename_map[col] = _PRODUCTS_COL_MAP[key]
        else:
            drop_cols.append(col)
    df = df.drop(columns=drop_cols, errors="ignore")
    df = df.rename(columns=rename_map)

    # 过滤 vib 为空的行（小计行、合计行等）
    if "vib" in df.columns:
        df = df[df["vib"].notna() & df["vib"].astype(str).str.strip().ne("")]
    df.reset_index(drop=True, inplace=True)

    # 清洗日期列：datetime → YYYY-MM-DD 字符串；仅有时间（无日期）的值 → None
    for date_col in ("sos", "eos"):
        if date_col not in df.columns:
            continue
        df[date_col] = df[date_col].apply(_clean_excel_date)

    return df, _rebuild_columns(df)


# ──────────────────────────────────────────────────────────────────
# 注册表：{ 前缀/全名: transform_fn }
# 匹配规则：先尝试完整名称精确匹配，再尝试前缀匹配（key 以 "_" 结尾）
# ──────────────────────────────────────────────────────────────────

TRANSFORM_REGISTRY: dict[str, Callable] = {
    "products": _transform_products,   # 精确匹配：products 表（0.Master sheet）
}


def apply_transform(
    table_name: str,
    df: pd.DataFrame,
    columns: list,
) -> tuple[pd.DataFrame, list]:
    """
    根据 table_name 查找并应用对应的 transform。
    找不到匹配则原样返回。
    """
    # 1. 精确匹配
    if table_name in TRANSFORM_REGISTRY:
        return TRANSFORM_REGISTRY[table_name](df, columns)

    # 2. 前缀匹配（key 以 "_" 结尾视为前缀）
    for key, fn in TRANSFORM_REGISTRY.items():
        if key.endswith("_") and table_name.startswith(key):
            return fn(df, columns)

    return df, columns
