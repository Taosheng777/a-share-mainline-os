import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decision_journal.py"
SPEC = importlib.util.spec_from_file_location("decision_journal", SCRIPT)
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)
NOW = datetime(2026, 9, 11, 23, 0, tzinfo=timezone.utc)


def record(identity="d1", day="2026-09-01", action="observe", previous=None):
    return {
        "schema_version": 1, "decision_id": identity, "theme_id": "test-theme",
        "data_date": day, "decided_at": day + "T18:00:00+08:00",
        "model": "gpt-6-astra", "method_version": "shadow-v1",
        "mode": "shadow", "theme_type": "industrial", "thesis": "模拟主题，不是投资建议",
        "evidence": [{"id": identity + "-e", "source": "local:test-fixture", "available_at": day + "T16:00:00+08:00", "kind": "observation"}],
        "action": action, "previous_decision_id": previous,
        "change_reason": {"kind": "continuation" if previous else "initial", "text": "仅测试连续记录", "evidence_ids": [identity + "-e"]},
        "invalidation": [{"id": "c1", "text": "事先定义的观察条件"}],
        "triggered_conditions": [], "discipline": {"1": "clear", "2": "clear", "3": "clear"},
        "user_execution": {"status": "unconfirmed"}, "confidence": "low",
        "key_data_status": "complete", "evaluation_context": "prospective",
    }


class DecisionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def add(self, item):
        return journal.append(self.path, item, journal.timestamp(item["decided_at"]) + timedelta(hours=1))

    def rejects(self, item, match, now=None):
        before = self.path.read_bytes() if self.path.exists() else b""
        with self.assertRaisesRegex(journal.JournalError, match):
            if now is None:
                self.add(item)
            else:
                journal.append(self.path, item, now)
        self.assertEqual(self.path.read_bytes(), before)

    def test_rotating_models_share_one_chain(self):
        self.add(record())
        second = record("d2", "2026-09-02", previous="d1")
        second["model"] = "claude-opus-5-5"
        self.add(second)
        third = record("d3", "2026-09-03", previous="d2")
        self.add(third)

    def test_unlisted_model_rejected(self):
        item = record()
        item["model"] = "other-model"
        self.rejects(item, "model")

    def test_cross_day_hold_with_preregistered_condition(self):
        first = record(action="enter")
        self.add(first)
        second = record("d2", "2026-09-02", "hold", "d1")
        second["change_reason"]["kind"] = "condition"
        second["triggered_conditions"] = ["c1"]
        self.add(second)
        third = record("d3", "2026-09-03", "hold", "d2")
        self.add(third)
        result = journal.inspect(self.path)
        self.assertEqual(result["latest"][0]["decision_id"], "d3")
        self.assertEqual(result["profitability"], "unverified")

    def test_enter_to_hold_is_continuation_without_new_trigger(self):
        self.add(record(action="enter"))
        self.add(record("d2", "2026-09-02", "hold", "d1"))
        self.assertEqual(journal.inspect(self.path)["latest"][0]["action"], "hold")

    def test_missing_data_does_not_allow_silent_thesis_reset(self):
        self.add(record(action="enter"))
        item=record("d2", "2026-09-02", "observe", "d1")
        item["key_data_status"]="missing"
        item["thesis"]="换一个故事不能借缺数据绕过"
        self.rejects(item,"action change requires")

    def test_observe_to_enter_needs_real_declared_new_event(self):
        self.add(record())
        second = record("d2", "2026-09-02", "enter", "d1")
        self.rejects(second, "action change requires")
        second["change_reason"]["kind"] = "new_event"
        self.rejects(second, "matching event")
        second["evidence"][0]["kind"] = "event"
        self.add(second)

    def test_user_instruction_action_change(self):
        self.add(record())
        second = record("d2", "2026-09-02", "reduce", "d1")
        second["change_reason"]["kind"] = "user_instruction"
        second["evidence"][0]["kind"] = "user_instruction"
        self.add(second)

    def test_unknown_key_data_and_discipline_cannot_enter(self):
        item = record(action="enter")
        item["key_data_status"] = "missing"
        self.rejects(item, "complete key data")
        item["key_data_status"] = "complete"
        for state in ("unknown", "triggered"):
            item["discipline"]["3"] = state
            self.rejects(item, "all discipline statuses clear")

    def test_hard_discipline_requires_exit(self):
        for key in ("1", "2"):
            item = record()
            item["discipline"][key] = "triggered"
            self.rejects(item, "requires exit")
            item["action"] = "exit"
            journal.validate_record(item, [], NOW)

    def test_explicit_user_case_override_is_sourced_and_cannot_enter(self):
        item = record()
        item["discipline"]["2"] = "triggered"
        item["override"] = {"source": "d1-e", "available_at": item["evidence"][0]["available_at"], "conditions": ["2"]}
        self.rejects(item, "user_instruction evidence")
        item["evidence"][0]["kind"] = "user_instruction"
        self.add(item)
        other = copy.deepcopy(item)
        other["decision_id"] = "new"
        other["theme_id"] = "other"
        other["action"] = "enter"
        self.rejects(other, "all discipline statuses clear")

    def test_stale_evidence_does_not_assert_safe_hold(self):
        first = record(action="hold")
        self.add(first)
        second = record("d2", "2026-09-02", "hold", "d1")
        second["evidence"] = first["evidence"]
        second["change_reason"]["evidence_ids"] = ["d1-e"]
        self.rejects(second, "hold requires")
        second["action"] = "observe"
        second["key_data_status"] = "missing"
        second["change_reason"]["text"] = "缺少当日关键证据，不能判定继续持有安全"
        self.add(second)

    def test_future_and_timezone_guards(self):
        for field, value, message in (
            ("decided_at", "2026-09-12T00:00:00Z", "future decided_at"),
            ("decided_at", "2026-09-01T18:00:00", "requires timezone"),
            ("data_date", "2026-09-02", "future data_date"),
        ):
            item = record()
            item[field] = value
            self.rejects(item, message, NOW)
        item = record()
        item["evidence"][0]["available_at"] = "2026-09-02T16:00:00+08:00"
        self.rejects(item, "future evidence")

    def test_latest_same_theme_same_mode_and_no_backdated_overwrite(self):
        self.add(record())
        second = record("d2", "2026-09-02", previous="d1")
        second["theme_id"] = "other"
        self.rejects(second, "same theme AND mode")
        second["theme_id"] = "test-theme"
        self.add(second)
        old = record("d3", "2026-09-03", previous="d1")
        self.rejects(old, "latest decision")
        old["previous_decision_id"] = "d2"
        old["data_date"] = "2026-08-31"
        self.rejects(old, "backdated")

    def test_existing_condition_and_evidence_cannot_be_rewritten(self):
        first = record()
        self.add(first)
        second = record("d2", "2026-09-02", previous="d1")
        second["invalidation"][0]["text"] = "事后改口"
        self.rejects(second, "condition text changed")
        second["invalidation"] = first["invalidation"]
        second["evidence"][0]["id"] = "d1-e"
        self.rejects(second, "evidence id cannot be rewritten")

    def test_same_day_new_condition_cannot_justify_changed_action(self):
        self.add(record())
        second = record("d2", "2026-09-02", "enter", "d1")
        second["change_reason"]["kind"] = "condition"
        second["invalidation"].append({"id": "new", "text": "今天新写"})
        second["triggered_conditions"] = ["new"]
        self.rejects(second, "preregistered")

    def test_historical_audit_is_separate_and_never_forward_evidence(self):
        self.add(record())
        historical = record("audit")
        historical["mode"] = "historical_audit"
        self.rejects(historical, "historical audit must be retrospective")
        historical["evaluation_context"] = "retrospective"
        self.add(historical)
        self.assertEqual(len(journal.inspect(self.path)["latest"]), 2)

    def test_execution_confirmation_and_shadow_boundary(self):
        item = record()
        item["user_execution"] = {"status": "confirmed"}
        self.rejects(item, "requires source/time")
        item["user_execution"].update(source="user report", available_at="2026-09-01T17:00:00+08:00")
        self.rejects(item, "shadow cannot claim")
        item["mode"] = "live_advice"
        self.add(item)

    def test_idempotent_retries_and_collision(self):
        first = record()
        self.assertEqual(self.add(first)["result"], "appended")
        before = self.path.read_bytes()
        self.assertEqual(self.add(first)["result"], "already_present")
        self.assertEqual(self.path.read_bytes(), before)
        first["thesis"] = "changed"
        self.rejects(first, "duplicate decision_id")

    def test_hash_and_truncation_detection(self):
        self.add(record())
        original = self.path.read_text()
        envelope = json.loads(original)
        envelope["record"]["thesis"] = "tampered"
        self.path.write_text(json.dumps(envelope) + "\n")
        with self.assertRaisesRegex(journal.JournalError, "hash mismatch"):
            journal.inspect(self.path)
        self.path.write_text(original.rstrip("\n"))
        with self.assertRaisesRegex(journal.JournalError, "incomplete JSONL"):
            journal.inspect(self.path)

    def test_validate_missing_path_is_read_only(self):
        absent = Path(self.temp.name) / "absent" / "journal.jsonl"
        result = subprocess.run([sys.executable, str(SCRIPT), "validate", "--journal", str(absent)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(absent.parent.exists())
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_concurrent_idempotence_and_independent_themes(self):
        inputs = []
        for index in range(4):
            current = datetime.now(journal.LOCAL_ZONE)
            item = record("d" + str(index), current.date().isoformat())
            item["decided_at"] = current.isoformat()
            item["evidence"][0]["available_at"] = current.isoformat()
            item["theme_id"] = "theme" + str(index)
            path = Path(self.temp.name) / f"input{index}.json"
            path.write_text(json.dumps(item))
            inputs.extend([path, path])
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "append", "--journal", str(self.path), "--record", str(path)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for path in inputs]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr + stdout)
            results.append(json.loads(stdout)["result"])
        self.assertEqual(results.count("appended"), 4)
        self.assertEqual(results.count("already_present"), 4)
        self.assertEqual(journal.inspect(self.path)["records"], 4)

    def test_backfill_cannot_claim_prospective_and_recorded_at_is_system_owned(self):
        item = record()
        self.rejects(item, "prospective record requires current", NOW)
        item["recorded_at"] = item["decided_at"]
        self.rejects(item, "unknown record fields", NOW)
        del item["recorded_at"]
        item["mode"] = "historical_audit"
        item["evaluation_context"] = "retrospective"
        journal.append(self.path, item, NOW)
        envelope = json.loads(self.path.read_text())
        self.assertEqual(envelope["recorded_at"], NOW.isoformat())

    def test_later_read_preserves_original_recording_day(self):
        item = record()
        self.add(item)
        with self.path.open() as handle:
            rows = journal.read_rows(handle, NOW)
        self.assertEqual(len(rows), 1)
        self.assertGreater(journal.timestamp(rows[0]["recorded_at"]), journal.timestamp(item["decided_at"]))

    def test_after_midnight_review_old_market_day_not_old_decision(self):
        after_midnight = datetime(2026, 9, 12, 0, 10, tzinfo=journal.LOCAL_ZONE)
        item = record(day="2026-09-11")
        self.rejects(item, "prospective record requires current", after_midnight)
        item["decided_at"] = "2026-09-12T00:05:00+08:00"
        journal.append(self.path, item, after_midnight)
        envelope = json.loads(self.path.read_text())
        self.assertEqual(envelope["record"]["data_date"], "2026-09-11")
        self.assertEqual(envelope["recorded_at"], after_midnight.isoformat())

    def test_research_target_is_shadow_only_and_obeys_action(self):
        item = record(action="enter")
        item["research_target"] = 0.5
        item["research_asset"] = "111111"
        self.add(item)
        second = record("d2", "2026-09-02", "hold", "d1")
        second["research_target"] = 0.6
        second["research_asset"] = "111111"
        second["change_reason"]["kind"] = "condition"
        second["triggered_conditions"] = ["c1"]
        self.rejects(second, "must equal prior")
        second["research_target"] = 0.5
        self.add(second)
        third = record("d3", "2026-09-03", "reduce", "d2")
        third["research_target"] = 0.6
        third["research_asset"] = "111111"
        third["change_reason"]["kind"] = "condition"
        third["triggered_conditions"] = ["c1"]
        self.rejects(third, "must decrease")
        third["research_target"] = 0.2
        self.add(third)
        for action, target, message in (("observe", 0, "observe cannot"), ("exit", 0.1, "must be zero"), ("enter", 0, "must be positive"), ("enter", True, "finite number")):
            invalid = record("other", "2026-09-04", action)
            invalid["theme_id"] = "other"
            invalid["research_target"] = target
            invalid["research_asset"] = "111111"
            self.rejects(invalid, message)
        invalid = record("other", "2026-09-04", "enter")
        invalid["mode"] = "live_advice"
        invalid["research_target"] = 0.5
        invalid["research_asset"] = "111111"
        self.rejects(invalid, "shadow-only")

    def test_discipline_exit_does_not_need_invented_preregistered_condition(self):
        first = record(action="hold")
        first["invalidation"] = []
        self.add(first)
        second = record("d2", "2026-09-02", "exit", "d1")
        second["invalidation"] = []
        second["discipline"]["2"] = "triggered"
        second["change_reason"]["kind"] = "discipline"
        second["thesis"] = "顺便换论点"
        self.rejects(second, "cannot also rewrite")
        second["thesis"] = first["thesis"]
        second["evidence"][0]["kind"] = "event"
        self.rejects(second, "fresh observation")
        second["evidence"][0]["kind"] = "observation"
        second["discipline"]["2"] = "unknown"
        self.rejects(second, "requires triggered")
        second["discipline"]["2"] = "triggered"
        self.add(second)

    def test_discipline_three_allows_defensive_actions_only(self):
        for action in ("observe", "reduce", "exit"):
            item = record(action=action)
            item["change_reason"]["kind"] = "discipline"
            item["discipline"]["3"] = "triggered"
            journal.validate_record(item, [], NOW)
        for action in ("enter", "hold"):
            item = record(action=action)
            item["change_reason"]["kind"] = "discipline"
            item["discipline"]["3"] = "triggered"
            self.rejects(item, "requires triggered")

    def test_research_asset_required_and_cannot_switch_inside_chain(self):
        item = record(action="enter")
        item["research_target"] = 0.5
        self.rejects(item, "must appear together")
        item["research_asset"] = "arbitrary name"
        self.rejects(item, "six-digit")
        item["research_asset"] = "111111"
        self.add(item)
        second = record("d2", "2026-09-02", "hold", "d1")
        second["research_asset"] = "222222"
        second["research_target"] = 0.5
        self.rejects(second, "cannot change")
        second["research_asset"] = "111111"
        self.add(second)
        third = record("d3", "2026-09-03", "hold", "d2")
        third["research_asset"] = "111111"
        self.rejects(third, "must appear together")


if __name__ == "__main__":
    unittest.main()
