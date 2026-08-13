#!/usr/bin/env python3
"""在线确认固定上游 revision，并低频探测其文档声明的免费行情主干。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


UPSTREAM = "https://github.com/simonlin1212/a-stock-data.git"
VERSION = "v3.6.1"
COMMIT = "3a3149dedbe30cda58b5c94387039d7e707cedcd"
SKILL_SHA256 = "875868520ab25653aebbcee720891b99d87ea41b21d087175f98703009e9cd69"
QUOTE_URL = "https://qt.gtimg.cn/q=sh000001"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="a-stock-data-smoke-") as temp:
        checkout = Path(temp) / "repo"
        checkout.mkdir()
        subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin", UPSTREAM],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--quiet",
                "--depth",
                "1",
                "origin",
                COMMIT,
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            check=True,
        )
        commit = subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
        ).strip()
        digest = hashlib.sha256((checkout / "SKILL.md").read_bytes()).hexdigest()

    request = urllib.request.Request(
        QUOTE_URL,
        headers={"User-Agent": "a-share-mainline-os-live-smoke/0.1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("gbk", errors="replace")
    fields = raw.split("~")
    quote_ok = len(fields) > 32 and fields[2] == "000001" and bool(fields[3])
    payload = {
        "ok": commit == COMMIT and digest == SKILL_SHA256 and quote_ok,
        "upstream": UPSTREAM,
        "version": VERSION,
        "commit": commit,
        "commit_expected": COMMIT,
        "skill_sha256": digest,
        "skill_sha256_expected": SKILL_SHA256,
        "quote_source": "腾讯财经公开行情",
        "quote_symbol": "sh000001",
        "quote_response_fields": len(fields),
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "note": "只读、低频健康检查；失败不阻断离线 CI 或发布。",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
