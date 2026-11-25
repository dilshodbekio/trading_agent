"""Three read-only UZEX tools. No Gemini key is required to call them directly."""
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from langchain.tools import tool
from langchain_core.tools import ToolException

DB_PATH = Path(__file__).resolve().parent / 'uzex.db'
UZS = '\u0423\u0437\u0431\u0435\u043a\u0441\u043a\u0438\u0439 \u0441\u0443\u043c'
USD = '\u0414\u043e\u043b\u043b\u0430\u0440 \u0421\u0428\u0410'


def _connect():
    connection = sqlite3.connect(DB_PATH.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _filters(start_date, end_date, status):
    clauses, values = [], []
    for column, value, operator in [('deal_date', start_date, '>='), ('deal_date', end_date, '<=')]:
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise ToolException('Dates must be valid YYYY-MM-DD strings.') from None
            clauses.append(f'd.{column} {operator} ?')
            values.append(value)
    if start_date and end_date and start_date > end_date:
        raise ToolException('start_date must not be after end_date.')
    if status is not None:
        with closing(_connect()) as con:
            if not con.execute('SELECT 1 FROM deals WHERE status = ? LIMIT 1', (status,)).fetchone():
                raise ToolException('Unknown status. Use the exact Russian status from the dataset.')
        clauses.append('d.status = ?')
        values.append(status)
    return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), values


def _scope(start_date, end_date, status):
    return {'start_date': start_date, 'end_date': end_date, 'status': status or 'ALL statuses, including cancelled trades'}


@tool
def count_deals(start_date: str | None = None, end_date: str | None = None, status: str | None = None) -> dict:
    """Count registered trades within inclusive YYYY-MM-DD dates and optional exact Russian status.
    Default includes all statuses, including cancelled trades.
    """
    where, params = _filters(start_date, end_date, status)
    with closing(_connect()) as con:
        count = con.execute('SELECT COUNT(*) FROM deals d' + where, params).fetchone()[0]
    return {'count': count, 'filters': _scope(start_date, end_date, status)}


@tool
def get_trade_value(start_date: str | None = None, end_date: str | None = None, status: str | None = None) -> dict:
    """Sum registered trade value in UZS, converting USD at the deal date's rate.
    Dates are inclusive YYYY-MM-DD; status is an optional exact Russian status.
    Default includes cancelled trades; this is registered value, not realized revenue.
    Returns a decimal string rounded once after summing exact conversions.
    """
    where, params = _filters(start_date, end_date, status)
    total_scaled, count, usd_count = 0, 0, 0
    with closing(_connect()) as con:
        rows = con.execute('''SELECT d.amount_hundredths, c.currency, r.rate_hundredths
            FROM deals d LEFT JOIN contracts c ON c.contract_id=d.contract_id
            LEFT JOIN usd_rates r ON r.rate_date=d.deal_date''' + where, params)
        for amount, currency, rate in rows:
            if amount is None:
                raise ToolException('Missing trade amount; cannot return a complete total.')
            if currency == UZS:
                total_scaled += amount * 100
            elif currency == USD and rate is not None:
                total_scaled += amount * rate
                usd_count += 1
            else:
                raise ToolException('Missing currency/rate or unsupported currency; cannot return a complete total.')
            count += 1
    total = (Decimal(total_scaled) / 10000).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'total_uzs': format(total, '.2f'), 'trade_count': count, 'usd_trades_converted': usd_count,
            'filters': _scope(start_date, end_date, status)}


@tool
def get_deal(deal_id: int) -> dict:
    """Get one trade by ID with currency and participant names; return found=false if absent.
    Money is returned as decimal strings in contract currency. Quantity counts lots.
    """
    with closing(_connect()) as con:
        row = con.execute('''SELECT d.*, c.currency, s.name AS seller_name, b.name AS buyer_name
            FROM deals d LEFT JOIN contracts c ON c.contract_id=d.contract_id
            LEFT JOIN participants s ON s.tax_id=d.seller_tax_id
            LEFT JOIN participants b ON b.tax_id=d.buyer_tax_id
            WHERE d.deal_id=?''', (deal_id,)).fetchone()
    if row is None:
        return {'found': False, 'deal_id': deal_id}
    result = dict(row)
    for column in ('amount', 'starting_price'):
        value = result.pop(column + '_hundredths')
        result[column] = format(Decimal(value) / 100, '.2f') if value is not None else None
    return {'found': True, 'deal': result}


