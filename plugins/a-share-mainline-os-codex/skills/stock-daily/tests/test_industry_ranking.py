import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_fund_flow_cache as cache
import industry_ranking as ranking

DAY = '2026-09-18'


def observation(i):
    return dict(taxonomy='eastmoney_dc', board_code=f'BK{i:04}', board_type='industry',
                trade_date=DAY, name=f'行业{i}', close=100, change_pct=float(i-100),
                main_net=1, main_pct=1, super_net=1, large_net=0, mid_net=0, small_net=0,
                provider='eastmoney_clist_snapshot')


def page(start, end, total=205, day='20260918'):
    response = MagicMock()
    response.json.return_value = {'data': {'total': total, 'diff': [
        dict(f12=f'BK{i:04}', f14=f'行业{i}', f3=float(i-100), f297=day)
        for i in range(start, end)]}}
    return response


class IndustryRankingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'cache.sqlite3'
        cache.init_db(self.db)

    def seed(self, certify=True):
        rows = [observation(i) for i in range(205)]
        cache.store_observations(self.db, rows)
        if certify:
            ranking.record_snapshot_coverage(self.db, 'industry', DAY,
                [r['board_code'] for r in rows], 'test: complete 205-row snapshot')

    def test_exact_day_complete_cache_ranks_full_universe_without_network(self):
        self.seed()
        get = MagicMock(side_effect=AssertionError('network not needed'))
        out = ranking.industry_comparison(3, DAY, db_path=self.db, get=get)
        self.assertEqual(out['total'], 205)
        self.assertEqual(out['top'][0]['code'], 'BK0204')
        self.assertEqual(out['bottom'][-1]['code'], 'BK0000')
        self.assertEqual(out['bottom'][-1]['rank'], 205)
        self.assertEqual(out['trade_date'], DAY)
        self.assertEqual(out['mode'], 'cache')
        self.assertIsNone(out['top'][0]['up_count'])
        self.assertIn('up_count', out['missing_fields'])
        get.assert_not_called()

    def test_uncertified_cache_cannot_claim_complete(self):
        self.seed(certify=False)
        with self.assertRaisesRegex(RuntimeError, '凭证'):
            ranking.industry_comparison(3, DAY, db_path=self.db,
                                       get=MagicMock(side_effect=RuntimeError('offline')))

    def test_old_day_and_missing_row_do_not_pass(self):
        self.seed()
        offline = MagicMock(side_effect=RuntimeError('offline'))
        with self.assertRaises(RuntimeError):
            ranking.industry_comparison(3, '2026-09-21', db_path=self.db, get=offline)
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM observations WHERE board_code='BK0000'")
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            ranking.industry_comparison(3, DAY, db_path=self.db, get=offline)

    def test_live_failure_falls_back_with_diagnostic(self):
        self.seed()
        out = ranking.industry_comparison(3, DAY, db_path=self.db, prefer_cache=False,
                                         get=MagicMock(side_effect=RuntimeError('reset')))
        self.assertEqual(out['mode'], 'cache')
        self.assertIn('reset', out['diagnostics'][0])

    def test_full_pagination_not_first_page_tail(self):
        get = MagicMock(side_effect=[page(0,100), page(100,200), page(200,205)])
        out = ranking.industry_comparison(3, DAY, db_path=self.db, get=get)
        self.assertEqual(out['total'], 205)
        self.assertEqual(out['top'][0]['code'], 'BK0204')
        self.assertEqual(out['bottom'][-1]['code'], 'BK0000')
        self.assertEqual([c.kwargs['params']['pn'] for c in get.call_args_list], [1,2,3])

    def test_partial_duplicate_changed_total_and_wrong_day_rejected(self):
        cases = [[page(0,100),page(100,150)], [page(0,100),page(0,100)],
                 [page(0,100),page(100,200,total=206)], [page(0,100,day='20260917')]]
        for pages in cases:
            with self.subTest(pages=len(pages)), self.assertRaises(RuntimeError):
                ranking.industry_comparison(3, DAY, db_path=self.db, get=MagicMock(side_effect=pages))

    def test_live_missing_width_and_leader_fields_are_declared(self):
        out = ranking.industry_comparison(1, DAY, db_path=self.db,
                                         get=MagicMock(return_value=page(0,1,total=1)))
        self.assertEqual(set(out['missing_fields']), {'up_count','down_count','leader','leader_change'})
        complete = page(0,1,total=1)
        complete.json.return_value['data']['diff'][0].update(f104=3,f105=2,f140='测试领涨',f136=1)
        out = ranking.industry_comparison(1, DAY, db_path=self.db,
                                         get=MagicMock(return_value=complete))
        self.assertEqual(out['missing_fields'], [])

    def test_invalid_price_and_future_date_rejected(self):
        bad = page(0,1,total=1)
        bad.json.return_value['data']['diff'][0]['f3'] = float('nan')
        with self.assertRaises(RuntimeError):
            ranking.industry_comparison(1, DAY, db_path=self.db, get=lambda *a,**k:bad)
        get = MagicMock()
        with self.assertRaises(ValueError):
            ranking.industry_comparison(1, '2999-01-01', db_path=self.db, get=get)
        get.assert_not_called()

    def test_coverage_cannot_certify_partial_database(self):
        self.seed(certify=False)
        with self.assertRaises(RuntimeError):
            ranking.record_snapshot_coverage(self.db, 'industry', DAY, ['BK0000'], 'invalid')

    def test_successful_close_sync_records_coverage_but_intraday_does_not(self):
        rows = [observation(i) for i in range(205)]
        with patch.object(cache, 'fetch_clist_snapshot', return_value=rows), patch.object(cache, '_new_session'):
            cache.sync_clist_snapshot(self.db, DAY, board_types=['industry'], allow_intraday=True)
            with self.assertRaisesRegex(RuntimeError, '凭证'):
                ranking._cache_rows(self.db, DAY)
            cache.sync_clist_snapshot(self.db, DAY, board_types=['industry'])
            result, meta = ranking._cache_rows(self.db, DAY)
        self.assertEqual(len(result), 205)
        self.assertIn('full pagination', meta['coverage_provenance'])

    def test_snapshot_sync_rejects_changed_or_boolean_total_before_certification(self):
        session = MagicMock()
        first, second = page(0,100,total=101), page(100,101,total=102)
        session.get.side_effect = [first, second]
        with self.assertRaisesRegex(cache.SourceUnavailableError, '总数变化'):
            cache.fetch_clist_snapshot('industry', DAY, session=session)
        first.json.return_value['data']['total'] = True
        session.get.side_effect = [first]
        with self.assertRaisesRegex(cache.SourceUnavailableError, '有效 total'):
            cache.fetch_clist_snapshot('industry', DAY, session=session)


if __name__ == '__main__':
    unittest.main()
