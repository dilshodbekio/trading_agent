"""Build uzex.db from the original CSVs using only Python's standard library.
Money/rates use integer hundredths (divide by 100); dates use YYYY-MM-DD.
Original CSVs remain unchanged. Refuses to overwrite an existing database.
"""
import csv
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'uzex_spot_data'
DB = ROOT / 'uzex.db'
# Columns follow the original CSV order. T=text, I=integer, D=date, M=money hundredths.
SPECS = {
    'participants': ('participants.csv', 'tax_id:T name:T region:T activity:T role:T vat_payer:T', 'tax_id'),
    'brokers': ('brokers.csv', 'broker_code:T name:T tax_id:T region:T', 'broker_code'),
    'usd_rates': ('usd_rates.csv', 'rate_date:D rate_hundredths:M', 'rate_date'),
    'contracts': ('contracts.csv', 'contract_id:I product_name:T brand:T size:T grade:T product_type:T product_class:T hs_code:T product_group:T category:T industry:T unit:T lot_size:N currency:T base_price_hundredths:M deposit_percent:T delivery_basis:T region:T warehouse_address:T auction_type:T contract_type:T contract_form:T payment_days:I delivery_days:I packaging:T seller_tax_id:T registration_date:D valid_until:D', 'contract_id'),
    'deals': ('deals_*.csv', 'deal_id:I deal_date:D deal_time:T auction_type:T quantity:N amount_hundredths:M starting_price_hundredths:M contract_id:I product_name:T lot_size:N unit:T region:T contract_type:T contract_form:T status:T seller_tax_id:T buyer_tax_id:T seller_broker_code:T buyer_broker_code:T payment_date:D delivery_date:D delivered_quantity:N', 'deal_id'),
}
REFERENCES = {
    'contracts': [('seller_tax_id', 'participants', 'tax_id')],
    'deals': [('contract_id', 'contracts', 'contract_id'), ('seller_tax_id', 'participants', 'tax_id'), ('buyer_tax_id', 'participants', 'tax_id'), ('seller_broker_code', 'brokers', 'broker_code'), ('buyer_broker_code', 'brokers', 'broker_code'), ('deal_date', 'usd_rates', 'rate_date')],
}

def convert(value, kind):
    if value == '':
        return None
    if kind == 'D':
        return datetime.strptime(value, '%d.%m.%Y').date().isoformat()
    if kind == 'M':
        scaled = Decimal(value.replace(' ', '').replace('\u00a0', '')) * 100
        if scaled != scaled.to_integral_value():
            raise ValueError(f'More than two decimal places: {value}')
        return int(scaled)
    if kind == 'I':
        return int(value)
    if kind == 'N':
        number = Decimal(value.replace(' ', ''))
        if number != number.to_integral_value():
            raise ValueError(f'Expected integral quantity/lot size: {value}')
        return int(number)
    return value

def main():
    if DB.exists():
        raise FileExistsError(f'Preserving existing database: {DB}')
    temporary = ROOT / 'uzex.building.db'
    if temporary.exists():
        raise FileExistsError(temporary)
    con = sqlite3.connect(temporary)
    try:
        con.execute('PRAGMA foreign_keys=ON')
        with con:
            for table, (pattern, spec, primary) in SPECS.items():
                columns = [entry.split(':') for entry in spec.split()]
                definitions = [f'{name} {"INTEGER" if kind in "IMN" else "TEXT"}' + (' PRIMARY KEY' if name == primary else '') for name, kind in columns]
                definitions += [f'FOREIGN KEY ({col}) REFERENCES {parent}({key})' for col, parent, key in REFERENCES.get(table, [])]
                con.execute(f'CREATE TABLE {table} ({", ".join(definitions)})')
                sql = f'INSERT INTO {table} VALUES ({", ".join("?" for _ in columns)})'
                count = 0
                files = sorted(DATA.glob(pattern))
                if not files:
                    raise FileNotFoundError(pattern)
                expected_header = None
                for path in files:
                    with path.open(encoding='utf-8-sig', newline='') as f:
                        reader = csv.reader(f)
                        header = next(reader)
                        if len(header) != len(columns) or (expected_header is not None and header != expected_header):
                            raise ValueError(f'Unexpected header: {path}')
                        expected_header = header
                        batch = []
                        for line, row in enumerate(reader, 2):
                            if len(row) != len(columns):
                                raise ValueError(f'Bad row: {path}:{line}')
                            batch.append(tuple(convert(value, kind) for value, (_, kind) in zip(row, columns)))
                            if len(batch) == 5000:
                                con.executemany(sql, batch); count += len(batch); batch.clear()
                        if batch:
                            con.executemany(sql, batch); count += len(batch)
                actual = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                assert count == actual, (table, count, actual)
                print(f'{table}: {actual:,} rows')
            for table, cols in {'deals': ['deal_date', 'contract_id', 'seller_tax_id', 'buyer_tax_id', 'seller_broker_code', 'buyer_broker_code', 'status', 'region'], 'contracts': ['seller_tax_id', 'product_name', 'category']}.items():
                for col in cols:
                    con.execute(f'CREATE INDEX idx_{table}_{col} ON {table}({col})')
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        con.execute('ANALYZE')
        con.commit()
        print('Integrity and foreign keys: OK')
        print('Trades by year:', con.execute("SELECT substr(deal_date,1,4), count(*) FROM deals GROUP BY 1").fetchall())
    except BaseException:
        con.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        con.close()
        temporary.rename(DB)
        print(f'Created: {DB}')

if __name__ == '__main__':
    main()
