#!/usr/bin/env python3
"""对上游公开腾讯行情端点做一次只读、低频、可审计 smoke。"""

from __future__ import annotations

import json
import urllib.request
from datetime import date


URL = "https://qt.gtimg.cn/q=sh000001"


def main() -> int:
    request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read().decode("gbk", errors="replace")
    fields = raw.split("~")
    ok = len(fields) > 32 and fields[2] == "000001" and fields[3]
    payload = {
        "ok": bool(ok),
        "source": "腾讯财经公开行情",
        "symbol": "sh000001",
        "response_fields": len(fields),
        "executed_on": date.today().isoformat(),
        "note": "只读连通性检查；接口可能变更、限流或失效，不写 vault。",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
