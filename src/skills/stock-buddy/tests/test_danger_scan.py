import importlib.util
import json
import os
import types
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "danger_scan.py"
SPEC = importlib.util.spec_from_file_location("danger_scan", MODULE_PATH)
danger_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(danger_scan)


class ComplianceNoticeTest(unittest.TestCase):
    def test_meta_uses_anchor_date_and_dynamic_sources(self):
        sources = ["东方财富（解禁/两融）", "财联社电报"]
        meta = danger_scan.build_meta(
            "2026-08-12", sources, executed_at="2026-08-13 10:00"
        )
        notice = meta["disclaimer"]
        for term in (
            "研究与决策支持",
            "非持牌证券投资咨询",
            "不构成投资建议",
            "不代下单",
            "风险自担",
            "以本次运行输出为准",
        ):
            self.assertIn(term, notice)
        self.assertEqual(meta["scan_anchor_date"], "2026-08-12")
        self.assertNotIn("trade_date", meta)
        self.assertEqual(meta["data_sources"], sources)
        self.assertIn("东方财富（解禁/两融）、财联社电报", notice)
        self.assertIn("数据日：无单一数据日", notice)
        self.assertIn("风险扫描基准日 2026-08-12", notice)
        self.assertIn("data_caliber 与正文", notice)
        self.assertEqual(meta["executed_at"], "2026-08-13 10:00")
        self.assertIn("2026-06-13", meta["data_caliber"]["公告_60天"])
        self.assertIn("2026-05-14", meta["data_caliber"]["互动易_90天"])
        self.assertIn("子项 t", meta["data_caliber"]["新闻"])
        self.assertIn("上游输出", meta["data_caliber"]["deep_news"])

    def test_runtime_sources_include_only_successes_in_stable_order(self):
        stocks = {
            "600323": {
                "解禁_未来90天": [],
                "公告_60天": "未跑(timeout)",
                "两融": "未跑(timeout)",
                "互动易_90天": {"条数": 0, "未回复": 0, "样例": []},
                "新闻": {
                    "状态": "已检索", "来源": "财联社电报", "公司": "600323",
                },
            },
            "000005": {
                "解禁_未来90天": "未跑(timeout)",
                "公告_60天": "未跑(timeout)",
                "两融": "未跑(timeout)",
                "互动易_90天": "未跑(请求失败)",
                "新闻": {
                    "状态": "来源不可用", "来源": "财联社电报", "公司": "平安银行",
                    "公司名来源": "腾讯行情",
                },
            },
        }
        deep_news_runs = {
            "600323": {"状态": "已运行", "来源": "同花顺问财 news-search"},
            "000005": {"状态": "运行失败", "来源": "同花顺问财 news-search"},
        }
        self.assertEqual(danger_scan.collect_runtime_sources(stocks, deep_news_runs), [
            "东方财富（解禁/两融）",
            "巨潮资讯（公告/互动易）",
            "腾讯行情（公司名）",
            "财联社电报",
            "同花顺问财 news-search",
        ])

    def test_runtime_sources_exclude_all_failed_sources(self):
        stocks = {"600323": {
            "解禁_未来90天": "未跑(timeout)",
            "公告_60天": "未跑(timeout)",
            "两融": "未跑(timeout)",
            "互动易_90天": "未跑(请求失败)",
            "新闻": {
                "状态": "来源不可用", "来源": "财联社电报", "公司": "600323",
            },
        }}
        deep_news_runs = {
            "600323": {"状态": "运行失败", "来源": "同花顺问财 news-search"},
        }
        self.assertEqual(danger_scan.collect_runtime_sources(stocks, deep_news_runs), [])


