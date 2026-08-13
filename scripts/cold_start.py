#!/usr/bin/env python3
"""在干净 HOME 中验证安装、配置、复盘前置读取与离线载体筛选。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VAULT_TEMPLATE = ROOT / "vault-template"


def run(command: list[str], *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    print("$ " + " ".join(command))
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}")
    return completed


def write_fake_wencai_cli(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3
import json

print(json.dumps({
    "success": True,
    "code_count": 2,
    "returned_count": 2,
    "has_more": False,
    "datas": [
        {
            "股票代码": "111111.SH",
            "股票简称": "示例甲",
            "最新价[20260812]": 10.0,
            "换手率[20260812]": 4.0,
            "主力资金流向[20260812]": 12000000,
            "归母净利润同比增长率": 18.0,
            "净资产收益率": 9.0,
            "最新市盈率ttm": 20.0,
            "最新市净率": 2.0,
            "流通市值[20260812]": 3000000000
        },
        {
            "股票代码": "222222.SZ",
            "股票简称": "示例乙",
            "最新价[20260812]": 8.0,
            "换手率[20260812]": 3.0,
            "主力资金流向[20260812]": 8000000,
            "归母净利润同比增长率": 12.0,
            "净资产收益率": 7.0,
            "最新市盈率ttm": 24.0,
            "最新市净率": 2.5,
            "流通市值[20260812]": 2200000000
        }
    ]
}, ensure_ascii=False))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def prepare_environment(base: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = base / "home"
    vault = home / "investment-vault"
    config = home / ".config/a-share-mainline/config.json"
    fake_cli = home / "bin/fake-wencai.py"

    shutil.copytree(VAULT_TEMPLATE, vault)
    write_fake_wencai_cli(fake_cli)

    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "vault_root": str(vault),
                "wencai_cli": str(fake_cli),
                "git_identity": {"default": "[ai]", "codex": "[ai:codex]"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "ASM_CONFIG": str(config),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env, home, vault, config


def validate_daily_preflight(vault: Path, skill_root: Path) -> dict:
    required = [
        vault / "00-系统/AI操作规则.md",
        vault / "01-纪律卡.md",
        vault / "03-持仓跟踪/持仓.md",
        vault / "05-主线追踪/README.md",
        vault / "02-盘面日志/README.md",
        skill_root / "stock-daily/SKILL.md",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("复盘前置文件缺失：" + ", ".join(missing))
    discipline = (vault / "01-纪律卡.md").read_text(encoding="utf-8")
    return {
        "ok": True,
        "mode": "offline-preflight",
        "required_files": len(required),
        "discipline_thresholds": "未配置（按合同不可判定）"
        if discipline.count("阈值：\n") == 3
        else "模板异常",
        "network_calls": 0,
        "writes": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="保留临时 HOME 供人工复查")
    parser.add_argument(
        "--skip-codex-plugin",
        action="store_true",
        help="仅在没有 Codex CLI 的 CI 环境跳过 marketplace 安装检查",
    )
    args = parser.parse_args()

    holder = tempfile.TemporaryDirectory(prefix="a-share-mainline-cold-start-")
    base = Path(holder.name)
    try:
        env, home, vault, config = prepare_environment(base)
        print(f"COLD_START_HOME={home}")

        codex_target = home / ".codex/skills"
        install_env = dict(env, CODEX_SKILLS_DIR=str(codex_target))
        run(["bash", str(ROOT / "adapters/codex/install.sh")], env=install_env, cwd=ROOT)

        plugin_version = "skipped"
        if not args.skip_codex_plugin:
            if shutil.which("codex") is None:
                raise RuntimeError("未找到 Codex CLI；CI 可显式加 --skip-codex-plugin")
            codex_home = home / ".codex-plugin-test"
            codex_home.mkdir(parents=True)
            plugin_env = dict(env, CODEX_HOME=str(codex_home))
            run(
                ["codex", "plugin", "marketplace", "add", str(ROOT), "--json"],
                env=plugin_env,
                cwd=ROOT,
            )
            plugin = run(
                ["codex", "plugin", "add", "a-share-mainline-os@a-share-mainline-os", "--json"],
                env=plugin_env,
                cwd=ROOT,
            )
            plugin_payload = json.loads(plugin.stdout)
            plugin_version = plugin_payload.get("version")
            if plugin_version != "0.1.0-beta.1":
                raise RuntimeError("Codex marketplace 未安装预期插件版本")

        run(
            [sys.executable, "-m", "unittest", "discover", "-s", ".tests", "-p", "test_*.py"],
            env=env,
            cwd=vault,
        )

        daily = validate_daily_preflight(vault, codex_target)
        print("$ stock-daily offline preflight")
        print(json.dumps(daily, ensure_ascii=False, indent=2))

        result = run(
            [
                sys.executable,
                str(codex_target / "stock-screener/scripts/screen.py"),
                "--query",
                "离线示例筛选",
                "--top",
                "2",
                "--no-enrich",
                "--no-exclude-holdings",
                "--out-dir",
                str(home / "outputs"),
                "--report-date",
                "cold-start",
            ],
            env=env,
            cwd=home,
        )
        payload = json.loads(result.stdout)
        if not payload.get("ok") or payload.get("returned") != 2:
            raise RuntimeError("载体筛选离线 smoke 未返回两个示例结果")
        if payload.get("date") != "2026-08-12":
            raise RuntimeError("载体筛选未保留 fixture 的可审计数据日")

        print(json.dumps({
            "ok": True,
            "home": str(home),
            "config": str(config),
            "daily_preflight": daily["ok"],
            "screener_returned": payload["returned"],
            "codex_plugin_version": plugin_version,
            "personal_paths_required": False,
        }, ensure_ascii=False, indent=2))
        if args.keep:
            holder.cleanup = lambda: None
            print(f"临时 HOME 已保留：{home}")
        return 0
    finally:
        holder.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
