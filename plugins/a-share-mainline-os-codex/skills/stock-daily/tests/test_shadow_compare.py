import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile
import sys
from datetime import timedelta

SPEC = importlib.util.spec_from_file_location("shadow_compare", Path(__file__).resolve().parents[1] / "scripts/shadow_compare.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def fixture():
    days = ["2026-08-03", "2026-08-04", "2026-08-05"]
    signal = {"date": days[0], "target": 1, "decided_at": days[0]+"T20:00:00+08:00", "evidence_ref": "synthetic:test"}
    return {"mode": "synthetic_test", "asset": "SYNTHETIC", "source": "synthetic fixture, not market prices", "price_basis": "unadjusted_no_actions", "cost_bps": 0, "calendar": days, "prices": [{"date": d, "open": p, "close": p, "buyable": True, "sellable": True} for d,p in zip(days,[10,20,22])], "strategies": [{"name": "test", "signals": [signal]}, {"name": "cash_control", "signals": []}]}


class ShadowTests(unittest.TestCase):
    def test_next_open_not_signal_close(self):
        r=M.simulate(fixture())["results"][0]
        self.assertAlmostEqual(r["return_pct"],10)
        self.assertEqual(r["fills"][0]["price"],20)

    def test_cost_is_paid_and_target_does_not_overdraw_cash(self):
        d=fixture();d["cost_bps"]=100
        r=M.simulate(d)["results"][0]
        self.assertAlmostEqual(r["return_pct"],(1.1/1.01-1)*100)
        self.assertGreater(r["cost_initial_units"],0)

    def test_unbuyable_does_not_fabricate_fill_or_retry(self):
        d=fixture();d["prices"][1]["buyable"]=False
        r=M.simulate(d)["results"][0]
        self.assertEqual(r["return_pct"],0);self.assertEqual(len(r["unexecuted"]),1)

    def test_exit_only_following_session(self):
        d=fixture();s=copy.deepcopy(d["strategies"][0]["signals"][0]);s.update(date="2026-08-04",decided_at="2026-08-04T20:00:00+08:00",target=0)
        d["strategies"][0]["signals"].append(s)
        r=M.simulate(d)["results"][0]
        self.assertEqual([f["simulation_date"] for f in r["fills"]],["2026-08-04","2026-08-05"])

    def test_missing_trading_session_rejected(self):
        d=fixture();d["prices"].pop(1)
        with self.assertRaises(ValueError):M.simulate(d)

    def test_final_signal_pending_not_filled(self):
        d=fixture();d["strategies"][0]["signals"][0].update(date="2026-08-05",decided_at="2026-08-05T20:00:00+08:00")
        r=M.simulate(d)["results"][0];self.assertEqual(r["fills"],[]);self.assertEqual(r["awaiting_next_session"],["2026-08-05"])

    def test_invalid_inputs_fail_closed(self):
        for field,value in [("cost_bps",float('nan')),("price_basis","unknown"),("mode","live"),("source","")]:
            with self.subTest(field=field):
                d=fixture();d[field]=value
                with self.assertRaises(ValueError):M.simulate(d)
        for key,value in [("target",1.5),("target",True),("decided_at","2026-08-03T14:59:00+08:00"),("decided_at","2026-08-03T20:00:00"),("evidence_ref","")]:
            with self.subTest(key=key,value=value):
                d=fixture();d["strategies"][0]["signals"][0][key]=value
                with self.assertRaises(ValueError):M.simulate(d)

    def test_unsellable_preserves_existing_exposure(self):
        d=fixture();s=copy.deepcopy(d["strategies"][0]["signals"][0]);s.update(date="2026-08-04",decided_at="2026-08-04T20:00:00+08:00",target=0)
        d["strategies"][0]["signals"].append(s);d["prices"][2]["sellable"]=False
        r=M.simulate(d)["results"][0];self.assertEqual(len(r["fills"]),1);self.assertAlmostEqual(r["path"][-1]["exposure"],1)

    def test_fake_prospective_claim_without_receipt_rejected(self):
        d=fixture();d['mode']='prospective_shadow'
        with self.assertRaisesRegex(ValueError,'journal'):M.simulate(d)

    def test_weekend_decision_waits_until_next_session(self):
        d=fixture();d['calendar']=['2026-07-31','2026-08-03','2026-08-04']
        for r,day in zip(d['prices'],d['calendar']):r['date']=day
        d['strategies'][0]['signals'][0].update(date='2026-08-01',decided_at='2026-08-01T10:00:00+08:00')
        r=M.simulate(d)['results'][0]
        self.assertEqual(r['fills'][0]['simulation_date'],'2026-08-03')

    def test_midnight_and_preopen_decisions_use_coming_open(self):
        for clock in ('00:30:00', '09:29:59'):
            with self.subTest(clock=clock):
                d = fixture()
                d['asset'] = '111111'
                d['strategies'][0]['signals'][0].update(
                    date='2026-08-04',
                    decided_at=f'2026-08-04T{clock}+08:00',
                    action='enter',
                )
                result = M.simulate(d)['results'][0]
                self.assertEqual(len(result['fills']), 1)
                self.assertEqual(result['fills'][0]['simulation_date'], '2026-08-04')
                self.assertEqual(result['fills'][0]['price'], d['prices'][1]['open'])

    def test_intraday_decisions_rejected_including_open_boundary(self):
        for clock in ('09:30:00', '14:59:00'):
            with self.subTest(clock=clock):
                d = fixture()
                d['asset'] = '111111'
                d['strategies'][0]['signals'][0].update(
                    date='2026-08-04',
                    decided_at=f'2026-08-04T{clock}+08:00',
                )
                with self.assertRaises(ValueError):
                    M.simulate(d)

    def test_prospective_execution_open_is_strictly_after_recorded_at(self):
        # Test-only clock injection writes real hash-chain receipts; these
        # synthetic records are not prospective investment evidence.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_decision_journal import record, journal
        for recorded_clock, expected_session in (
            ('09:29:59', '2026-08-04'),
            ('09:30:00', '2026-08-05'),
            ('09:30:01', '2026-08-05'),
        ):
            with self.subTest(recorded_at=recorded_clock), tempfile.TemporaryDirectory() as directory:
                strategies = []
                for name in ('main', 'hold'):
                    path = Path(directory) / f'{name}.jsonl'
                    entry = record(identity='overnight-entry', day='2026-08-03', action='enter')
                    entry.update(
                        decided_at='2026-08-04T00:30:00+08:00',
                        research_target=0.5,
                        research_asset='111111',
                    )
                    journal.append(path, entry, journal.timestamp(f'2026-08-04T{recorded_clock}+08:00'))
                    strategies.append({
                        'name': name,
                        'theme_id': entry['theme_id'],
                        'method_version': entry['method_version'],
                        'journal': str(path),
                        'signals': [{
                            # Signal date follows the actual decision day,
                            # while data_date stays on the prior market day.
                            'date': '2026-08-04',
                            'decided_at': entry['decided_at'],
                            'target': entry['research_target'],
                            'evidence_ref': 'synthetic unit test only',
                            'decision_id': entry['decision_id'],
                        }],
                    })
                d = fixture()
                d.update(
                    mode='prospective_shadow',
                    asset='111111',
                    evaluation_as_of='2026-08-05T23:00:00+08:00',
                    strategies=strategies,
                )
                for result in M.simulate(d)['results']:
                    self.assertEqual(len(result['fills']), 1)
                    self.assertEqual(result['fills'][0]['simulation_date'], expected_session)

    def test_hold_does_not_rebalance_existing_units(self):
        d=fixture();d['prices'][1]['open']=10;d['prices'][1]['close']=20
        first=d['strategies'][0]['signals'][0];first['target']=0.5
        s=copy.deepcopy(first);s.update(date='2026-08-04',decided_at='2026-08-04T20:00:00+08:00',action='hold')
        d['strategies'][0]['signals'].append(s)
        r=M.simulate(d)['results'][0]
        self.assertEqual(len(r['fills']),1)

    def test_reduce_never_buys_and_enter_never_sells(self):
        for action,target,price in [('reduce',0.49,7),('enter',0.6,20)]:
            with self.subTest(action=action):
                d=fixture();d['prices'][1].update(open=10,close=10);d['prices'][2].update(open=price,close=price)
                first=d['strategies'][0]['signals'][0];first['target']=0.5
                s=copy.deepcopy(first);s.update(date='2026-08-04',decided_at='2026-08-04T20:00:00+08:00',target=target,action=action)
                d['strategies'][0]['signals'].append(s)
                r=M.simulate(d)['results'][0]
                self.assertEqual(len(r['fills']),1);self.assertEqual(len(r['action_noops']),1)

    def test_prospective_omitted_exit_and_asset_swap_rejected(self):
        d=fixture();d['mode']='prospective_shadow';d['evaluation_as_of']='2026-08-05T23:00:00+08:00'
        d['strategies'][0].update(theme_id='t',method_version='v1')
        first=d['strategies'][0]['signals'][0];first['decision_id']='entry'
        def receipt(identity, day, target, action):
            return {'recorded_at':day+'T21:00:00+08:00','record':{'decision_id':identity,'data_date':day,'mode':'shadow','theme_id':'t','method_version':'v1','research_target':target,'research_asset':'SYNTHETIC','action':action,'decided_at':day+'T20:00:00+08:00'}}
        rows={'entry':receipt('entry','2026-08-03',1,'enter'),'exit':receipt('exit','2026-08-04',0,'exit')}
        with patch.object(M,'prospective_records',return_value=rows):
            with self.assertRaisesRegex(ValueError,'every locked'):M.simulate(d)
            s=copy.deepcopy(first);s.update(date='2026-08-04',decided_at='2026-08-04T20:00:00+08:00',target=0,decision_id='exit')
            d['strategies'][0]['signals'].append(s);d['asset']='OTHER'
            with self.assertRaisesRegex(ValueError,'does not match'):M.simulate(d)

    def test_full_receipt_to_simulation_integration(self):
        # Synthetic past-clock fixture in a temporary directory, never a real
        # prospective record or a source of investment performance evidence.
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_decision_journal import record, journal
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/'hold.jsonl',Path(directory)/'exit.jsonl']
            strategy_list=[]
            for path,exit_next in zip(paths,[False,True]):
                first=record(identity='d1',day='2026-08-03',action='enter')
                first.update(research_target=0.5,research_asset='111111')
                journal.append(path,first,journal.timestamp(first['decided_at'])+timedelta(minutes=1))
                second=record(identity='d2',day='2026-08-04',action='exit' if exit_next else 'hold',previous='d1')
                second.update(research_target=0 if exit_next else 0.5,research_asset='111111')
                if exit_next:
                    second['discipline']['2']='triggered';second['change_reason']['kind']='discipline'
                journal.append(path,second,journal.timestamp(second['decided_at'])+timedelta(minutes=1))
                strategy_list.append({'name':path.stem,'theme_id':'test-theme','method_version':'shadow-v1','journal':str(path),'signals':[{'date':r['data_date'],'decided_at':r['decided_at'],'target':r['research_target'],'evidence_ref':'synthetic unit test only','decision_id':r['decision_id']} for r in [first,second]]})
            d=fixture();d.update(mode='prospective_shadow',asset='111111',evaluation_as_of='2026-08-05T23:00:00+08:00',strategies=strategy_list)
            d['prices'][1].update(open=10,close=20);d['prices'][2].update(open=20,close=22)
            results=M.simulate(d)['results']
            self.assertEqual(len(results[0]['fills']),1)
            self.assertEqual(len(results[1]['fills']),2)
            self.assertGreater(results[0]['return_pct'],results[1]['return_pct'])
            d['strategies'][1]['signals'].pop()
            with self.assertRaisesRegex(ValueError,'every locked'):M.simulate(d)


if __name__ == "__main__":unittest.main()
