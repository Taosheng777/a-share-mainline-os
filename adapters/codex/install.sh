#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
args=(--platform codex)
if [[ "${ASM_FORCE_INSTALL:-0}" == "1" ]]; then
  args+=(--upgrade)
fi
exec python3 "$repo_root/scripts/manage_install.py" "${args[@]}" "$@"
