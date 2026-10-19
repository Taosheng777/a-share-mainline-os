#!/usr/bin/env python3
"""Single-asset, long-only research simulation. Never sends orders or infers trades.

JSON input: calendar, prices, strategies, source, mode, cost_bps. Calendar is an
explicit list of exchange trading dates; prices contain date/open/close/buyable/
sellable. Strategies contain outside-session date/target decisions, not holdings.
"""
import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def prospective_records(data):
    """Self-declared timestamps are insufficient; read locked journal receipts."""
    if data["mode"] != "prospective_shadow":
        return {}
    if not data.get("journal"):
        raise ValueError("prospective simulation requires the original decision journal")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import decision_journal
    with decision_journal.locked_file(data["journal"]) as handle:
        rows = decision_journal.read_rows(handle)
    return {r["record"]["decision_id"]: r for r in rows}


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}: finite number required")
    return float(value)


def simulate(data):
    if data.get("mode") not in {"historical_diagnostic", "prospective_shadow", "synthetic_test"}:
        raise ValueError("explicit research mode required")
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise ValueError("source required")
    if not isinstance(data.get("asset"), str) or not data["asset"].strip():
        raise ValueError("single asset identity required")
    calendar = data["calendar"]
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError("calendar must be nonempty, unique and sorted")
    for day in calendar:
        date.fromisoformat(day)
        if day > datetime.now(TZ).date().isoformat():
            raise ValueError("future market dates are not allowed")
    rows = data["prices"]
    if [r["date"] for r in rows] != calendar:
        raise ValueError("price dates must exactly cover declared trading calendar; no gap filling")
    for row in rows:
        for key in ("open", "close"):
            if number(row[key], key) <= 0:
                raise ValueError("prices must be positive and on the same adjustment basis")
        for key in ("buyable", "sellable"):
            if not isinstance(row.get(key), bool):
                raise ValueError("explicit buyable/sellable evidence required")
    if data.get("price_basis") not in {"adjusted_consistent", "unadjusted_no_actions"}:
        raise ValueError("declare consistent price_basis; corporate actions cannot be ignored")
    cost = number(data["cost_bps"], "cost_bps") / 10000
    if not 0 <= cost < 0.1:
        raise ValueError("cost_bps outside research range [0,1000)")
    strategies = data["strategies"]
    if len(strategies) < 2 or len({s["name"] for s in strategies}) != len(strategies):
        raise ValueError("at least two uniquely named strategies required")
    results = []
    for strategy in strategies:
        receipts = prospective_records({**data, "journal": strategy.get("journal", data.get("journal"))})
        signals = strategy["signals"]
        if data["mode"] == "prospective_shadow":
            for key in ("theme_id", "method_version"):
                if not strategy.get(key):
                    raise ValueError("prospective strategy requires locked theme_id and method_version")
            cutoff = datetime.fromisoformat(data["evaluation_as_of"])
            if cutoff.tzinfo is None or cutoff > datetime.now(TZ) or cutoff < datetime.combine(date.fromisoformat(calendar[-1]), time(15, 0), TZ):
                raise ValueError("evaluation_as_of must be known and include last market close")
            start = datetime.combine(date.fromisoformat(calendar[0]), time(0, 0), TZ)
            theme_rows = [row for row in receipts.values() if row["record"]["mode"] == "shadow" and row["record"]["theme_id"] == strategy["theme_id"] and datetime.fromisoformat(row["recorded_at"]) <= cutoff]
            before = [row for row in theme_rows if datetime.fromisoformat(row["recorded_at"]) < start and "research_target" in row["record"]]
            if before and before[-1]["record"]["research_target"] != 0:
                raise ValueError("study starts inside existing exposure; start at episode inception")
            window = [row for row in theme_rows if datetime.fromisoformat(row["recorded_at"]) >= start]
            if any(row["record"]["method_version"] != strategy["method_version"] for row in window):
                raise ValueError("method changed inside study window")
            expected = {row["record"]["decision_id"] for row in window if "research_target" in row["record"]}
            supplied = [s.get("decision_id") for s in signals]
            if len(supplied) != len(set(supplied)) or set(supplied) != expected:
                raise ValueError("signals must include every locked research decision in interval; no selective omission")
            if not expected:
                raise ValueError("no prospective research allocation records")
        dates = [s["date"] for s in signals]
        if dates != sorted(set(dates)):
            raise ValueError("one locked signal per strategy/date; no duplicate or unordered dates")
        effective_times, actions = {}, {}
        for sig in signals:
            if sig["date"] < calendar[0]:
                raise ValueError("signal predates study calendar")
            if not 0 <= number(sig["target"], "target") <= 1:
                raise ValueError("long-only research allocation [0,1] required")
            stamp = datetime.fromisoformat(sig["decided_at"])
            if stamp.tzinfo is None:
                raise ValueError("decided_at must contain timezone")
            local = stamp.astimezone(TZ)
            if local.date().isoformat() != sig["date"] or (sig["date"] in calendar and time(9, 30) <= local.time() < time(15, 0)):
                raise ValueError("market-session signals must be outside 09:30–15:00; use actual decision date/time")
            if local > datetime.now(TZ):
                raise ValueError("future decision time")
            if not sig.get("evidence_ref"):
                raise ValueError("signal evidence_ref required")
            effective_times[sig["date"]] = local
            actions[sig["date"]] = sig.get("action", "rebalance")
            if data["mode"] == "prospective_shadow":
                row = receipts.get(sig.get("decision_id"))
                if row is None:
                    raise ValueError("signal has no journal receipt")
                rec = row["record"]
                if rec["mode"] != "shadow" or rec.get("research_target") != sig["target"] or rec["decided_at"] != sig["decided_at"] or rec.get("research_asset") != data["asset"] or rec["theme_id"] != strategy["theme_id"] or rec["method_version"] != strategy["method_version"]:
                    raise ValueError("signal does not match locked shadow decision and research target")
                effective_times[sig["date"]] = max(local, datetime.fromisoformat(row["recorded_at"]).astimezone(TZ))
                actions[sig["date"]] = rec["action"]
            if actions[sig["date"]] not in {"observe", "enter", "hold", "reduce", "exit", "rebalance"}:
                raise ValueError("invalid simulation action")
            if actions[sig["date"]] == "exit" and sig["target"] != 0:
                raise ValueError("exit requires zero research target")
        scheduled, pending, superseded = {}, [], []
        for sig in signals:
            eligible = [day for day in calendar if datetime.combine(date.fromisoformat(day), time(9, 30), TZ) > effective_times[sig["date"]]]
            if not eligible:
                pending.append(sig["date"])
                continue
            day = eligible[0]
            if day in scheduled:
                superseded.append(scheduled[day]["date"])
            scheduled[day] = sig
        cash, units, peak, max_dd, turnover, fees = 1.0, 0.0, 1.0, 0.0, 0.0, 0.0
        path, fills, missed, noops = [], [], [], []
        for i, row in enumerate(rows):
            # A signal is usable only at the first strictly later session open. No
            # retry of missed trades, and no fabricated decisions on gap days.
            sig = scheduled.get(row["date"])
            if sig is not None and actions[sig["date"]] not in ("hold", "observe"):
                holding = units * row["open"]
                value = cash + holding
                desired = sig["target"] * value - holding
                action = actions[sig["date"]]
                if (action == "reduce" and desired > 1e-12) or (action == "enter" and desired < -1e-12):
                    noops.append({"signal_date": sig["date"], "date": row["date"], "reason": f"{action}: target already beyond actual exposure; no opposite-direction trade"})
                    desired = 0
                if abs(desired) > 1e-12:
                    side = "buy" if desired > 0 else "sell"
                    if row["buyable" if side == "buy" else "sellable"]:
                        # Allocation is defined AFTER transaction costs.
                        amount = desired / (1 + sig["target"] * cost) if desired > 0 else desired / (1 - sig["target"] * cost)
                        fee = abs(amount) * cost
                        cash -= amount + fee
                        units += amount / row["open"]
                        if cash < -1e-10 or units < -1e-10:
                            raise ValueError("invalid cash or short exposure")
                        cash, units = max(0.0, cash), max(0.0, units)
                        fees += fee
                        turnover += abs(amount)
                        fills.append({"signal_date": sig["date"], "simulation_date": row["date"], "side": side, "price": row["open"], "notional": amount, "cost": fee})
                    else:
                        missed.append({"signal_date": sig["date"], "date": row["date"], "reason": f"{side}_not_executable; no automatic retry"})
            value = cash + units * row["close"]
            peak = max(peak, value)
            max_dd = min(max_dd, value / peak - 1)
            path.append({"date": row["date"], "research_value": value, "exposure": units * row["close"] / value})
        results.append({"name": strategy["name"], "return_pct": (path[-1]["research_value"] - 1) * 100, "max_drawdown_pct": max_dd * 100, "mean_exposure": sum(p["exposure"] for p in path) / len(path), "turnover_initial_units": turnover, "cost_initial_units": fees, "fills": fills, "unexecuted": missed, "action_noops": noops, "awaiting_next_session": pending, "superseded_before_execution": superseded, "path": path})
    return {"schema_version": 1, "mode": data["mode"], "status": "research_simulation_only", "profitability": "not_established", "source": data["source"], "input_sha256": hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), "assumptions": ["same asset, same initial unit, long only; no account data", "signals outside market hours; first declared exchange open strictly after effective recording time", "cash return 0; terminal positions marked, not liquidated", "proportional costs include fee/slippage assumption; no minimum lot/minimum commission model", "daily trading at most once; missed executions expire", "caller must provide complete calendar, corporate-action basis and executable-price evidence", "historical or synthetic simulation cannot establish prospective AI skill"], "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = simulate(json.loads(args.input.read_text()))
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n")
        else:
            print(text)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"not_evaluable: {exc}\n")


if __name__ == "__main__":
    main()
