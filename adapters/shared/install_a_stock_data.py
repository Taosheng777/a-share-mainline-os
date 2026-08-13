#!/usr/bin/env python3
"""安装并校验外部 a-stock-data Skill；不把其源码纳入本仓库。"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path


UPSTREAM = "https://github.com/simonlin1212/a-stock-data.git"
VERSION = "v3.6.1"
COMMIT = "3a3149dedbe30cda58b5c94387039d7e707cedcd"
SKILL_SHA256 = "875868520ab25653aebbcee720891b99d87ea41b21d087175f98703009e9cd69"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="目标 a-stock-data Skill 目录")
    parser.add_argument("--force", action="store_true", help="覆盖已有目标，先保留 .backup")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = Path(args.target).expanduser().resolve()
    if target.exists() and not args.force:
        raise SystemExit(f"拒绝覆盖已有目录：{target}；确认后加 --force")

    with tempfile.TemporaryDirectory(prefix="a-stock-data-install-") as temp:
        checkout = Path(temp) / "repo"
        checkout.mkdir()
        subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin", UPSTREAM],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "fetch", "--quiet", "--depth", "1", "origin", COMMIT],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            check=True,
        )
        commit = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
        ).strip()
        if commit != COMMIT:
            raise SystemExit(f"上游 commit 漂移：期望 {COMMIT}，实际 {commit}")
        skill = checkout / "SKILL.md"
        digest = hashlib.sha256(skill.read_bytes()).hexdigest()
        if digest != SKILL_SHA256:
            raise SystemExit(f"上游 SKILL.md 校验失败：{digest}")

        if target.exists():
            backup = target.with_name(target.name + ".backup")
            if backup.exists():
                shutil.rmtree(backup)
            target.rename(backup)
            print(f"已备份：{backup}")
        target.mkdir(parents=True)
        shutil.copy2(skill, target / "SKILL.md")
        shutil.copy2(checkout / "LICENSE", target / "LICENSE.upstream")

    print(f"installed {VERSION} ({COMMIT}) to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
