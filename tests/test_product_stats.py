import csv
import sqlite3
import tempfile
import unittest
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch
import tools


class ProductStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.db'
        con=sqlite3.connect(self.path)
        con.executescript('''CREATE TABLE contracts(contract_id INTEGER PRIMARY KEY,product_name TEXT,brand TEXT,size TEXT,grade TEXT,product_type TEXT,product_class TEXT,unit TEXT,currency TEXT);
        CREATE TABLE deals(deal_id INTEGER,contract_id INTEGER,deal_date TEXT,status TEXT,quantity INTEGER,lot_size INTEGER,unit TEXT,amount_hundredths INTEGER);
        CREATE TABLE usd_rates(rate_date TEXT,rate_hundredths INTEGER);''')
        con.executemany('INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?)',[(i,'Wheat',None,None,'first',None,None,'kg',tools.UZS) for i in range(1,26)])
        con.executemany('INSERT INTO deals VALUES(?,?,?,?,?,?,?,?)',[(i,i,'2024-01-01','done',2,5,'kg',10000) for i in range(1,26)])
        con.execute('INSERT INTO contracts VALUES(26,?,?,?,?,?,?,?,?)',('Wheat',None,None,'second',None,None,'tonne',tools.USD))
        con.execute("INSERT INTO deals VALUES(26,26,'2024-01-02','cancelled',3,2,'tonne',200)")
        con.execute("INSERT INTO usd_rates VALUES('2024-01-02',1200000)")
        con.commit(); con.close()
        self.p=patch.object(tools,'DB_PATH',self.path); self.p.start()

    def tearDown(self):
        self.p.stop(); self.temp.cleanup()

    def test_all_contracts_and_separate_units(self):
        r=tools.get_product_stats.invoke({'query':'wHeAt'})
        self.assertEqual(r['matching_contract_count'],26)
        self.assertTrue(r['contract_preview_truncated'])
        self.assertEqual(len(r['contract_preview']),20)
        self.assertEqual(r['trade_count'],26)
        self.assertEqual(r['total_value_uzs'],'26500.00')
        groups={g['unit']:g for g in r['by_unit']}
        self.assertEqual(groups['kg']['volume'],250)
        self.assertEqual(groups['kg']['weighted_average_price_uzs_per_unit'],'10.00')
        self.assertEqual(groups['tonne']['volume'],6)
        self.assertEqual(groups['tonne']['weighted_average_price_uzs_per_unit'],'4000.00')

    def test_weighted_price_is_not_simple_average(self):
        con=sqlite3.connect(self.path)
        con.execute('UPDATE deals SET quantity=10,amount_hundredths=100000 WHERE deal_id=1')
        con.commit(); con.close()
        r=tools.get_product_stats.invoke({'query':'wheat first'})
        self.assertEqual(r['by_unit'][0]['weighted_average_price_uzs_per_unit'],'11.72')

    def test_dates_and_status(self):
        r=tools.get_product_stats.invoke({'query':'wheat','start_date':'2024-01-01','end_date':'2024-01-01','status':'done'})
        self.assertEqual(r['trade_count'],25)
        self.assertEqual(r['total_value_uzs'],'2500.00')

    def test_empty_and_literal_query(self):
        for query in ['missing',"' OR 1=1 --",'%']:
            r=tools.get_product_stats.invoke({'query':query})
            self.assertEqual(r['matching_contract_count'],0)
            self.assertEqual(r['by_unit'],[])
            self.assertEqual(r['total_value_uzs'],'0.00')
        r=tools.get_product_stats.invoke({'query':'wheat','start_date':'2030-01-01'})
        self.assertEqual(r['matching_contract_count'],26)
        self.assertEqual(r['trade_count'],0)

    def test_bad_inputs(self):
        for args in [{'query':' '},{'query':'x'*201},{'query':'wheat','start_date':'bad'}]:
            self.assertIsInstance(tools.get_product_stats.invoke(args),str)

    def test_missing_rate_and_invalid_volume_fail(self):
        con=sqlite3.connect(self.path); con.execute('DELETE FROM usd_rates'); con.commit(); con.close()
        self.assertIsInstance(tools.get_product_stats.invoke({'query':'wheat'}),str)
        con=sqlite3.connect(self.path); con.execute('UPDATE deals SET quantity=0 WHERE deal_id=1'); con.commit(); con.close()
        self.assertIsInstance(tools.get_product_stats.invoke({'query':'wheat first'}),str)


class RealProductStatsTests(unittest.TestCase):
    def test_wheat_against_original_csvs(self):
        root=tools.DB_PATH.parent/'uzex_spot_data'
        def read(name):
            with (root/name).open(encoding='utf-8-sig',newline='') as f:
                r=csv.reader(f); next(r); yield from r
        query='\u043f\u0448\u0435\u043d\u0438\u0446\u0430'
        contracts={r[0]:r for r in read('contracts.csv') if query in ' '.join(r[1:7]).casefold()}
        rates={r[0]:Decimal(r[1]) for r in read('usd_rates.csv')}
        groups={}; count=0
        for row in read('deals_2024.csv'):
            if row[7] not in contracts: continue
            amount=Decimal(row[5].replace(' ',''))
            if contracts[row[7]][13]==tools.USD: amount*=rates[row[1]]
            g=groups.setdefault(row[10],[0,Decimal(0),0])
            g[0]+=1; g[1]+=amount; g[2]+=int(row[4])*int(row[9]); count+=1
        result=tools.get_product_stats.invoke({'query':query,'start_date':'2024-01-01','end_date':'2024-12-31'})
        self.assertGreater(count,0)
        self.assertEqual(result['trade_count'],count)
        self.assertEqual(result['matching_contract_count'],len(contracts))
        money=lambda v:format(v.quantize(Decimal('.01'),rounding=ROUND_HALF_UP),'.2f')
        self.assertEqual(result['total_value_uzs'],money(sum((g[1] for g in groups.values()),Decimal(0))))
        for g in result['by_unit']:
            expected=groups[g['unit']]
            self.assertEqual(g['volume'],expected[2])
            self.assertEqual(g['weighted_average_price_uzs_per_unit'],money(expected[1]/expected[2]))
        print('\nWheat 2024:',count,'trades; CSV statistics match.')


if __name__=='__main__': unittest.main()
