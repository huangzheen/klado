"""Conservative, shared inference for upload and cleaned import frames.

Every nonempty value must fit. Identifiers and leading zeros stay text; ambiguous
or mixed columns stay text rather than losing values through errors='coerce'.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import pandas as pd

_IDENTIFIER = re.compile(r'(?:^|_)(?:id|code|sku|phone|zip|postcode|account)(?:$|_)|编号|编码|邮编|电话|手机号|账号', re.I)
_NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$')
_GROUPED = re.compile(r'^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$')
_DATE = re.compile(r'^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:日)?(?:[ T].*)?$')
_BOOL = {'true': True, 'false': False, 'yes': True, 'no': False, '是': True, '否': False, '真': True, '假': False}


def infer_column(series: pd.Series, name: str = '', *, protect_identifiers: bool = True) -> tuple[str, pd.Series, str]:
    original = series.copy()
    present = series.notna() & series.astype(str).str.strip().ne('')
    values = series[present]
    if values.empty:
        return 'TEXT', original, '空列保留文本 / Empty columns stay text'
    if protect_identifiers and _IDENTIFIER.search(name):
        return 'TEXT', original.map(lambda v: str(v) if pd.notna(v) else None), '编号保留原值 / Identifiers stay text'
    if pd.api.types.is_bool_dtype(series):
        return 'BOOLEAN', original, '布尔值 / Boolean values'
    if pd.api.types.is_datetime64_any_dtype(series):
        return 'TIMESTAMP', original, '日期时间 / Date and time values'
    texts = values.astype(str).str.strip()
    if protect_identifiers and texts.str.match(r'^[+-]?0\d+$').any():
        return 'TEXT', original, '保留前导零 / Leading zeros preserved'
    lowered = texts.str.lower()
    if lowered.isin(_BOOL).all():
        converted = pd.Series(None, index=series.index, dtype=object)
        converted.loc[present] = lowered.map(_BOOL)
        return 'BOOLEAN', converted, '明确的布尔标签 / Explicit boolean labels'
    if texts.str.match(_DATE).all():
        normalized = texts.str.replace('年', '-', regex=False).str.replace('月', '-', regex=False).str.replace('日', '', regex=False)
        try:
            dates = pd.to_datetime(normalized, format='mixed', errors='raise')
            converted = pd.Series(None, index=series.index, dtype=object)
            converted.loc[present] = dates
            return 'TIMESTAMP', converted, '明确的年月日 / Unambiguous calendar dates'
        except (ValueError, TypeError, OverflowError):
            pass
    # Symbols are accepted only when ALL values use the same convention.
    percent = texts.str.endswith('%')
    currency = texts.str.match(r'^[¥￥$€£]')
    formatted = percent.all() or currency.all() or texts.str.contains(',', regex=False).any()
    if percent.any() and not percent.all() or currency.any() and not currency.all():
        return 'TEXT', original, '混合格式保留文本 / Mixed formats stay text'
    normalized = texts.str.rstrip('%') if percent.all() else texts
    if currency.all():
        if texts.str[0].nunique() != 1:
            return 'TEXT', original, '混合币种保留文本 / Mixed currencies stay text'
        normalized = normalized.str.slice(1)
    if normalized.map(lambda v: bool(_NUMBER.fullmatch(v) or _GROUPED.fullmatch(v))).all():
        try:
            numbers = normalized.str.replace(',', '', regex=False).map(Decimal)
            if not numbers.map(lambda v: v.is_finite()).all():
                raise InvalidOperation
            if percent.all():
                numbers = numbers.map(lambda v: v / 100)
            integer = numbers.map(lambda v: v == v.to_integral_value() and -(2**63) <= v < 2**63).all()
            kind = 'BIGINT' if integer and not formatted else 'NUMERIC'
            converted = pd.Series(None, index=series.index, dtype=object)
            converted.loc[present] = numbers.map(int) if kind == 'BIGINT' else numbers
            reason = ('百分比转换为比例 / Percentages converted to ratios' if percent.all() else
                      '数值格式已识别 / Numeric values recognized')
            return kind, converted, reason
        except (InvalidOperation, ValueError, OverflowError):
            pass
    return 'TEXT', original, '混合或不明确的值保留文本 / Mixed or ambiguous values stay text'


def infer_dataframe(df: pd.DataFrame, forced_types: dict[str, str] | None = None):
    result = df.copy()
    columns, hints = [], []
    for column in result.columns:
        name = str(column)
        forced = (forced_types or {}).get(name)
        if forced == 'text':
            kind, values, reason = 'TEXT', result[column], '按你的选择保留文本 / Text kept by your choice'
        elif forced in ('number', 'date', 'boolean'):
            kind, values, reason = infer_column(result[column], '', protect_identifiers=False)
            if forced == 'date': kind = 'TIMESTAMP'
            elif forced == 'boolean': kind = 'BOOLEAN'
            elif kind == 'TEXT' and result[column].isna().all(): kind = 'NUMERIC'
            reason = '按你的选择转换 / Converted by your choice'
        else:
            kind, values, reason = infer_column(result[column], name)
        result[column] = values
        columns.append((name, kind))
        hints.append({'column': name, 'type': kind, 'reason': reason})
    return result, columns, hints
