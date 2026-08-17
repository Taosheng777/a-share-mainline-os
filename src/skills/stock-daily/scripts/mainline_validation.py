#!/usr/bin/env python3
"""主线判断的确定性判定器、真实案例回放与板块历史研究。"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path


INDUSTRY_TAXONOMY_CUTOFF = "2025-09-03"
RESEARCH_ENTRY_CUTOFF = "2026-07-31"
HOLDOUT_START = "2026-08-03"
BREAKOUT_RETURN = 0.10
HIGH_VOL_PERCENTILE = 0.75
EPISODE_DAYS = 20
FORWARD_DAYS = 10


def classify_retreat(
    *,
    fund_key: bool | None,
    price_key: bool | None,
    standalone_price_trigger: bool | None = False,
    rule_version: str = "double_key_v1",
) -> dict:
    """机械判定状态；任何必需键缺失都 fail-closed 为 unverified。"""
    if rule_version not in {"legacy_any", "double_key_v1"}:
        raise ValueError(f"unknown rule_version: {rule_version}")
    if fund_key is None or price_key is None or standalone_price_trigger is None:
        return {
            "state": "unverified",
            "action": "data_block",
            "rule_version": rule_version,
            "reason": "required_key_missing",
        }

    if rule_version == "legacy_any":
        triggered = fund_key or price_key or standalone_price_trigger
        return {
            "state": "retreat_review_required" if triggered else "maintain",
            "action": "run_preregistered_review" if triggered else "none",
            "rule_version": rule_version,
            "reason": "any_registered_condition" if triggered else "no_condition",
        }

    if standalone_price_trigger:
        return {
            "state": "retreat_review_required",
            "action": "run_preregistered_review",
            "rule_version": rule_version,
            "reason": "standalone_price_trigger",
        }
    if fund_key and price_key:
        return {
            "state": "retreat_review_required",
            "action": "run_preregistered_review",
            "rule_version": rule_version,
            "reason": "fund_and_price_keys",
        }
    if fund_key:
        return {
            "state": "warning",
            "action": "freeze_additions_and_propose_stop_review",
            "rule_version": rule_version,
            "reason": "fund_key_only",
        }
    return {
        "state": "maintain",
        "action": "none",
        "rule_version": rule_version,
        "reason": "double_key_not_met",
    }


def assess_nomination_shadow(
    *,
    limit_up_mapping_count: int | None,
    a_share_catalyst_landing: bool | None,
    board_member_count: int | None,
    uses_absolute_rank_condition: bool,
    next_day_falsification: str | None,
) -> dict:
    """记录尚处修订窗口内的立项诊断，不改变现行提名裁决。"""
    flags = []
    missing = []
    if limit_up_mapping_count is None:
        missing.append("limit_up_mapping_count")
    elif limit_up_mapping_count == 0:
        flags.append("zero_limit_up_mapping")

    if a_share_catalyst_landing is None:
        missing.append("a_share_catalyst_landing")
    elif not a_share_catalyst_landing:
        flags.append("no_a_share_catalyst_landing")

    if board_member_count is None:
        missing.append("board_member_count")
    elif uses_absolute_rank_condition and board_member_count < 50:
        flags.append("narrow_absolute_rank_anchor")

    if not (next_day_falsification or "").strip():
        missing.append("next_day_falsification")

    return {
        "flags": flags,
        "missing": missing,
        "risk": "high" if len(flags) >= 2 else "medium" if flags else "none",
        "shadow_only": True,
        "blocks_live_nomination": False,
    }


def assess_next_day_falsification(
    *, preregistered: bool, falsification_observed: bool | None
) -> dict:
    if not preregistered:
        verdict = "not_preregistered"
    elif falsification_observed is None:
        verdict = "unverified"
    elif falsification_observed:
        verdict = "withdraw_to_watch"
    else:
        verdict = "not_falsified"
    return {
        "shadow_verdict": verdict,
        "shadow_only": True,
        "blocks_live_nomination": False,
    }


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def audit_database(db_path: Path | str) -> dict:
    with _connect(db_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        if not table:
            return {"gate": "fail", "reason": "missing_observations_table"}
        row_count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        duplicate_board_dates = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT taxonomy, board_code, trade_date
                FROM observations
                GROUP BY taxonomy, board_code, trade_date
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        invalid_close = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE close IS NULL OR close <= 0"
        ).fetchone()[0]
        invalid_main_net = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE main_net IS NULL"
        ).fetchone()[0]
        invalid_type = conn.execute(
            """
            SELECT COUNT(*) FROM observations
            WHERE board_type NOT IN ('industry', 'concept', 'region')
            """
        ).fetchone()[0]
        date_row = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM observations"
        ).fetchone()
        providers = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT provider, COUNT(*) FROM observations GROUP BY provider"
            )
        }
        daily_counts = {
            row[0]: {"min": row[1], "max": row[2], "dates": row[3]}
            for row in conn.execute(
                """
                SELECT board_type, MIN(n), MAX(n), COUNT(*)
                FROM (
                    SELECT board_type, trade_date, COUNT(*) AS n
                    FROM observations
                    GROUP BY board_type, trade_date
                )
                GROUP BY board_type
                """
            )
        }

    hard_failures = duplicate_board_dates + invalid_close + invalid_main_net + invalid_type
    return {
        "gate": "pass" if hard_failures == 0 else "fail",
        "rows": row_count,
        "duplicate_board_dates": duplicate_board_dates,
        "invalid_close": invalid_close,
        "invalid_main_net": invalid_main_net,
        "invalid_board_type": invalid_type,
        "min_date": date_row[0],
        "max_date": date_row[1],
        "trading_dates": date_row[2],
        "providers": providers,
        "daily_board_counts": daily_counts,
        "warnings": [
            "industry taxonomy before 2025-09-03 is excluded",
            "concept cross-sectional measures use each date's available denominator",
            "provider transitions are retained only after prior source audit",
        ],
    }


def load_rows(db_path: Path | str) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT taxonomy, board_code, board_type, name, trade_date,
                   close, change_pct, main_net, main_pct, provider
            FROM observations
            WHERE taxonomy = 'eastmoney_dc'
            ORDER BY trade_date, board_type, board_code
            """
        ).fetchall()
    return [dict(row) for row in rows]


