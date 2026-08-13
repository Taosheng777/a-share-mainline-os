#!/usr/bin/env python3
"""从 Git 跟踪文件构建可审计的源码归档。"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--prefix", default="a-share-mainline-os-v0.1-beta/")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "archive", "--format=tar.gz", f"--prefix={args.prefix}", f"--output={output}", "HEAD"],
        cwd=ROOT,
        check=True,
    )
    expected = {
        f"{args.prefix}README.md",
        f"{args.prefix}LICENSE",
        f"{args.prefix}NOTICE",
        f"{args.prefix}CHANGELOG.md",
        f"{args.prefix}.agents/plugins/marketplace.json",
        f"{args.prefix}.claude-plugin/marketplace.json",
    }
    with tarfile.open(output, "r:gz") as archive:
        names = set(archive.getnames())
    missing = sorted(expected - names)
    if missing:
        raise SystemExit(f"发行归档缺文件：{missing}")
    forbidden = ("__pycache__", ".pyc", "/outputs/", "/.env")
    leaked = sorted(name for name in names if any(item in name for item in forbidden))
    if leaked:
        raise SystemExit(f"发行归档含本地产物：{leaked[:5]}")

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(output.name + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(output)
    print(checksum)
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
