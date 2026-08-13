#!/usr/bin/env python3
"""本地与 GitHub Actions 共用的离线验收入口。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    print("$ " + " ".join(args), flush=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def main() -> int:
    suites = (
        ("tests", "test_*.py"),
        ("vault-template/.tests", "test_*.py"),
        ("plugins/a-share-mainline-os-codex/skills/stock-screener/tests", "test_screen.py"),
        ("plugins/a-share-mainline-os-codex/skills/stock-buddy/tests", "test_danger_scan.py"),
        ("plugins/a-share-mainline-os-claude/skills/stock-screener/tests", "test_screen.py"),
        ("plugins/a-share-mainline-os-claude/skills/stock-buddy/tests", "test_danger_scan.py"),
    )
    for directory, pattern in suites:
        run(sys.executable, "-m", "unittest", "discover", "-s", directory, "-p", pattern)
    run(sys.executable, "scripts/validate_manifests.py")
    run(sys.executable, "scripts/release_audit.py")
    run(sys.executable, "scripts/cold_start.py", "--skip-codex-plugin")
    print("OFFLINE CI: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
