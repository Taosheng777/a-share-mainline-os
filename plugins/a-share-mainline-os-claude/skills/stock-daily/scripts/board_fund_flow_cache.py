#!/usr/bin/env python3
"""东财板块逐日资金流：SQLite 缓存、低频补洞与持久熔断。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time as time_module
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests


SHANGHAI = ZoneInfo("Asia/Shanghai")
PUSH2HIS_ENDPOINT = "eastmoney:push2his:fflow/daykline"
PUSH2HIS_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
CLIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
TUSHARE_DEFAULT_URL = "https://api.tushare.pro"
TUSHARE_FIELDS = (
    "trade_date", "content_type", "ts_code", "name", "pct_change", "close",
    "net_amount", "net_amount_rate", "buy_elg_amount", "buy_lg_amount",
    "buy_md_amount", "buy_sm_amount",
)
TUSHARE_AUDIT_ANCHORS: tuple[str, ...] = ()
"""审计锚点默认留空，必须由使用者按自己实际跟踪的板块显式指定（CLI `--anchors`）。

不预置具体板块代码：把某一个人的关注面固化成公共默认值，既误导使用者，也会让
「锚点覆盖是否达标」这类结论在别人的库上失去意义。"""
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

BOARD_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
    "region": "m:90+t:1",
}
CONTENT_TYPE_TO_BOARD_TYPE = {
    "行业": "industry",
    "概念": "concept",
    "地域": "region",
}
PROVIDER_PRIORITY = {
    "tushare_moneyflow_ind_dc": 1,
    "eastmoney_push2his": 2,
    "eastmoney_clist_snapshot": 3,
}
NET_FIELDS = ("main_net", "super_net", "large_net", "mid_net", "small_net")
PCT_FIELDS = ("change_pct", "main_pct")


class BoardFlowError(RuntimeError):
    pass


class SourceUnavailableError(BoardFlowError):
    pass


class CircuitOpenError(SourceUnavailableError):
    pass


class DataConflictError(BoardFlowError):
    pass


class InsufficientHistoryError(BoardFlowError):
    pass


class SnapshotDateError(BoardFlowError):
    pass


def default_db_path() -> Path:
    configured = os.environ.get("A_STOCK_DATA_HOME")
    root = Path(configured).expanduser() if configured else (
        Path.home() / "Library" / "Application Support" / "a-stock-data"
    )
    return root / "board_fund_flow.sqlite3"


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path = None) -> Path:
    path = Path(db_path or default_db_path()).expanduser()
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                taxonomy TEXT NOT NULL,
                board_code TEXT NOT NULL,
                board_type TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
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
            CREATE INDEX IF NOT EXISTS observations_lookup
                ON observations(taxonomy, board_code, trade_date);
            CREATE TABLE IF NOT EXISTS circuit_breakers (
                endpoint TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                retry_after TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backfill_checkpoints (
                source TEXT NOT NULL,
                scope TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                remote_rows INTEGER NOT NULL,
                stored_rows INTEGER NOT NULL,
                invalid_rows INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'remote',
                completed_at TEXT NOT NULL,
                PRIMARY KEY (source, scope, trade_date)
            );
            CREATE TABLE IF NOT EXISTS backfill_rejections (
                source TEXT NOT NULL,
                scope TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                board_code TEXT NOT NULL,
                board_type TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (source, scope, trade_date, board_code)
            );
            """
        )
        checkpoint_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(backfill_checkpoints)")
        }
        if "invalid_rows" not in checkpoint_columns:
            conn.execute(
                "ALTER TABLE backfill_checkpoints "
                "ADD COLUMN invalid_rows INTEGER NOT NULL DEFAULT 0"
            )
    return path


def normalize_board_code(value: str) -> str:
    code = value.strip().upper()
    if code.startswith("90."):
        code = code[3:]
    if code.endswith(".DC"):
        code = code[:-3]
    if code.isdigit() and len(code) == 4:
        code = "BK" + code
    if not (code.startswith("BK") and len(code) == 6 and code[2:].isdigit()):
        raise ValueError(f"东财板块代码格式错误: {value!r}，应为 BKxxxx")
    return code


def _finite(value: Any, field: str, *, allow_none: bool = False) -> float | None:
    if value is None or value == "-" or value == "":
        if allow_none:
            return None
        raise ValueError(f"字段 {field} 缺失")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"字段 {field} 非有限数: {value!r}")
    return number


def _normalize_date(value: Any) -> str:
    raw = str(value).strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"交易日格式错误: {value!r}")
    return datetime.strptime(raw, "%Y%m%d").date().isoformat()


