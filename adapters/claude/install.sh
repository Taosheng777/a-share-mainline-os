#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
target="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$target"

for skill in stock-daily stock-screener stock-buddy; do
  destination="$target/$skill"
  if [[ -e "$destination" && "${ASM_FORCE_INSTALL:-0}" != "1" ]]; then
    echo "拒绝覆盖已有 Skill：${destination}；确认后设置 ASM_FORCE_INSTALL=1 重试" >&2
    exit 2
  fi
  if [[ -e "$destination" ]]; then
    backup="$destination.backup.$(date +%Y%m%d%H%M%S)"
    mv "$destination" "$backup"
    echo "已备份：$backup"
  fi
  cp -R "$repo_root/plugins/a-share-mainline-os-claude/skills/$skill" "$destination"
done

echo "Claude Skills 已安装到 ${target}"
