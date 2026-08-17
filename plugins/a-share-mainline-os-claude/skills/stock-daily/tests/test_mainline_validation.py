import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "mainline_validation.py"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mainline_cases.json"


def load_module():
    spec = importlib.util.spec_from_file_location("mainline_validation", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MainlineDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_recorded_cases_match_actual_and_counterfactual_rules(self):
        for case in self.cases:
            if case["kind"] != "retreat":
                continue
            actual = self.module.classify_retreat(
                fund_key=case["fund_key"],
                price_key=case["price_key"],
                standalone_price_trigger=case["standalone_price_trigger"],
                rule_version=case["actual_rule_version"],
            )
            counterfactual = self.module.classify_retreat(
                fund_key=case["fund_key"],
                price_key=case["price_key"],
                standalone_price_trigger=case["standalone_price_trigger"],
                rule_version=case["counterfactual_rule_version"],
            )
            self.assertEqual(case["expected_actual_state"], actual["state"], case["id"])
            self.assertEqual(
                case["expected_counterfactual_state"],
                counterfactual["state"],
                case["id"],
            )

    def test_missing_key_is_unverified_not_silently_false(self):
        result = self.module.classify_retreat(
            fund_key=True,
            price_key=None,
            standalone_price_trigger=False,
            rule_version="double_key_v1",
        )
        self.assertEqual("unverified", result["state"])
        self.assertEqual("data_block", result["action"])

    def test_price_half_of_double_key_does_not_trigger_by_itself(self):
        result = self.module.classify_retreat(
            fund_key=False,
            price_key=True,
            standalone_price_trigger=False,
            rule_version="double_key_v1",
        )
        self.assertEqual("maintain", result["state"])

    def test_registered_standalone_price_condition_still_triggers(self):
        result = self.module.classify_retreat(
            fund_key=False,
            price_key=False,
            standalone_price_trigger=True,
            rule_version="double_key_v1",
        )
        self.assertEqual("retreat_review_required", result["state"])
        self.assertEqual("standalone_price_trigger", result["reason"])

    def test_nomination_shadow_flags_are_diagnostic_only(self):
        case = next(x for x in self.cases if x["kind"] == "nomination_shadow")
        result = self.module.assess_nomination_shadow(
            limit_up_mapping_count=case["limit_up_mapping_count"],
            a_share_catalyst_landing=case["a_share_catalyst_landing"],
            board_member_count=case["board_member_count"],
            uses_absolute_rank_condition=case["uses_absolute_rank_condition"],
            next_day_falsification=case["next_day_falsification"],
        )
        self.assertEqual(case["expected_flags"], result["flags"])
        self.assertTrue(result["shadow_only"])
        self.assertFalse(result["blocks_live_nomination"])

    def test_missing_shadow_observations_are_auditable_but_not_live_gates(self):
        result = self.module.assess_nomination_shadow(
            limit_up_mapping_count=None,
            a_share_catalyst_landing=None,
            board_member_count=None,
            uses_absolute_rank_condition=True,
            next_day_falsification=None,
        )
        self.assertEqual(
            [
                "limit_up_mapping_count",
                "a_share_catalyst_landing",
                "board_member_count",
                "next_day_falsification",
            ],
            result["missing"],
        )
        self.assertTrue(result["shadow_only"])
        self.assertFalse(result["blocks_live_nomination"])

    def test_preregistered_next_day_falsification_is_replayed(self):
        case = next(x for x in self.cases if x["kind"] == "next_day_falsification")
        result = self.module.assess_next_day_falsification(
            preregistered=case["preregistered"],
            falsification_observed=case["falsification_observed"],
        )
        self.assertEqual(case["expected_shadow_verdict"], result["shadow_verdict"])
        self.assertTrue(result["shadow_only"])


class StudyDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "boards.sqlite3"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE observations (
                    taxonomy TEXT NOT NULL,
                    board_code TEXT NOT NULL,
                    board_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    close REAL NOT NULL,
                    change_pct REAL,
                    main_net REAL NOT NULL,
                    main_pct REAL,
                    super_net REAL,
                    large_net REAL,
                    mid_net REAL,
                    small_net REAL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (taxonomy, board_code, trade_date, provider)
                );
                """
            )

    def tearDown(self):
        self.temp.cleanup()

    def _insert(self, code, board_type, date, close, main_net, provider="test"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO observations (
                    taxonomy, board_code, board_type, name, trade_date, close,
                    main_net, provider, fetched_at
                ) VALUES ('eastmoney_dc', ?, ?, ?, ?, ?, ?, ?, '2026-08-15T00:00:00+08:00')
                """,
                (code, board_type, code, date, close, main_net, provider),
            )

    def test_audit_rejects_duplicate_board_dates_across_providers(self):
        self._insert("BK0001", "concept", "2026-01-05", 100.0, 1.0, "p1")
        self._insert("BK0001", "concept", "2026-01-05", 100.0, 1.0, "p2")
        audit = self.module.audit_database(self.db_path)
        self.assertEqual("fail", audit["gate"])
        self.assertEqual(1, audit["duplicate_board_dates"])

    def test_industry_rows_before_taxonomy_cutoff_are_excluded(self):
        self._insert("BK0001", "industry", "2025-09-02", 100.0, 1.0)
        self._insert("BK0001", "industry", "2025-09-03", 101.0, 1.0)
        self._insert("BK0002", "concept", "2025-09-02", 100.0, 1.0)
        rows = self.module.load_rows(self.db_path)
        filtered = self.module.filter_taxonomy_window(rows)
        dates = {(x["board_code"], x["trade_date"]) for x in filtered}
        self.assertNotIn(("BK0001", "2025-09-02"), dates)
        self.assertIn(("BK0001", "2025-09-03"), dates)
        self.assertIn(("BK0002", "2025-09-02"), dates)

    def test_episode_summary_separates_signal_days_from_first_hits(self):
        episodes = [
            {
                "id": "BK0001@2026-01-01",
                "board_type": "concept",
                "entry_date": "2026-01-01",
                "days": [
                    {
                        "e1": True,
                        "e2": False,
                        "d1": False,
                        "d2": False,
                        "f10": 0.02,
                        "drawdown10": -0.01,
                    },
                    {
                        "e1": True,
                        "e2": False,
                        "d1": True,
                        "d2": False,
                        "f10": -0.03,
                        "drawdown10": -0.07,
                    },
                ],
            }
        ]
        summary = self.module._summarize_episodes(episodes)
        self.assertEqual(2, summary["events"]["e1"]["n"])
        self.assertEqual(1, summary["events"]["e1_only"]["n"])
        self.assertEqual(1, summary["events"]["d1"]["n"])
        self.assertEqual(1, summary["first_hit_events"]["e1"]["n"])
        self.assertEqual(1, summary["first_hit_events"]["d1"]["n"])
        self.assertEqual(1.0, summary["episode_filtering_multiple"]["e1_to_d1"])


if __name__ == "__main__":
    unittest.main()
