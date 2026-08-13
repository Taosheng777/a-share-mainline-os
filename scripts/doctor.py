#!/usr/bin/env python3
"""只读检查安装、配置、vault 合同与免费数据 Skill 是否可用。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PRODUCT_SKILLS = ("stock-daily", "stock-screener", "stock-buddy")
VAULT_FILES = (
    "00-系统/AI操作规则.md",
    "01-纪律卡.md",
    "02-盘面日志/README.md",
    "03-持仓跟踪/持仓.md",
    "05-主线追踪/README.md",
)


def default_skills_root(platform: str) -> Path:
    home = Path.home()
    if platform == "codex":
        explicit = os.environ.get("CODEX_SKILLS_DIR")
        if explicit:
            return Path(explicit).expanduser()
        codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
        return codex_home / "skills"
    explicit = os.environ.get("CLAUDE_SKILLS_DIR")
    return Path(explicit).expanduser() if explicit else home / ".claude/skills"


def default_config() -> Path:
    configured = os.environ.get("ASM_CONFIG")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config/a-share-mainline/config.json"
    )


def check(ok: bool, detail: str, **extra: object) -> dict:
    return {"ok": ok, "detail": detail, **extra}


def load_config(path: Path) -> tuple[dict | None, dict]:
    if not path.is_file():
        return None, check(False, f"配置文件不存在：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, check(False, f"配置文件无法解析：{error}")
    if not isinstance(value, dict):
        return None, check(False, "配置顶层必须是 JSON 对象")
    vault_root = value.get("vault_root")
    if not isinstance(vault_root, str) or not vault_root.strip():
        return value, check(False, "缺少非空 vault_root")
    if not Path(vault_root).expanduser().is_absolute():
        return value, check(False, "vault_root 必须是绝对路径")
    return value, check(True, f"配置可解析：{path}")


def inspect(platform: str, skills_root: Path, config_path: Path) -> dict:
    skills_root = skills_root.expanduser()
    checks: dict[str, dict] = {}
    checks["python"] = check(
        sys.version_info >= (3, 11),
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    checks["product_version"] = check(True, VERSION)

    missing_skills = [
        skill for skill in PRODUCT_SKILLS if not (skills_root / skill / "SKILL.md").is_file()
    ]
    receipt_path = skills_root / ".a-share-mainline-os/install.json"
    receipt = None
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {"version": "无法读取"}
    installed_version = receipt.get("version") if isinstance(receipt, dict) else None
    version_ok = installed_version in (None, VERSION)
    checks["product_skills"] = check(
        not missing_skills and version_ok,
        "三个产品 Skill 已安装"
        if not missing_skills and version_ok
        else f"缺失={missing_skills}，安装记录版本={installed_version or '未记录'}",
        installed_version=installed_version,
    )

    config, config_result = load_config(config_path)
    checks["config"] = config_result
    if config_result["ok"] and config is not None:
        vault = Path(config["vault_root"]).expanduser()
        missing_vault = [relative for relative in VAULT_FILES if not (vault / relative).is_file()]
        checks["vault_contract"] = check(
            vault.is_dir() and not missing_vault,
            f"vault 合同完整：{vault}"
            if vault.is_dir() and not missing_vault
            else f"vault 不完整：missing={missing_vault}",
        )
    else:
        checks["vault_contract"] = check(False, "配置未通过，无法验证 vault")

    data_skill = skills_root / "a-stock-data/SKILL.md"
    checks["free_data_skill"] = check(
        data_skill.is_file(),
        "a-stock-data 已安装" if data_skill.is_file() else "缺少 a-stock-data 免费数据 Skill",
    )
    checks["optional_hithink_finance"] = check(
        shutil.which("hithink-finance") is not None,
        "已发现 hithink-finance" if shutil.which("hithink-finance") else "未安装（可选，不阻塞）",
        required=False,
    )
    wencai = config.get("wencai_cli") if isinstance(config, dict) else None
    checks["optional_wencai"] = check(
        bool(wencai),
        "已配置问财 CLI" if wencai else "未配置（可选，不阻塞）",
        required=False,
    )

    required = (
        "python",
        "product_version",
        "product_skills",
        "config",
        "vault_contract",
        "free_data_skill",
    )
    return {
        "project": "a-share-mainline-os",
        "version": VERSION,
        "platform": platform,
        "ready": all(checks[name]["ok"] for name in required),
        "checks": checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("codex", "claude"), required=True)
    parser.add_argument("--skills-root")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    skills_root = (
        Path(args.skills_root) if args.skills_root else default_skills_root(args.platform)
    )
    config_path = Path(args.config) if args.config else default_config()
    payload = inspect(args.platform, skills_root, config_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name, result in payload["checks"].items():
            marker = "PASS" if result["ok"] else ("INFO" if result.get("required") is False else "FAIL")
            print(f"[{marker}] {name}: {result['detail']}")
        print("DOCTOR: READY" if payload["ready"] else "DOCTOR: NOT READY")
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