def filter_taxonomy_window(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if row["board_type"] == "concept":
            out.append(row)
        elif (
            row["board_type"] == "industry"
            and row["trade_date"] >= INDUSTRY_TAXONOMY_CUTOFF
        ):
            out.append(row)
    return out


def _series_by_board(rows: list[dict]) -> dict[tuple[str, str], dict[str, dict]]:
    grouped = defaultdict(dict)
    for row in rows:
        key = (row["board_type"], row["board_code"])
        if row["trade_date"] in grouped[key]:
            raise ValueError(f"duplicate board-date after audit: {key} {row['trade_date']}")
        grouped[key][row["trade_date"]] = row
    return dict(grouped)


def _exact_rows(
    mapping: dict[str, dict], trading_dates: list[str], start: int, end: int
) -> list[dict] | None:
    if start < 0 or end >= len(trading_dates):
        return None
    dates = trading_dates[start : end + 1]
    if len(dates) != end - start + 1:
        return None
    rows = [mapping.get(day) for day in dates]
    return rows if all(rows) else None


def _std(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n <= 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _point_metrics(
    mapping: dict[str, dict], trading_dates: list[str], idx: int
) -> dict | None:
    rows = _exact_rows(mapping, trading_dates, idx - 21, idx)
    if not rows:
        return None
    closes = [float(row["close"]) for row in rows]
    flows = [float(row["main_net"]) for row in rows]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    ret20 = closes[-1] / closes[-21] - 1
    prev_ret20 = closes[-2] / closes[-22] - 1
    ma20 = statistics.mean(closes[-20:])
    sum5 = sum(flows[-5:])
    prev_sum5 = sum(flows[-6:-1])
    e1 = flows[-1] < 0 and flows[-2] < 0 and flows[-3] >= 0
    e2 = sum5 < 0 <= prev_sum5
    return {
        "ret20": ret20,
        "prev_ret20": prev_ret20,
        "vol20": _std(returns[-20:]),
        "ma20": ma20,
        "e1": e1,
        "e2": e2,
        "d1": e1 and closes[-1] < ma20,
        "d2": e2 and closes[-1] < ma20,
        "price_key": closes[-1] < ma20,
    }


def _percentile_rank(value: float, values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(x <= value for x in values) / len(values)


def _forward_record(
    mapping: dict[str, dict], trading_dates: list[str], idx: int
) -> dict | None:
    rows = _exact_rows(mapping, trading_dates, idx, idx + FORWARD_DAYS)
    if not rows:
        return None
    base = float(rows[0]["close"])
    future = [float(row["close"]) for row in rows[1:]]
    return {
        "f10": future[-1] / base - 1,
        "drawdown10": min(future) / base - 1,
    }


def _event_summary(records: list[dict]) -> dict:
    f10 = [row["f10"] for row in records if row.get("f10") is not None]
    drawdowns = [
        row["drawdown10"] for row in records if row.get("drawdown10") is not None
    ]
    return {
        "n": len(records),
        "f10_median": _median(f10),
        "f10_win_rate": sum(x > 0 for x in f10) / len(f10) if f10 else None,
        "drawdown_over_5_rate": (
            sum(x < -0.05 for x in drawdowns) / len(drawdowns) if drawdowns else None
        ),
    }


def _summarize_episodes(episodes: list[dict]) -> dict:
    event_names = ("baseline", "e1", "e1_only", "e2", "e2_only", "d1", "d2")
    records = {name: [] for name in event_names}
    episode_hits = {name: set() for name in event_names[1:]}
    first_hit_records = {name: [] for name in event_names[1:]}
    type_counts = defaultdict(int)
    for episode in episodes:
        type_counts[episode["board_type"]] += 1
        first_hits = {}
        for row in episode["days"]:
            records["baseline"].append(row)
            flags = {
                "e1": row["e1"],
                "e1_only": row["e1"] and not row["d1"],
                "e2": row["e2"],
                "e2_only": row["e2"] and not row["d2"],
                "d1": row["d1"],
                "d2": row["d2"],
            }
            for name, triggered in flags.items():
                if triggered:
                    records[name].append(row)
                    episode_hits[name].add(episode["id"])
                    first_hits.setdefault(name, row)
        for name, row in first_hits.items():
            first_hit_records[name].append(row)

    summaries = {name: _event_summary(records[name]) for name in event_names}
    for name in event_names[1:]:
        hits = len(episode_hits[name])
        summaries[name]["episodes_with_signal"] = hits
        summaries[name]["episode_rate"] = hits / len(episodes) if episodes else None
        summaries[name]["episode_rate_wilson95"] = _wilson(hits, len(episodes))

    e1_n, d1_n = summaries["e1"]["n"], summaries["d1"]["n"]
    e2_n, d2_n = summaries["e2"]["n"], summaries["d2"]["n"]
    e1_episodes = len(episode_hits["e1"])
    d1_episodes = len(episode_hits["d1"])
    e2_episodes = len(episode_hits["e2"])
    d2_episodes = len(episode_hits["d2"])
    return {
        "episodes": len(episodes),
        "entry_date_min": min((x["entry_date"] for x in episodes), default=None),
        "entry_date_max": max((x["entry_date"] for x in episodes), default=None),
        "episode_types": dict(sorted(type_counts.items())),
        "at_risk_days": summaries["baseline"]["n"],
        "events": summaries,
        "first_hit_events": {
            name: _event_summary(value) for name, value in first_hit_records.items()
        },
        "filtering_multiple": {
            "e1_to_d1": e1_n / d1_n if d1_n else None,
            "e2_to_d2": e2_n / d2_n if d2_n else None,
        },
        "episode_filtering_multiple": {
            "e1_to_d1": e1_episodes / d1_episodes if d1_episodes else None,
            "e2_to_d2": e2_episodes / d2_episodes if d2_episodes else None,
        },
    }


def _robustness_slices(episodes: list[dict]) -> dict:
    by_type = {
        board_type: _summarize_episodes(
            [episode for episode in episodes if episode["board_type"] == board_type]
        )
        for board_type in ("concept", "industry")
    }
    by_entry_period = {
        "through_2025": _summarize_episodes(
            [episode for episode in episodes if episode["entry_date"] <= "2025-12-31"]
        ),
        "2026_research_window": _summarize_episodes(
            [episode for episode in episodes if episode["entry_date"] >= "2026-01-01"]
        ),
    }
    return {
        "status": "post_gate_diagnostic_only",
        "may_not_override_primary_gate": True,
        "by_board_type": by_type,
        "by_entry_period": by_entry_period,
    }


def build_breakout_episodes(rows: list[dict]) -> dict[str, list[dict]]:
    filtered = filter_taxonomy_window(rows)
    series = _series_by_board(filtered)
    trading_dates = sorted({row["trade_date"] for row in filtered})
    date_index = {day: idx for idx, day in enumerate(trading_dates)}

    metrics = {}
    vol_cross_sections = defaultdict(list)
    for key, mapping in series.items():
        for day in sorted(mapping):
            idx = date_index[day]
            point = _point_metrics(mapping, trading_dates, idx)
            if point:
                metrics[(key, day)] = point
                vol_cross_sections[(key[0], day)].append(point["vol20"])

    candidates = []
    for (key, day), point in metrics.items():
        if day > RESEARCH_ENTRY_CUTOFF:
            continue
        if not (point["ret20"] >= BREAKOUT_RETURN > point["prev_ret20"]):
            continue
        idx = date_index[day]
        mapping = series[key]
        complete = _exact_rows(
            mapping,
            trading_dates,
            idx - 21,
            idx + EPISODE_DAYS - 1 + FORWARD_DAYS,
        )
        if not complete:
            continue
        percentile = _percentile_rank(
            point["vol20"], vol_cross_sections[(key[0], day)]
        )
        candidates.append(
            {
                "key": key,
                "day": day,
                "idx": idx,
                "vol_percentile": percentile,
                "ret20": point["ret20"],
            }
        )

    candidates.sort(key=lambda x: (x["key"], x["day"]))
    last_entry = {}
    all_episodes = []
    for candidate in candidates:
        key = candidate["key"]
        if candidate["idx"] - last_entry.get(key, -10_000) < EPISODE_DAYS:
            continue
        last_entry[key] = candidate["idx"]
        mapping = series[key]
        days = []
        for offset in range(EPISODE_DAYS):
            idx = candidate["idx"] + offset
            point = _point_metrics(mapping, trading_dates, idx)
            forward = _forward_record(mapping, trading_dates, idx)
            if not point or not forward:
                raise RuntimeError("episode completeness gate failed after precheck")
            days.append(
                {
                    "date": trading_dates[idx],
                    "offset": offset,
                    "e1": point["e1"],
                    "e2": point["e2"],
                    "d1": point["d1"],
                    "d2": point["d2"],
                    **forward,
                }
            )
        all_episodes.append(
            {
                "id": f"{key[1]}@{candidate['day']}",
                "board_type": key[0],
                "board_code": key[1],
                "entry_date": candidate["day"],
                "entry_ret20": candidate["ret20"],
                "entry_vol_percentile": candidate["vol_percentile"],
                "days": days,
            }
        )

    high_vol = [
        episode
        for episode in all_episodes
        if episode["entry_vol_percentile"] >= HIGH_VOL_PERCENTILE
    ]
    return {"all_new_breakouts": all_episodes, "high_vol_new_breakouts": high_vol}


def reproduce_original_window(rows: list[dict]) -> dict:
    window = [
        row
        for row in rows
        if row["board_type"] == "industry"
        and "2026-02-11" <= row["trade_date"] <= "2026-08-12"
    ]
    series = _series_by_board(window)
    trading_dates = sorted({row["trade_date"] for row in window})
    complete_series = {
        key: mapping
        for key, mapping in series.items()
        if all(day in mapping for day in trading_dates)
    }
    records = {name: [] for name in ("baseline", "e1", "e2", "d1", "d2")}
    for key, mapping in complete_series.items():
        for idx in range(21, len(trading_dates) - FORWARD_DAYS):
            point = _point_metrics(mapping, trading_dates, idx)
            if not point or point["ret20"] < BREAKOUT_RETURN:
                continue
            forward = _forward_record(mapping, trading_dates, idx)
            row = {**point, **forward, "board": key[1], "date": trading_dates[idx]}
            records["baseline"].append(row)
            for name in ("e1", "e2", "d1", "d2"):
                if point[name]:
                    records[name].append(row)

    summaries = {name: _event_summary(value) for name, value in records.items()}
    baseline_n = summaries["baseline"]["n"]
    for name in ("e1", "e2", "d1", "d2"):
        n = summaries[name]["n"]
        summaries[name]["average_strong_days_per_trigger"] = baseline_n / n if n else None
    return {
        "window": ["2026-02-11", "2026-08-12"],
        "complete_boards": len(complete_series),
        "trading_dates": len(trading_dates),
        "events": summaries,
        "reference_report_counts": {
            "complete_boards": 487,
            "baseline": 3701,
            "e1": 501,
            "e2": 349,
            "d1": 22,
            "d2": 5,
        },
    }


def _acceptance_gate(cohort: dict) -> dict:
    events = cohort["events"]
    filtering = cohort["filtering_multiple"]["e1_to_d1"]
    e1_median = events["e1"]["f10_median"]
    d1_median = events["d1"]["f10_median"]
    e1_draw = events["e1"]["drawdown_over_5_rate"]
    d1_draw = events["d1"]["drawdown_over_5_rate"]
    checks = {
        "episodes_at_least_50": cohort["episodes"] >= 50,
        "d1_events_at_least_10": events["d1"]["n"] >= 10,
        "filtering_at_least_2x": filtering is not None and filtering >= 2.0,
        "d1_f10_at_least_2pp_worse": (
            e1_median is not None
            and d1_median is not None
            and d1_median <= e1_median - 0.02
        ),
        "d1_drawdown_rate_at_least_10pp_higher": (
            e1_draw is not None and d1_draw is not None and d1_draw >= e1_draw + 0.10
        ),
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "verdict": "supported" if all(checks.values()) else "not_supported_or_insufficient",
        "secondary_e2_d2": (
            "eligible_for_interpretation" if events["d2"]["n"] >= 10 else "insufficient_d2_events"
        ),
    }


def run_study(db_path: Path | str) -> dict:
    audit = audit_database(db_path)
    if audit.get("gate") != "pass":
        raise RuntimeError(f"data quality gate failed: {audit}")
    rows = load_rows(db_path)
    cohorts = build_breakout_episodes(rows)
    summaries = {name: _summarize_episodes(value) for name, value in cohorts.items()}
    gate = _acceptance_gate(summaries["high_vol_new_breakouts"])
    diagnostics = _robustness_slices(cohorts["high_vol_new_breakouts"])
    return {
        "spec_version": "mainline-validation-v1",
        "as_of": audit["max_date"],
        "data_audit": audit,
        "point_in_time_spec": {
            "industry_start": INDUSTRY_TAXONOMY_CUTOFF,
            "concept_denominator": "available boards on each date",
            "research_entry_cutoff": RESEARCH_ENTRY_CUTOFF,
            "holdout_start": HOLDOUT_START,
            "breakout": "20d return crosses from below +10% to >= +10%",
            "high_volatility": "20d realized volatility percentile >= 75% within board_type/date",
            "episode_days": EPISODE_DAYS,
            "forward_days": FORWARD_DAYS,
            "overlap": "same board entries less than 20 trading days apart are deduplicated",
        },
        "original_window_reproduction": reproduce_original_window(rows),
        "cohorts": summaries,
        "acceptance_gate": gate,
        "robustness_diagnostics": diagnostics,
    }


def run_case_replay(fixtures_path: Path | str) -> dict:
    cases = json.loads(Path(fixtures_path).read_text(encoding="utf-8"))
    results = []
    for case in cases:
        if case["kind"] == "retreat":
            actual = classify_retreat(
                fund_key=case["fund_key"],
                price_key=case["price_key"],
                standalone_price_trigger=case["standalone_price_trigger"],
                rule_version=case["actual_rule_version"],
            )
            counterfactual = classify_retreat(
                fund_key=case["fund_key"],
                price_key=case["price_key"],
                standalone_price_trigger=case["standalone_price_trigger"],
                rule_version=case["counterfactual_rule_version"],
            )
            passed = (
                actual["state"] == case["expected_actual_state"]
                and counterfactual["state"] == case["expected_counterfactual_state"]
            )
            result = {"id": case["id"], "actual": actual, "counterfactual": counterfactual}
        elif case["kind"] == "nomination_shadow":
            shadow = assess_nomination_shadow(
                limit_up_mapping_count=case["limit_up_mapping_count"],
                a_share_catalyst_landing=case["a_share_catalyst_landing"],
                board_member_count=case["board_member_count"],
                uses_absolute_rank_condition=case["uses_absolute_rank_condition"],
                next_day_falsification=case["next_day_falsification"],
            )
            passed = shadow["flags"] == case["expected_flags"]
            result = {"id": case["id"], "shadow": shadow}
        else:
            shadow = assess_next_day_falsification(
                preregistered=case["preregistered"],
                falsification_observed=case["falsification_observed"],
            )
            passed = shadow["shadow_verdict"] == case["expected_shadow_verdict"]
            result = {"id": case["id"], "shadow": shadow}
        result["passed"] = passed
        result["evidence"] = case.get("evidence", "")
        results.append(result)
    return {
        "fixture": str(fixtures_path),
        "cases": results,
        "passed": all(row["passed"] for row in results),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def render_report(result: dict) -> str:
    audit = result["data_audit"]
    original = result["original_window_reproduction"]
    high = result["cohorts"]["high_vol_new_breakouts"]
    all_breakouts = result["cohorts"]["all_new_breakouts"]
    gate = result["acceptance_gate"]
    diagnostics = result["robustness_diagnostics"]

    lines = [
        "# 主线退潮信号历史回放",
        "",
        f"> 数据截至 **{result['as_of']}**。本报告只验证信号结构，不改现行阈值；2026-08-03 以后案例不进入研究样本。",
        "",
        "## 数据质量 Gate",
        "",
        f"- 结果：**{audit['gate'].upper()}**；{audit['rows']:,} 行、{audit['trading_dates']} 个交易日（{audit['min_date']} → {audit['max_date']}）。",
        f"- 重复板块日：{audit['duplicate_board_dates']}；无效收盘：{audit['invalid_close']}；无效资金：{audit['invalid_main_net']}。",
        f"- 行业仅用 **{INDUSTRY_TAXONOMY_CUTOFF}** 起的新分类；概念横截面按当日可用分母，不固化 504。",
        "",
        "## 口径",
        "",
        f"- 刚爆发：20 日涨幅从前一日低于 +10% 首次上穿至 ≥ +10%；高波动：当日同类型 20 日实现波动率前 25%。",
        f"- 每个事件观察随后 {EPISODE_DAYS} 个交易日；同板块 {EPISODE_DAYS} 日内重复上穿去重；每个信号再看 {FORWARD_DAYS} 日结果。",
        f"- 高波动样本实际入场日为 **{high['entry_date_min']} → {high['entry_date_max']}**；晚于该范围的上穿因观察/结果窗口不完整而不入样本。",
        "- E1 = 连续两日流出的新触发；E2 = 5 日资金和由非负转负；D1/D2 = 对应资金键且收盘低于 MA20。",
        "",
        "## 原 120 日研究复跑",
        "",
        f"完整行业板块 **{original['complete_boards']}** 个（原报告成功抓取 487 个），强势板块日 **{original['events']['baseline']['n']}** 个。",
        "",
        "| 事件 | 本次 N | 原报告 N | 10 日中位 | 平均几个强势日触发一次 |",
        "|---|---:|---:|---:|---:|",
    ]
    refs = original["reference_report_counts"]
    for name, label in (("e1", "E1"), ("e2", "E2"), ("d1", "D1"), ("d2", "D2")):
        event = original["events"][name]
        lines.append(
            f"| {label} | {event['n']} | {refs[name]} | {_pct(event['f10_median'])} | "
            f"{event['average_strong_days_per_trigger']:.1f} |"
            if event["average_strong_days_per_trigger"] is not None
            else f"| {label} | {event['n']} | {refs[name]} | {_pct(event['f10_median'])} | — |"
        )

    lines += [
        "",
        "原报告与本次数据库的完整板块集合不同，因此这里是**同公式复跑与偏差检查**，不是要求逐数相等。",
        "",
        "## 目标样本：刚爆发板块",
        "",
        "| 样本 | 事件数 | 风险日 | E1 | D1 | E1→D1 压缩 | E2 | D2 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, cohort in (("全部刚爆发", all_breakouts), ("高波动刚爆发", high)):
        mult = cohort["filtering_multiple"]["e1_to_d1"]
        lines.append(
            f"| {label} | {cohort['episodes']} | {cohort['at_risk_days']} | "
            f"{cohort['events']['e1']['n']} | {cohort['events']['d1']['n']} | "
            f"{'—' if mult is None else f'{mult:.2f}×'} | "
            f"{cohort['events']['e2']['n']} | {cohort['events']['d2']['n']} |"
        )

    lines += [
        "",
        "### 高波动刚爆发样本的后果",
        "",
        "| 事件 | N | 10 日中位 | 10 日胜率 | 10 日内跌超 5% |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, label in (("baseline", "全部风险日"), ("e1", "E1"), ("d1", "D1"), ("e2", "E2"), ("d2", "D2")):
        event = high["events"][name]
        lines.append(
            f"| {label} | {event['n']} | {_pct(event['f10_median'])} | "
            f"{_pct(event['f10_win_rate'])} | {_pct(event['drawdown_over_5_rate'])} |"
        )

    event_mult = high["filtering_multiple"]["e1_to_d1"]
    episode_mult = high["episode_filtering_multiple"]["e1_to_d1"]
    e1_only = high["events"]["e1_only"]
    first_e1 = high["first_hit_events"]["e1"]
    first_d1 = high["first_hit_events"]["d1"]
    lines += [
        "",
        "### 实盘语义诊断（不改变预注册 Gate）",
        "",
        f"- 按全部信号日计，E1→D1 压缩 **{event_mult:.2f}×**；按每条主线 20 日内是否至少触发一次计，只压缩 **{episode_mult:.2f}×**。",
        f"- 资金键成立但价格键健康（E1-only）共 **{e1_only['n']}** 次，10 日中位 **{_pct(e1_only['f10_median'])}**、10 日内跌超 5% **{_pct(e1_only['drawdown_over_5_rate'])}**；D1 对应为 **{_pct(high['events']['d1']['f10_median'])}** 与 **{_pct(high['events']['d1']['drawdown_over_5_rate'])}**。",
        f"- 每条主线只取首次触发：首次 E1 N={first_e1['n']}、10 日中位 {_pct(first_e1['f10_median'])}；首次 D1 N={first_d1['n']}、10 日中位 {_pct(first_d1['f10_median'])}。",
        "",
        "### 稳健性切片（Gate 失败后追加，只能诊断，不能救回结论）",
        "",
        "| 切片 | 事件数 | D1 | 信号日压缩 | 主线级压缩 | E1-only 10 日中位 | D1 10 日中位 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    slice_rows = [
        ("概念", diagnostics["by_board_type"]["concept"]),
        ("行业", diagnostics["by_board_type"]["industry"]),
        ("截至 2025", diagnostics["by_entry_period"]["through_2025"]),
        ("2026 研究窗", diagnostics["by_entry_period"]["2026_research_window"]),
    ]
    for label, cohort in slice_rows:
        signal_mult = cohort["filtering_multiple"]["e1_to_d1"]
        mainline_mult = cohort["episode_filtering_multiple"]["e1_to_d1"]
        lines.append(
            f"| {label} | {cohort['episodes']} | {cohort['events']['d1']['n']} | "
            f"{'—' if signal_mult is None else f'{signal_mult:.2f}×'} | "
            f"{'—' if mainline_mult is None else f'{mainline_mult:.2f}×'} | "
            f"{_pct(cohort['events']['e1_only']['f10_median'])} | "
            f"{_pct(cohort['events']['d1']['f10_median'])} |"
        )

    lines += [
        "",
        "## 预注册支持 Gate",
        "",
    ]
    for check, passed in gate["checks"].items():
        lines.append(f"- {'✅' if passed else '❌'} `{check}`")
    lines += [
        "",
        f"**裁决：{gate['verdict']}。** E2→D2：`{gate['secondary_e2_d2']}`。",
        "",
        "该裁决只回答“双钥匙在真正会被立项的高波动刚爆发板块里是否仍有可辨识价值”。稳健性切片是在主 Gate 失败后追加，不能改变裁决。它也不验证涨停梯队映射或 A 股催化落点；后两项当前只有真实案例回归与未来影子样本，不能冒充已完成统计验证。",
        "",
        "研究分析，非持牌投顾建议，风险自担。数据来源：本地 `board_fund_flow.sqlite3`；数据日按上文。",
    ]
    return "\n".join(lines) + "\n"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    study_parser = sub.add_parser("study")
    study_parser.add_argument("--db", required=True, type=Path)
    study_parser.add_argument("--output-dir", required=True, type=Path)

    case_parser = sub.add_parser("cases")
    case_parser.add_argument("--fixtures", required=True, type=Path)
    case_parser.add_argument("--output", type=Path)

    all_parser = sub.add_parser("all")
    all_parser.add_argument("--db", required=True, type=Path)
    all_parser.add_argument("--fixtures", required=True, type=Path)
    all_parser.add_argument("--output-dir", required=True, type=Path)

    args = parser.parse_args()
    if args.command in {"study", "all"}:
        result = run_study(args.db)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(args.output_dir / "板块历史回放结果.json", result)
        (args.output_dir / "板块历史回放报告.md").write_text(
            render_report(result), encoding="utf-8"
        )
        print(json.dumps(result["acceptance_gate"], ensure_ascii=False, indent=2))
    if args.command in {"cases", "all"}:
        cases = run_case_replay(args.fixtures)
        output = args.output if args.command == "cases" else args.output_dir / "行为案例回放.json"
        if output:
            _write_json(output, cases)
        print(json.dumps({"case_replay_passed": cases["passed"]}, ensure_ascii=False))
        if not cases["passed"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
