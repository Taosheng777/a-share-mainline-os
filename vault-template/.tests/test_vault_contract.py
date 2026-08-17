import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VaultTemplateContract(unittest.TestCase):
    def test_core_paths_exist(self):
        for relative in (
            "投资笔记.md",
            "00-系统/AI操作规则.md",
            "01-纪律卡.md",
            "02-盘面日志/README.md",
            "02-盘面日志/示例复盘.md",
            "03-持仓跟踪/持仓.md",
            "05-主线追踪/README.md",
            "05-主线追踪/_主线页模板.md",
            "05-主线追踪/归档/_退潮判定台账.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_discipline_is_empty_but_complete(self):
        text = (ROOT / "01-纪律卡.md").read_text(encoding="utf-8")
        for phrase in (
            "type: discipline",
            "version: v3",
            "① 主线失效退出",
            "② 单笔止损线",
            "③ 账户回撤熔断",
            "不产生自动卖出",
            "不产生委托",
        ):
            self.assertIn(phrase, text)
        self.assertEqual(text.count("阈值：\n"), 3)

    def test_holdings_is_empty(self):
        text = (ROOT / "03-持仓跟踪/持仓.md").read_text(encoding="utf-8")
        self.assertIn("| 标的 | 角色 | 所属主线 | 止损参考位 | 状态 |", text)
        self.assertIn("|---|---|---|---:|---|", text)
        self.assertIn("| _ | _ | _ | _ | _ |", text)

    def test_example_is_explicitly_synthetic(self):
        text = (ROOT / "02-盘面日志/示例复盘.md").read_text(encoding="utf-8")
        self.assertIn("纯虚构结构示例", text)
        self.assertIn("未取得（示例不联网）", text)
        self.assertIn("不构成投资建议", text)

    def test_mainline_template_has_lifecycle_sections(self):
        text = (ROOT / "05-主线追踪/_主线页模板.md").read_text(encoding="utf-8")
        for phrase in (
            "type: mainline-template",
            "阶段: 提名",
            "死亡条件:",
            "判定规则版本: double_key_v1",
            "## ■ 状态头",
            "## ■ 退潮复核判据（预注册）",
            "## ■ 叙事",
            "## ■ 立项影子观察（`shadow_only`）",
            "## ■ 证据流（按日增量）",
            "## ■ 载体清单",
            "### 载体排序 T+N 跟踪",
            "## ■ 参考条件位",
            "## ■ AI 决策建议（金融专家模式）",
            "## ■ 我的操作",
            "## ■ 退潮判定 T+N 复核（退潮后建档，逐日回填）",
            "## ■ 归档复盘（退潮后填）",
        ):
            self.assertIn(phrase, text)

    def test_mainline_lifecycle_carries_warning_stage_and_double_key(self):
        readme = (ROOT / "05-主线追踪/README.md").read_text(encoding="utf-8")
        for phrase in (
            "提名 → 启动 → 发酵 → 分歧 → 警戒 → 退潮 → 归档",
            "资金键单独成立只进警戒",
            "不联动纪律①",
            "立项误报回测是强制项",
            "判定规则版本: double_key_v1",
            "复活哨",
        ):
            self.assertIn(phrase, readme)

        template = (ROOT / "05-主线追踪/_主线页模板.md").read_text(encoding="utf-8")
        self.assertIn("死亡条件 · 清仓级", template)
        self.assertIn("死亡条件 · 警戒级", template)
        self.assertIn("禁止裸用全市场绝对额排名", template)

    def test_exit_ledger_preregisters_classification_and_counting(self):
        text = (ROOT / "05-主线追踪/归档/_退潮判定台账.md").read_text(encoding="utf-8")
        for phrase in (
            "type: mainline-exit-ledger",
            "## ■ 分类判据（预注册 · 定稿即冻结）",
            "## ■ 计数口径",
            "不得为了归类另换指标",
            "剔除窗口内最负一日",
            "不构成买回信号",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
