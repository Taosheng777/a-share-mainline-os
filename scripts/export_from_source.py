#!/usr/bin/env python3
"""按白名单从 Claude 单一事实源导出公开 Skill，并应用窄平台适配。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAUDE_DESTINATION = ROOT / "plugins/a-share-mainline-os-claude/skills"
DEFAULT_CODEX_DESTINATION = ROOT / "plugins/a-share-mainline-os-codex/skills"
MANIFEST = ROOT / "config/export-manifest.json"

PUBLIC_REPLACEMENTS = {
    "stock-daily/SKILL.md": (
        ("Key 存 **macOS 钥匙串**", "Key 由用户按 `hithink-finance` 官方方式配置"),
        ("（`~/.zshrc`）", "（本机环境变量）"),
        ("> **⚠️ `hithink-finance doctor` 的 `api_key_present:false` 不代表没配 Key** —— 它只检查环境变量 `HITHINK_FINANCE_API_KEY`。Key 实际存在 macOS 钥匙串里，判断是否可用要看 **`auth status` 的 `configured`** 字段，或直接发一次远端请求看 `ok`。（2026-08-04 曾据 doctor 误判为\"未配置、不可用\"，导致本可补齐的数据被写成缺口。）", "> **认证检查**：以 `hithink-finance auth status` 的 `configured` 字段或一次只读请求为准；不得仅凭某个环境变量缺失就推断服务不可用。"),
        ("> 加一个 `非ST` 会让家数少两百家左右（2026-08-04 实测：全池 3,406/1,664 vs 非ST 3,296/1,589，差 185 家），比值和历史序列都会漂。**五档矩阵与全部历史记录都用全池口径，不要改问法。**", "> 加 `非ST` 会改变样本池与历史可比性。**五档矩阵与全部历史记录都用全池口径，不要改问法。**"),
    ),
    "stock-buddy/SKILL.md": (
        ("Key 存 **macOS 钥匙串**，`hithink-finance auth status` 的 `configured` 才是可用性判据；**`doctor` 的 `api_key_present` 只查环境变量，为 false 不代表没配 Key**。", "认证按 `hithink-finance` 官方方式配置，以 `auth status` 的 `configured` 或一次只读请求为可用性判据；不得只凭某个环境变量缺失就推断不可用。"),
    ),
    "stock-buddy/references/discipline.md": (
        ("，取代 v2 的《股票账户重构计划书》与月度执行体系（均已归档至 `90-归档/2026-08-04 v2系统归档/`）", "；不得从旧文档或模型记忆恢复已经废除的规则"),
    ),
    "stock-buddy/references/regular-tier.md": (
        ("v2 的个股 10% / 主题ETF 15% / 同主题 35% / 现金 15% / 单笔 " + "0.75% 等硬线**已全部废除，不得再套**", "v2 曾固化的个股、主题 ETF、同主题、现金和单笔风险等具体硬线**已全部废除，不得再套或从旧记录恢复**"),
    ),
    "stock-buddy/references/decision-support.md": (
        ("不得直接复用已废除的 v2 `" + "0.75%` 硬线", "不得直接复用任何已废除的 v2 固定风险比例"),
    ),
}

CODEX_REPLACEMENTS = {
    "stock-buddy/SKILL.md": (
        ("用 AskUserQuestion 工具", "直接向用户提问"),
        ("Agent 工具派发", "Codex `collaboration.spawn_agent` 派发"),
    ),
    "stock-buddy/agents/chief-analyst.md": (
        ("首席分析师（orchestrator）", "首席分析师（Codex orchestrator）"),
        ("用 **Agent 工具**", "用 **`collaboration.spawn_agent`**"),
        ("收齐五份独立结果后", "用 `collaboration.wait_agent` 收齐五份独立结果后"),
        ("只有 Agent 工具不可用", "只有 `collaboration` 工具不可用"),
        ("先用 AskUserQuestion 向用户说明", "先直接向用户说明"),
    ),
    "stock-buddy/references/expert-mode.md": (
        ("不必再 AskUserQuestion 二次确认", "不必再二次确认"),
        ("用 AskUserQuestion 问", "直接向用户提问"),
        ("蜂群 = Agent 工具派发", "蜂群 = Codex collaboration 工具派发"),
        ("（Agent 工具，subagent_type 用 general-purpose 或 claude）", "（用 `collaboration.spawn_agent`）"),
        ("首席**收齐 5 份独立结果**", "首席用 `collaboration.wait_agent` **收齐 5 份独立结果**"),
        ("只有 Agent 工具不可用", "只有 `collaboration` 工具不可用"),
    ),
    "stock-buddy/references/discipline.md": (
        ("账户纪律（v3，Claude Code 版）", "账户纪律（v3，Codex 版）"),
    ),
}

SECURITY_CODE_RE = re.compile(
    r"(?<!\d)(?:000|001|002|003|159|300|301|510|511|512|513|515|516|517|518|"
    r"520|521|522|560|561|562|563|588|600|601|603|605|688|689)\d{3}(?!\d)"
)

OPENAI_YAML = {
    "stock-daily": {
        "display_name": "复盘",
        "short_description": "判定盘面环境、更新主线状态并按纪律卡做持仓对照",
        "default_prompt": "使用 $stock-daily 执行最新交易日复盘，走完五区流程并报告阻塞项。",
    },
    "stock-screener": {
        "display_name": "载体筛选",
        "short_description": "为已立项主线发现 ETF 与龙头股并完成四维排序",
        "default_prompt": "使用 $stock-screener 为已立项主线筛选 ETF 与龙头股，完成相关度校验和四维排序。",
    },
    "stock-buddy": {
        "display_name": "Stock Buddy",
        "short_description": "主线论证、持仓体检、目标区间与风险预算仓位建议",
        "default_prompt": "使用 $stock-buddy 分析指定主线或持仓；我明确开启金融专家模式时，运行五个 sub-agent。",
    },
}


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def apply_replacements(relative: str, text: str, *, platform: str) -> str:
    replacements = list(PUBLIC_REPLACEMENTS.get(relative, ()))
    if platform == "codex":
        replacements.extend(CODEX_REPLACEMENTS.get(relative, ()))
    for old, new in replacements:
        if old not in text:
            raise ValueError(f"适配锚点不存在：{relative}: {old}")
        text = text.replace(old, new)
    if relative.endswith("/tests/test_screen.py") or relative.endswith(
        "/tests/test_danger_scan.py"
    ) or relative.endswith("/scripts/danger_scan.py"):
        text = SECURITY_CODE_RE.sub(pseudonymize_security_code, text)
    if relative == "stock-daily/SKILL.md":
        text = re.sub(
            r"> 依据：[^\n]+旧规则「别处复现不出这个口径」的前提已不成立。",
            "> 影子源用于发现口径漂移；任何差异都必须逐项解释，不能静默吸收。",
            text,
        )
    if relative == "stock-buddy/references/report-template.md":
        text = re.sub(
            r"2026-08-07 增补。[^\n]+",
            "这三条是强制检查。漏了不是风格问题，**是把本来可核验的结论写成了不可核验的**。",
            text,
            count=1,
        )
        text = re.sub(
            r"页面从可核验退回「AI 说的」。漏例：[^|]+",
            "页面从可核验退回「AI 说的」；任何无法回溯到技术锚点的数字都视为失败 ",
            text,
            count=1,
        )
        text = re.sub(
            r"「5 日 [^|]+只看单周期会把脉冲读成趋势",
            "只看单周期可能把短期脉冲误读为持续趋势",
            text,
            count=1,
        )
    if relative == "stock-screener/tests/test_screen.py":
        text = re.sub(
            r'class TestIntegrationRealFixture\(unittest\.TestCase\):.*?\n\n# ---- C2:',
            render_synthetic_integration_fixture() + "\n\n# ---- C2:",
            text,
            count=1,
            flags=re.S,
        )
        source_private_fixture = (
            'private_path = "' + "/" + 'Users/private/work/ifind_evidence.mjs"'
        )
        text = text.replace(
            source_private_fixture,
            'private_path = "/" + "Users/placeholder/work/ifind_evidence.mjs"',
        )
    return text


def pseudonymize_security_code(match: re.Match[str]) -> str:
    code = match.group(0)
    suffix = int.from_bytes(hashlib.sha256(code.encode()).digest()[:2], "big") % 1000
    return f"{code[:3]}{suffix:03d}"


def render_synthetic_integration_fixture() -> str:
    return '''class TestIntegrationSyntheticFixture(unittest.TestCase):
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
        self.assertIn("非持牌证券投资咨询", out)'''


def render_openai_yaml(skill: str) -> str:
    values = OPENAI_YAML[skill]
    return (
        "interface:\n"
        f'  display_name: "{values["display_name"]}"\n'
        f'  short_description: "{values["short_description"]}"\n'
        f'  default_prompt: "{values["default_prompt"]}"\n'
    )


def materialize_source(repo: Path, revision: str, destination: Path) -> None:
    resolved = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{revision}^{{commit}}"],
        text=True,
    ).strip()
    if resolved != revision:
        raise ValueError(f"源 revision 不匹配：期望 {revision}，实际 {resolved}")
    archive = subprocess.Popen(
        ["git", "-C", str(repo), "archive", revision],
        stdout=subprocess.PIPE,
    )
    try:
        subprocess.run(
            ["tar", "-x", "-C", str(destination)],
            stdin=archive.stdout,
            check=True,
        )
    finally:
        if archive.stdout:
            archive.stdout.close()
    if archive.wait() != 0:
        raise RuntimeError("无法导出固定源 revision")


def export(source: Path, destination: Path, *, platform: str) -> None:
    manifest = load_manifest()
    files = manifest["files"]
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("/agents/openai.yaml"):
            skill = relative.split("/", 1)[0]
            target.write_text(render_openai_yaml(skill), encoding="utf-8")
            continue
        source_file = source / relative
        if not source_file.is_file():
            raise FileNotFoundError(source_file)
        if source_file.suffix.lower() in {".md", ".py", ".json", ".yaml", ".yml"}:
            text = source_file.read_text(encoding="utf-8")
            target.write_text(
                apply_replacements(relative, text, platform=platform),
                encoding="utf-8",
            )
        else:
            shutil.copyfile(source_file, target)

    allowed = set(files)
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(destination).as_posix() not in allowed:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", default=load_manifest()["source_repo"])
    parser.add_argument("--source-revision", default=load_manifest()["source_revision"])
    parser.add_argument("--claude-destination", default=str(DEFAULT_CLAUDE_DESTINATION))
    parser.add_argument("--codex-destination", default=str(DEFAULT_CODEX_DESTINATION))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.source_repo).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="a-share-mainline-export-") as temp:
        source = Path(temp)
        materialize_source(repo, args.source_revision, source)
        export(source, Path(args.claude_destination).resolve(), platform="claude")
        export(source, Path(args.codex_destination).resolve(), platform="codex")
    print(
        f"exported {len(load_manifest()['files'])} files per platform to "
        f"{Path(args.claude_destination).resolve()} and {Path(args.codex_destination).resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
