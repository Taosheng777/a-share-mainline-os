#!/usr/bin/env python3
"""Append-only research/advice journal. No market calls, trades, or return claims."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

GENESIS = "0" * 64
LOCAL_ZONE = timezone(timedelta(hours=8))
# 可选研究运行层支持的模型标识；record.model 必须如实填写实际执行模型。
ALLOWED_MODELS = {"gpt-6-astra", "claude-opus-5-5"}
REQUIRED = {
    "schema_version", "decision_id", "theme_id", "data_date", "decided_at",
    "model", "method_version", "mode", "theme_type", "thesis", "evidence",
    "action", "previous_decision_id", "change_reason", "invalidation",
    "triggered_conditions", "discipline", "user_execution", "confidence",
    "key_data_status", "evaluation_context",
}


class JournalError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise JournalError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def timestamp(value):
    require(nonempty(value), "timestamp must be a timezone-aware ISO string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JournalError("invalid timestamp") from exc
    require(result.tzinfo is not None and result.utcoffset() is not None,
            "timestamp requires timezone")
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(record, previous_hash, recorded_at):
    return hashlib.sha256(canonical({"record": record, "previous_hash": previous_hash, "recorded_at": recorded_at})
                          .encode("utf-8")).hexdigest()


def validate_recording_time(record, recorded_at, now):
    recorded = timestamp(recorded_at)
    require(recorded <= now, "future recorded_at")
    require(timestamp(record["decided_at"]) <= recorded, "decided_at cannot exceed recorded_at")
    if record["mode"] != "historical_audit":
        recording_day = recorded.astimezone(LOCAL_ZONE).date()
        require(timestamp(record["decided_at"]).astimezone(LOCAL_ZONE).date() == recording_day,
                "prospective record requires current +08 decision date; backfill uses historical_audit")


def validate_record(record, prior, now=None):
    now = now or datetime.now(timezone.utc)
    require(isinstance(record, dict), "record must be an object")
    require(REQUIRED <= record.keys(), "missing fields: " + ", ".join(sorted(REQUIRED - record.keys())))
    require(record.keys() <= REQUIRED | {"override", "research_target", "research_asset"}, "unknown record fields")
    require(type(record["schema_version"]) is int and record["schema_version"] == 1,
            "schema_version must be 1")
    for field in ("decision_id", "theme_id", "method_version", "thesis"):
        require(nonempty(record[field]), field + " must be nonempty")
    require(record["model"] in ALLOWED_MODELS, "model must be one of: " + ", ".join(sorted(ALLOWED_MODELS)))
    require(record["mode"] in ("shadow", "live_advice", "historical_audit"), "invalid mode")
    require(record["theme_type"] in ("industrial", "event", "sentiment", "unknown"), "invalid theme_type")
    require(record["action"] in ("observe", "enter", "hold", "reduce", "exit"), "invalid action")
    require(record["confidence"] in ("low", "medium", "high", "unknown"), "confidence must be qualitative")
    require(record["key_data_status"] in ("complete", "missing"), "invalid key_data_status")
    expected_context = "retrospective" if record["mode"] == "historical_audit" else "prospective"
    require(record["evaluation_context"] == expected_context,
            "historical audit must be retrospective; other modes prospective")
    decided = timestamp(record["decided_at"])
    require(decided <= now, "future decided_at")
    try:
        data_day = date.fromisoformat(record["data_date"])
    except (ValueError, TypeError) as exc:
        raise JournalError("data_date must be YYYY-MM-DD") from exc
    require(record["data_date"] == data_day.isoformat(), "data_date must be YYYY-MM-DD")
    require(data_day <= decided.date() and data_day <= now.astimezone(decided.tzinfo).date(), "future data_date")
    require(not any(item["decision_id"] == record["decision_id"] for item in prior), "duplicate decision_id")

    chain = [item for item in prior if item["theme_id"] == record["theme_id"] and item["mode"] == record["mode"]]
    previous = chain[-1] if chain else None
    require(record["previous_decision_id"] == (previous["decision_id"] if previous else None),
            "previous_decision_id must reference latest decision in same theme AND mode")
    if previous:
        require(decided >= timestamp(previous["decided_at"]) and data_day >= date.fromisoformat(previous["data_date"]),
                "backdated decision cannot replace latest state")
    require(("research_asset" in record) == ("research_target" in record), "research_target and research_asset must appear together")
    if "research_target" in record:
        target = record["research_target"]
        asset = record["research_asset"]
        require(isinstance(asset, str) and re.fullmatch(r"[0-9]{6}", asset) is not None,
                "research_asset must be a six-digit stock/ETF code")
        previous_assets = [item["research_asset"] for item in chain if "research_asset" in item]
        require(not previous_assets or asset == previous_assets[-1], "research_asset cannot change within theme/mode chain; use a new episode theme_id")
        require(record["mode"] == "shadow", "research_target is shadow-only, not a user allocation")
        require(type(target) in (int, float) and math.isfinite(target) and 0 <= target <= 1, "research_target must be finite number in [0,1]")
        require(record["action"] != "observe", "observe cannot carry research_target")
        if record["action"] == "enter":
            require(target > 0, "enter research_target must be positive")
        if record["action"] == "exit":
            require(target == 0, "exit research_target must be zero")
        if record["action"] in ("hold", "reduce"):
            previous_targets = [item["research_target"] for item in chain if "research_target" in item]
            require(bool(previous_targets), "hold/reduce research_target requires prior research target")
            if record["action"] == "hold":
                require(target == previous_targets[-1], "hold research_target must equal prior target")
            else:
                require(target < previous_targets[-1], "reduce research_target must decrease prior target")

    evidence = record["evidence"]
    require(isinstance(evidence, list), "evidence must be a list")
    evidence_by_id = {}
    for item in evidence:
        require(isinstance(item, dict) and set(item) == {"id", "source", "available_at", "kind"},
                "evidence requires id/source/available_at/kind")
        require(nonempty(item["id"]) and nonempty(item["source"]), "evidence id/source must be nonempty")
        require(item["id"] not in evidence_by_id, "duplicate evidence id")
        require(item["kind"] in ("observation", "event", "user_instruction"), "invalid evidence kind")
        require(timestamp(item["available_at"]) <= decided, "future evidence unavailable at decision time")
        evidence_by_id[item["id"]] = item
    old_evidence = {item["id"]: item for row in chain for item in row["evidence"]}
    for identity, item in evidence_by_id.items():
        require(identity not in old_evidence or old_evidence[identity] == item,
                "existing evidence id cannot be rewritten")
    fresh_ids = {identity for identity, item in evidence_by_id.items()
                 if identity not in old_evidence and
                 (previous is None or timestamp(item["available_at"]) > timestamp(previous["decided_at"]))}

    invalidation = record["invalidation"]
    require(isinstance(invalidation, list), "invalidation must be a list")
    condition_ids = set()
    for item in invalidation:
        require(isinstance(item, dict) and set(item) == {"id", "text"} and
                nonempty(item["id"]) and nonempty(item["text"]), "invalidation requires id/text")
        require(item["id"] not in condition_ids, "duplicate invalidation id")
        condition_ids.add(item["id"])
    triggered = record["triggered_conditions"]
    require(isinstance(triggered, list) and all(nonempty(x) for x in triggered) and len(triggered) == len(set(triggered)),
            "triggered_conditions must be unique string ids")
    previous_conditions = {item["id"]: item["text"] for item in previous["invalidation"]} if previous else {}
    require(set(triggered) <= previous_conditions.keys(), "triggered condition must have been preregistered in previous decision")
    for item in invalidation:
        require(item["id"] not in previous_conditions or item["text"] == previous_conditions[item["id"]],
                "condition text changed under existing id; use a new id")

    reason = record["change_reason"]
    require(isinstance(reason, dict) and set(reason) == {"kind", "text", "evidence_ids"}, "change_reason requires kind/text/evidence_ids")
    require(reason["kind"] in ("initial", "condition", "user_instruction", "new_event", "continuation", "discipline"), "invalid change_reason kind")
    require(nonempty(reason["text"]), "change_reason text must be nonempty")
    reason_ids = reason["evidence_ids"]
    require(isinstance(reason_ids, list) and all(nonempty(x) for x in reason_ids), "invalid reason evidence_ids")
    require(set(reason_ids) <= evidence_by_id.keys(), "reason references missing evidence")
    if previous is None:
        require(reason["kind"] in ("initial", "discipline"), "first decision requires initial or discipline reason")
    else:
        require(reason["kind"] != "initial", "initial reason only valid for first decision")
        # Enter -> hold (or reduce -> hold) is continuation, not a new trade.
        continued_action = (previous["action"] in ("enter", "hold", "reduce") and record["action"] == "hold") or (previous["action"] == "exit" and record["action"] == "observe")
        action_changed = record["action"] != previous["action"] and not continued_action
        content_changed = any(record[field] != previous[field] for field in
                              ("thesis", "theme_type", "method_version", "invalidation"))
        changed = action_changed or content_changed
        target_changed = False
        if "research_target" in record:
            previous_targets = [item["research_target"] for item in chain if "research_target" in item]
            target_changed = not previous_targets or record["research_target"] != previous_targets[-1]
            changed = changed or target_changed
        # Missing data may always move to observation; it must not manufacture a safe hold.
        missing_observation = record["action"] == "observe" and record["key_data_status"] == "missing" and not content_changed and not target_changed
        if changed and not missing_observation:
            require(reason["kind"] in ("condition", "user_instruction", "new_event", "discipline"),
                    "action change requires preregistered condition, user instruction, or new event (also applies to thesis/method/conditions)")
        if reason["kind"] == "condition":
            require(bool(triggered) and bool(set(reason_ids) & fresh_ids), "condition trigger requires fresh supporting evidence")
        if reason["kind"] in ("user_instruction", "new_event"):
            kind = "event" if reason["kind"] == "new_event" else "user_instruction"
            require(any(identity in fresh_ids and evidence_by_id[identity]["kind"] == kind for identity in reason_ids),
                    "change requires fresh evidence with matching event/instruction kind")

    discipline = record["discipline"]
    require(isinstance(discipline, dict) and set(discipline) == {"1", "2", "3"} and
            all(x in ("clear", "triggered", "unknown") for x in discipline.values()), "discipline requires 1/2/3 statuses")
    if reason["kind"] == "discipline":
        allowed = (any(discipline[key] == "triggered" for key in ("1", "2")) and record["action"] == "exit") or \
                  (discipline["3"] == "triggered" and record["action"] in ("observe", "reduce", "exit"))
        require(allowed, "discipline reason requires triggered 1/2 with exit or triggered 3 with observe/reduce/exit")
        require(any(identity in fresh_ids and evidence_by_id[identity]["kind"] == "observation" for identity in reason_ids),
                "discipline reason requires fresh observation evidence")
        require(previous is None or all(record[field] == previous[field] for field in
                ("thesis", "theme_type", "method_version", "invalidation")), "discipline action cannot also rewrite thesis/method/type/conditions")
    override = record.get("override")
    exempt = set()
    if override is not None:
        require(isinstance(override, dict) and set(override) == {"source", "available_at", "conditions"}, "invalid override")
        require(override["source"] in evidence_by_id and evidence_by_id[override["source"]]["kind"] == "user_instruction",
                "override source must reference user_instruction evidence id")
        require(timestamp(override["available_at"]) == timestamp(evidence_by_id[override["source"]]["available_at"]),
                "override time must match its instruction evidence")
        require(isinstance(override["conditions"], list) and bool(override["conditions"]) and
                all(x in ("1", "2", "3") for x in override["conditions"]), "invalid override discipline ids")
        exempt = set(override["conditions"])
    active_hard = {key for key in ("1", "2") if discipline[key] == "triggered"} - exempt
    require(not active_hard or record["action"] == "exit", "discipline 1/2 requires exit unless sourced user case override")
    if record["action"] == "enter":
        require(record["key_data_status"] == "complete" and record["theme_type"] != "unknown", "enter requires complete key data and known theme type")
        require(all(value == "clear" for value in discipline.values()), "enter requires all discipline statuses clear; override cannot authorize entry")
        require(bool(invalidation) and bool(fresh_ids), "enter requires preregistered invalidation and fresh evidence")
    if record["action"] == "hold":
        require(record["key_data_status"] == "complete" and bool(fresh_ids), "hold requires complete key data and fresh evidence; use observe with explanation")

    execution = record["user_execution"]
    require(isinstance(execution, dict) and execution.get("status") in ("unconfirmed", "confirmed"), "invalid user_execution")
    if execution["status"] == "unconfirmed":
        require(set(execution) == {"status"}, "unconfirmed execution must not contain invented facts")
    else:
        require(set(execution) == {"status", "source", "available_at"} and nonempty(execution["source"]), "confirmed execution requires source/time")
        require(timestamp(execution["available_at"]) <= decided, "future execution confirmation")
        require(record["mode"] != "shadow", "shadow cannot claim confirmed real execution")


def read_rows(handle, now=None):
    now = now or datetime.now(timezone.utc)
    rows, records, previous_hash = [], [], GENESIS
    for number, line in enumerate(handle, 1):
        try:
            require(line.endswith("\n"), "incomplete JSONL line")
            row = json.loads(line)
            require(isinstance(row, dict) and set(row) == {"record", "previous_hash", "recorded_at", "hash"}, "invalid envelope")
            require(row["previous_hash"] == previous_hash, "hash chain broken")
            require(row["hash"] == digest(row["record"], previous_hash, row["recorded_at"]), "hash mismatch")
            validate_record(row["record"], records, timestamp(row["recorded_at"]))
            validate_recording_time(row["record"], row["recorded_at"], now)
            require(not rows or timestamp(row["recorded_at"]) >= timestamp(rows[-1]["recorded_at"]), "recording clock moved backwards")
            rows.append(row)
            records.append(row["record"])
            previous_hash = row["hash"]
        except (JournalError, ValueError, TypeError, KeyError) as exc:
            raise JournalError(f"line {number}: {exc}") from exc
    return rows


@contextmanager
def locked_file(path, write=False):
    path = Path(path)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+" if write else "r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            handle.seek(0)
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append(path, record, now=None):
    with locked_file(path, write=True) as handle:
        now = now or datetime.now(timezone.utc)
        rows = read_rows(handle, now)
        for row in rows:
            if row["record"]["decision_id"] == record.get("decision_id"):
                require(row["record"] == record, "duplicate decision_id with different content")
                return {"ok": True, "result": "already_present", "hash": row["hash"]}
        validate_record(record, [row["record"] for row in rows], now)
        recorded_at = now.isoformat()
        validate_recording_time(record, recorded_at, now)
        require(not rows or now >= timestamp(rows[-1]["recorded_at"]), "recording clock moved backwards")
        previous_hash = rows[-1]["hash"] if rows else GENESIS
        row = {"record": record, "previous_hash": previous_hash, "recorded_at": recorded_at,
               "hash": digest(record, previous_hash, recorded_at)}
        handle.write(canonical(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return {"ok": True, "result": "appended", "hash": row["hash"]}


def inspect(path, theme_id=None):
    with locked_file(path) as handle:
        rows = read_rows(handle)
    latest = {}
    for row in rows:
        record = row["record"]
        if theme_id is None or record["theme_id"] == theme_id:
            latest[(record["theme_id"], record["mode"])] = row
    fields = ("decision_id", "theme_id", "mode", "evaluation_context", "data_date", "decided_at", "action", "thesis",
              "method_version", "invalidation", "discipline", "key_data_status", "confidence", "user_execution", "change_reason")
    return {"ok": True, "engineering_validation": "passed", "profitability": "unverified",
            "integrity_scope": "hash-chain consistency only; not protection against deliberate full rewrite or suffix deletion",
            "records": len(rows), "head_hash": rows[-1]["hash"] if rows else GENESIS,
            "latest": [{**{field: row["record"][field] for field in fields},
                        **({"research_target": row["record"]["research_target"], "research_asset": row["record"]["research_asset"]} if "research_target" in row["record"] else {}),
                        "recorded_at": row["recorded_at"],
                        "evaluation_effective_at": max(timestamp(row["record"]["decided_at"]), timestamp(row["recorded_at"])).isoformat()}
                       for row in latest.values()]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("append", "validate", "status"))
    parser.add_argument("--journal", required=True)
    parser.add_argument("--record", help="input JSON object file (append only)")
    parser.add_argument("--theme-id", help="filter latest states (status only)")
    args = parser.parse_args(argv)
    try:
        if args.command == "append":
            require(args.record is not None and args.theme_id is None, "append requires --record; no --theme-id")
            with open(args.record, encoding="utf-8") as handle:
                record = json.load(handle)
            require(isinstance(record, dict), "record must be an object")
            result = append(args.journal, record)
        else:
            require(args.record is None, "--record is append-only")
            require(args.command == "status" or args.theme_id is None, "--theme-id is status-only")
            result = inspect(args.journal, args.theme_id)
            if args.command == "validate":
                result.pop("latest")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (JournalError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "profitability": "unverified"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