def _validate_observation(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if result.get("taxonomy") != "eastmoney_dc":
        raise ValueError("本缓存只接受 eastmoney_dc 分类，THS 等分类不得混入")
    result["board_code"] = normalize_board_code(result["board_code"])
    result["trade_date"] = _normalize_date(result["trade_date"])
    result["close"] = _finite(result.get("close"), "close")
    if result["close"] <= 0:
        raise ValueError("close 必须大于 0")
    result["main_net"] = _finite(result.get("main_net"), "main_net")
    for field in NET_FIELDS[1:] + PCT_FIELDS:
        result[field] = _finite(result.get(field), field, allow_none=True)
    result["board_type"] = result.get("board_type") or ""
    result["name"] = result.get("name") or ""
    provider = result.get("provider") or ""
    if provider not in PROVIDER_PRIORITY:
        raise ValueError(f"未知 provider: {provider!r}")
    result["provider"] = provider
    return result


def store_observations(
    db_path: str | Path,
    rows: Iterable[dict[str, Any]],
    *,
    fetched_at: datetime | None = None,
) -> int:
    init_db(db_path)
    timestamp = (fetched_at or _now()).astimezone(SHANGHAI).isoformat()
    values = []
    for raw in rows:
        row = _validate_observation(raw)
        values.append((
            row["taxonomy"], row["board_code"], row["board_type"], row["name"],
            row["trade_date"], row["close"], row["change_pct"], row["main_net"],
            row["main_pct"], row["super_net"], row["large_net"], row["mid_net"],
            row["small_net"], row["provider"], timestamp,
        ))
    if not values:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO observations (
                taxonomy, board_code, board_type, name, trade_date, close,
                change_pct, main_net, main_pct, super_net, large_net, mid_net,
                small_net, provider, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(taxonomy, board_code, trade_date, provider) DO UPDATE SET
                board_type=excluded.board_type, name=excluded.name, close=excluded.close,
                change_pct=excluded.change_pct, main_net=excluded.main_net,
                main_pct=excluded.main_pct, super_net=excluded.super_net,
                large_net=excluded.large_net, mid_net=excluded.mid_net,
                small_net=excluded.small_net, fetched_at=excluded.fetched_at
            """,
            values,
        )
    return len(values)


def _materially_different(field: str, left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    delta = abs(left - right)
    scale = max(abs(left), abs(right), 1.0)
    if field in NET_FIELDS:
        return delta > max(1_000_000.0, scale * 0.0005)
    if field == "close":
        return delta > max(0.02, scale * 0.000001)
    return delta > 0.02


def _reconcile(rows: list[sqlite3.Row]) -> dict[str, Any]:
    providers = sorted(row["provider"] for row in rows)
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            conflicts = []
            for field in ("close",) + NET_FIELDS + PCT_FIELDS:
                if _materially_different(field, left[field], right[field]):
                    conflicts.append(f"{field}: {left[field]} vs {right[field]}")
            if conflicts:
                raise DataConflictError(
                    f"{left['board_code']} {left['trade_date']} 跨源冲突 "
                    f"({left['provider']} / {right['provider']}): " + "; ".join(conflicts)
                )
    selected = max(rows, key=lambda row: PROVIDER_PRIORITY[row["provider"]])
    result = dict(selected)
    result["date"] = result["trade_date"]
    result["providers"] = providers
    result["source_count"] = len(providers)
    return result


def load_history(
    db_path: str | Path,
    board_code: str,
    *,
    limit: int = 120,
    taxonomy: str = "eastmoney_dc",
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    code = normalize_board_code(board_code)
    where = ["taxonomy=?", "board_code=?"]
    values: list[Any] = [taxonomy, code]
    if start_date:
        where.append("trade_date>=?")
        values.append(_normalize_date(start_date))
    if end_date:
        where.append("trade_date<=?")
        values.append(_normalize_date(end_date))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM observations
            WHERE {' AND '.join(where)}
            ORDER BY trade_date ASC, provider ASC
            """,
            values,
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["trade_date"]].append(row)
    reconciled = [_reconcile(grouped[date]) for date in sorted(grouped)]
    return reconciled[-limit:] if limit > 0 else reconciled


def _next_probe_time(now: datetime) -> datetime:
    local = now.astimezone(SHANGHAI)
    today_probe = datetime.combine(local.date(), time(15, 35), SHANGHAI)
    return today_probe if local < today_probe else today_probe + timedelta(days=1)


def open_circuit(
    db_path: str | Path,
    endpoint: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> None:
    init_db(db_path)
    opened = (now or _now()).astimezone(SHANGHAI)
    retry_after = _next_probe_time(opened)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO circuit_breakers(endpoint, status, opened_at, retry_after, reason)
            VALUES (?, 'open', ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET status='open', opened_at=excluded.opened_at,
                retry_after=excluded.retry_after, reason=excluded.reason
            """,
            (endpoint, opened.isoformat(), retry_after.isoformat(), reason[:500]),
        )


def close_circuit(db_path: str | Path, endpoint: str) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM circuit_breakers WHERE endpoint=?", (endpoint,))


def get_circuit_state(db_path: str | Path, endpoint: str) -> dict[str, Any] | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM circuit_breakers WHERE endpoint=?", (endpoint,)
        ).fetchone()
    return dict(row) if row else None


def _assert_circuit_allows(
    db_path: str | Path,
    endpoint: str,
    *,
    now: datetime,
    force: bool,
) -> None:
    if force:
        return
    state = get_circuit_state(db_path, endpoint)
    if state and state["status"] == "open" and now < parse_time(state["retry_after"]):
        raise CircuitOpenError(
            f"{endpoint} 熔断至 {state['retry_after']}；期间禁止重试。原因: {state['reason']}"
        )


def _new_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"User-Agent": UA})
    return session


def _parse_push2his(payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        raise SourceUnavailableError("push2his 返回空 klines；按端点故障处理，不是无历史")
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 13:
            raise SourceUnavailableError(f"push2his 字段数异常: {line[:120]}")
        rows.append({
            "taxonomy": "eastmoney_dc",
            "board_code": code,
            "board_type": "",
            "name": data.get("name") or "",
            "trade_date": parts[0],
            "main_net": parts[1],
            "small_net": parts[2],
            "mid_net": parts[3],
            "large_net": parts[4],
            "super_net": parts[5],
            "main_pct": parts[6],
            "close": parts[11],
            "change_pct": parts[12],
            "provider": "eastmoney_push2his",
        })
    return [_validate_observation(row) for row in rows]


def fetch_push2his_history(
    board_code: str,
    db_path: str | Path,
    *,
    limit: int = 120,
    session: requests.Session | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """最多发一个请求；失败即开启持久熔断，不做退避重试或换域。"""
    code = normalize_board_code(board_code)
    current = (now or _now()).astimezone(SHANGHAI)
    _assert_circuit_allows(db_path, PUSH2HIS_ENDPOINT, now=current, force=force)
    client = session or _new_session()
    params = {
        "secid": f"90.{code}",
        "klt": "101",
        "lmt": str(limit),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
    }
    try:
        response = client.get(
            PUSH2HIS_URL,
            params=params,
            headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=12,
        )
        response.raise_for_status()
        rows = _parse_push2his(response.json(), code)
    except CircuitOpenError:
        raise
    except Exception as exc:
        open_circuit(db_path, PUSH2HIS_ENDPOINT, f"{type(exc).__name__}: {exc}", now=current)
        if isinstance(exc, SourceUnavailableError):
            raise
        raise SourceUnavailableError(
            f"push2his 单次探测失败，已熔断且未重试: {type(exc).__name__}: {exc}"
        ) from exc
    close_circuit(db_path, PUSH2HIS_ENDPOINT)
    return rows


def resolve_tushare_url(api_url: str | None = None) -> str:
    """读取可选镜像地址；只接受无内嵌凭据的 HTTPS URL。"""
    value = (api_url or os.environ.get("TUSHARE_HTTP_URL") or TUSHARE_DEFAULT_URL).strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("TUSHARE_HTTP_URL 必须是无内嵌凭据的 HTTPS URL")
    return value.rstrip("/")


def _request_tushare(
    params: dict[str, str],
    token: str,
    *,
    session: requests.Session | None = None,
    api_url: str | None = None,
) -> tuple[list[str], list[list[Any]]]:
    if not token:
        raise SourceUnavailableError("未配置 TUSHARE_TOKEN")
    payload = {
        "api_name": "moneyflow_ind_dc",
        "token": token,
        "params": params,
        "fields": ",".join(TUSHARE_FIELDS),
    }
    client = session or _new_session()
    try:
        response = client.post(
            resolve_tushare_url(api_url), json=payload, timeout=30
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise SourceUnavailableError(
            f"Tushare moneyflow_ind_dc 请求失败: {type(exc).__name__}: {exc}"
        ) from exc
    if body.get("code") != 0:
        raise SourceUnavailableError(
            f"Tushare moneyflow_ind_dc 不可用: {body.get('msg') or '未知错误'}"
        )
    data = body.get("data") or {}
    return data.get("fields") or [], data.get("items") or []


def _parse_tushare_rows(
    names: list[str],
    items: list[list[Any]],
    *,
    expected_date: str | None = None,
    board_types: Iterable[str] = BOARD_FS,
    board_type_overrides: dict[str, str] | None = None,
    invalid_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    selected = tuple(board_types)
    invalid = sorted(set(selected) - set(BOARD_FS))
    if invalid:
        raise ValueError(f"未知板块类型: {invalid}")
    rows = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        raw = dict(zip(names, item))
        source_board_type = CONTENT_TYPE_TO_BOARD_TYPE.get(raw.get("content_type"))
        if source_board_type is None:
            raise SourceUnavailableError(
                f"Tushare 返回未知 content_type: {raw.get('content_type')!r}"
            )
        code = normalize_board_code(raw.get("ts_code") or "")
        board_type = (board_type_overrides or {}).get(code, source_board_type)
        actual_date = _normalize_date(raw.get("trade_date"))
        if expected_date and actual_date != expected_date:
            raise SnapshotDateError(
                f"Tushare 数据日 {actual_date} != 请求日 {expected_date}"
            )
        if board_type not in selected:
            continue
        try:
            row = _validate_observation({
                "taxonomy": "eastmoney_dc",
                "board_code": code,
                "board_type": board_type,
                "name": raw.get("name") or "",
                "trade_date": actual_date,
                "close": raw.get("close"),
                "change_pct": raw.get("pct_change"),
                "main_net": raw.get("net_amount"),
                "main_pct": raw.get("net_amount_rate"),
                "super_net": raw.get("buy_elg_amount"),
                "large_net": raw.get("buy_lg_amount"),
                "mid_net": raw.get("buy_md_amount"),
                "small_net": raw.get("buy_sm_amount"),
                "provider": "tushare_moneyflow_ind_dc",
            })
        except ValueError as exc:
            if invalid_rows is None or str(exc) != "close 必须大于 0":
                raise
            invalid_rows.append({
                "trade_date": actual_date,
                "board_code": normalize_board_code(raw.get("ts_code") or ""),
                "name": raw.get("name") or "",
                "board_type": board_type,
                "reason": str(exc),
            })
            continue
        key = (row["board_code"], row["trade_date"])
        if key in seen:
            raise SourceUnavailableError(f"Tushare 返回重复板块日期键: {key}")
        seen.add(key)
        rows.append(row)
    return sorted(rows, key=lambda row: (row["trade_date"], row["board_code"]))


def fetch_tushare_history(
    board_code: str,
    token: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    session: requests.Session | None = None,
    api_url: str | None = None,
) -> list[dict[str, Any]]:
    """调用 Tushare moneyflow_ind_dc；token 只进请求，不写日志或数据库。"""
    code = normalize_board_code(board_code)
    params: dict[str, str] = {"ts_code": f"{code}.DC"}
    if start_date:
        params["start_date"] = _normalize_date(start_date).replace("-", "")
    if end_date:
        params["end_date"] = _normalize_date(end_date).replace("-", "")
    names, items = _request_tushare(
        params, token, session=session, api_url=api_url
    )
    if not items:
        raise SourceUnavailableError("Tushare moneyflow_ind_dc 返回空数据")
    return _parse_tushare_rows(names, items)


def fetch_tushare_snapshot(
    trade_date: str,
    token: str,
    *,
    board_types: Iterable[str] = BOARD_FS,
    session: requests.Session | None = None,
    api_url: str | None = None,
    board_type_overrides: dict[str, str] | None = None,
    invalid_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按单个交易日取完整截面；单日约千行，低于接口 5000 行上限。"""
    expected = _normalize_date(trade_date)
    names, items = _request_tushare(
        {"trade_date": expected.replace("-", "")}, token,
        session=session, api_url=api_url,
    )
    return _parse_tushare_rows(
        names, items, expected_date=expected, board_types=board_types,
        board_type_overrides=board_type_overrides,
        invalid_rows=invalid_rows,
    )


def fetch_tushare_calendar(
    token: str,
    *,
    anchors: Iterable[str] = TUSHARE_AUDIT_ANCHORS,
    start_date: str,
    end_date: str,
    session: requests.Session | None = None,
    api_url: str | None = None,
) -> list[str]:
    """用锚点板块的远端逐日史当交易日历。

    回填窗口早于本地库时，`_backfill_trade_dates` 无日可给。此处不引入第三方
    交易日历——直接问数据源自己「哪几天有这个板块的数据」，避免请求非交易日
    （空返回会被 backfill 判成缺口并中断）。取多锚点并集是为了容忍单个板块
    中途停牌或晚于窗口起点才建板。
    """
    codes = [normalize_board_code(raw) for raw in anchors]
    if not codes:
        raise ValueError(
            "未指定审计锚点：请用 --anchors 传入自己跟踪的板块代码，"
            "本工具不预置默认板块。"
        )
    client = session or _new_session()
    dates: set[str] = set()
    for raw_code in codes:
        rows = fetch_tushare_history(
            raw_code, token, start_date=start_date,
            end_date=end_date, session=client, api_url=api_url,
        )
        dates.update(row["trade_date"] for row in rows)
    return sorted(dates)


def audit_anchor_rows(
    local_rows: list[dict[str, Any]],
    remote_rows: list[dict[str, Any]],
    *,
    minimum_overlap: int = 115,
) -> dict[str, Any]:
    """对账核心字段。允许极小历史修订，但不允许方向翻转或决策级偏差。"""
    local = {row["trade_date"]: row for row in local_rows}
    remote = {row["trade_date"]: row for row in remote_rows}
    overlap = sorted(set(local) & set(remote))
    exact_diff_days: set[str] = set()
    strict_conflict_days: set[str] = set()
    sign_flips = 0
    relaxed_conflicts: list[dict[str, Any]] = []
    maxima = {
        "main_net_abs": 0.0, "main_net_rel": 0.0,
        "close_abs": 0.0, "close_rel": 0.0,
    }
    for day in overlap:
        left, right = local[day], remote[day]
        if any(left.get(field) != right.get(field)
               for field in ("close", "main_net", "change_pct", "main_pct")):
            exact_diff_days.add(day)
        if any(_materially_different(field, left.get(field), right.get(field))
               for field in ("close", "main_net", "change_pct", "main_pct")):
            strict_conflict_days.add(day)

        local_net, remote_net = float(left["main_net"]), float(right["main_net"])
        if local_net and remote_net and (local_net > 0) != (remote_net > 0):
            sign_flips += 1
        net_abs = abs(local_net - remote_net)
        net_rel = net_abs / max(abs(local_net), abs(remote_net), 1.0)
        close_abs = abs(float(left["close"]) - float(right["close"]))
        close_rel = close_abs / max(abs(float(left["close"])), abs(float(right["close"])), 1.0)
        maxima["main_net_abs"] = max(maxima["main_net_abs"], net_abs)
        maxima["main_net_rel"] = max(maxima["main_net_rel"], net_rel)
        maxima["close_abs"] = max(maxima["close_abs"], close_abs)
        maxima["close_rel"] = max(maxima["close_rel"], close_rel)

        reasons = []
        if net_abs > 5_000_000:
            reasons.append("main_net_abs>500万")
        if net_rel > 0.0025:
            reasons.append("main_net_rel>0.25%")
        if _materially_different("close", left.get("close"), right.get("close")):
            reasons.append("close")
        for field in ("change_pct", "main_pct"):
            if _materially_different(field, left.get(field), right.get(field)):
                reasons.append(field)
        if reasons:
            relaxed_conflicts.append({"trade_date": day, "reasons": reasons})

    passed = (
        len(overlap) >= minimum_overlap
        and sign_flips == 0
        and not relaxed_conflicts
    )
    return {
        "passed": passed,
        "minimum_overlap": minimum_overlap,
        "overlap": len(overlap),
        "local_only": sorted(set(local) - set(remote)),
        "remote_only": sorted(set(remote) - set(local)),
        "exact_diff_days": len(exact_diff_days),
        "strict_conflict_days": len(strict_conflict_days),
        "sign_flips": sign_flips,
        "relaxed_conflicts": relaxed_conflicts,
        "max_main_net_abs": maxima["main_net_abs"],
        "max_main_net_rel_pct": maxima["main_net_rel"] * 100,
        "max_close_abs": maxima["close_abs"],
        "max_close_rel_pct": maxima["close_rel"] * 100,
        "acceptance": {
            "main_net_abs_max": 5_000_000,
            "main_net_rel_max_pct": 0.25,
            "sign_flips": 0,
            "other_fields": "沿用缓存跨源冲突阈值",
        },
    }


def audit_tushare_anchors(
    db_path: str | Path,
    token: str,
    *,
    anchors: Iterable[str] = TUSHARE_AUDIT_ANCHORS,
    start_date: str | None = None,
    end_date: str | None = None,
    minimum_overlap: int = 115,
    session: requests.Session | None = None,
    api_url: str | None = None,
) -> dict[str, Any]:
    codes = [normalize_board_code(raw) for raw in anchors]
    if not codes:
        raise ValueError(
            "未指定审计锚点：请用 --anchors 传入自己跟踪的板块代码，"
            "本工具不预置默认板块。"
        )
    client = session or _new_session()
    reports = {}
    for code in codes:
        local_rows = load_history(
            db_path, code, limit=0, start_date=start_date, end_date=end_date
        )
        remote_rows = fetch_tushare_history(
            code, token, start_date=start_date, end_date=end_date,
            session=client, api_url=api_url,
        )
        reports[code] = audit_anchor_rows(
            local_rows, remote_rows, minimum_overlap=minimum_overlap
        )
    return {
        "passed": bool(reports) and all(item["passed"] for item in reports.values()),
        "endpoint_host": urlsplit(resolve_tushare_url(api_url)).hostname,
        "anchors": reports,
    }


def filter_missing_observations(
    db_path: str | Path,
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只保留主库尚无任何 provider 的板块日期，避免制造跨源覆盖。"""
    candidates = list(rows)
    if not candidates:
        return []
    dates = sorted({row["trade_date"] for row in candidates})
    placeholders = ",".join("?" for _ in dates)
    with _connect(db_path) as conn:
        existing = {
            (row["board_code"], row["trade_date"])
            for row in conn.execute(
                "SELECT DISTINCT board_code, trade_date FROM observations "
                f"WHERE taxonomy='eastmoney_dc' AND trade_date IN ({placeholders})",
                dates,
            )
        }
    return [
        row for row in candidates
        if (row["board_code"], row["trade_date"]) not in existing
    ]


def latest_board_type_map(db_path: str | Path) -> dict[str, str]:
    """以最近一次完整 clist 快照为单值分类目录，避免历史源瞬时错标。"""
    init_db(db_path)
    with _connect(db_path) as conn:
        latest = conn.execute(
            "SELECT max(trade_date) FROM observations "
            "WHERE taxonomy='eastmoney_dc' "
            "AND provider='eastmoney_clist_snapshot'"
        ).fetchone()[0]
        if not latest:
            return {}
        rows = conn.execute(
            "SELECT board_code, board_type FROM observations "
            "WHERE taxonomy='eastmoney_dc' "
            "AND provider='eastmoney_clist_snapshot' "
            "AND trade_date=? AND board_type<>''",
            (latest,),
        )
    return {row["board_code"]: row["board_type"] for row in rows}


def normalize_stored_board_types(
    db_path: str | Path,
    board_type_map: dict[str, str] | None = None,
) -> int:
    """按最近 clist 目录统一同一 BK 代码的类型；只改分类，不改行情值。"""
    type_map = board_type_map or latest_board_type_map(db_path)
    if not type_map:
        return 0
    with _connect(db_path) as conn:
        before = conn.total_changes
        conn.executemany(
            "UPDATE observations SET board_type=? "
            "WHERE taxonomy='eastmoney_dc' AND board_code=? AND board_type<>?",
            [(board_type, code, board_type) for code, board_type in type_map.items()],
        )
        return conn.total_changes - before


def _backfill_trade_dates(
    db_path: str | Path,
    start_date: str,
    end_date: str,
) -> list[str]:
    start, end = _normalize_date(start_date), _normalize_date(end_date)
    with _connect(db_path) as conn:
        return [
            row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM observations "
                "WHERE taxonomy='eastmoney_dc' AND trade_date BETWEEN ? AND ? "
                "ORDER BY trade_date",
                (start, end),
            )
        ]


def _checkpoint_states(db_path: str | Path, scope: str) -> dict[str, dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
                "SELECT * FROM backfill_checkpoints WHERE source=? AND scope=?",
                ("tushare_moneyflow_ind_dc", scope),
            ).fetchall()
    return {row["trade_date"]: dict(row) for row in rows}


def _save_checkpoint(
    db_path: str | Path,
    scope: str,
    trade_date: str,
    *,
    remote_rows: int,
    stored_rows: int,
    invalid_rows: int = 0,
    mode: str,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO backfill_checkpoints(
                source, scope, trade_date, remote_rows, stored_rows,
                invalid_rows, mode, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, scope, trade_date) DO UPDATE SET
                remote_rows=excluded.remote_rows, stored_rows=excluded.stored_rows,
                invalid_rows=excluded.invalid_rows, mode=excluded.mode,
                completed_at=excluded.completed_at
            """,
            (
                "tushare_moneyflow_ind_dc", scope, trade_date,
                remote_rows, stored_rows, invalid_rows, mode, _now().isoformat(),
            ),
        )


def _save_rejections(
    db_path: str | Path,
    scope: str,
    rows: Iterable[dict[str, Any]],
) -> int:
    values = [
        (
            "tushare_moneyflow_ind_dc", scope, row["trade_date"],
            normalize_board_code(row["board_code"]), row["board_type"],
            row.get("name") or "", row["reason"], _now().isoformat(),
        )
        for row in rows
    ]
    if not values:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO backfill_rejections(
                source, scope, trade_date, board_code, board_type,
                name, reason, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, scope, trade_date, board_code) DO UPDATE SET
                board_type=excluded.board_type, name=excluded.name,
                reason=excluded.reason, recorded_at=excluded.recorded_at
            """,
            values,
        )
    return len(values)


def _rejection_count(db_path: str | Path, scope: str, trade_date: str) -> int:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT count(*) FROM backfill_rejections "
            "WHERE source=? AND scope=? AND trade_date=?",
            ("tushare_moneyflow_ind_dc", scope, trade_date),
        ).fetchone()[0]


def _local_clist_counts(
    db_path: str | Path,
    trade_date: str,
    board_types: Iterable[str],
) -> dict[str, int]:
    with _connect(db_path) as conn:
        return {
            board_type: conn.execute(
                "SELECT count(*) FROM observations WHERE taxonomy='eastmoney_dc' "
                "AND provider='eastmoney_clist_snapshot' AND trade_date=? AND board_type=?",
                (trade_date, board_type),
            ).fetchone()[0]
            for board_type in board_types
        }


def backfill_tushare_history(
    db_path: str | Path,
    token: str,
    *,
    start_date: str,
    end_date: str,
    board_types: Iterable[str] = ("concept", "region"),
    anchors: Iterable[str] = TUSHARE_AUDIT_ANCHORS,
    minimum_overlap: int = 115,
    minimum_interval: float = 0.25,
    api_url: str | None = None,
    progress=None,
    audit_start_date: str | None = None,
    audit_end_date: str | None = None,
    calendar: str = "local",
    skip_incomplete: bool = False,
) -> dict[str, Any]:
    """先审计锚点，再按交易日回填缺口；重叠数据永不写入。

    `audit_*_date` 让 Gate 跑在与本地库有重叠的窗口上：回填窗口整段早于本地库时，
    同窗口审计的重叠必为 0，Gate 会因「重叠不足」而不是「对不上」失败，等于自废。
    `calendar='anchors'` 则改由源端锚点日历给交易日，供本地库无日可给的窗口使用。
    """
    path = init_db(db_path)
    selected = tuple(dict.fromkeys(board_types))
    invalid = sorted(set(selected) - set(BOARD_FS))
    if invalid or not selected:
        raise ValueError(f"board_types 非法: {invalid or selected}")
    if minimum_interval < 0:
        raise ValueError("minimum_interval 不得为负")
    if calendar not in ("local", "anchors"):
        raise ValueError(f"calendar 只接受 local / anchors，收到: {calendar!r}")
    session = _new_session()
    audit = audit_tushare_anchors(
        path, token, anchors=anchors,
        start_date=audit_start_date or start_date,
        end_date=audit_end_date or end_date,
        minimum_overlap=minimum_overlap, session=session, api_url=api_url,
    )
    if not audit["passed"]:
        raise DataConflictError("Tushare 锚点对账 Gate 未通过，拒绝写入主库")

    board_type_overrides = latest_board_type_map(path)
    normalized_type_rows = normalize_stored_board_types(path, board_type_overrides)

    dates = _backfill_trade_dates(path, start_date, end_date)
    if calendar == "anchors":
        window = (_normalize_date(start_date), _normalize_date(end_date))
        remote_dates = fetch_tushare_calendar(
            token, anchors=anchors, start_date=start_date, end_date=end_date,
            session=session, api_url=api_url,
        )
        dates = sorted(set(dates) | {
            day for day in remote_dates if window[0] <= day <= window[1]
        })
    if not dates:
        raise InsufficientHistoryError("主库在指定区间没有可用交易日，无法安全回填")
    scope = ",".join(sorted(selected))
    checkpoints = _checkpoint_states(path, scope)
    summary = {
        "audit": audit,
        "scope": list(selected),
        "calendar": calendar,
        "trade_dates": len(dates),
        "checkpoint_skipped": 0,
        "rejection_rechecks": 0,
        "local_snapshot_skipped": 0,
        "incomplete_dates": [],
        "normalized_type_rows": normalized_type_rows,
        "remote_rows": 0,
        "stored_rows": 0,
        "invalid_rows": 0,
        "invalid_samples": [],
        "completed_dates": 0,
    }
    for index, trade_date in enumerate(dates, 1):
        checkpoint = checkpoints.get(trade_date)
        if checkpoint:
            expected_rejections = int(checkpoint.get("invalid_rows") or 0)
            if expected_rejections > _rejection_count(path, scope, trade_date):
                invalid_rows = []
                fetch_tushare_snapshot(
                    trade_date, token, board_types=selected,
                    session=session, api_url=api_url, invalid_rows=invalid_rows,
                    board_type_overrides=board_type_overrides,
                )
                if len(invalid_rows) != expected_rejections:
                    raise SourceUnavailableError(
                        f"Tushare {trade_date} 拒绝行复核 {len(invalid_rows)} "
                        f"!= 检查点 {expected_rejections}"
                    )
                _save_rejections(path, scope, invalid_rows)
                summary["rejection_rechecks"] += 1
                if minimum_interval:
                    time_module.sleep(minimum_interval)
            summary["checkpoint_skipped"] += 1
            summary["completed_dates"] += 1
            continue
        invalid_rows = []
        rows = fetch_tushare_snapshot(
            trade_date, token, board_types=selected,
            session=session, api_url=api_url, invalid_rows=invalid_rows,
            board_type_overrides=board_type_overrides,
        )
        if not rows:
            local_counts = _local_clist_counts(path, trade_date, selected)
            if not all(local_counts.values()):
                raise SourceUnavailableError(
                    f"Tushare {trade_date} 返回空，且本地 clist 不完整: {local_counts}"
                )
            _save_checkpoint(
                path, scope, trade_date, remote_rows=0, stored_rows=0,
                invalid_rows=0, mode="local_clist_complete",
            )
            summary["local_snapshot_skipped"] += 1
            summary["completed_dates"] += 1
        else:
            counts = {
                board_type: sum(row["board_type"] == board_type for row in rows)
                for board_type in selected
            }
            if not all(counts.values()):
                if not skip_incomplete:
                    raise SourceUnavailableError(
                        f"Tushare {trade_date} 截面缺类型，拒绝落盘: {counts}"
                    )
                # 残缺日一行都不落：只存到的那部分会让当日排名在少数板块间算出来，
                # 静默错得比缺数据更危险。记成检查点留痕，续跑时不再请求。
                _save_checkpoint(
                    path, scope, trade_date, remote_rows=len(rows), stored_rows=0,
                    invalid_rows=0, mode="incomplete_source",
                )
                summary["incomplete_dates"].append(
                    {"trade_date": trade_date, "counts": counts}
                )
                summary["completed_dates"] += 1
                if progress and (index == 1 or index % 10 == 0 or index == len(dates)):
                    progress(index, len(dates), summary)
                if minimum_interval and index < len(dates):
                    time_module.sleep(minimum_interval)
                continue
            missing = filter_missing_observations(path, rows)
            stored = store_observations(path, missing)
            _save_rejections(path, scope, invalid_rows)
            _save_checkpoint(
                path, scope, trade_date, remote_rows=len(rows),
                stored_rows=stored, invalid_rows=len(invalid_rows), mode="remote",
            )
            summary["remote_rows"] += len(rows)
            summary["stored_rows"] += stored
            summary["invalid_rows"] += len(invalid_rows)
            remaining_samples = 20 - len(summary["invalid_samples"])
            if remaining_samples > 0:
                summary["invalid_samples"].extend(invalid_rows[:remaining_samples])
            summary["completed_dates"] += 1
        if progress and (index == 1 or index % 10 == 0 or index == len(dates)):
            progress(index, len(dates), summary)
        if minimum_interval and index < len(dates):
            time_module.sleep(minimum_interval)
    return summary


def import_push2his_jsonl(
    db_path: str | Path,
    source_path: str | Path,
) -> dict[str, Any]:
    """导入既有全板块抓取：每行 {code,name,rows:[[date,main,main_pct,close,change]]}。

    JSONL 里没有板块类型字段。早期实现把 `board_type` 一律硬写成 `"industry"`，
    于是所有概念板块的历史都被标成行业——读取按 `board_code` 查所以不影响判定，
    但按类型做横截面会拿到空，且「概念板块覆盖不足」的统计会失真。改为**从库内
    `clist` 快照行反查权威类型**，查不到就留空字符串，**不猜、不硬写**。
    """
    source = Path(source_path).expanduser()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with _connect(db_path) as _conn:
        type_map = {
            row["board_code"]: row["board_type"]
            for row in _conn.execute(
                "SELECT DISTINCT board_code, board_type FROM observations "
                "WHERE provider='eastmoney_clist_snapshot' AND board_type<>''"
            )
        }
    line_count = 0
    board_codes: set[str] = set()
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_observations = 0
    with source.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            line_count += 1
            try:
                item = json.loads(line)
                code = normalize_board_code(item["code"])
                name = item.get("name") or ""
                for raw in item.get("rows") or []:
                    if len(raw) < 5:
                        raise ValueError(f"rows 字段不足: {raw!r}")
                    row = _validate_observation({
                        "taxonomy": "eastmoney_dc",
                        "board_code": code,
                        "board_type": type_map.get(code, ""),
                        "name": name,
                        "trade_date": raw[0],
                        "main_net": raw[1],
                        "main_pct": raw[2],
                        "close": raw[3],
                        "change_pct": raw[4],
                        "super_net": None,
                        "large_net": None,
                        "mid_net": None,
                        "small_net": None,
                        "provider": "eastmoney_push2his",
                    })
                    key = (row["board_code"], row["trade_date"])
                    existing = observations.get(key)
                    if existing is not None:
                        duplicate_observations += 1
                        if existing != row:
                            raise ValueError(
                                f"重复键 {key} 的内容不一致，拒绝静默覆盖"
                            )
                    else:
                        observations[key] = row
                if not item.get("rows"):
                    raise ValueError("rows 为空")
            except Exception as exc:
                raise ValueError(f"{source}:{line_number} 格式错误: {exc}") from exc
            board_codes.add(code)
    store_observations(db_path, observations.values())
    return {
        "source_sha256": digest,
        "lines": line_count,
        "boards": len(board_codes),
        "observations": len(observations),
        "duplicate_observations": duplicate_observations,
    }


def parse_clist_snapshot(
    payload: dict[str, Any],
    board_type: str,
    trade_date: str,
) -> list[dict[str, Any]]:
    if board_type not in BOARD_FS:
        raise ValueError(f"未知板块类型: {board_type}")
    expected = _normalize_date(trade_date)
    items = (payload.get("data") or {}).get("diff") or []
    if not items:
        raise SourceUnavailableError(f"clist {board_type} 返回空 diff")
    rows = []
    for item in items:
        actual = _normalize_date(item.get("f297"))
        if actual != expected:
            raise SnapshotDateError(
                f"clist 数据日 {actual} != 请求日 {expected}；拒绝以旧快照冒充当日"
            )
        rows.append(_validate_observation({
            "taxonomy": "eastmoney_dc",
            "board_code": item.get("f12"),
            "board_type": board_type,
            "name": item.get("f14") or "",
            "trade_date": actual,
            "close": item.get("f2"),
            "change_pct": item.get("f3"),
            "main_net": item.get("f62"),
            "main_pct": item.get("f184"),
            "super_net": item.get("f66"),
            "large_net": item.get("f72"),
            "mid_net": item.get("f78"),
            "small_net": item.get("f84"),
            "provider": "eastmoney_clist_snapshot",
        }))
    return rows


def fetch_clist_snapshot(
    board_type: str,
    trade_date: str,
    *,
    session: requests.Session | None = None,
    allow_intraday: bool = False,
    now: datetime | None = None,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    expected = _normalize_date(trade_date)
    current = (now or _now()).astimezone(SHANGHAI)
    if expected == current.date().isoformat() and current.time() < time(15, 30) and not allow_intraday:
        raise SnapshotDateError("当日 15:30 前禁止落盘收盘快照；可显式 --allow-intraday 调试但不应供判定")
    if board_type not in BOARD_FS:
        raise ValueError(f"board_type 须为 {list(BOARD_FS)}")
    params = {
        "pn": "1", "pz": str(page_size), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f62", "fs": BOARD_FS[board_type],
        "fields": "f2,f3,f12,f14,f62,f66,f72,f78,f84,f124,f184,f297",
    }
    client = session or _new_session()
    try:
        all_items = []
        reported_total = None
        page = 1
        while reported_total is None or len(all_items) < reported_total:
            params["pn"] = str(page)
            response = client.get(
                CLIST_URL, params=params, headers={"User-Agent": UA}, timeout=20
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") or {}
            items = data.get("diff") or []
            if reported_total is None:
                raw_total = data.get("total")
                if raw_total is None:
                    raise SourceUnavailableError("clist 响应缺少 total，无法验证分页完整性")
                reported_total = int(raw_total)
            if not items:
                break
            all_items.extend(items)
            page += 1
        if reported_total is None or len(all_items) != reported_total:
            raise SourceUnavailableError(
                f"clist {board_type} 分页不完整: {len(all_items)}/{reported_total}"
            )
        unique_codes = {normalize_board_code(item.get("f12") or "") for item in all_items}
        if len(unique_codes) != reported_total:
            raise SourceUnavailableError(
                f"clist {board_type} 代码去重后不完整: {len(unique_codes)}/{reported_total}"
            )
        return parse_clist_snapshot(
            {"data": {"diff": all_items}}, board_type, expected
        )
    except BoardFlowError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(
            f"clist {board_type} 快照失败: {type(exc).__name__}: {exc}"
        ) from exc


def sync_clist_snapshot(
    db_path: str | Path,
    trade_date: str,
    *,
    board_types: Iterable[str] = ("industry", "concept", "region"),
    allow_intraday: bool = False,
) -> dict[str, int]:
    session = _new_session()
    fetched: dict[str, list[dict[str, Any]]] = {}
    for board_type in board_types:
        fetched[board_type] = fetch_clist_snapshot(
            board_type, trade_date, session=session, allow_intraday=allow_intraday
        )
    all_rows = [row for rows in fetched.values() for row in rows]
    store_observations(db_path, all_rows)
    return {board_type: len(rows) for board_type, rows in fetched.items()}


def board_fund_flow_daily(
    board_code: str,
    lmt: int = 120,
    *,
    db_path: str | Path = None,
    provider: str = "auto",
    start_date: str | None = None,
    end_date: str | None = None,
    force_probe: bool = False,
) -> list[dict[str, Any]]:
    """缓存优先；远端仅补洞。请求 N 行却不足 N 行时显式失败。"""
    if lmt <= 0:
        raise ValueError("lmt 必须大于 0")
    path = init_db(db_path or default_db_path())
    code = normalize_board_code(board_code)
    errors = []
    expected_end = _normalize_date(end_date) if end_date else None

    def complete(rows: list[dict[str, Any]]) -> bool:
        if len(rows) < lmt:
            return False
        return not expected_end or rows[-1]["trade_date"] == expected_end

    def shortage(rows: list[dict[str, Any]]) -> str:
        detail = f"{len(rows)}/{lmt} 行"
        if expected_end and rows:
            detail += f"，末日 {rows[-1]['trade_date']} != 要求 {expected_end}"
        elif expected_end:
            detail += f"，缺少要求末日 {expected_end}"
        return detail

    cached = load_history(
        path, code, limit=lmt, start_date=start_date, end_date=end_date
    )
    if provider == "cache":
        if complete(cached):
            return cached
        raise InsufficientHistoryError(f"{code} 缓存覆盖不足：{shortage(cached)}")
    if provider == "auto" and complete(cached):
        return cached

    if provider in ("auto", "tushare"):
        token = os.environ.get("TUSHARE_TOKEN", "")
        try:
            rows = fetch_tushare_history(
                code, token, start_date=start_date, end_date=end_date
            )
            store_observations(path, rows)
        except SourceUnavailableError as exc:
            errors.append(str(exc))
            if provider == "tushare":
                raise

    refreshed = load_history(
        path, code, limit=lmt, start_date=start_date, end_date=end_date
    )
    if complete(refreshed) and provider != "push2his":
        return refreshed

    if provider in ("auto", "push2his"):
        try:
            rows = fetch_push2his_history(code, path, limit=lmt, force=force_probe)
            store_observations(path, rows)
        except SourceUnavailableError as exc:
            errors.append(str(exc))
            if provider == "push2his":
                raise

    final = load_history(
        path, code, limit=lmt, start_date=start_date, end_date=end_date
    )
    if not complete(final):
        detail = " | ".join(errors) if errors else "无可用补洞源"
        raise InsufficientHistoryError(
            f"{code} 覆盖不足（{shortage(final)}）；本项未验证。{detail}"
        )
    return final


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="收盘后把全板块当日值落入 SQLite")
    snapshot.add_argument("--trade-date", default=_now().date().isoformat())
    snapshot.add_argument("--types", nargs="+", choices=BOARD_FS, default=list(BOARD_FS))
    snapshot.add_argument("--allow-intraday", action="store_true")

    history = sub.add_parser("history", help="读取缓存，必要时用 Tushare/push2his 补洞")
    history.add_argument("board_code")
    history.add_argument("--limit", type=int, default=120)
    history.add_argument("--provider", choices=("auto", "cache", "tushare", "push2his"), default="auto")
    history.add_argument("--start-date")
    history.add_argument("--end-date")
    history.add_argument("--force-probe", action="store_true")

    audit = sub.add_parser("audit-tushare", help="只读对账 Tushare 与既有锚点，不落库")
    audit.add_argument(
        "--anchors", nargs="+", required=True,
        help="审计/回填锚点板块代码，必填；本工具不预置默认板块",
    )
    audit.add_argument("--start-date", required=True)
    audit.add_argument("--end-date", required=True)
    audit.add_argument("--minimum-overlap", type=int, default=115)
    audit.add_argument("--api-url")

    backfill = sub.add_parser("backfill-tushare", help="对账通过后按交易日补概念/地域历史")
    backfill.add_argument("--start-date", required=True)
    backfill.add_argument("--end-date", required=True)
    backfill.add_argument("--types", nargs="+", choices=BOARD_FS,
                          default=["concept", "region"])
    backfill.add_argument(
        "--anchors", nargs="+", required=True,
        help="审计/回填锚点板块代码，必填；本工具不预置默认板块",
    )
    backfill.add_argument("--minimum-overlap", type=int, default=115)
    backfill.add_argument("--minimum-interval", type=float, default=0.25)
    backfill.add_argument("--api-url")
    backfill.add_argument("--audit-start-date",
                          help="Gate 窗口起点，默认与 --start-date 相同；"
                               "回填窗口早于本地库时必须另给有重叠的区间")
    backfill.add_argument("--audit-end-date", help="Gate 窗口终点，默认同 --end-date")
    backfill.add_argument("--skip-incomplete-dates", action="store_true",
                          help="源端当日截面缺板块类型时，记录该日并继续，"
                               "而不是中断整轮；残缺日一行都不落盘")
    backfill.add_argument("--calendar", choices=("local", "anchors"), default="local",
                          help="交易日来源：local=本地库已有交易日（默认）；"
                               "anchors=锚点板块的远端逐日史")

    importer = sub.add_parser("import-jsonl", help="把既有 push2his 全板块 JSONL 导入缓存")
    importer.add_argument("source", type=Path)

    status = sub.add_parser("status", help="查看缓存与熔断状态")
    status.add_argument("--board-code")

    args = parser.parse_args()
    init_db(args.db)
    return_code = 0
    try:
        if args.command == "snapshot":
            _print_json({
                "db": str(args.db),
                "trade_date": _normalize_date(args.trade_date),
                "stored": sync_clist_snapshot(
                    args.db, args.trade_date, board_types=args.types,
                    allow_intraday=args.allow_intraday,
                ),
            })
        elif args.command == "history":
            _print_json(board_fund_flow_daily(
                args.board_code, args.limit, db_path=args.db, provider=args.provider,
                start_date=args.start_date, end_date=args.end_date,
                force_probe=args.force_probe,
            ))
        elif args.command == "audit-tushare":
            result = audit_tushare_anchors(
                args.db, os.environ.get("TUSHARE_TOKEN", ""),
                anchors=args.anchors, start_date=args.start_date,
                end_date=args.end_date, minimum_overlap=args.minimum_overlap,
                api_url=args.api_url,
            )
            _print_json(result)
            return_code = 0 if result["passed"] else 2
        elif args.command == "backfill-tushare":
            def report_progress(done, total, current):
                print(
                    f"[{done}/{total}] 已写 {current['stored_rows']} 行，"
                    f"检查点跳过 {current['checkpoint_skipped']} 日",
                    file=sys.stderr, flush=True,
                )

            _print_json(backfill_tushare_history(
                args.db, os.environ.get("TUSHARE_TOKEN", ""),
                start_date=args.start_date, end_date=args.end_date,
                board_types=args.types, anchors=args.anchors,
                minimum_overlap=args.minimum_overlap,
                minimum_interval=args.minimum_interval, api_url=args.api_url,
                progress=report_progress,
                audit_start_date=args.audit_start_date,
                audit_end_date=args.audit_end_date,
                calendar=args.calendar,
                skip_incomplete=args.skip_incomplete_dates,
            ))
        elif args.command == "import-jsonl":
            _print_json({"db": str(args.db), **import_push2his_jsonl(args.db, args.source)})
        else:
            result: dict[str, Any] = {
                "db": str(args.db),
                "push2his_circuit": get_circuit_state(args.db, PUSH2HIS_ENDPOINT),
            }
            if args.board_code:
                result["history"] = load_history(args.db, args.board_code, limit=10)
            _print_json(result)
    except (BoardFlowError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
