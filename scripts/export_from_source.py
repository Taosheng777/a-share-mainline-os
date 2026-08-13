#!/usr/bin/env python3
"""从公开 canonical source 确定性生成 Claude 与 Codex Skill 分发树。"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "src/skills"
DEFAULT_CLAUDE_DESTINATION = ROOT / "plugins/a-share-mainline-os-claude/skills"
DEFAULT_CODEX_DESTINATION = ROOT / "plugins/a-share-mainline-os-codex/skills"
MANIFEST = ROOT / "config/export-manifest.json"
TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml"}

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
        (
            "（Agent 工具，subagent_type 用 general-purpose 或 claude）",
            "（用 `collaboration.spawn_agent`）",
        ),
        (
            "首席**收齐 5 份独立结果**",
            "首席用 `collaboration.wait_agent` **收齐 5 份独立结果**",
        ),
        ("只有 Agent 工具不可用", "只有 `collaboration` 工具不可用"),
    ),
    "stock-buddy/references/discipline.md": (
        ("账户纪律（v3，Claude Code 版）", "账户纪律（v3，Codex 版）"),
    ),
}


def load_manifest() -> dict:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("export manifest 缺少非空 files 列表")
    return value


def listed_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def validate_source(source: Path, expected: set[str]) -> None:
    actual = listed_files(source)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"canonical source 文件集不匹配：missing={missing}, extra={extra}")


def adapt_text(relative: str, text: str, *, platform: str) -> str:
    if platform != "codex":
        return text
    for old, new in CODEX_REPLACEMENTS.get(relative, ()):
        if old not in text:
            raise ValueError(f"Codex 适配锚点不存在：{relative}: {old}")
        text = text.replace(old, new)
    return text


def export(source: Path, destination: Path, *, platform: str) -> None:
    expected = set(load_manifest()["files"])
    validate_source(source, expected)
    destination.mkdir(parents=True, exist_ok=True)
    for relative in sorted(expected):
        source_file = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_file.suffix.lower() in TEXT_SUFFIXES:
            text = source_file.read_text(encoding="utf-8")
            target.write_text(adapt_text(relative, text, platform=platform), encoding="utf-8")
        else:
            shutil.copy2(source_file, target)

    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(destination).as_posix() not in expected:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def compare_trees(expected: Path, actual: Path) -> list[str]:
    expected_files = listed_files(expected)
    actual_files = listed_files(actual)
    failures = [f"missing:{item}" for item in sorted(expected_files - actual_files)]
    failures.extend(f"extra:{item}" for item in sorted(actual_files - expected_files))
    for relative in sorted(expected_files & actual_files):
        if (expected / relative).read_bytes() != (actual / relative).read_bytes():
            failures.append(f"changed:{relative}")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--claude-destination", default=str(DEFAULT_CLAUDE_DESTINATION))
    parser.add_argument("--codex-destination", default=str(DEFAULT_CODEX_DESTINATION))
    parser.add_argument("--check", action="store_true", help="验证已提交分发树可由公开源复现")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source).expanduser().resolve()
    claude_destination = Path(args.claude_destination).expanduser().resolve()
    codex_destination = Path(args.codex_destination).expanduser().resolve()
    count = len(load_manifest()["files"])

    if args.check:
        with tempfile.TemporaryDirectory(prefix="a-share-mainline-export-") as temp:
            root = Path(temp)
            claude_generated = root / "claude"
            codex_generated = root / "codex"
            export(source, claude_generated, platform="claude")
            export(source, codex_generated, platform="codex")
            failures = [
                *(f"claude:{item}" for item in compare_trees(claude_generated, claude_destination)),
                *(f"codex:{item}" for item in compare_trees(codex_generated, codex_destination)),
            ]
        if failures:
            print("PUBLIC EXPORT CHECK: FAIL")
            for failure in failures:
                print(f"- {failure}")
            return 1
        print(f"PUBLIC EXPORT CHECK: PASS ({count} files per platform)")
        return 0

    export(source, claude_destination, platform="claude")
    export(source, codex_destination, platform="codex")
    print(
        f"exported {count} files per platform from {source} to "
        f"{claude_destination} and {codex_destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
