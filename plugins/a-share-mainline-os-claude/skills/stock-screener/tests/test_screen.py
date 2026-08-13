# SENTINEL_TEST_C1
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import screen


class ConfigIsolationMixin:
    """把配置解析完全隔离到临时目录：清 env、清缓存、指定假的默认配置位置。

    没有这层隔离，测试会读到开发者本机 ~/.config/a-share-mainline/config.json，
    在 CI 或陌生用户机器上结论相反——这正是本次开源改造要根除的那类耦合。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._saved_env = {k: os.environ.get(k)
                           for k in (screen.CONFIG_ENV, screen.VAULT_ENV)}
        for k in self._saved_env:
            os.environ.pop(k, None)
        self._saved_default = screen.DEFAULT_CONFIG_PATH
        screen.DEFAULT_CONFIG_PATH = os.path.join(self.tmp, "absent", "config.json")
        screen.reset_config_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        screen.DEFAULT_CONFIG_PATH = self._saved_default
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        screen.reset_config_cache()
        self._tmp.cleanup()

    def write_config(self, payload, name="config.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(payload, str):
                fh.write(payload)
            else:
                json.dump(payload, fh, ensure_ascii=False)
        os.environ[screen.CONFIG_ENV] = path
        screen.reset_config_cache()
        return path


class TestConfigResolution(ConfigIsolationMixin, unittest.TestCase):
    def test_config_env_is_read(self):
        self.write_config({"vault_root": "/tmp/vault-a"})
        self.assertEqual(screen.vault_root(), "/tmp/vault-a")

    def test_default_path_is_read_when_env_absent(self):
        path = os.path.join(self.tmp, "default", "config.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"vault_root": "/tmp/vault-default"}, fh)
        screen.DEFAULT_CONFIG_PATH = path
        screen.reset_config_cache()
        self.assertEqual(screen.vault_root(), "/tmp/vault-default")

    def test_vault_env_overrides_config_file(self):
        self.write_config({"vault_root": "/tmp/vault-from-file"})
        os.environ[screen.VAULT_ENV] = "/tmp/vault-from-env"
        screen.reset_config_cache()
        self.assertEqual(screen.vault_root(), "/tmp/vault-from-env")

    def test_user_home_is_expanded(self):
        self.write_config({"vault_root": "~/some-vault"})
        self.assertEqual(screen.vault_root(),
                         os.path.expanduser("~/some-vault"))

    def test_missing_config_fails_closed_with_guidance(self):
        with self.assertRaises(screen.ScreenError) as ctx:
            screen.vault_root()
        msg = str(ctx.exception)
        self.assertIn("vault_root", msg)
        self.assertIn(screen.CONFIG_ENV, msg)
        self.assertIn(screen.VAULT_ENV, msg)

    def test_config_present_but_vault_root_missing_fails_closed(self):
        self.write_config({"screener_out_dir": "/tmp/out"})
        with self.assertRaises(screen.ScreenError):
            screen.vault_root()

    def test_malformed_config_raises_not_silently_empty(self):
        self.write_config("{not json", name="broken.json")
        with self.assertRaises(screen.ScreenError) as ctx:
            screen.load_config()
        self.assertIn("解析失败", str(ctx.exception))

    def test_config_env_pointing_nowhere_fails_closed(self):
        os.environ[screen.CONFIG_ENV] = os.path.join(self.tmp, "nope.json")
        screen.reset_config_cache()
        with self.assertRaises(screen.ScreenError) as ctx:
            screen.load_config()
        self.assertIn("不存在", str(ctx.exception))


class TestDerivedPaths(ConfigIsolationMixin, unittest.TestCase):
    def test_holdings_path_derives_from_vault(self):
        self.write_config({"vault_root": "/tmp/v"})
        self.assertEqual(screen.holdings_path(),
                         os.path.join("/tmp/v", "03-持仓跟踪", "持仓.md"))

    def test_holdings_path_can_be_overridden(self):
        self.write_config({"vault_root": "/tmp/v",
                           "holdings_path": "/tmp/elsewhere/持仓.md"})
        self.assertEqual(screen.holdings_path(), "/tmp/elsewhere/持仓.md")

    def test_out_dir_defaults_under_vault(self):
        self.write_config({"vault_root": "/tmp/v"})
        self.assertEqual(screen.out_dir(),
                         os.path.join("/tmp/v", "outputs", "screener"))

    def test_out_dir_can_be_overridden(self):
        self.write_config({"vault_root": "/tmp/v",
                           "screener_out_dir": "~/custom-out"})
        self.assertEqual(screen.out_dir(), os.path.expanduser("~/custom-out"))

    def test_cli_path_autodiscovers_sibling_skill(self):
        """问财 CLI 默认按 skill 目录相对定位，Claude/Codex 两侧都能自解析。"""
        expected = os.path.join(screen.SKILLS_ROOT, "hithink-market-query",
                                "scripts", "cli.py")
        self.assertEqual(screen.cli_path(), expected)

    def test_cli_path_can_be_overridden(self):
        self.write_config({"vault_root": "/tmp/v",
                           "wencai_cli": "/tmp/cli.py"})
        self.assertEqual(screen.cli_path(), "/tmp/cli.py")

    def test_ifind_helper_absent_when_unconfigured(self):
        self.write_config({"vault_root": "/tmp/v"})
        self.assertIsNone(screen.ifind_helper_path())

    def test_ifind_helper_from_config(self):
        self.write_config({"vault_root": "/tmp/v",
                           "ifind_evidence_helper": "/tmp/h.mjs"})
        self.assertEqual(screen.ifind_helper_path(), "/tmp/h.mjs")

    def test_ifind_evidence_unavailable_when_helper_unconfigured(self):
        """可选适配器未配置时必须优雅降级，而不是抛栈或跑到 realpath(None)。"""
        self.write_config({"vault_root": "/tmp/v"})
        ev = screen.attach_ifind_evidence([{"code": "600323.SH", "name": "招商银行"}])
        self.assertFalse(ev["available"])
        self.assertIn("未配置", ev["reason"])


class TestNoPersonalPathsInSource(unittest.TestCase):
    """回归护栏：源码里不得再出现作者私人路径，否则开源改造被悄悄改回去。"""

    FORBIDDEN = ("tao" + "sheng", "iCloud~md~" + "obsidian",
                 "com~apple~" + "CloudDocs", "08-AI-" + "Workplace")

    def test_screen_py_has_no_personal_paths(self):
        src = os.path.join(os.path.dirname(__file__), "..", "scripts", "screen.py")
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        for token in self.FORBIDDEN:
            self.assertNotIn(token, text, "screen.py 残留私人路径片段：%s" % token)


class TestParseValue(unittest.TestCase):
    def test_float_passthrough(self):
        self.assertEqual(screen.parse_value(31.5), 31.5)

    def test_int_to_float(self):
        self.assertEqual(screen.parse_value(5), 5.0)

    def test_yi_unit(self):
        self.assertAlmostEqual(screen.parse_value("2.13亿"), 213000000.0)

    def test_wan_unit(self):
        self.assertAlmostEqual(screen.parse_value("1234万"), 12340000.0)

    def test_liutong_yi(self):
        self.assertAlmostEqual(screen.parse_value("2843.71亿"), 284371000000.0)

    def test_percent_keeps_magnitude(self):
        self.assertAlmostEqual(screen.parse_value("1.84%"), 1.84)

    def test_thousands_comma(self):
        self.assertAlmostEqual(screen.parse_value("1,234.5"), 1234.5)

    def test_none(self):
        self.assertIsNone(screen.parse_value(None))

    def test_empty_string(self):
        self.assertIsNone(screen.parse_value(""))

    def test_garbage(self):
        self.assertIsNone(screen.parse_value("--"))

    def test_scientific_notation_string(self):
        self.assertAlmostEqual(screen.parse_value("2.5356130847E8"), 253561308.47, places=2)

    def test_raw_decimal_string(self):
        self.assertAlmostEqual(screen.parse_value("12003367200.000"), 12003367200.0)

    def test_negative_number_string(self):
        self.assertAlmostEqual(screen.parse_value("-12126646"), -12126646.0)

    def test_bool_is_not_numeric(self):
        self.assertIsNone(screen.parse_value(True))


class TestNormalizeFieldName(unittest.TestCase):
    def test_strip_at_prefix(self):
        self.assertEqual(screen.normalize_field_name("@涨跌幅"), "涨跌幅")

    def test_strip_date_suffix_and_infer_market_date_frequency(self):
        self.assertEqual(screen.normalize_field_name("最新价[20260623]"), "最新价")
        pool = [
            {
                "换手率[20260714]": "1.2%",
                "成交额[20260714]": "1亿",
                "净资产收益率roe(加权,公布值)[20260331]": "10%",
            },
            {
                "最新价[20260715]": "10.2",
                "总市值[20260715]": "100亿",
            },
        ]
        # 20260714 与 20260715 同频时取较晚日期；财务报告期不参与。
        self.assertEqual(screen.infer_market_data_date(pool), "2026-07-15")

    def test_strip_sub_marker(self):
        self.assertEqual(screen.normalize_field_name("涨跌幅:前复权"), "涨跌幅")

    def test_strip_all_layers(self):
        self.assertEqual(
            screen.normalize_field_name("近5日区间涨跌幅:前复权[20260619-20260623]"),
            "近5日区间涨跌幅",
        )

    def test_plain_unchanged(self):
        self.assertEqual(screen.normalize_field_name("股票代码"), "股票代码")

    def test_at_quality_field(self):
        self.assertEqual(screen.normalize_field_name("@净资产收益率"), "净资产收益率")

    def test_validated_quality_aliases(self):
        self.assertEqual(
            screen.normalize_field_name("归属于母公司所有者的净利润同比增长率[20260331]"),
            "归母净利润同比增长率",
        )
        self.assertEqual(
            screen.normalize_field_name("归属母公司股东的净利润(同比增长率)[20260331]"),
            "归母净利润同比增长率",
        )
        self.assertEqual(
            screen.normalize_field_name("净资产收益率roe(加权,公布值)[20260331]"),
            "净资产收益率",
        )
        self.assertIsNone(screen.infer_market_data_date([
            {
                "净资产收益率roe(加权,公布值)[20260331]": "10%",
                "成交额[20260230]": "1亿",
            }
        ]))


class TestPickValue(unittest.TestCase):
    def test_prefer_non_null_variant(self):
        rec = {"@净资产收益率": None, "净资产收益率": 19.87}
        self.assertAlmostEqual(screen.pick_value(rec, "净资产收益率"), 19.87)

    def test_unit_string_parsed(self):
        rec = {"@主力净流入": "2.13亿", "主力净流入": "2.13亿"}
        self.assertAlmostEqual(screen.pick_value(rec, "主力净流入"), 213000000.0)

    def test_single_at_variant(self):
        rec = {"@市盈率": 22.23}
        self.assertAlmostEqual(screen.pick_value(rec, "市盈率"), 22.23)

    def test_no_match_returns_none(self):
        rec = {"@涨跌幅": 1.84}
        self.assertIsNone(screen.pick_value(rec, "换手率"))

    def test_all_none_returns_none(self):
        rec = {"@净利润同比增长率": None, "净利润同比增长率": None}
        self.assertIsNone(screen.pick_value(rec, "净利润同比增长率"))

    def test_profit_amount_label_is_not_treated_as_growth_rate(self):
        rec = {"归属于母公司所有者的净利润同比增长[20260331]": 29963266.73}
        self.assertIsNone(screen.pick_value(rec, "归母净利润同比增长率"))


class TestRankPercentile(unittest.TestCase):
    def test_ascending(self):
        self.assertEqual(
            screen.rank_percentile([10, 20, 30, 40, 50]), [0.0, 0.25, 0.5, 0.75, 1.0]
        )

    def test_descending_input(self):
        self.assertEqual(
            screen.rank_percentile([50, 40, 30, 20, 10]), [1.0, 0.75, 0.5, 0.25, 0.0]
        )

    def test_none_skipped(self):
        self.assertEqual(screen.rank_percentile([10, None, 30]), [0.0, None, 1.0])

    def test_ties_average(self):
        self.assertEqual(screen.rank_percentile([10, 10, 20]), [0.25, 0.25, 1.0])

    def test_single_is_neutral(self):
        self.assertEqual(screen.rank_percentile([42]), [0.5])

    def test_all_none(self):
        self.assertEqual(screen.rank_percentile([None, None]), [None, None])

    def test_empty(self):
        self.assertEqual(screen.rank_percentile([]), [])


class TestScore(unittest.TestCase):
    def _pool(self):
        return [
            {"code": "A", "x": 10, "y": 100},
            {"code": "B", "x": 20, "y": 50},
            {"code": "C", "x": 30, "y": None},
        ]

    def test_weighted_score_and_gap_renorm(self):
        fields = [("x", 0.5, "higher"), ("y", 0.5, "lower")]
        res = screen.score(self._pool(), fields)
        by = {r["record"]["code"]: r["score"] for r in res}
        self.assertAlmostEqual(by["A"], 0.0)
        self.assertAlmostEqual(by["B"], 75.0)
        self.assertAlmostEqual(by["C"], 100.0)

    def test_parts_exposed(self):
        res = screen.score(self._pool(), [("x", 1.0, "higher")])
        self.assertIn("x", res[0]["parts"])

    def test_all_fields_none_score_none(self):
        res = screen.score([{"code": "A", "x": None}], [("x", 1.0, "higher")])
        self.assertIsNone(res[0]["score"])


class TestRecordCodeName(unittest.TestCase):
    """问财按标的类型下发两套列（parsing.md §1）：股票 股票代码/股票简称；ETF 基金代码/基金简称。"""

    def test_stock_record(self):
        rec = {"股票代码": "002595.SZ", "股票简称": "海康威视"}
        self.assertEqual(screen.record_code(rec), "002595.SZ")
        self.assertEqual(screen.record_name(rec), "海康威视")

    def test_etf_record(self):
        rec = {"基金代码": "159447.SZ", "基金简称": "港股通创新药ETF"}
        self.assertEqual(screen.record_code(rec), "159447.SZ")
        self.assertEqual(screen.record_name(rec), "港股通创新药ETF")

    def test_etf_extended_name_fallback(self):
        rec = {"基金代码": "159790.SZ", "基金扩位简称": "黄金ETF易方达"}
        self.assertEqual(screen.record_name(rec), "黄金ETF易方达")

    def test_at_prefix_and_date_suffix_keys(self):
        self.assertEqual(screen.record_code({"@基金代码[20260805]": "515752.SH"}), "515752.SH")

    def test_empty_value_falls_through_to_next_key(self):
        self.assertEqual(screen.record_code({"股票代码": "", "基金代码": "515084.SH"}), "515084.SH")

    def test_missing_returns_none(self):
        self.assertIsNone(screen.record_code({"x": 1}))
        self.assertIsNone(screen.record_name({"x": 1}))

    def test_explicit_code_key_restricts_to_that_column(self):
        self.assertIsNone(screen.record_code({"基金代码": "159447.SZ"}, code_key="股票代码"))


class TestDedup(unittest.TestCase):
    def test_removes_duplicate_keeps_first(self):
        pool = [
            {"股票代码": "002595.SZ", "股票简称": "海康威视"},
            {"股票代码": "000394.SZ", "股票简称": "深科技"},
            {"股票代码": "002595.SZ", "股票简称": "海康威视-dup"},
        ]
        out = screen.dedup(pool)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["股票简称"], "海康威视")

    def test_empty(self):
        self.assertEqual(screen.dedup([]), [])


class TestDedupEtfAndMixed(unittest.TestCase):
    """ETF 记录用 '基金代码'。修前写死 '股票代码' → 全池取到 None → 3 只 ETF 只剩 1 只。"""

    def _etf(self):
        return [
            {"基金代码": "515752.SH", "基金简称": "通信ETF"},
            {"基金代码": "515084.SH", "基金简称": "5GETF"},
            {"基金代码": "159447.SZ", "基金简称": "算力ETF"},
        ]

    def test_distinct_etfs_all_kept(self):
        self.assertEqual(len(screen.dedup(self._etf())), 3)

    def test_etf_duplicate_removed_keeps_first(self):
        pool = self._etf() + [{"基金代码": "515752.SH", "基金简称": "通信ETF-dup"}]
        out = screen.dedup(pool)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["基金简称"], "通信ETF")

    def test_mixed_stock_and_etf_pool(self):
        pool = [
            {"基金代码": "159447.SZ"},
            {"股票代码": "002595.SZ"},
            {"基金代码": "159447.SZ"},
            {"股票代码": "002595.SZ"},
            {"股票代码": "000394.SZ"},
        ]
        self.assertEqual(
            [screen.record_code(r) for r in screen.dedup(pool)],
            ["159447.SZ", "002595.SZ", "000394.SZ"],
        )

    def test_records_without_code_are_kept_not_swallowed(self):
        # 无代码 ≠ 互为重复：None 不得进 seen，否则第二条起会被静默吞掉
        pool = [{"股票简称": "无码A"}, {"股票简称": "无码B"}, {"基金代码": "159447.SZ"}]
        self.assertEqual(len(screen.dedup(pool)), 3)


class TestExcludeHoldings(unittest.TestCase):
    def _pool(self):
        return [
            {"股票代码": "002595.SZ", "股票简称": "海康威视"},
            {"股票代码": "000394.SZ", "股票简称": "深科技"},
        ]

    def test_exclude_by_6digit(self):
        out = screen.exclude_holdings(self._pool(), ["002595"])
        self.assertEqual([r["股票代码"] for r in out], ["000394.SZ"])

    def test_exclude_by_suffixed_code(self):
        out = screen.exclude_holdings(self._pool(), ["000394.SZ"])
        self.assertEqual([r["股票代码"] for r in out], ["002595.SZ"])

    def test_empty_holdings_keeps_all(self):
        self.assertEqual(len(screen.exclude_holdings(self._pool(), [])), 2)

    def test_none_holdings_keeps_all(self):
        self.assertEqual(len(screen.exclude_holdings(self._pool(), None)), 2)


class TestExcludeHoldingsEtfAndMixed(unittest.TestCase):
    """修前 ETF 的 '基金代码' 取不到 → None not in hold 恒真 → 已持仓 ETF 永远排不掉。"""

    def _etf(self):
        return [
            {"基金代码": "515752.SH", "基金简称": "通信ETF"},
            {"基金代码": "515084.SH", "基金简称": "5GETF"},
            {"基金代码": "159447.SZ", "基金简称": "算力ETF"},
        ]

    def test_exclude_etf_by_6digit(self):
        out = screen.exclude_holdings(self._etf(), ["159447"])
        self.assertEqual([r["基金代码"] for r in out], ["515752.SH", "515084.SH"])

    def test_exclude_etf_by_suffixed_code(self):
        out = screen.exclude_holdings(self._etf(), ["159447.SZ"])
        self.assertEqual(len(out), 2)

    def test_mixed_pool_excludes_both_types(self):
        pool = [
            {"基金代码": "159447.SZ"},
            {"股票代码": "002595.SZ"},
            {"股票代码": "000394.SZ"},
        ]
        out = screen.exclude_holdings(pool, ["159447", "000394.SZ"])
        self.assertEqual([screen.record_code(r) for r in out], ["002595.SZ"])

    def test_record_without_code_is_kept(self):
        # 证明不了它是持仓，就不能剔除（剔除等于静默丢候选）
        out = screen.exclude_holdings([{"股票简称": "无码A"}], ["159447"])
        self.assertEqual(len(out), 1)


class TestRenderHtml(unittest.TestCase):
    def _results(self):
        return [
            {"record": {"股票代码": "002595.SZ", "股票简称": "海康威视"}, "score": 88.5, "parts": {"涨跌幅": 0.9}},
            {"record": {"股票代码": "000394.SZ", "股票简称": "深科技"}, "score": 72.0, "parts": {"涨跌幅": 0.5}},
        ]

    def _meta(self):
        return {
            "title": "放量突破",
            "report_label": "2025-01-01",
            "data_date": "2026-06-24",
            "sources": "腾讯行情",
        }

    def test_contains_codes_and_names(self):
        out = screen.render_html(self._results(), self._meta())
        self.assertIn("002595", out)
        self.assertIn("海康威视", out)
        self.assertIn("深科技", out)

    def test_contains_score(self):
        out = screen.render_html(self._results(), self._meta())
        self.assertIn("88.5", out)

    def test_report_label_is_separate_from_disclaimer_data_date(self):
        out = screen.render_html(self._results(), self._meta())
        for term in (
            "研究与决策支持",
            "非持牌证券投资咨询",
            "不构成投资建议",
            "不代下单",
            "风险自担",
            "以本次运行输出为准",
        ):
            self.assertIn(term, out)
        self.assertIn("腾讯行情", out)
        self.assertIn("报告标签：2025-01-01", out)
        self.assertIn("2026-06-24", out)
        footer = out.split("<footer>", 1)[1].split("</footer>", 1)[0]
        self.assertNotIn("2025-01-01", footer)

    def test_is_html_document(self):
        out = screen.render_html(self._results(), self._meta())
        self.assertTrue(out.lstrip().lower().startswith("<!doctype html"))

    def test_escapes_dynamic_values(self):
        out = screen.render_html(self._results(), {
            "title": "<script>x",
            "report_label": "<b>2026-06-24",
            "data_date": None,
            "sources": None,
        })
        self.assertNotIn("<script>x", out)
        self.assertNotIn("<b>2026-06-24", out)
        self.assertIn("数据日：未取得", out)


class TestIntegrationSyntheticFixture(unittest.TestCase):
    """纯虚构字段结构覆盖：日期后缀、数字字符串、评分与 HTML。"""

    def setUp(self):
        self.pool = [
            {
                "股票代码": "699901.SH", "股票简称": "示例甲",
                "最新涨跌幅": 2.5, "换手率[20260624]": "3.2%",
                "主力资金流向": "12000000", "归母净利润同比增长率": 18.0,
                "净资产收益率[20260331]": 9.0, "最新市盈率ttm": 20.0,
                "最新市净率": 2.0, "流通市值[20260624]": "3000000000",
                "成交额[20260624]": "2.5E8",
            },
            {
                "股票代码": "299902.SZ", "股票简称": "示例乙",
                "最新涨跌幅": -0.5, "换手率[20260624]": "1.1%",
                "主力资金流向": "-3000000", "归母净利润同比增长率": 8.0,
                "净资产收益率[20260331]": 6.0, "最新市盈率ttm": 28.0,
                "最新市净率": 3.0, "流通市值[20260624]": "1800000000",
                "成交额[20260624]": "8.0E7",
            },
        ]

    def _fields(self):
        return [
            ("最新涨跌幅", 0.20, "higher"), ("换手率", 0.15, "higher"),
            ("主力资金流向", 0.25, "higher"),
            ("归母净利润同比增长率", 0.15, "higher"),
            ("净资产收益率", 0.15, "higher"),
            ("最新市盈率ttm", 0.05, "lower"), ("最新市净率", 0.05, "lower"),
        ]

    def test_scores_synthetic_pool_in_range(self):
        res = screen.score(self.pool, self._fields())
        self.assertEqual(len(res), len(self.pool))
        for row in res:
            self.assertGreaterEqual(row["score"], 0.0)
            self.assertLessEqual(row["score"], 100.0)

    def test_date_suffix_and_string_number_parsing(self):
        rec = self.pool[0]
        self.assertIsNotNone(screen.pick_value(rec, "换手率"))
        self.assertIsNotNone(screen.pick_value(rec, "净资产收益率"))
        self.assertGreater(screen.pick_value(rec, "成交额"), 1e6)
        self.assertEqual(screen.infer_market_data_date(self.pool), "2026-06-24")

    def test_render_synthetic_pool(self):
        out = screen.render_html(screen.score(self.pool, self._fields()), {
            "title": "离线示例", "report_label": "demo",
            "data_date": "2026-06-24", "sources": "离线虚构 fixture",
        })
        self.assertIn("699901", out)
        self.assertIn("非持牌证券投资咨询", out)

# ---- C2: 数据访问 + 编排（holdings / fetch 翻页 / pipeline / presets / cli 投影）----


class TestLoadHoldings(unittest.TestCase):
    def _write(self, text):
        import tempfile

        fd, p = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, p)
        return p

    def test_extracts_suffixed_codes_from_table(self):
        p = self._write(
            "| 标的 | 代码 | 持仓 |\n"
            "| 黄金ETF | 159790.SZ | 1700 |\n"
            "| 东阳光 | 600584.SH | 300 |\n"
        )
        self.assertEqual(set(screen.load_holdings(p)), {"159790", "600584"})

    def test_dedupes_repeated_code(self):
        p = self._write("600584.SH 建仓\n变更记录里 600584.SH 再次出现\n")
        self.assertEqual(screen.load_holdings(p), ["600584"])

    def test_ignores_funds_without_codes_and_money(self):
        # 基金账户无代码、带千分位逗号的金额、日期都不应被误当成代码
        p = self._write(
            "| 前海开源国企精选混合C | 112,243.00 | -0.34% |\n"
            "现价 9.347 成交额 10,105.00 数据日 2026-06-24\n"
            "| 招商银行 | 600323.SH | 200 |\n"
        )
        self.assertEqual(screen.load_holdings(p), ["600323"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(screen.load_holdings("/no/such/holdings-xyz.md"), [])


class TestFetchPool(unittest.TestCase):
    def _runner(self, pages):
        def run(query, page, limit):
            return pages[page - 1]

        return run

    def test_accumulates_across_pages_until_no_more(self):
        pages = [
            {"datas": [{"股票代码": "1"}, {"股票代码": "2"}], "has_more": True},
            {"datas": [{"股票代码": "3"}], "has_more": False},
        ]
        out = screen.fetch_pool("q", max_records=100, page_limit=2, runner=self._runner(pages))
        self.assertEqual([r["股票代码"] for r in out], ["1", "2", "3"])

    def test_respects_max_records_cap(self):
        pages = [
            {"datas": [{"股票代码": "1"}, {"股票代码": "2"}], "has_more": True},
            {"datas": [{"股票代码": "3"}, {"股票代码": "4"}], "has_more": True},
        ]
        out = screen.fetch_pool("q", max_records=3, page_limit=2, runner=self._runner(pages))
        self.assertEqual(len(out), 3)

    def test_stops_on_empty_datas(self):
        pages = [{"datas": [], "has_more": True}]
        out = screen.fetch_pool("q", max_records=50, page_limit=10, runner=self._runner(pages))
        self.assertEqual(out, [])
        self.assertIsNone(screen.infer_market_data_date(out))

    def test_missing_datas_raises(self):
        def run(query, page, limit):
            return {"error": "额度不足", "status_code": 403}

        with self.assertRaises(screen.ScreenError):
            screen.fetch_pool("q", runner=run)


class TestScreenPool(unittest.TestCase):
    def _pool(self):
        return [
            {"股票代码": "000005.SZ", "股票简称": "A", "x": 10},
            {"股票代码": "000776.SZ", "股票简称": "B", "x": 30},
            {"股票代码": "000776.SZ", "股票简称": "B-dup", "x": 30},
            {"股票代码": "000528.SZ", "股票简称": "C", "x": 20},
        ]

    def test_dedup_exclude_sort_top(self):
        fields = [("x", 1.0, "higher")]
        out = screen.screen_pool(self._pool(), fields, holding_codes=["000528"], top=10)
        codes = [r["record"]["股票代码"].split(".")[0] for r in out]
        self.assertEqual(codes, ["000776", "000005"])

    def test_top_limit(self):
        fields = [("x", 1.0, "higher")]
        out = screen.screen_pool(self._pool(), fields, holding_codes=None, top=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["record"]["股票代码"], "000776.SZ")


class TestPresets(unittest.TestCase):
    def test_known_preset_has_query_and_fields(self):
        p = screen.get_preset("放量突破")
        self.assertTrue(p["query"])
        self.assertTrue(p["fields"])

    def test_unknown_preset_raises(self):
        with self.assertRaises(KeyError):
            screen.get_preset("不存在的模板")

    def test_all_presets_weights_normalized_and_valid_direction(self):
        self.assertGreaterEqual(len(screen.PRESETS), 4)
        for name, p in screen.PRESETS.items():
            wsum = sum(w for _, w, _ in p["fields"])
            self.assertAlmostEqual(wsum, 1.0, places=6, msg=name)
            for _, _, d in p["fields"]:
                self.assertIn(d, ("higher", "lower"), msg=name)

    def test_default_fields_valid(self):
        wsum = sum(w for _, w, _ in screen.DEFAULT_FIELDS)
        self.assertAlmostEqual(wsum, 1.0, places=6)


class TestBuildListing(unittest.TestCase):
    def test_summary_and_html(self):
        pool = [
            {"股票代码": "000005.SZ", "股票简称": "A", "x": 10},
            {"股票代码": "000776.SZ", "股票简称": "B", "x": 30},
        ]
        fields = [("x", 1.0, "higher")]
        meta = {
            "title": "测试",
            "report_label": "2026-06-24",
            "data_date": "2026-06-24",
            "sources": "同花顺问财",
        }
        out = screen.build_listing(
            pool, fields, holding_codes=None, top=15,
            meta=meta,
        )
        self.assertEqual(out["meta"], meta)
        self.assertEqual(len(out["results"]), 2)
        self.assertIn("code", out["results"][0])
        self.assertIn("name", out["results"][0])
        self.assertIn("score", out["results"][0])
        self.assertIn("非持牌证券投资咨询", out["html"])
        self.assertEqual(out["results"][0]["code"], "000776.SZ")


class TestTencentPrefix(unittest.TestCase):
    """行情 enrichment 的市场前缀:优先用问财返回的交易所后缀,规避 5 开头误判。"""

    def test_suffix_sz(self):
        self.assertEqual(screen.tencent_prefix("002595.SZ"), "sz002595")

    def test_suffix_sh(self):
        self.assertEqual(screen.tencent_prefix("600323.SH"), "sh600323")

    def test_suffix_bj(self):
        self.assertEqual(screen.tencent_prefix("832000.BJ"), "bj832000")

    def test_suffix_sh_etf(self):
        self.assertEqual(screen.tencent_prefix("510340.SH"), "sh510340")

    def test_bare_6(self):
        self.assertEqual(screen.tencent_prefix("600454"), "sh600454")

    def test_bare_5_is_sh(self):
        # a-stock-data 原版 bug:5 开头判 sz;本实现必须判 sh
        self.assertEqual(screen.tencent_prefix("510340"), "sh510340")

    def test_bare_0_is_sz(self):
        self.assertEqual(screen.tencent_prefix("002595"), "sz002595")

    def test_bare_8_is_bj(self):
        self.assertEqual(screen.tencent_prefix("832000"), "bj832000")

    def test_invalid_none(self):
        self.assertIsNone(screen.tencent_prefix("abc"))


class TestParseTencentPayload(unittest.TestCase):
    # 字段索引与真实腾讯载荷对齐:2=代码 31=涨跌额 32=涨跌幅 38=换手率 39=PEttm 46=PB
    _F = "~".join(str(i) for i in range(6, 31))  # 索引 6..30 的占位
    SAMPLE = (
        'v_sz002595="51~海康威视~002595~30.00~29.50~29.60~' + _F +
        "~0.50~1.69~30.5~29.4~35~36~187040~4.55~18.2~40~41~42~7.22~2800.5~2700.4"
        '~2.85~32.4~27.6~1.2~50~51~314.7~53";\n'
        'v_sh600323="1~招商银行~600323~37.18~37.25~37.10~' + _F +
        "~-0.07~-0.19~37.24~36.82~35~36~296~0.33~6.8~40~41~42~1.13~9376~9376"
        '~1.05~40.9~33.5~0.9~50~51~6.9~53";'
    )

    def test_parse_两只(self):
        got = screen.parse_tencent_payload(self.SAMPLE)
        self.assertIn("002595", got)
        self.assertIn("600323", got)

    def test_std_fields(self):
        got = screen.parse_tencent_payload(self.SAMPLE)["002595"]
        self.assertAlmostEqual(got["最新涨跌幅"], 1.69)
        self.assertAlmostEqual(got["换手率"], 4.55)
        self.assertAlmostEqual(got["最新市盈率ttm"], 18.2)
        self.assertAlmostEqual(got["最新市净率"], 2.85)

    def test_negative_change(self):
        got = screen.parse_tencent_payload(self.SAMPLE)["600323"]
        self.assertAlmostEqual(got["最新涨跌幅"], -0.19)

    def test_short_line_skipped(self):
        self.assertEqual(screen.parse_tencent_payload('v_sh600922="1~x~600922";'), {})

    def test_pe_pb_zero_is_sentinel_none(self):
        # 腾讯对亏损股/ETF 的 PE/PB 给 0.00(哨兵值,非真值):必须转 None,
        # 否则 'lower' 方向打分会把亏损股排成"估值最优"
        sample = self.SAMPLE.replace("~4.55~18.2~", "~4.55~0.00~").replace(
            "~2.85~32.4~", "~0.00~32.4~")
        got = screen.parse_tencent_payload(sample)["002595"]
        self.assertIsNone(got["最新市盈率ttm"])
        self.assertIsNone(got["最新市净率"])
        self.assertAlmostEqual(got["换手率"], 4.55)  # 换手率 0 合法,不受影响


class TestEnrichRecords(unittest.TestCase):
    POOL = [
        {"股票代码": "002595.SZ", "股票简称": "海康威视",
         "换手率[20260714]": "9.99%", "主力资金流向": "1.2亿"},
        {"股票代码": "600323.SH", "股票简称": "招商银行"},
    ]

    @staticmethod
    def fake_fetcher(codes):
        assert "sz002595" in codes and "sh600323" in codes
        return {"002595": {"最新涨跌幅": 1.69, "换手率": 4.55,
                           "最新市盈率ttm": 18.2, "最新市净率": 2.85},
                "600323": {"最新涨跌幅": -0.19, "换手率": 0.33,
                           "最新市盈率ttm": 6.8, "最新市净率": 1.05}}

    def test_override_wencai_variant(self):
        pool = [dict(r) for r in self.POOL]
        _, info = screen.enrich_records(pool, fetcher=self.fake_fetcher)
        # 腾讯值覆盖问财变体列,pick_value 取到的是腾讯值
        self.assertAlmostEqual(screen.pick_value(pool[0], "换手率"), 4.55)
        self.assertTrue(info["enriched"])
        self.assertEqual(info["hit"], 2)

    def test_fill_missing(self):
        pool = [dict(r) for r in self.POOL]
        screen.enrich_records(pool, fetcher=self.fake_fetcher)
        self.assertAlmostEqual(screen.pick_value(pool[1], "最新市净率"), 1.05)

    def test_keep_t2_only_fields(self):
        pool = [dict(r) for r in self.POOL]
        screen.enrich_records(pool, fetcher=self.fake_fetcher)
        self.assertAlmostEqual(screen.pick_value(pool[0], "主力资金流向"), 1.2e8)

    def test_fetcher_partial_missing_keeps_wencai(self):
        pool = [dict(r) for r in self.POOL]
        screen.enrich_records(
            pool, fetcher=lambda codes: {"600323": {"换手率": 0.33}})
        # 002595 腾讯缺失 → 保留问财原值
        self.assertAlmostEqual(screen.pick_value(pool[0], "换手率"), 9.99)

    def test_fetcher_failure_graceful(self):
        def boom(codes):
            raise OSError("network down")
        pool = [dict(r) for r in self.POOL]
        _, info = screen.enrich_records(pool, fetcher=boom)
        self.assertFalse(info["enriched"])
        self.assertIn("network down", info["note"])
        self.assertAlmostEqual(screen.pick_value(pool[0], "换手率"), 9.99)


class TestEtfPipelineColumns(unittest.TestCase):
    """ETF 记录在整条链路上都要拿到代码与名称：enrich → screen_pool → build_listing → HTML。"""

    def _etf_pool(self):
        return [
            {"基金代码": "515752.SH", "基金简称": "通信ETF", "x": 10},
            {"基金代码": "159447.SZ", "基金简称": "算力ETF", "x": 30},
            {"基金代码": "515084.SH", "基金简称": "5GETF", "x": 20},
        ]

    def test_render_html_shows_etf_code_and_name(self):
        results = [{"record": {"基金代码": "159447.SZ", "基金简称": "算力ETF"},
                    "score": 88.5, "parts": {}}]
        out = screen.render_html(results, {
            "title": "载体筛选",
            "report_label": "2026-08-05",
            "data_date": "2026-08-05",
            "sources": "同花顺问财",
        })
        self.assertIn("159447", out)
        self.assertIn("算力ETF", out)

    def test_build_listing_projects_etf_code_and_name(self):
        out = screen.build_listing(
            self._etf_pool(), [("x", 1.0, "higher")], holding_codes=["515084"], top=15,
            meta={"title": "载体筛选", "report_label": "2026-08-05",
                  "data_date": "2026-08-05", "sources": "同花顺问财"})
        self.assertEqual([r["code"] for r in out["results"]], ["159447.SZ", "515752.SH"])
        self.assertEqual(out["results"][0]["name"], "算力ETF")

    def test_screen_pool_mixed_dedup_and_exclude(self):
        pool = self._etf_pool() + [
            {"基金代码": "159447.SZ", "基金简称": "算力ETF-dup", "x": 30},
            {"股票代码": "002595.SZ", "股票简称": "海康威视", "x": 40},
        ]
        out = screen.screen_pool(pool, [("x", 1.0, "higher")], holding_codes=["515752"], top=10)
        self.assertEqual(
            [screen.record_code(r["record"]) for r in out],
            ["002595.SZ", "159447.SZ", "515084.SH"],
        )

    def test_enrich_records_hits_etf_by_fund_code(self):
        seen = {}

        def fake_fetcher(codes):
            seen["codes"] = list(codes)
            return {"159447": {"最新涨跌幅": 2.5, "换手率": 6.0}}

        pool = self._etf_pool()
        _, info = screen.enrich_records(pool, fetcher=fake_fetcher)
        self.assertIn("sz159447", seen["codes"])
        self.assertIn("sh515752", seen["codes"])
        self.assertEqual(info["hit"], 1)
        self.assertAlmostEqual(screen.pick_value(pool[1], "换手率"), 6.0)


class TestPresetQueriesSlim(unittest.TestCase):
    """打分列改源后:行情类展示词缀移出问句;筛选条件与 T2 专属词缀保留;字段表不变。"""

    MARKET_DISPLAY_WORDS = ["换手率", "市净率"]

    def test_fangliang_query(self):
        q = screen.PRESETS["放量突破"]["query"]
        for w in self.MARKET_DISPLAY_WORDS + ["市盈率"]:
            self.assertNotIn(w, q)
        self.assertIn("主力资金流向", q)
        self.assertIn("非ST", q)

    def test_diwei_query(self):
        q = screen.PRESETS["低位反转"]["query"]
        for w in self.MARKET_DISPLAY_WORDS:
            self.assertNotIn(w, q)
        self.assertIn("股价站上5日均线", q)

    def test_yeji_keeps_filter_pe(self):
        q = screen.PRESETS["业绩成长"]["query"]
        self.assertIn("市盈率小于40", q)  # 条件保留
        self.assertNotIn("市净率", q)      # 纯展示词移除

    def test_zhuli_query(self):
        q = screen.PRESETS["主力异动"]["query"]
        self.assertNotIn("换手率", q)
        self.assertIn("资金流入", q)

    def test_fields_unchanged(self):
        # 打分字段契约不变:换手率等仍参与打分(由 enrichment 供数)
        for name in ("放量突破", "低位反转", "主力异动"):
            self.assertIn(
                "换手率",
                [f[0] for f in screen.PRESETS[name]["fields"]],
            )


# ---- C3: iFinD 本地历史证据 opt-in 接入（2026-07-23）----


class TestAssembleOutputBackwardCompat(unittest.TestCase):
    """阶段 2 固定输出声明；iFinD 仍保持独立 opt-in 键。"""

    def _fake_listing(self):
        return {
            "meta": {
                "report_label": "2025-01-01",
                "data_date": "2026-07-16",
                "sources": "同花顺问财 + 腾讯行情",
            },
            "results": [{"code": "600323.SH", "name": "招商银行", "score": 88.5, "parts": {}}],
        }

    def test_no_optin_has_no_ifind_key(self):
        out = screen.assemble_output(
            "自选", "q", "2025-01-01", [{"x": 1}], self._fake_listing(),
            holdings=[], holdings_missing=False,
            enrich_info={"enriched": False, "hit": 0, "note": ""},
            html_path="/tmp/x.html", ifind_evidence=None)
        self.assertNotIn("ifind_local_evidence", out)
        self.assertEqual(
            list(out.keys()),
            ["ok", "title", "query", "date", "report_label", "disclaimer", "fetched", "returned",
             "holdings_excluded", "holdings_missing", "enrich", "html_path", "top"],
        )
        self.assertEqual(out["date"], "2026-07-16")
        self.assertEqual(out["report_label"], "2025-01-01")
        self.assertIn("同花顺问财 + 腾讯行情", out["disclaimer"])
        self.assertIn("2026-07-16", out["disclaimer"])
        self.assertNotIn("2025-01-01", out["disclaimer"])
        for term in (
            "研究与决策支持",
            "非持牌证券投资咨询",
            "不构成投资建议",
            "不代下单",
            "风险自担",
            "以本次运行输出为准",
        ):
            self.assertIn(term, out["disclaimer"])

    def test_optin_appends_isolated_key_only(self):
        ev = {"available": True, "opt_in": True}
        listing = self._fake_listing()
        listing["meta"]["data_date"] = None
        out = screen.assemble_output(
            "自选", "q", "2025-01-01", [{"x": 1}], listing,
            holdings=[], holdings_missing=False,
            enrich_info={"enriched": False, "hit": 0, "note": ""},
            html_path="/tmp/x.html", ifind_evidence=ev)
        # 既有键不变，新键仅追加在末尾
        self.assertEqual(list(out.keys())[-1], "ifind_local_evidence")
        self.assertEqual(out["date"], "未取得")
        self.assertEqual(out["report_label"], "2025-01-01")
        self.assertNotIn("2025-01-01", out["disclaimer"])
        self.assertIn("数据日：未取得", out["disclaimer"])
        self.assertEqual(out["top"], listing["results"])
        self.assertIs(out["ifind_local_evidence"], ev)


class TestAttachIfindEvidence(unittest.TestCase):
    """opt-in 附加层：注入 runner 做离线测试；核心保证 fail-closed 且不改动入参。"""

    TOP = [{"code": "600323.SH", "name": "招商银行"},
           {"code": "999999.SZ", "name": ""}]

    def _good_doc(self, as_of=None, cutoff="2026-07-16"):
        disclaimer = ("本地冻结数据截止日：%s。"
                      "本接口仅提供历史覆盖与证据缺口，非实时行情。" % cutoff)
        return {"ok": True, "数据截止日": cutoff, "市场状态对齐日": as_of,
                "覆盖统计口径": "覆盖统计固定截至 %s，非市场状态对齐日的 PIT 截面。" % cutoff,
                "候选证据": [{"代码6": "600323", "匹配状态": "已匹配"}],
                "口径校验": {"候选数": 2},
                "市场状态": [{"指数名称": "上证指数", "综合状态": "不适用（未提供历史日期）"}],
                "声明": disclaimer}

    def test_success_with_injected_runner(self):
        def runner(codes, as_of, helper):
            self.assertEqual(codes, ["600323.SH", "999999.SZ"])
            return self._good_doc(as_of)
        ev = screen.attach_ifind_evidence(self.TOP, as_of="2026-06-30", runner=runner)
        self.assertTrue(ev["available"])
        self.assertEqual(ev["data_cutoff"], "2026-07-16")
        self.assertEqual(ev["market_state_asof"], "2026-06-30")
        self.assertNotIn("as_of", ev)  # 已改名
        self.assertIn("不参与排名", ev["note"])
        for term in (
            "研究与决策支持",
            "非持牌证券投资咨询",
            "不构成投资建议",
            "不代下单",
            "风险自担",
            "以本次运行输出为准",
        ):
            self.assertIn(term, ev["disclaimer"])
        self.assertIn("iFinD 本地历史证据", ev["disclaimer"])

    def test_contract_accepts_runtime_historical_cutoff(self):
        def runner(codes, as_of, helper):
            return self._good_doc(as_of, cutoff="2026-07-15")
        ev = screen.attach_ifind_evidence(self.TOP, runner=runner)
        self.assertTrue(ev["available"])
        self.assertEqual(ev["data_cutoff"], "2026-07-15")
        self.assertIn("2026-07-15", ev["note"])

    def test_contract_rejects_future_cutoff(self):
        """helper 不能用未来日期冒充已冻结的本地证据。"""
        def runner(codes, as_of, helper):
            return self._good_doc(as_of, cutoff="2099-01-01")
        ev = screen.attach_ifind_evidence(self.TOP, runner=runner)
        self.assertFalse(ev["available"])
        self.assertIn("未来", ev["reason"])

    def test_contract_rejects_invalid_cutoff(self):
        def runner(codes, as_of, helper):
            return self._good_doc(as_of, cutoff="not-a-date")
        ev = screen.attach_ifind_evidence(self.TOP, runner=runner)
        self.assertFalse(ev["available"])
        self.assertIn("YYYY-MM-DD", ev["reason"])

    def test_contract_rejects_missing_disclaimer_boundary(self):
        def runner(codes, as_of, helper):
            doc = self._good_doc(as_of)
            doc["声明"] = "随便一句没有边界的话"
            return doc
        ev = screen.attach_ifind_evidence(self.TOP, runner=runner)
        self.assertFalse(ev["available"])

    def test_contract_rejects_bad_array_shape(self):
        def runner(codes, as_of, helper):
            doc = self._good_doc(as_of)
            doc["候选证据"] = "不是数组"
            return doc
        ev = screen.attach_ifind_evidence(self.TOP, runner=runner)
        self.assertFalse(ev["available"])

    def test_canonical_helper_guard_rejects_arbitrary_path(self):
        """生产 runner 不得执行任意 Node helper（realpath 不等于 canonical 即拒绝）。"""
        private_path = "/Users/private/work/ifind_evidence.mjs"
        original = screen.ifind_helper_path
        screen.ifind_helper_path = lambda: private_path
        try:
            with self.assertRaises(ValueError) as caught:
                screen._ifind_subprocess_runner(
                    ["600323.SH"], None, "/tmp/evil_helper.mjs")
            self.assertEqual(
                str(caught.exception),
                "helper 路径非法：仅允许配置中的 canonical helper",
            )
            ev = screen.attach_ifind_evidence(
                self.TOP, helper_path="/tmp/evil_helper.mjs")
            self.assertFalse(ev["available"])
            for text in (str(caught.exception), ev["reason"]):
                self.assertNotIn(private_path, text)
                self.assertNotIn("/Users/", text)
        finally:
            screen.ifind_helper_path = original

    def test_does_not_mutate_input(self):
        snapshot = [dict(r) for r in self.TOP]
        screen.attach_ifind_evidence(
            self.TOP, runner=lambda c, a, h: {"ok": True})
        self.assertEqual(self.TOP, snapshot)

    def test_fail_closed_on_runner_exception(self):
        def boom(codes, as_of, helper):
            raise RuntimeError("helper 崩了")
        ev = screen.attach_ifind_evidence(self.TOP, runner=boom)
        self.assertFalse(ev["available"])
        self.assertIn("helper 崩了", ev["reason"])

    def test_fail_closed_on_missing_node(self):
        def missing(codes, as_of, helper):
            raise FileNotFoundError("node")
        ev = screen.attach_ifind_evidence(self.TOP, runner=missing)
        self.assertFalse(ev["available"])
        self.assertIn("core 名单不受影响", ev["reason"])

    def test_fail_closed_on_bad_doc(self):
        ev = screen.attach_ifind_evidence(self.TOP, runner=lambda c, a, h: {"ok": False})
        self.assertFalse(ev["available"])

    def test_empty_top_returns_unavailable(self):
        ev = screen.attach_ifind_evidence([], runner=lambda c, a, h: {"ok": True})
        self.assertFalse(ev["available"])
        self.assertIn("无可查代码", ev["reason"])


class TestIfindEvidenceEndToEnd(unittest.TestCase):
    """真实端到端：fixture 候选 → 离线 Node helper → 附加区块。node/helper/parquet 缺失则跳过。"""

    def setUp(self):
        try:
            helper = screen.ifind_helper_path()
        except screen.ScreenError:
            helper = None
        if not helper or not os.path.exists(helper):
            self.skipTest("iFinD helper 未配置或不存在，跳过端到端")
        self.HELPER = helper

    def test_real_helper_attaches_coverage(self):
        top = [{"code": "600323.SH", "name": "招商银行"},
               {"code": "999999.SZ", "name": ""}]
        ev = screen.attach_ifind_evidence(top)  # 默认 runner = 真实 node helper
        if not ev.get("available"):
            self.skipTest("helper 未就绪：%s" % ev.get("reason"))
        codes = {c["代码6"] for c in ev["coverage"]}
        self.assertIn("600323", codes)
        self.assertIn("2026-07-16", ev["disclaimer"])
        # 未提供历史日期 → 市场状态全部不适用
        self.assertTrue(all("不适用" in m["综合状态"] for m in ev["market_state"]))


if __name__ == "__main__":
    unittest.main()
