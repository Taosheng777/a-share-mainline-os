from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import market_breadth as breadth

DAY = '2026-09-18'


def response(rows, total=None):
    r = MagicMock()
    r.json.return_value = {'data': {'total': len(rows) if total is None else total, 'diff': rows}}
    return r


def row(code='600001', **kwargs):
    return dict(f12=code, f14='测试ST名称', f3=10, f2=11, f18=10, f297=20260918, **kwargs)


def quote(code='600001', upper='11', lower='9', day='20260918'):
    fields = [''] * 49
    fields[2], fields[30], fields[47], fields[48] = code, day + '150000', upper, lower
    r = MagicMock()
    r.content = ('q="' + '~'.join(fields) + '";').encode('gbk')
    return r


class MarketBreadthTest(unittest.TestCase):
    def test_source_limit_price_overrides_st_name(self):
        get = MagicMock(side_effect=[response([row()]), quote()])
        out = breadth.market_breadth(trade_date=DAY, get=get)
        self.assertEqual((out['limit_up'], out['limit_up_st'], out['up']), (1, 1, 1))
        self.assertTrue(breadth.breadth_reconcile(1, 0, 1, 0, out)['ok'])
        self.assertFalse(breadth.breadth_reconcile(1, 0, 0, 0, out)['ok'])

    def test_missing_or_stale_limit_quotes_do_not_pass_reconciliation(self):
        for value in [quote(day='20260917'), quote(upper='nan'), quote(upper='-1', lower='9')]:
            out = breadth.market_breadth(trade_date=DAY, get=MagicMock(side_effect=[response([row()]), value]))
            self.assertEqual(out['limit_price_missing'], 1)
            self.assertFalse(breadth.breadth_reconcile(1, 0, 0, 0, out)['ok'])

    def test_missing_previous_close_does_not_skip_source_limit_validation(self):
        item = row(); item['f18'] = None
        get = MagicMock(side_effect=[response([item]), quote()])
        out = breadth.market_breadth(trade_date=DAY, get=get)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(out['limit_up'], 1)
        self.assertFalse(breadth.breadth_reconcile(1, 0, 0, 0, out)['ok'])

    def test_active_row_missing_price_blocks_reconciliation(self):
        for price in (None, 0, True, float('nan')):
            item = row(); item['f2'] = price
            out = breadth.market_breadth(trade_date=DAY, get=MagicMock(return_value=response([item])))
            self.assertEqual(out['limit_price_missing'], 1)
            self.assertFalse(breadth.breadth_reconcile(1, 0, 0, 0, out)['ok'])

    def test_unlimited_source_price_is_separate(self):
        out = breadth.market_breadth(trade_date=DAY, get=MagicMock(side_effect=[response([row()]), quote(upper='-1', lower='-1')]))
        self.assertEqual(out['limit_price_missing'], 0)
        self.assertEqual(out['limit_price_unlimited'], 1)

    def test_truncated_duplicate_changed_total_and_old_dates_are_rejected(self):
        rows = [row(f'600{i:03}') for i in range(100)]
        old = row(); old['f297'] = 20260917
        cases = [[response(rows[:50], 101)], [response(rows, 101), response([rows[0]], 101)],
                 [response(rows, 101), response([row('600100')], 102)], [response([old])],
                 [response([], True)], [response([])]]
        for pages in cases:
            with self.subTest(case=len(pages)), self.assertRaises(ValueError):
                breadth.market_breadth(trade_date=DAY, get=MagicMock(side_effect=pages))

    def test_quote_network_failure_keeps_missing_evidence(self):
        get = MagicMock(side_effect=[response([row()]), OSError('offline')])
        out = breadth.market_breadth(trade_date=DAY, get=get)
        self.assertEqual(out['limit_price_missing'], 1)
        self.assertFalse(breadth.breadth_reconcile(1, 0, 0, 0, out)['ok'])

    def test_future_date_never_calls_network(self):
        get = MagicMock()
        with self.assertRaises(ValueError):
            breadth.market_breadth(trade_date='2999-01-01', get=get)
        get.assert_not_called()

    def test_incomplete_shadow_and_invalid_counts_are_rejected(self):
        out = breadth.market_breadth(trade_date=DAY, get=MagicMock(side_effect=[response([row()]), quote()]))
        out['complete'] = False
        self.assertFalse(breadth.breadth_reconcile(1, 0, 1, 0, out)['ok'])
        with self.assertRaises(ValueError):
            breadth.breadth_reconcile(True, 0, 1, 0, out)


if __name__ == '__main__':
    unittest.main()
