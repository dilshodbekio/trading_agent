"""Run: python -m unittest discover -s tests -v (no API keys required)."""
import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch

import tools


class IsolatedToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fixture.db'
        with closing(sqlite3.connect(self.path)) as con, con:
            con.executescript('''
                CREATE TABLE deals (deal_id INTEGER PRIMARY KEY, deal_date TEXT,
                    amount_hundredths INTEGER, starting_price_hundredths INTEGER,
                    contract_id INTEGER, status TEXT, seller_tax_id TEXT, buyer_tax_id TEXT);
                CREATE TABLE contracts (contract_id INTEGER PRIMARY KEY, currency TEXT);
                CREATE TABLE usd_rates (rate_date TEXT PRIMARY KEY, rate_hundredths INTEGER);
                CREATE TABLE participants (tax_id TEXT PRIMARY KEY, name TEXT);
            ''')
            con.executemany('INSERT INTO contracts VALUES (?,?)', [(1, tools.UZS), (2, tools.USD)])
            con.executemany('INSERT INTO usd_rates VALUES (?,?)', [('2024-01-01', 1200050), ('2024-01-02', 1250075)])
            con.executemany('INSERT INTO participants VALUES (?,?)', [('001', 'Seller'), ('002', 'Buyer')])
            con.executemany('INSERT INTO deals VALUES (?,?,?,?,?,?,?,?)', [
                (1, '2024-01-01', 10001, 9000, 1, 'completed', '001', '002'),
                (2, '2024-01-01', 101, 100, 2, 'completed', '001', '002'),
                (3, '2024-01-02', 201, 200, 2, 'cancelled', '001', '002'),
            ])
        self.patch = patch.object(tools, 'DB_PATH', self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def change(self, sql):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(sql)

    def test_count_and_inclusive_dates(self):
        self.assertEqual(tools.count_deals.invoke({})['count'], 3)
        self.assertEqual(tools.count_deals.invoke({'start_date':'2024-01-01','end_date':'2024-01-01'})['count'], 2)
        self.assertEqual(tools.count_deals.invoke({'status':'completed'})['count'], 2)

    def test_exact_conversion_and_round_once(self):
        # 100.01 UZS + 1.01 USD * 12000.50 + 2.01 USD * 12500.75
        result = tools.get_trade_value.invoke({})
        self.assertEqual(result['total_uzs'], '37347.02')
        self.assertEqual(result['trade_count'], 3)
        self.assertEqual(result['usd_trades_converted'], 2)
        self.assertEqual(tools.get_trade_value.invoke({'status':'completed'})['total_uzs'], '12220.52')

    def test_lookup_and_missing_id(self):
        result = tools.get_deal.invoke({'deal_id':1})['deal']
        self.assertEqual(result['amount'], '100.01')
        self.assertEqual(result['starting_price'], '90.00')
        self.assertEqual(result['seller_name'], 'Seller')
        self.assertEqual(result['currency'], tools.UZS)
        self.assertEqual(tools.get_deal.invoke({'deal_id':999}), {'found':False, 'deal_id':999})

    def test_empty_range(self):
        args = {'start_date':'2030-01-01'}
        self.assertEqual(tools.count_deals.invoke(args)['count'], 0)
        self.assertEqual(tools.get_trade_value.invoke(args)['total_uzs'], '0.00')

    def test_invalid_filters(self):
        for tool in (tools.count_deals, tools.get_trade_value):
            for args in ({'start_date':'bad'}, {'start_date':'2024-02-30'},
                         {'start_date':'20240101'}, {'start_date':'2025-01-01','end_date':'2024-01-01'},
                         {'status':"completed' OR 1=1 --"}):
                with self.subTest(tool=tool.name, args=args):
                    self.assertIsInstance(tool.invoke(args), str)

    def test_missing_rate_fails_instead_of_partial_total(self):
        self.change("DELETE FROM usd_rates WHERE rate_date='2024-01-02'")
        self.assertIsInstance(tools.get_trade_value.invoke({}), str)

    def test_missing_contract_fails(self):
        self.change('DELETE FROM contracts WHERE contract_id=2')
        self.assertIsInstance(tools.get_trade_value.invoke({}), str)

    def test_unsupported_currency_fails(self):
        self.change("UPDATE contracts SET currency='EUR' WHERE contract_id=2")
        self.assertIsInstance(tools.get_trade_value.invoke({}), str)

    def test_missing_amount_fails(self):
        self.change('UPDATE deals SET amount_hundredths=NULL WHERE deal_id=1')
        self.assertIsInstance(tools.get_trade_value.invoke({}), str)

    def test_database_is_read_only(self):
        with closing(tools._connect()) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM deals')


class RealDatasetTests(unittest.TestCase):
    def test_known_counts(self):
        self.assertEqual(tools.count_deals.invoke({})['count'], 500000)
        for year, expected in [(2022,84299),(2023,93985),(2024,106647),(2025,115763),(2026,99306)]:
            with self.subTest(year=year):
                self.assertEqual(tools.count_deals.invoke({'start_date':f'{year}-01-01','end_date':f'{year}-12-31'})['count'], expected)

    def test_first_deal_matches_source_csv(self):
        path = tools.DB_PATH.parent / 'uzex_spot_data' / 'deals_2022.csv'
        with path.open(encoding='utf-8-sig', newline='') as f:
            row = next(csv.reader(f)); row = next(csv.reader(f))
        result = tools.get_deal.invoke({'deal_id':int(row[0])})['deal']
        self.assertEqual(result['amount'], format(Decimal(row[5].replace(' ','')), '.2f'))
        self.assertEqual(result['product_name'], row[8])
        self.assertEqual(result['deal_date'], datetime.strptime(row[1], '%d.%m.%Y').date().isoformat())

    def test_full_total_against_independent_csv_calculation(self):
        root = tools.DB_PATH.parent / 'uzex_spot_data'
        def read(name):
            with (root/name).open(encoding='utf-8-sig', newline='') as f:
                rows = csv.reader(f); next(rows)
                yield from rows
        currencies = {r[0]:r[13] for r in read('contracts.csv')}
        rates = {r[0]:Decimal(r[1]) for r in read('usd_rates.csv')}
        total, count, usd_count = Decimal(0), 0, 0
        for path in sorted(root.glob('deals_*.csv')):
            for row in read(path.name):
                amount = Decimal(row[5].replace(' ',''))
                if currencies[row[7]] == tools.USD:
                    amount *= rates[row[1]]
                    usd_count += 1
                else:
                    self.assertEqual(currencies[row[7]], tools.UZS)
                total += amount
                count += 1
        result = tools.get_trade_value.invoke({})
        self.assertEqual(result['total_uzs'], format(total.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f'))
        self.assertEqual(result['trade_count'], count)
        self.assertEqual(result['usd_trades_converted'], usd_count)
        print('\nIndependent CSV total (UZS):', result['total_uzs'])


if __name__ == '__main__':
    unittest.main()

