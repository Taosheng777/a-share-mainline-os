import json
import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLAUDE_PLUGIN = ROOT / "plugins/a-share-mainline-os-claude"
CODEX_PLUGIN = ROOT / "plugins/a-share-mainline-os-codex"
CLAUDE_SKILLS = CLAUDE_PLUGIN / "skills"
CODEX_SKILLS = CODEX_PLUGIN / "skills"
VAULT = ROOT / "vault-template"


class ProductContract(unittest.TestCase):
    def test_required_repository_structure_exists(self):
        for path in (
            ROOT / ".claude-plugin/marketplace.json",
            ROOT / ".agents/plugins/marketplace.json",
            CLAUDE_PLUGIN / ".claude-plugin/plugin.json",
            CODEX_PLUGIN / ".codex-plugin/plugin.json",
            CLAUDE_PLUGIN / "LICENSE",
            CLAUDE_PLUGIN / "NOTICE",
            CODEX_PLUGIN / "LICENSE",
            CODEX_PLUGIN / "NOTICE",
            ROOT / "adapters/claude/install.sh",
            ROOT / "adapters/codex/install.sh",
            ROOT / "config/config.example.json",
            ROOT / "scripts/cold_start.py",
            ROOT / "scripts/export_from_source.py",
            ROOT / "adapters/shared/install_a_stock_data.py",
            ROOT / "adapters/shared/a_stock_data_smoke.py",
            ROOT / "adapters/shared/a_stock_data_upstream_smoke.py",
            ROOT / ".github/workflows/ci.yml",
            ROOT / ".github/workflows/live-smoke.yml",
        ):
            self.assertTrue(path.is_file(), path)

        for skill in ("stock-daily", "stock-screener", "stock-buddy"):
            self.assertTrue((CLAUDE_SKILLS / skill / "SKILL.md").is_file(), skill)
            self.assertTrue((CODEX_SKILLS / skill / "SKILL.md").is_file(), skill)

    def test_manifests_have_consistent_identity(self):
        codex = json.loads((CODEX_PLUGIN / ".codex-plugin/plugin.json").read_text())
        claude = json.loads((CLAUDE_PLUGIN / ".claude-plugin/plugin.json").read_text())
        codex_market = json.loads(
            (ROOT / ".agents/plugins/marketplace.json").read_text()
        )
        claude_market = json.loads(
            (ROOT / ".claude-plugin/marketplace.json").read_text()
        )

        self.assertEqual(codex["name"], "a-share-mainline-os")
        self.assertEqual(claude["name"], codex["name"])
        self.assertEqual(codex["version"], "0.1.0-beta.1")
        self.assertEqual(claude["version"], codex["version"])
        self.assertEqual(claude["skills"], "./skills/")
        self.assertEqual(codex["skills"], "./skills/")
        self.assertEqual(
            codex_market["plugins"][0]["source"]["path"],
            "./plugins/a-share-mainline-os-codex",
        )
        self.assertEqual(
            claude_market["plugins"][0]["source"],
            "./plugins/a-share-mainline-os-claude",
        )

    def test_export_manifest_matches_public_skill_tree(self):
        manifest = json.loads((ROOT / "config/export-manifest.json").read_text())
        expected = set(manifest["files"])
        for root in (CLAUDE_SKILLS, CODEX_SKILLS):
            actual = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual, expected)
            self.assertTrue(all(not path.endswith(".pyc") for path in actual))
            self.assertTrue(all("__pycache__" not in path for path in actual))

    def test_skill_frontmatter_and_codex_metadata(self):
        for skill in ("stock-daily", "stock-screener", "stock-buddy"):
            for root in (CLAUDE_SKILLS, CODEX_SKILLS):
                text = (root / skill / "SKILL.md").read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\n"), skill)
                frontmatter = text.split("---", 2)[1]
                self.assertRegex(frontmatter, rf"(?m)^name: {re.escape(skill)}$")
                self.assertRegex(frontmatter, r"(?m)^description: .+")
            agent = (CODEX_SKILLS / skill / "agents/openai.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"${skill}", agent)

        claude_buddy = (CLAUDE_SKILLS / "stock-buddy/SKILL.md").read_text()
        codex_buddy = (CODEX_SKILLS / "stock-buddy/SKILL.md").read_text()
        self.assertIn("AskUserQuestion", claude_buddy)
        self.assertNotIn("collaboration.spawn_agent", claude_buddy)
        self.assertIn("collaboration.spawn_agent", codex_buddy)

    def test_vault_template_has_required_empty_contract(self):
        required = (
            "投资笔记.md",
            "00-系统/AI操作规则.md",
            "01-纪律卡.md",
            "02-盘面日志/README.md",
            "02-盘面日志/示例复盘.md",
            "03-持仓跟踪/持仓.md",
            "05-主线追踪/README.md",
            "05-主线追踪/_主线页模板.md",
            "05-主线追踪/归档/_退潮判定台账.md",
            ".tests/test_vault_contract.py",
        )
        for relative in required:
            self.assertTrue((VAULT / relative).is_file(), relative)

        discipline = (VAULT / "01-纪律卡.md").read_text(encoding="utf-8")
        for label in ("① 主线失效退出", "② 单笔止损线", "③ 账户回撤熔断"):
            self.assertIn(label, discipline)
        self.assertIn("阈值：", discipline)

        holdings = (VAULT / "03-持仓跟踪/持仓.md").read_text(encoding="utf-8")
        self.assertIn("| 标的 | 角色 | 所属主线 | 止损参考位 | 状态 |", holdings)
        self.assertNotRegex(holdings, r"(?m)^\| [^|_]+ \| (?:底仓|机动)")

    def test_config_example_is_portable(self):
        config = json.loads((ROOT / "config/config.example.json").read_text())
        self.assertEqual(config["vault_root"], "__VAULT_ROOT__")
        self.assertEqual(config["git_identity"]["default"], "[ai]")
        self.assertNotIn("wencai_cli", config)
        self.assertNotIn("ifind_evidence_helper", config)

    def test_public_text_has_no_private_or_stale_runtime_values(self):
        forbidden = (
            "tao" + "sheng",
            "iCloud~md~" + "obsidian",
            "com~apple~" + "CloudDocs",
            "08-AI-" + "Workplace",
            "B" + "105",
        )
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix.lower() not in {".md", ".py", ".json", ".yaml", ".yml", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path}: {token}")

        personal_home = "/" + "Users/" + "tao" + "sheng"
        for path in ROOT.rglob("*"):
            if (
                path.is_file()
                and ".git" not in path.parts
                and path.suffix.lower() in {".md", ".py", ".json", ".yaml", ".yml", ".sh"}
            ):
                self.assertNotIn(
                    personal_home,
                    path.read_text(encoding="utf-8", errors="replace"),
                    str(path),
                )

        stale_risk = "0." + "75%"
        private_examples = (
            "600" + "036.SH",
            "002" + "466.SZ",
            "515" + "880",
            "159" + "770",
        )
        for root in (CLAUDE_SKILLS, CODEX_SKILLS):
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".md", ".py", ".json", ".yaml", ".yml"}:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    self.assertNotIn(stale_risk, text, str(path))
                    for token in private_examples:
                        self.assertNotIn(token, text, str(path))

    def test_release_archive_excludes_local_artifacts(self):
        if not (ROOT / "CHANGELOG.md").is_file():
            self.skipTest("release archive is checked after the release commit exists")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "release.tar.gz"
            import subprocess

            subprocess.run(
                ["python3", str(ROOT / "scripts/build_release.py"), "--output", str(output)],
                check=True,
                cwd=ROOT,
            )
            self.assertTrue(output.is_file())

    def test_ci_and_live_smoke_stay_separate(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        live = (ROOT / ".github/workflows/live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/run_ci.py", ci)
        self.assertNotIn("schedule:", ci)
        self.assertNotIn("a_stock_data_upstream_smoke.py", ci)
        self.assertIn("schedule:", live)
        self.assertIn("workflow_dispatch:", live)
        self.assertIn("continue-on-error: true", live)
        self.assertIn("python -u", live)


if __name__ == "__main__":
    unittest.main()
