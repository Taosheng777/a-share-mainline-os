#!/usr/bin/env python3
"""发布前静态审计：路径、凭据、真实账户材料和发行树结构。"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml", ".sh", ".toml", ".txt"}
FORBIDDEN_LITERALS = (
    "iCloud~md~" + "obsidian",
    "com~apple~" + "CloudDocs",
    "08-AI-" + "Workplace",
    "B" + "105",
    ".iwencai-" + "skillhub",
    "ifind." + "duckdb",
    "投资顾问" + "可以被替代",
)
PRIVATE_PATH_PATTERNS = (
    re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z]:\\\\Users\\\\[A-Za-z0-9._-]+\\\\"),
)
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(?:api[_-]?key|token|secret)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
)
ACCOUNT_ARTIFACTS = re.compile(
    r"(?i)(?:broker|account|holding|portfolio|position|券商|持仓|账户).*(?:png|jpe?g|heic|pdf)$"
)


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT
    )
    return [ROOT / item.decode() for item in output.split(b"\0") if item]


def audit(paths: list[Path]) -> list[str]:
    failures: list[str] = []
    for path in paths:
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            relative = path.name
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            failures.append(f"禁止发行 Python 缓存：{relative}")
        if ACCOUNT_ARTIFACTS.search(relative):
            failures.append(f"疑似账户附件：{relative}")
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in FORBIDDEN_LITERALS:
            if token in text:
                failures.append(f"私人或旧路径字面量：{relative}: {token}")
        for pattern in PRIVATE_PATH_PATTERNS:
            if pattern.search(text):
                failures.append(f"私人绝对路径：{relative}: {pattern.pattern}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"疑似凭据：{relative}: {pattern.pattern}")

    for plugin in (
        ROOT / "plugins/a-share-mainline-os-codex",
        ROOT / "plugins/a-share-mainline-os-claude",
    ):
        if not (plugin / "LICENSE").is_file() or not (plugin / "NOTICE").is_file():
            failures.append(f"插件缓存分发缺许可证或 NOTICE：{plugin.relative_to(ROOT)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    failures = audit(tracked_files())
    if failures:
        print("RELEASE AUDIT: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"RELEASE AUDIT: PASS ({len(tracked_files())} release files)")
    print("private paths=0, likely secrets=0, account artifacts=0, plugin notices=2/2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
