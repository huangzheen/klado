"""Isolation and conversion invariants across owner, recipient and administrator."""
import unittest
from unittest.mock import patch
from decimal import Decimal
import pandas as pd
from fastapi import HTTPException
from processors.column_types import infer_column
from processors.excel import parse_excel, dataframe_to_records
from processors.cleaner import apply_cleaning_rules
from routers import data_center as router
from services import data_center_db as db, dataset_query


class TypesTests(unittest.TestCase):
    def test_identifiers_and_leading_zeros_survive_csv(self):
        sheet = parse_excel(b'customer_id,amount,code\n123,4.50,001\n456,8.75,002\n', 'a.csv')[0]
        self.assertEqual(dict(sheet['columns']), {'customer_id': 'TEXT', 'amount': 'NUMERIC', 'code': 'TEXT'})
        rows = dataframe_to_records(sheet['df'], sheet['columns'])
        self.assertEqual(rows[0]['code'], '001')
        self.assertEqual(rows[0]['amount'], Decimal('4.50'))

    def test_entire_column_is_checked_not_just_first_200(self):
        self.assertEqual(infer_column(pd.Series(['12'] * 250 + ['unknown']))[0], 'TEXT')

    def test_percent_currency_boolean_and_calendar_dates(self):
        cases = [(['20%', '3.5%'], 'NUMERIC', Decimal('.20')),
                 (['$1,200.50', '$5.00'], 'NUMERIC', Decimal('1200.50')),
                 (['是', '否'], 'BOOLEAN', True),
                 (['2026-10-03', '2026-10-04'], 'TIMESTAMP', pd.Timestamp('2026-10-03'))]
        for values, kind, first in cases:
            with self.subTest(values=values):
                actual, converted, _ = infer_column(pd.Series(values))
                self.assertEqual(actual, kind)
                self.assertEqual(converted.iloc[0], first)

    def test_ambiguous_dates_mixed_units_and_overflow_keep_values(self):
        for values in (['01/02/2026', '02/03/2026'], ['10%', '10'], ['$10', '€20'], ['001', '2']):
            kind, converted, _ = infer_column(pd.Series(values))
            self.assertEqual(kind, 'TEXT')
            self.assertEqual(converted.tolist(), values)
        self.assertEqual(infer_column(pd.Series([str(2**64)]))[0], 'NUMERIC')

    def test_explicit_bad_cast_does_not_silently_write_null(self):
        for dtype in ('number', 'date', 'boolean'):
            with self.subTest(dtype=dtype), self.assertRaises(ValueError):
                apply_cleaning_rules(pd.DataFrame({'v': ['bad']}),
                    {'columns': [{'original': 'v', 'dtype': dtype}]})

    def test_explicit_numeric_choice_overrides_identifier_protection(self):
        from processors.column_types import infer_dataframe
        df = apply_cleaning_rules(pd.DataFrame({'customer_id':['001','002']}),
                                 {'columns':[{'original':'customer_id','dtype':'number'}]})
        actual, columns, _ = infer_dataframe(df, {'customer_id':'number'})
        self.assertEqual(columns, [('customer_id','BIGINT')])
        self.assertEqual(actual['customer_id'].tolist(), [1,2])


class OwnershipTests(unittest.TestCase):
    def test_admin_and_regular_viewers_have_identical_filters(self):
        for fn in (db._owner_filter, db._file_filter):
            self.assertEqual(fn('a@test.com', True), fn('a@test.com', False))
            self.assertNotEqual(fn('a@test.com', False), ('TRUE', []))

    def test_readable_recipient_cannot_manage_or_delegate(self):
        with patch.object(router, '_viewer', return_value=('recipient@test.com', False)), \
             patch.object(db, 'get_dataset', return_value={'owner_email': 'owner@test.com'}), \
             patch.object(db, 'get_file', return_value={'owner_email': 'owner@test.com'}):
            for action in (lambda: router._assert_dataset_owner('t', None),
                           lambda: router._assert_file_owner(1, None)):
                with self.assertRaises(HTTPException) as exc:
                    action()
                self.assertEqual(exc.exception.status_code, 404)

    def test_admin_cannot_query_another_accounts_table_or_catalog(self):
        with patch.object(db, 'list_datasets', return_value=[{'table_name': 'mine'}]):
            for sql in ('select * from someone_else', 'select * from 他人数据集', 'select * from "他人数据集"', 'select 自定义函数()', 'select * from U&"secret"', 'select * from (table someone_else) s', 'select * from mine union table someone_else', 'select * from mine /* note */ , someone_else',
                        'select * from mine /* nested /* x */ note */ , someone_else', 'select * from public.pg_secret',
                        'select * from pg_catalog.pg_authid', 'select * from other_schema.mine', 'select public.count(1)',
                        "select query_to_xml('select * from secret',false,false,'')"):
                with self.subTest(sql=sql), self.assertRaises(HTTPException):
                    dataset_query.assert_query_allowed(None, sql, 'admin@test.com', True)
            dataset_query.assert_query_allowed(None, 'select sum(amount) from mine', 'admin@test.com', True)
        with patch.object(db, 'list_datasets', return_value=[{'table_name':'我的数据'}]):
            dataset_query.assert_query_allowed(None, 'select * from 我的数据', 'owner@test.com', False)

    def test_folder_delete_removes_only_callers_objects(self):
        with patch.object(db, '_visible_object_names', return_value={'shared/own.xlsx', 'other/own.xlsx'}), \
             patch.object(db, 'delete_file_by_path') as remove:
            db.delete_folder_by_path('shared', 'owner@test.com')
            remove.assert_called_once_with('shared/own.xlsx')

    def test_move_refuses_unregistered_existing_object(self):
        with patch.object(db, '_get_library_row_by_object', return_value=None), \
             patch.object(db.oss_storage, 'object_exists', return_value=True), \
             patch.object(db.oss_storage, 'copy_object') as copy:
            with self.assertRaises(ValueError):
                db.move_file('one/file.xlsx', 'two')
            copy.assert_not_called()

    def test_failed_storage_delete_keeps_ownership_record(self):
        with patch.object(db.oss_storage, 'remove_object', side_effect=RuntimeError('storage offline')), \
             patch.object(db, 'get_pg_conn') as connect:
            with self.assertRaises(RuntimeError):
                db.delete_file_by_path('owned/file.csv')
            connect.assert_not_called()

    def test_agent_public_dashboard_write_stops_before_cover_or_store(self):
        import asyncio
        from routers import dashboard
        body = dashboard.DashboardIn(title='Public', html='<p>test</p>', visibility='public')
        with patch.object(dashboard.store, 'ensure_schema'), \
             patch.object(dashboard, '_viewer', return_value=('owner@test.com', False)), \
             patch.object(dashboard, '_require_browser', side_effect=HTTPException(403, 'browser required')), \
             patch.object(dashboard.report_cover, 'resolve_cover') as cover, \
             patch.object(dashboard.store, 'save_dashboard') as save:
            with self.assertRaises(HTTPException) as exc:
                asyncio.run(dashboard.save_dashboard('public-test', body, None))
            self.assertEqual(exc.exception.status_code, 403)
            cover.assert_not_called(); save.assert_not_called()
