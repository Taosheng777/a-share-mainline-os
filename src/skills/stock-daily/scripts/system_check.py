#!/usr/bin/env python3
"""Read-only project wiring and journal checks, never a profitability certificate."""
import argparse
import json
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decision_journal


def check(vault):
    vault = Path(vault).resolve()
    profile = json.loads((vault / "00-系统/投资运行配置.json").read_text())
    if not isinstance(profile, dict):
        raise ValueError("research profile must be an object")
    models = profile.get("allowed_models")
    if profile.get("workflow") != "astra_full_v1" or not isinstance(models, list) or not models \
            or not set(models) <= decision_journal.ALLOWED_MODELS:
        raise ValueError("wrong workflow or allowed models")
    if profile.get("schema_version") != 1 or not profile.get("method_version"):
        raise ValueError("unsupported or missing version")
    resolved = {}
    for key in ("contract", "journal", "shadow_journal", "validation_status"):
        relative = Path(profile[key])
        path = (vault / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(vault):
            raise ValueError(f"{key}: path must stay inside vault")
        if not path.is_file():
            raise ValueError(f"{key}: missing file")
        resolved[key] = path
    status = json.loads(resolved["validation_status"].read_text())
    if not isinstance(status, dict):
        raise ValueError("validation status must be an object")
    if status.get("method_version") != profile["method_version"]:
        raise ValueError("method versions disagree")
    if status.get("profitability") != "not_established":
        raise ValueError("this version has no approved promotion mechanism; review new evidence explicitly")
    runtime_manifest = vault / "00-系统/运行文件校验.json"
    runtime_check = "not_configured"
    if runtime_manifest.exists():
        hashes = json.loads(runtime_manifest.read_text())
        skills_root = Path(__file__).resolve().parents[2]
        # 两侧安装副本各有平台差异，指纹分平台登记：~/.claude 用 files_claude，其余用 files。
        platform_key = "files_claude" if any(p == ".claude" or p.endswith("-claude") for p in skills_root.parts) else "files"
        if not isinstance(hashes, dict) or platform_key not in hashes:
            raise ValueError("runtime manifest has no entry for this platform: " + platform_key)
        if not isinstance(hashes[platform_key], dict) or not hashes[platform_key]:
            raise ValueError("runtime manifest requires nonempty file hashes")
        for relative, expected in hashes[platform_key].items():
            target = (skills_root / relative).resolve()
            if Path(relative).is_absolute() or not target.is_relative_to(skills_root) or not target.is_file():
                raise ValueError("invalid runtime manifest path: " + relative)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("runtime changed since validation: " + relative)
        runtime_check = "matched_validated_snapshot"
    journals = {key: decision_journal.inspect(resolved[key]) for key in ("journal", "shadow_journal")}
    baselines = profile.get("baseline_journals", {})
    if not isinstance(baselines, dict):
        raise ValueError("baseline_journals must be an object")
    for key, value in baselines.items():
        relative = Path(value)
        path = (vault / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(vault) or not path.is_file():
            raise ValueError("baseline journal missing or outside vault")
        journals["baseline_" + key] = decision_journal.inspect(path)
    decision_ops_check = "not_configured"
    if "decision_ops" in profile:
        ops_config = profile["decision_ops"]
        if not isinstance(ops_config, dict):
            raise ValueError("decision_ops must be an object")
        for key in ("contract", "script", "calendar", "candidates", "actions", "outcomes"):
            relative = Path(ops_config[key])
            path = (vault / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(vault) or not path.is_file():
                raise ValueError("decision_ops missing or outside vault: " + key)
        # 配置审计不执行项目代码；运行行为另由项目回归测试检查。
        for key in ("candidates", "actions"):
            rows = [json.loads(line) for line in (vault / ops_config[key]).read_text().splitlines() if line.strip()]
            if len({r["id"] for r in rows}) != len(rows):
                raise ValueError("decision_ops duplicate ids: " + key)
        decision_ops_check = "paths_and_ids_checked"
    return {"ok": True, "configuration_consistency": "passed", "runtime_integrity": runtime_check, "decision_ops": decision_ops_check, "allowed_models": profile["allowed_models"], "actual_model_verification": "host_responsibility; config is not proof of model execution", "method_version": profile["method_version"], "profitability": "not_established", "engineering_tests": status.get("engineering_tests", "not_recorded"), "journals": journals, "next": "continue evidence capture during user-triggered reviews; no automatic trades or scheduled tasks"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vault", type=Path, required=True)
    a = p.parse_args()
    try:
        print(json.dumps(check(a.vault), ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, KeyError, OSError, TypeError) as e:
        p.exit(2, f"system_check_failed: {e}\n")


if __name__ == "__main__":
    main()
