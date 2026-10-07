import unittest
from io import BytesIO
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

from processors.cleaner import apply_cleaning_rules
from processors.excel import dataframe_to_records, parse_excel
from processors.transforms import apply_transform
from routers.data_center import QueryRequest, _display_filename, run_query
from services import data_center_db as db


class _FakeCursor:
    def __init__(self):
        self.commands = []
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, command, _params=None):
        text = str(command)
        self.commands.append(text)
        if "information_schema.columns" in text:
            self._rows = [("known_column",)]

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def cursor(self):
        return self.cursor_instance


class _FakeConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class SmartImportTests(unittest.TestCase):
    def test_parses_only_requested_sheet_and_converts_nulls(self):
        content = BytesIO()
        with pd.ExcelWriter(content, engine="openpyxl") as writer:
            pd.DataFrame({"value": [1, None], "label": ["first", "second"]}).to_excel(
                writer, sheet_name="Detail", index=False
            )
            pd.DataFrame({"ignored": [2]}).to_excel(writer, sheet_name="Reference", index=False)

        sheets = parse_excel(content.getvalue(), "test.xlsx", sheet_names=["Detail"])
        self.assertEqual([sheet["sheet_name"] for sheet in sheets], ["Detail"])
        records = dataframe_to_records(sheets[0]["df"], sheets[0]["columns"])
        self.assertEqual(
            records,
            [
                {"value": 1.0, "label": "first"},
                {"value": None, "label": "second"},
            ],
        )

    def test_rejects_unknown_columns_before_replace_truncate(self):
        cursor = _FakeCursor()
        connection = _FakeConnection(cursor)
        with patch.object(db, "get_pg_conn", return_value=_FakeConnectionContext(connection)):
            with self.assertRaisesRegex(ValueError, "不会自动扩展表结构"):
                db.insert_pg_data(
                    "target_dataset",
                    [{"known_column": "ok", "unexpected_column": "reject"}],
                    mode="replace",
                )

        commands = "\n".join(cursor.commands).upper()
        self.assertNotIn("TRUNCATE", commands)
        self.assertNotIn("ALTER TABLE", commands)


    def test_uses_deployed_smart_import_job_table(self):
        self.assertEqual(db.IMPORT_JOB_TABLE, "public.system_import_jobs")

    def test_data_center_query_rejects_multiple_statements(self):
        # `_viewer` is patched because this case is about the statement-count guard, not
        # about who is asking: the raw query endpoint is identity-scoped since Data Center
        # became per-user, and a real caller needs a Request to resolve.
        with patch("routers.data_center._ensure_init"), \
             patch("routers.data_center._viewer", return_value=("tester@example.com", False)):
            with self.assertRaises(HTTPException) as raised:
                run_query(QueryRequest(sql="SELECT 1; DELETE FROM public.bsr_dataset"), None)
        self.assertEqual(raised.exception.status_code, 400)


class DerivedColumnTests(unittest.TestCase):
    """derived_columns rule: stamp a constant column from the source filename."""

    RULES = {
        "filters": [{"column": "品类", "op": "eq", "value": "CL"}],
        "derived_columns": [
            {"column": "数据月份", "from": "filename", "regex": r"(\d{6})", "default": ""}
        ],
    }

    def test_derived_column_stamps_month_from_filename(self):
        df = pd.DataFrame({"品类": ["CL", "CP"], "qty": [1, 2]})
        rules = {"derived_columns": [
            {"column": "数据月份", "from": "filename", "regex": r"(\d{6})", "default": ""}
        ]}
        out = apply_cleaning_rules(df, rules, source_filename="客户库存_202608.xlsx")
        self.assertEqual(list(out.columns), ["品类", "qty", "数据月份"])
        self.assertEqual(out["数据月份"].tolist(), ["202608", "202608"])

    def test_filter_runs_before_derived_column(self):
        df = pd.DataFrame({"品类": ["CL", "CP", None], "qty": [1, 2, 3]})
        out = apply_cleaning_rules(df, self.RULES, source_filename="客户库存_202608.xlsx")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["品类"], "CL")
        self.assertEqual(out.iloc[0]["数据月份"], "202608")

    def test_derived_column_falls_back_to_default_without_match(self):
        df = pd.DataFrame({"a": [1]})
        rules = {"derived_columns": [
            {"column": "数据月份", "regex": r"(\d{6})", "default": ""}
        ]}
        self.assertEqual(apply_cleaning_rules(df, rules)["数据月份"].tolist(), [""])
        self.assertEqual(
            apply_cleaning_rules(df, rules, source_filename="report.xlsx")["数据月份"].tolist(),
            [""],
        )

    def test_empty_regex_never_matches(self):
        df = pd.DataFrame({"a": [1]})
        rules = {"derived_columns": [
            {"column": "数据月份", "from": "filename", "regex": "", "default": "-"}
        ]}
        out = apply_cleaning_rules(df, rules, source_filename="客户库存_202608.xlsx")
        self.assertEqual(out["数据月份"].tolist(), ["-"])

    def test_display_filename_strips_oss_upload_prefix(self):
        self.assertEqual(
            _display_filename("2026-09/20260921_123456_客户库存_202608.xlsx"),
            "客户库存_202608.xlsx",
        )
        self.assertEqual(
            _display_filename("Customer Inventory/客户库存_202608.xlsx"),
            "客户库存_202608.xlsx",
        )

    def test_import_paths_derive_month_after_stripping_upload_prefix(self):
        """Regression (production incident 2026-09-24): a file that reaches the
        library via register-oss keeps the raw OSS object basename
        (`20260923_092112_客户库存_202608.xlsx`), so every import path must run
        the rule against the normalised name — otherwise `(\\d{6})` picks the
        upload date and the rows land in the wrong `数据月份` bucket (202609),
        where Monthly Review can never select them."""
        df = pd.DataFrame({"品类": ["CL"]})
        raw = "20260923_092112_客户库存_202608.xlsx"
        self.assertEqual(_display_filename(raw), "客户库存_202608.xlsx")
        out = apply_cleaning_rules(df, self.RULES, source_filename=_display_filename(raw))
        self.assertEqual(out.iloc[0]["数据月份"], "202608")


if __name__ == "__main__":
    unittest.main()