@tool
def search_products(query: str, limit: int = 10) -> dict:
    """Find contract products by literal words in name, brand, size, grade, type or class.
    Use Russian terms from the dataset; no automatic translation or synonyms.
    All words must match, Unicode case-insensitive. Punctuation is literal.
    Multiple contracts can represent one product. If truncated, narrow the query;
    never treat the returned page as all matching contracts.
    """
    words = query.casefold().split()
    if not words or len(query) > 200:
        raise ToolException('Provide a product query of 1 to 200 characters.')
    if not 1 <= limit <= 50:
        raise ToolException('limit must be between 1 and 50.')
    fields = ('product_name', 'brand', 'size', 'grade', 'product_type', 'product_class')
    searchable = " || ' ' || ".join(f"COALESCE({field}, '')" for field in fields)
    where = ' AND '.join(f'instr(casefold({searchable}), ?) > 0' for _ in words)
    with closing(_connect()) as con:
        con.create_function('casefold', 1, str.casefold, deterministic=True)
        total = con.execute('SELECT COUNT(*) FROM contracts WHERE ' + where, words).fetchone()[0]
        rows = con.execute('SELECT contract_id, product_name, brand, size, grade, product_type, '
                           'product_class, category, unit, currency FROM contracts WHERE ' + where +
                           ' ORDER BY product_name, contract_id LIMIT ?', [*words, limit]).fetchall()
    return {'query': query, 'total_matches': total, 'returned_count': len(rows),
            'truncated': total > len(rows), 'products': [dict(row) for row in rows]}


@tool
def get_product_stats(query: str, start_date: str | None = None,
                      end_date: str | None = None, status: str | None = None) -> dict:
    """Calculate trade count, total UZS value, physical volume and weighted UZS price for a product.
    Uses ALL contracts matching the same literal Russian words/attributes as search_products,
    never only a search page. Dates are inclusive YYYY-MM-DD. Default includes cancelled trades.
    Units are reported separately without conversion; weighted price = total UZS / physical volume.
    Broad matches may include different grades/products: disclose scope and clarify if needed.
    Contract preview is limited to 20; aggregation includes every match. No predictions.
    """
    words = query.casefold().split()
    if not words or len(query) > 200:
        raise ToolException('Provide a product query of 1 to 200 characters.')
    where, params = _filters(start_date, end_date, status)
    fields = ('product_name', 'brand', 'size', 'grade', 'product_type', 'product_class')
    searchable = " || ' ' || ".join(f"COALESCE(c.{field}, '')" for field in fields)
    product_where = ' AND '.join(f'instr(casefold({searchable}), ?) > 0' for _ in words)
    groups, matched_contracts, preview = {}, 0, []
    with closing(_connect()) as con:
        con.create_function('casefold', 1, str.casefold, deterministic=True)
        matched_contracts = con.execute('SELECT COUNT(*) FROM contracts c WHERE ' + product_where, words).fetchone()[0]
        preview = [dict(row) for row in con.execute(
            'SELECT c.contract_id,c.product_name,c.grade,c.unit,c.currency FROM contracts c WHERE ' +
            product_where + ' ORDER BY c.contract_id LIMIT 20', words)]
        sql = '''SELECT d.quantity,d.lot_size,d.unit,d.amount_hundredths,c.currency,r.rate_hundredths
                 FROM deals d JOIN contracts c ON c.contract_id=d.contract_id
                 LEFT JOIN usd_rates r ON r.rate_date=d.deal_date'''
        sql += where + (' AND ' if where else ' WHERE ') + product_where
        for quantity, lot_size, unit, amount, currency, rate in con.execute(sql, [*params, *words]):
            if quantity is None or lot_size is None or quantity <= 0 or lot_size <= 0 or not unit:
                raise ToolException('Missing or invalid volume/unit; cannot calculate complete statistics.')
            if amount is None:
                raise ToolException('Missing trade amount; cannot calculate complete statistics.')
            if currency == UZS:
                scaled = amount * 100
            elif currency == USD and rate is not None and rate > 0:
                scaled = amount * rate
            else:
                raise ToolException('Missing or unsupported currency/rate; cannot calculate complete statistics.')
            group = groups.setdefault(unit, {'trade_count': 0, 'volume': 0, 'scaled': 0})
            group['trade_count'] += 1
            group['volume'] += quantity * lot_size
            group['scaled'] += scaled
    def money(value):
        return format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '.2f')
    by_unit = []
    for unit, group in sorted(groups.items()):
        value = Decimal(group['scaled']) / 10000
        by_unit.append({'unit': unit, 'trade_count': group['trade_count'],
                        'volume': group['volume'], 'total_value_uzs': money(value),
                        'weighted_average_price_uzs_per_unit': money(value / group['volume'])})
    return {'query': query, 'filters': _scope(start_date, end_date, status),
            'matching_contract_count': matched_contracts, 'contract_preview': preview,
            'contract_preview_truncated': matched_contracts > len(preview),
            'aggregation_includes_all_matching_contracts': True,
            'trade_count': sum(g['trade_count'] for g in groups.values()),
            'total_value_uzs': money(Decimal(sum(g['scaled'] for g in groups.values())) / 10000),
            'by_unit': by_unit}


TOOLS = [count_deals, get_trade_value, get_deal, search_products, get_product_stats]
for registered_tool in TOOLS:
    registered_tool.handle_tool_error = True