class FakeResponse:
    def __init__(self, *, content=b"", payload=None, status_code=200):
        self.content = content
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class DangerScanNewsLayersTest(unittest.TestCase):
    def tearDown(self):
        if hasattr(danger_scan.cls_telegraph, "cache_clear"):
            danger_scan.cls_telegraph.cache_clear()

    def test_stock_name_uses_tencent_quote(self):
        response = FakeResponse(content='v_sh600323="1~招商银行~600323~";'.encode("gbk"))
        with patch.object(danger_scan.requests, "get", return_value=response):
            self.assertEqual(danger_scan.stock_name("600323"), "招商银行")

    def test_cls_telegraph_parses_rows(self):
        response = FakeResponse(payload={
            "errno": 0,
            "data": {"roll_data": [{
                "ctime": 1786300800,
                "title": "招商银行发布公告",
                "brief": "",
                "content": "招商银行发布公告",
            }]},
        })
        danger_scan.cls_telegraph.cache_clear()
        with patch.object(danger_scan.requests, "get", return_value=response) as get:
            rows = danger_scan.cls_telegraph(100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "招商银行发布公告")
        self.assertIn("rn=50", get.call_args.args[0])

    def test_breaking_news_matches_company_name(self):
        rows = [
            {"time": "2026-08-10 08:00:00", "title": "招商银行发布公告", "content": ""},
            {"time": "2026-08-10 07:00:00", "title": "市场早知道", "content": ""},
        ]
        with patch.object(danger_scan, "stock_name", return_value="招商银行"), \
             patch.object(danger_scan, "cls_telegraph", return_value=rows):
            result = danger_scan.breaking_news("600323")
        self.assertEqual(result["状态"], "已检索")
        self.assertEqual(result["公司"], "招商银行")
        self.assertEqual(len(result["命中"]), 1)
        self.assertEqual(result["命中"][0]["t"], "2026-08-10 08:00:00")

    def test_breaking_news_zero_match_is_scoped_not_no_news(self):
        rows = [{"time": "2026-08-10 07:00:00", "title": "市场早知道", "content": ""}]
        with patch.object(danger_scan, "stock_name", return_value="招商银行"), \
             patch.object(danger_scan, "cls_telegraph", return_value=rows):
            result = danger_scan.breaking_news("600323")
        self.assertEqual(result["状态"], "已检索")
        self.assertEqual(result["命中"], [])
        self.assertIn("不等于无新闻", result["说明"])

    def test_breaking_news_unavailable_is_explicit(self):
        with patch.object(danger_scan, "stock_name", return_value="招商银行"), \
             patch.object(danger_scan, "cls_telegraph", side_effect=RuntimeError("blocked")):
            result = danger_scan.breaking_news("600323")
        self.assertEqual(result["状态"], "来源不可用")
        self.assertEqual(result["命中"], [])
        self.assertIn("未形成媒体新闻结论", result["说明"])

    def test_hard_risk_scan_survives_news_failure(self):
        with patch.object(danger_scan, "lockup", return_value=[]), \
             patch.object(danger_scan, "announcements", return_value=[{
                 "date": "2026-08-09", "title": "关于监管措施的公告", "flags": ["监管"]
             }]), \
             patch.object(danger_scan, "margin", return_value={"rows": [], "trend": None}), \
             patch.object(danger_scan, "irm", return_value=[]), \
             patch.object(danger_scan, "breaking_news", return_value={
                 "状态": "来源不可用", "命中": [], "说明": "未形成媒体新闻结论"
             }):
            result = danger_scan.scan_stock("600323", "2026-08-10")
        self.assertTrue(any("公告[监管]" in text for text in result["红字信号"]))
        self.assertEqual(result["新闻"]["状态"], "来源不可用")

    def test_deep_news_is_opt_in_and_requires_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = danger_scan.run_deep_news("招商银行")
        self.assertEqual(result["状态"], "未运行")
        self.assertIn("IWENCAI_API_KEY", result["原因"])

    def test_deep_news_requires_configured_cli(self):
        with patch.dict(os.environ, {"IWENCAI_API_KEY": "test-key"}, clear=True):
            result = danger_scan.run_deep_news("招商银行")
        self.assertEqual(result["状态"], "未运行")
        self.assertIn("ASM_NEWS_SEARCH_CLI", result["原因"])

    def test_deep_news_streams_installed_cli(self):
        completed = types.SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"IWENCAI_API_KEY": "test-key",
                                     "ASM_NEWS_SEARCH_CLI": "/tmp/news-search.py"}, clear=False), \
             patch.object(danger_scan.os.path, "isfile", return_value=True), \
             patch("builtins.print"), \
             patch.object(danger_scan.subprocess, "run", return_value=completed) as run:
            result = danger_scan.run_deep_news("招商银行")
        self.assertEqual(result["状态"], "已运行")
        self.assertIn("招商银行", run.call_args.args[0])

    def test_main_builds_meta_after_stock_and_deep_news(self):
        stock_result = {
            "新闻": {"公司": "招商银行"},
            "红字信号": [],
        }
        deep_result = {"状态": "已运行", "来源": "同花顺问财 news-search"}
        sources = ["同花顺问财 news-search"]
        expected_runs = {
            "600323": {"query": "招商银行", **deep_result},
        }
        with patch.object(
            danger_scan.sys, "argv",
            ["danger_scan.py", "600323", "--date", "2026-08-12", "--deep-news"],
        ), patch.object(
            danger_scan, "scan_stock", return_value=stock_result
        ), patch.object(
            danger_scan, "run_deep_news", return_value=deep_result
        ), patch.object(
            danger_scan, "collect_runtime_sources", return_value=sources
        ) as collect, patch.object(
            danger_scan, "build_meta", return_value={"scan_anchor_date": "2026-08-12"}
        ) as build_meta, patch("builtins.print") as output:
            danger_scan.main()

        collect.assert_called_once_with({"600323": stock_result}, expected_runs)
        build_meta.assert_called_once_with("2026-08-12", sources)
        rendered = next(
            call.args[0].strip() for call in output.call_args_list
            if call.args and isinstance(call.args[0], str)
            and call.args[0].strip().startswith("{")
        )
        self.assertEqual(list(json.loads(rendered)), ["meta", "stocks"])


if __name__ == "__main__":
    unittest.main()
