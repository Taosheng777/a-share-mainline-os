#!/usr/bin/env python3
"""无第三方依赖地校验 marketplace、插件元数据和 Skill frontmatter。"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"必须是 JSON 对象：{path}")
    return value


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert SEMVER.fullmatch(version)
    codex_plugin = ROOT / "plugins/a-share-mainline-os-codex"
    claude_plugin = ROOT / "plugins/a-share-mainline-os-claude"
    codex = load(codex_plugin / ".codex-plugin/plugin.json")
    claude = load(claude_plugin / ".claude-plugin/plugin.json")
    codex_market = load(ROOT / ".agents/plugins/marketplace.json")
    claude_market = load(ROOT / ".claude-plugin/marketplace.json")

    assert codex["name"] == claude["name"] == "a-share-mainline-os"
    assert codex["version"] == claude["version"] == version
    assert claude_market["metadata"]["version"] == version
    assert claude_market["plugins"][0]["version"] == version
    assert codex["skills"] == claude["skills"] == "./skills/"
    assert codex_market["plugins"][0]["source"]["path"].endswith("-codex")
    assert claude_market["plugins"][0]["source"].endswith("-claude")

    for plugin in (codex_plugin, claude_plugin, ROOT / "src"):
        for skill in ("stock-daily", "stock-screener", "stock-buddy"):
            text = (plugin / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            assert text.startswith("---\n")
            frontmatter = text.split("---", 2)[1]
            assert re.search(rf"(?m)^name: {re.escape(skill)}$", frontmatter)
            assert re.search(r"(?m)^description: .+", frontmatter)
    print("MANIFEST VALIDATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
