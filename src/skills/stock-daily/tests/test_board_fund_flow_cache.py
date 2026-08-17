import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_fund_flow_cache as cache


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.trust_env = True
        self.last_url = None
        self.last_json = None

    def get(self, *args, **kwargs):
        self.calls += 1
        self.last_url = args[0] if args else None
        return FakeResponse(self.payload)

    def post(self, *args, **kwargs):
        self.calls += 1
        self.last_url = args[0] if args else None
        self.last_json = kwargs.get("json")
        return FakeResponse(self.payload)


class PagedSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0
        self.trust_env = True

    def get(self, *args, **kwargs):
        self.calls += 1
        page = int(kwargs["params"]["pn"])
        return FakeResponse(self.pages[page])


def row(provider="eastmoney_clist_snapshot", main_net=100.0, close=1234.56):
    return {
        "taxonomy": "eastmoney_dc",
        "board_code": "BK9001",
        "board_type": "industry",
        "name": "通信网络设备及器件",
        "trade_date": "2026-08-13",
        "close": close,
        "change_pct": 1.2,
        "main_net": main_net,
        "main_pct": 2.3,
        "super_net": 60.0,
        "large_net": 40.0,
        "mid_net": -20.0,
        "small_net": -80.0,
        "provider": provider,
    }


class BoardFundFlowCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "board-flow.sqlite3"
        cache.init_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_dc_board_code(self):
        for raw in ("BK9001", "9001", "90.BK9001", "BK9001.DC"):
            self.assertEqual(cache.normalize_board_code(raw), "BK9001")
        with self.assertRaises(ValueError):
            cache.normalize_board_code("600519")

    def test_same_taxonomy_observations_reconcile_and_preserve_provenance(self):
        cache.store_observations(self.db_path, [row()])
        cache.store_observations(
            self.db_path,
            [row(provider="tushare_moneyflow_ind_dc", main_net=100.5, close=1234.56)],
        )
        rows = cache.load_history(self.db_path, "BK9001", limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "eastmoney_clist_snapshot")
        self.assertEqual(
            rows[0]["providers"],
            ["eastmoney_clist_snapshot", "tushare_moneyflow_ind_dc"],
        )

    def test_material_cross_provider_conflict_fails_closed(self):
        cache.store_observations(self.db_path, [row()])
        cache.store_observations(
            self.db_path,
            [row(provider="tushare_moneyflow_ind_dc", main_net=3_000_000.0)],
        )
        with self.assertRaises(cache.DataConflictError):
            cache.load_history(self.db_path, "BK9001", limit=5)

    def test_empty_push2his_payload_opens_circuit(self):
        session = FakeSession({"data": {"klines": []}})
        with self.assertRaises(cache.SourceUnavailableError):
            cache.fetch_push2his_history(
                "BK9001", self.db_path, session=session, now=cache.parse_time("2026-08-13T17:00:00+08:00")
            )
        self.assertEqual(session.calls, 1)
        state = cache.get_circuit_state(self.db_path, cache.PUSH2HIS_ENDPOINT)
        self.assertEqual(state["status"], "open")

    def test_open_circuit_makes_zero_network_calls(self):
        cache.open_circuit(
            self.db_path,
            cache.PUSH2HIS_ENDPOINT,
            "RemoteDisconnected",
            now=cache.parse_time("2026-08-13T17:00:00+08:00"),
        )
        session = FakeSession({"data": {"klines": ["unused"]}})
        with self.assertRaises(cache.CircuitOpenError):
            cache.fetch_push2his_history(
                "BK9001", self.db_path, session=session, now=cache.parse_time("2026-08-13T18:00:00+08:00")
            )
        self.assertEqual(session.calls, 0)

    def test_push2his_success_parses_and_closes_circuit(self):
        line = (
            "2026-08-13,100,-20,-30,40,60,2.3,0,0,0,0,1234.56,1.2"
        )
        session = FakeSession({"data": {"name": "测试板块", "klines": [line]}})
        rows = cache.fetch_push2his_history(
            "BK9001", self.db_path, session=session, now=cache.parse_time("2026-08-13T17:00:00+08:00")
        )
        self.assertEqual(session.calls, 1)
        self.assertEqual(rows[0]["main_net"], 100.0)
        self.assertEqual(rows[0]["close"], 1234.56)
        self.assertIsNone(cache.get_circuit_state(self.db_path, cache.PUSH2HIS_ENDPOINT))

    def test_tushare_dc_mapping_keeps_dc_taxonomy(self):
        payload = {
            "code": 0,
            "data": {
                "fields": [
                    "trade_date", "content_type", "ts_code", "name", "pct_change",
                    "close", "net_amount", "net_amount_rate", "buy_elg_amount",
                    "buy_lg_amount", "buy_md_amount", "buy_sm_amount",
                ],
                "items": [[
                    "20260813", "行业", "BK9001.DC", "测试板块", 1.2,
                    1234.56, 100.0, 2.3, 60.0, 40.0, -20.0, -80.0,
                ]],
            },
        }
        rows = cache.fetch_tushare_history(
            "BK9001", "token-not-persisted", session=FakeSession(payload)
        )
        self.assertEqual(rows[0]["taxonomy"], "eastmoney_dc")
        self.assertEqual(rows[0]["board_code"], "BK9001")
        self.assertEqual(rows[0]["provider"], "tushare_moneyflow_ind_dc")

    def test_tushare_custom_url_is_used_without_returning_token(self):
        payload = {
            "code": 0,
            "data": {
                "fields": [
                    "trade_date", "content_type", "ts_code", "name", "pct_change",
                    "close", "net_amount", "net_amount_rate", "buy_elg_amount",
                    "buy_lg_amount", "buy_md_amount", "buy_sm_amount",
                ],
                "items": [[
                    "20260813", "概念", "BK9002.DC", "测试概念板块", 1.2,
                    1234.56, 100.0, 2.3, 60.0, 40.0, -20.0, -80.0,
                ]],
            },
        }
        session = FakeSession(payload)
        rows = cache.fetch_tushare_history(
            "BK9002", "secret-token", session=session,
            api_url="https://mirror.example.test",
        )
        self.assertEqual(session.last_url, "https://mirror.example.test")
        self.assertEqual(session.last_json["token"], "secret-token")
        self.assertNotIn("token", rows[0])

    def test_tushare_url_requires_https(self):
        with self.assertRaises(ValueError):
            cache.resolve_tushare_url("http://mirror.example.test")

    def test_tushare_snapshot_maps_types_and_locks_trade_date(self):
        fields = [
            "trade_date", "content_type", "ts_code", "name", "pct_change",
            "close", "net_amount", "net_amount_rate", "buy_elg_amount",
            "buy_lg_amount", "buy_md_amount", "buy_sm_amount",
        ]
        payload = {
            "code": 0,
            "data": {
                "fields": fields,
                "items": [
                    ["20260812", "概念", "BK9002.DC", "测试概念板块", 1.2,
                     1234.56, 100.0, 2.3, 60.0, 40.0, -20.0, -80.0],
                    ["20260812", "地域", "BK0175.DC", "浙江板块", 0.5,
                     2345.67, 200.0, 1.3, 80.0, 120.0, -50.0, -150.0],
                    ["20260812", "概念", "BK1643.DC", "小盘股", 0.0,
                     0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                ],
            },
        }
        invalid_rows = []
        rows = cache.fetch_tushare_snapshot(
            "2026-08-12", "secret-token", board_types=("concept", "region"),
            session=FakeSession(payload), api_url="https://mirror.example.test",
            invalid_rows=invalid_rows,
        )
        self.assertEqual({row["board_type"] for row in rows}, {"concept", "region"})
        self.assertEqual(len(rows), 2)
        self.assertEqual(invalid_rows[0]["board_code"], "BK1643")

        payload["data"]["items"][0][0] = "20260811"
        with self.assertRaises(cache.SnapshotDateError):
            cache.fetch_tushare_snapshot(
                "2026-08-12", "secret-token", board_types=("concept",),
                session=FakeSession(payload), api_url="https://mirror.example.test",
            )

    def test_tushare_snapshot_applies_current_type_before_scope_filter(self):
        payload = {
            "code": 0,
            "data": {
                "fields": [
                    "trade_date", "content_type", "ts_code", "name", "pct_change",
                    "close", "net_amount", "net_amount_rate", "buy_elg_amount",
                    "buy_lg_amount", "buy_md_amount", "buy_sm_amount",
                ],
                "items": [[
                    "20260211", "行业", "BK0425.DC", "工程建设", 1.2,
                    1234.56, 100.0, 2.3, 60.0, 40.0, -20.0, -80.0,
                ]],
            },
        }
        rows = cache.fetch_tushare_snapshot(
            "2026-02-11", "secret-token", board_types=("concept",),
            board_type_overrides={"BK0425": "concept"},
            session=FakeSession(payload), api_url="https://mirror.example.test",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["board_type"], "concept")

    def test_stored_types_follow_latest_clist_catalog(self):
        historical = row(provider="tushare_moneyflow_ind_dc")
        historical.update({
            "board_code": "BK0425", "board_type": "industry",
            "name": "工程建设", "trade_date": "2026-08-12",
        })
        current = dict(historical, provider="eastmoney_clist_snapshot",
                       board_type="concept", trade_date="2026-08-13")
        cache.store_observations(self.db_path, [historical, current])
        self.assertEqual(cache.normalize_stored_board_types(self.db_path), 1)
        with cache._connect(self.db_path) as conn:
            types = {
                item[0] for item in conn.execute(
                    "SELECT DISTINCT board_type FROM observations WHERE board_code='BK0425'"
                )
            }
        self.assertEqual(types, {"concept"})

    def test_anchor_audit_accepts_small_revision_but_rejects_sign_flip(self):
        local = [row(provider="eastmoney_push2his", main_net=1_068_534_528.0)]
        remote = [dict(local[0], provider="tushare_moneyflow_ind_dc",
                       main_net=1_066_511_360.0)]
        report = cache.audit_anchor_rows(local, remote, minimum_overlap=1)
        self.assertTrue(report["passed"])
        self.assertEqual(report["sign_flips"], 0)
        self.assertEqual(report["strict_conflict_days"], 1)

        remote[0]["main_net"] = -1_066_511_360.0
        report = cache.audit_anchor_rows(local, remote, minimum_overlap=1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["sign_flips"], 1)

    def test_missing_only_filter_preserves_existing_provider(self):
        cache.store_observations(self.db_path, [row()])
        candidate = row(provider="tushare_moneyflow_ind_dc")
        candidate["trade_date"] = "2026-08-13"
        new_row = dict(candidate, board_code="BK9002", board_type="concept")
        missing = cache.filter_missing_observations(
            self.db_path, [candidate, new_row]
        )
        self.assertEqual([item["board_code"] for item in missing], ["BK9002"])

    def test_backfill_is_resumable_and_stores_only_missing_rows(self):
        calendar_rows = []
        for day in ("2026-08-11", "2026-08-12"):
            item = row()
            item["trade_date"] = day
            calendar_rows.append(item)
        cache.store_observations(self.db_path, calendar_rows)

        def snapshot(day, *args, **kwargs):
            concept = row(provider="tushare_moneyflow_ind_dc")
            concept.update({
                "board_code": "BK9002", "board_type": "concept",
                "name": "测试概念板块", "trade_date": day,
            })
            region = dict(concept, board_code="BK0175", board_type="region",
                          name="浙江板块")
            return [concept, region]

        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(cache, "fetch_tushare_snapshot", side_effect=snapshot):
            result = cache.backfill_tushare_history(
                self.db_path, "secret-token", start_date="2026-08-11",
                end_date="2026-08-12", minimum_interval=0,
            )
        self.assertEqual(result["stored_rows"], 4)
        self.assertEqual(result["completed_dates"], 2)

        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(cache, "fetch_tushare_snapshot",
                               side_effect=AssertionError("不应再次请求已完成日期")):
            resumed = cache.backfill_tushare_history(
                self.db_path, "secret-token", start_date="2026-08-11",
                end_date="2026-08-12", minimum_interval=0,
            )
        self.assertEqual(resumed["checkpoint_skipped"], 2)
        self.assertEqual(resumed["stored_rows"], 0)

    def test_calendar_unions_anchor_trade_dates(self):
        histories = {
            "BK9002": [{"trade_date": "2025-03-03"}, {"trade_date": "2025-03-04"}],
            "BK9001": [{"trade_date": "2025-03-04"}, {"trade_date": "2025-03-05"}],
        }
        with mock.patch.object(
            cache, "fetch_tushare_history",
            side_effect=lambda code, *a, **k: histories[code],
        ):
            dates = cache.fetch_tushare_calendar(
                "secret-token", anchors=("BK9002", "BK9001"),
                start_date="2025-03-01", end_date="2025-03-31",
            )
        self.assertEqual(dates, ["2025-03-03", "2025-03-04", "2025-03-05"])

    def test_backfill_audit_window_can_differ_from_backfill_window(self):
        """回填窗口在本地库之外时，Gate 必须仍跑在有重叠的窗口上，否则重叠为 0 必然失败。"""
        seen = {}

        def audit(db_path, token, **kwargs):
            seen.update(kwargs)
            return {"passed": True, "anchors": {}}

        with mock.patch.object(cache, "audit_tushare_anchors", side_effect=audit), \
             mock.patch.object(cache, "fetch_tushare_calendar", return_value=[]), \
             mock.patch.object(cache, "fetch_tushare_snapshot", return_value=[]):
            with self.assertRaises(cache.InsufficientHistoryError):
                cache.backfill_tushare_history(
                    self.db_path, "secret-token",
                    start_date="2025-02-26", end_date="2026-02-10",
                    audit_start_date="2026-02-11", audit_end_date="2026-08-13",
                    minimum_interval=0,
                )
        self.assertEqual(seen["start_date"], "2026-02-11")
        self.assertEqual(seen["end_date"], "2026-08-13")

    def test_anchor_calendar_backfills_dates_absent_from_local_db(self):
        """本地库没有这些交易日，calendar='anchors' 时应改由源端日历驱动。"""
        def snapshot(day, *args, **kwargs):
            concept = row(provider="tushare_moneyflow_ind_dc")
            concept.update({
                "board_code": "BK9002", "board_type": "concept",
                "name": "测试概念板块", "trade_date": day,
            })
            region = dict(concept, board_code="BK0175", board_type="region",
                          name="浙江板块")
            return [concept, region]

        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(cache, "fetch_tushare_calendar",
                               return_value=["2025-03-03", "2025-03-04"]), \
             mock.patch.object(cache, "fetch_tushare_snapshot", side_effect=snapshot):
            result = cache.backfill_tushare_history(
                self.db_path, "secret-token",
                start_date="2025-02-26", end_date="2026-02-10",
                audit_start_date="2026-02-11", audit_end_date="2026-08-13",
                calendar="anchors", minimum_interval=0,
            )
        self.assertEqual(result["trade_dates"], 2)
        self.assertEqual(result["stored_rows"], 4)
        self.assertEqual(result["calendar"], "anchors")
        stored = cache.load_history(self.db_path, "BK9002", limit=0)
        self.assertEqual([item["trade_date"] for item in stored],
                         ["2025-03-03", "2025-03-04"])

    def test_anchor_calendar_dates_outside_window_are_dropped(self):
        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(
                 cache, "fetch_tushare_calendar",
                 return_value=["2025-02-25", "2025-03-03", "2026-02-11"]), \
             mock.patch.object(cache, "fetch_tushare_snapshot",
                               side_effect=lambda day, *a, **k: []):
            with self.assertRaises(cache.SourceUnavailableError):
                cache.backfill_tushare_history(
                    self.db_path, "secret-token",
                    start_date="2025-02-26", end_date="2026-02-10",
                    calendar="anchors", minimum_interval=0,
                )

    def _partial_snapshot(self, broken_day):
        """broken_day 只回行业，模拟源端当日截面残缺（实测 2025-09-23 即如此）。"""
        def snapshot(day, *args, **kwargs):
            industry = row(provider="tushare_moneyflow_ind_dc")
            industry["trade_date"] = day
            if day == broken_day:
                return [industry]
            concept = dict(industry, board_code="BK9002", board_type="concept",
                           name="测试概念板块")
            region = dict(industry, board_code="BK0175", board_type="region",
                          name="浙江板块")
            return [industry, concept, region]
        return snapshot

    def test_incomplete_cross_section_aborts_by_default(self):
        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(cache, "fetch_tushare_calendar",
                               return_value=["2025-09-22", "2025-09-23"]), \
             mock.patch.object(cache, "fetch_tushare_snapshot",
                               side_effect=self._partial_snapshot("2025-09-23")):
            with self.assertRaises(cache.SourceUnavailableError):
                cache.backfill_tushare_history(
                    self.db_path, "secret-token", start_date="2025-09-22",
                    end_date="2025-09-23", calendar="anchors",
                    board_types=("concept", "region", "industry"),
                    minimum_interval=0,
                )

    def test_incomplete_cross_section_is_recorded_not_stored_when_skipped(self):
        common = dict(
            board_types=("concept", "region", "industry"), calendar="anchors",
            minimum_interval=0, skip_incomplete=True,
        )
        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(cache, "fetch_tushare_calendar",
                               return_value=["2025-09-22", "2025-09-23", "2025-09-24"]), \
             mock.patch.object(cache, "fetch_tushare_snapshot",
                               side_effect=self._partial_snapshot("2025-09-23")):
            result = cache.backfill_tushare_history(
                self.db_path, "secret-token", start_date="2025-09-22",
                end_date="2025-09-24", **common,
            )
        self.assertEqual(result["completed_dates"], 3)
        self.assertEqual([item["trade_date"] for item in result["incomplete_dates"]],
                         ["2025-09-23"])
        self.assertEqual(result["incomplete_dates"][0]["counts"]["concept"], 0)
        # 残缺日一行都不许落盘：只存了 09-22 与 09-24 各 3 行
        self.assertEqual(result["stored_rows"], 6)
        stored = {item["trade_date"] for item in
                  cache.load_history(self.db_path, "BK9001", limit=0)}
        self.assertEqual(stored, {"2025-09-22", "2025-09-24"})

        # 续跑时残缺日已有检查点，不得再请求
        with mock.patch.object(cache, "audit_tushare_anchors",
                               return_value={"passed": True, "anchors": {}}), \
             mock.patch.object(cache, "fetch_tushare_calendar",
                               return_value=["2025-09-22", "2025-09-23", "2025-09-24"]), \
             mock.patch.object(cache, "fetch_tushare_snapshot",
                               side_effect=AssertionError("不应再次请求已完成日期")):
            resumed = cache.backfill_tushare_history(
                self.db_path, "secret-token", start_date="2025-09-22",
                end_date="2025-09-24", **common,
            )
        self.assertEqual(resumed["checkpoint_skipped"], 3)

    def test_unknown_calendar_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            cache.backfill_tushare_history(
                self.db_path, "secret-token", start_date="2025-02-26",
                end_date="2026-02-10", calendar="guess",
            )

    def test_import_push2his_jsonl_is_idempotent(self):
        source = Path(self.tmp.name) / "boards.jsonl"
        source.write_text(json.dumps({
            "code": "BK9001",
            "name": "通信网络设备及器件",
            "rows": [
                ["2026-08-12", 100.0, 2.3, 1234.56, 1.2],
                ["2026-08-13", 200.0, 3.3, 1240.00, 0.44],
            ],
        }, ensure_ascii=False) + "\n")
        first = cache.import_push2his_jsonl(self.db_path, source)
        second = cache.import_push2his_jsonl(self.db_path, source)
        self.assertEqual(first["lines"], 1)
        self.assertEqual(first["boards"], 1)
        self.assertEqual(first["observations"], 2)
        self.assertEqual(first["duplicate_observations"], 0)
        self.assertEqual(len(first["source_sha256"]), 64)
        self.assertEqual(second, first)
        rows = cache.load_history(self.db_path, "BK9001", limit=5)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["provider"], "eastmoney_push2his")
        self.assertEqual(rows[-1]["main_net"], 200.0)

    def test_import_deduplicates_identical_board_lines(self):
        source = Path(self.tmp.name) / "duplicate.jsonl"
        item = {
            "code": "BK9001", "name": "测试板块",
            "rows": [["2026-08-13", 200.0, 3.3, 1240.00, 0.44]],
        }
        source.write_text(
            json.dumps(item, ensure_ascii=False) + "\n"
            + json.dumps(item, ensure_ascii=False) + "\n"
        )
        result = cache.import_push2his_jsonl(self.db_path, source)
        self.assertEqual(result["lines"], 2)
        self.assertEqual(result["boards"], 1)
        self.assertEqual(result["observations"], 1)
        self.assertEqual(result["duplicate_observations"], 1)

    def test_snapshot_rejects_wrong_trade_date(self):
        payload = {
            "data": {
                "diff": [{
                    "f2": 1234.56, "f3": 1.2, "f12": "BK9001", "f14": "测试板块",
                    "f62": 100.0, "f66": 60.0, "f72": 40.0, "f78": -20.0,
                    "f84": -80.0, "f184": 2.3, "f297": 20260812,
                }]
            }
        }
        with self.assertRaises(cache.SnapshotDateError):
            cache.parse_clist_snapshot(payload, "industry", "2026-08-13")

    def test_snapshot_fetch_paginates_to_reported_total(self):
        def item(code):
            return {
                "f2": 1234.56, "f3": 1.2, "f12": code, "f14": code,
                "f62": 100.0, "f66": 60.0, "f72": 40.0, "f78": -20.0,
                "f84": -80.0, "f184": 2.3, "f297": 20260813,
            }
        session = PagedSession({
            1: {"data": {"total": 3, "diff": [item("BK1001"), item("BK1002")]}},
            2: {"data": {"total": 3, "diff": [item("BK1003")]}},
        })
        rows = cache.fetch_clist_snapshot(
            "industry", "2026-08-13", session=session,
            now=cache.parse_time("2026-08-13T17:00:00+08:00"), page_size=2,
        )
        self.assertEqual(session.calls, 2)
        self.assertEqual([x["board_code"] for x in rows], ["BK1001", "BK1002", "BK1003"])

    def test_snapshot_fetch_rejects_incomplete_pagination(self):
        item = {
            "f2": 1234.56, "f3": 1.2, "f12": "BK1001", "f14": "测试",
            "f62": 100.0, "f66": 60.0, "f72": 40.0, "f78": -20.0,
            "f84": -80.0, "f184": 2.3, "f297": 20260813,
        }
        session = PagedSession({
            1: {"data": {"total": 3, "diff": [item]}},
            2: {"data": {"total": 3, "diff": []}},
        })
        with self.assertRaises(cache.SourceUnavailableError):
            cache.fetch_clist_snapshot(
                "industry", "2026-08-13", session=session,
                now=cache.parse_time("2026-08-13T17:00:00+08:00"), page_size=2,
            )

    def test_complete_cache_path_never_calls_remote(self):
        rows = []
        for day in range(1, 4):
            item = row()
            item["trade_date"] = f"2026-08-{day:02d}"
            rows.append(item)
        cache.store_observations(self.db_path, rows)
        got = cache.board_fund_flow_daily(
            "BK9001", lmt=3, db_path=self.db_path, provider="cache"
        )
        self.assertEqual([x["trade_date"] for x in got], [
            "2026-08-01", "2026-08-02", "2026-08-03"
        ])

    def test_cache_end_date_gate_rejects_stale_120_rows(self):
        rows = []
        for day in range(1, 121):
            item = row()
            item["trade_date"] = (
                cache.parse_time("2026-01-01T00:00:00+08:00").date()
                + cache.timedelta(days=day - 1)
            ).isoformat()
            rows.append(item)
        cache.store_observations(self.db_path, rows)
        with self.assertRaises(cache.InsufficientHistoryError):
            cache.board_fund_flow_daily(
                "BK9001", lmt=120, db_path=self.db_path, provider="cache",
                end_date="2026-05-02",
            )

    def test_historical_end_date_filters_newer_cache_rows(self):
        rows = []
        for day in range(1, 5):
            item = row()
            item["trade_date"] = f"2026-08-{day:02d}"
            rows.append(item)
        cache.store_observations(self.db_path, rows)
        got = cache.board_fund_flow_daily(
            "BK9001", lmt=2, db_path=self.db_path, provider="cache",
            end_date="2026-08-03",
        )
        self.assertEqual(
            [item["trade_date"] for item in got], ["2026-08-02", "2026-08-03"]
        )


if __name__ == "__main__":
    unittest.main()
