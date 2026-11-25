import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import search_products


class ProductSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.db'
        con = sqlite3.connect(self.path)
        con.execute('CREATE TABLE contracts(contract_id INTEGER, product_name TEXT, brand TEXT, size TEXT, grade TEXT, product_type TEXT, product_class TEXT, category TEXT, unit TEXT, currency TEXT)')
        con.executemany('INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?)', [
            (1,'Пшеница',None,None,'1 сорт',None,None,'Food','тонна','UZS'),
            (2,'ПШЕНИЦА',None,None,'2 сорт',None,None,'Food','тонна','USD'),
            (3,'Кирпич М-100',None,None,None,None,None,'Building','штука','UZS'),
        ])
        con.commit(); con.close()
        self.patcher = patch('tools.DB_PATH',self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop(); self.temp.cleanup()

    def test_unicode_casefold(self):
        self.assertEqual(search_products.invoke({'query':'пШеНиЦа'})['total_matches'],2)

    def test_all_words_and_attributes(self):
        result=search_products.invoke({'query':'пшеница 2 сорт'})
        self.assertEqual([r['contract_id'] for r in result['products']],[2])

    def test_punctuation(self):
        self.assertEqual(search_products.invoke({'query':'М-100'})['total_matches'],1)

    def test_limit_reports_truncation(self):
        result=search_products.invoke({'query':'пшеница','limit':1})
        self.assertEqual(result['total_matches'],2)
        self.assertEqual(result['returned_count'],1)
        self.assertTrue(result['truncated'])

    def test_literal_input_and_missing(self):
        for query in ['%', '_', "' OR 1=1 --", 'nonexistent']:
            self.assertEqual(search_products.invoke({'query':query})['total_matches'],0)

    def test_invalid_input(self):
        for args in [{'query':' '},{'query':'a'*201},{'query':'a','limit':0},{'query':'a','limit':51}]:
            self.assertIsInstance(search_products.invoke(args),str)


if __name__=='__main__':
    unittest.main()
