#!/usr/bin/env python3
"""安装、升级或回滚三个 Skill；不读取或修改用户 vault 与业务配置。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
SKILLS = ("stock-daily", "stock-screener", "stock-buddy")
STATE_DIRECTORY = ".a-share-mainline-os"


def default_target(platform: str) -> Path:
    home = Path.home()
    if platform == "codex":
        explicit = os.environ.get("CODEX_SKILLS_DIR")
        if explicit:
            return Path(explicit).expanduser()
        codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
        return codex_home / "skills"
    explicit = os.environ.get("CLAUDE_SKILLS_DIR")
    return Path(explicit).expanduser() if explicit else home / ".claude/skills"


def default_source(platform: str) -> Path:
    return ROOT / f"plugins/a-share-mainline-os-{platform}/skills"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("codex", "claude"), required=True)
    parser.add_argument("--target", help="Skill 安装根目录；默认按平台和环境变量解析")
    parser.add_argument("--source-root", help="测试或本地构建使用的 Skill 分发树")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--upgrade", action="store_true", help="显式替换已有 Skill 并创建批次备份")
    action.add_argument("--rollback", action="store_true", help="恢复最近一次升级前的三个 Skill")
    parser.add_argument("--dry-run", action="store_true", help="只报告将执行的动作")
    return parser.parse_args()


def validate_skill_tree(root: Path) -> None:
    for skill in SKILLS:
        skill_file = root / skill / "SKILL.md"
        if not skill_file.is_file():
            raise ValueError(f"分发树缺少 {skill_file}")
        text = skill_file.read_text(encoding="utf-8")
        if not text.startswith("---\n") or f"\nname: {skill}\n" not in text:
            raise ValueError(f"Skill frontmatter 无效：{skill_file}")


def read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"状态文件必须是 JSON 对象：{path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def install(
    *, platform: str, target: Path, source: Path, upgrade: bool, dry_run: bool
) -> int:
    validate_skill_tree(source)
    existing = [skill for skill in SKILLS if (target / skill).exists()]
    if existing and not upgrade:
        print(
            "拒绝覆盖已有 Skill："
            + ", ".join(str(target / skill) for skill in existing)
            + "；请先运行 --upgrade --dry-run，再显式使用 --upgrade",
            file=sys.stderr,
        )
        return 2

    verb = "升级" if existing else "安装"
    if dry_run:
        print(f"DRY RUN：将{verb} {platform} Skills 到 {target}")
        print("范围：" + ", ".join(SKILLS))
        print("不会读取或修改用户 config 与 vault")
        return 0

    target = target.resolve()
    if target in (Path("/"), Path.home().resolve()):
        raise ValueError(f"拒绝使用过宽安装目录：{target}")
    target.mkdir(parents=True, exist_ok=True)
    state = target / STATE_DIRECTORY
    stage = state / f"stage-{uuid.uuid4().hex}"
    stage_skills = stage / "skills"
    for skill in SKILLS:
        shutil.copytree(source / skill, stage_skills / skill)
    validate_skill_tree(stage_skills)

    receipt_path = state / "install.json"
    previous_receipt = read_json(receipt_path)
    batch = None
    moved: list[str] = []
    if existing:
        batch_id = f"{timestamp()}-{uuid.uuid4().hex[:8]}"
        batch = state / "backups" / batch_id
        (batch / "skills").mkdir(parents=True)
        write_json(
            batch / "metadata.json",
            {
                "id": batch_id,
                "platform": platform,
                "to_version": VERSION,
                "present": existing,
                "previous_receipt": previous_receipt,
            },
        )

    try:
        if batch:
            for skill in existing:
                shutil.move(str(target / skill), str(batch / "skills" / skill))
                moved.append(skill)
        for skill in SKILLS:
            shutil.move(str(stage_skills / skill), str(target / skill))
        validate_skill_tree(target)
        write_json(
            receipt_path,
            {
                "project": "a-share-mainline-os",
                "version": VERSION,
                "platform": platform,
                "installed_at": datetime.now(UTC).isoformat(),
                "backup_id": batch.name if batch else None,
            },
        )
    except Exception:
        for skill in SKILLS:
            remove_tree(target / skill)
        if batch:
            for skill in moved:
                shutil.move(str(batch / "skills" / skill), str(target / skill))
        if previous_receipt is None:
            receipt_path.unlink(missing_ok=True)
        else:
            write_json(receipt_path, previous_receipt)
        if batch:
            remove_tree(batch)
        raise
    finally:
        remove_tree(stage)

    print(f"{verb}完成：{platform} Skills {VERSION} → {target}")
    if batch:
        print(f"升级前备份：{batch}")
    print("用户 config 与 vault 未读取、未修改")
    return 0


def latest_backup(state: Path) -> Path:
    root = state / "backups"
    candidates = sorted(
        (path for path in root.iterdir() if path.is_dir() and (path / "metadata.json").is_file()),
        reverse=True,
    ) if root.is_dir() else []
    if not candidates:
        raise ValueError("没有可用的升级备份")
    return candidates[0]


def rollback(*, platform: str, target: Path, dry_run: bool) -> int:
    target = target.expanduser().resolve()
    state = target / STATE_DIRECTORY
    backup = latest_backup(state)
    metadata = read_json(backup / "metadata.json") or {}
    if metadata.get("platform") != platform:
        raise ValueError(f"最近备份不属于 {platform}：{backup.name}")
    present = metadata.get("present")
    if not isinstance(present, list) or not set(present).issubset(SKILLS):
        raise ValueError(f"备份 metadata 无效：{backup}")

    if dry_run:
        print(f"DRY RUN：将从 {backup} 回滚 {platform} Skills")
        print("不会读取或修改用户 config 与 vault")
        return 0

    safety = state / f"rollback-stage-{uuid.uuid4().hex}"
    safety.mkdir(parents=True)
    moved_current: list[str] = []
    restored: list[str] = []
    receipt_path = state / "install.json"
    current_receipt = read_json(receipt_path)
    try:
        for skill in SKILLS:
            current = target / skill
            if current.exists():
                shutil.move(str(current), str(safety / skill))
                moved_current.append(skill)
        for skill in present:
            source = backup / "skills" / skill
            if not source.is_dir():
                raise ValueError(f"备份缺少 {skill}：{backup}")
            shutil.copytree(source, target / skill)
            restored.append(skill)
        previous_receipt = metadata.get("previous_receipt")
        if previous_receipt is None:
            receipt_path.unlink(missing_ok=True)
        elif isinstance(previous_receipt, dict):
            write_json(receipt_path, previous_receipt)
        else:
            raise ValueError(f"备份 receipt 无效：{backup}")
    except Exception:
        for skill in restored:
            remove_tree(target / skill)
        for skill in moved_current:
            shutil.move(str(safety / skill), str(target / skill))
        if current_receipt is not None:
            write_json(receipt_path, current_receipt)
        raise
    finally:
        remove_tree(safety)

    print(f"回滚完成：{platform} Skills ← {backup.name}")
    print("用户 config 与 vault 未读取、未修改")
    return 0


def main() -> int:
    args = parse_args()
    target = Path(args.target).expanduser() if args.target else default_target(args.platform)
    source = (
        Path(args.source_root).expanduser().resolve()
        if args.source_root
        else default_source(args.platform)
    )
    if args.rollback:
        return rollback(platform=args.platform, target=target, dry_run=args.dry_run)
    return install(
        platform=args.platform,
        target=target,
        source=source,
        upgrade=args.upgrade,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
