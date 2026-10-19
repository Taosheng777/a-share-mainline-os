#!/usr/bin/env python3
"""沪深A股含ST的独立宽度校验；完整分页、精确日期、行情源封板价。"""
import argparse
from datetime import datetime
import json
import math
import re
import random
import time
from zoneinfo import ZoneInfo

from board_fund_flow_cache import _new_session

ZONE = ZoneInfo('Asia/Shanghai')
BREADTH_FS = 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
BREADTH_URL = 'https://push2delay.eastmoney.com/api/qt/clist/get'


def _day(value):
    return datetime.strptime(str(value), '%Y%m%d' if len(str(value)) == 8 else '%Y-%m-%d').date().isoformat()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _tencent_limit_prices(codes, trade_date, get):
    out, errors = {}, []
    for start in range(0, len(codes), 80):
        batch = codes[start:start + 80]
        symbols = [('sh' if c.startswith(('6', '9')) else 'sz') + c for c in batch]
        try:
            response = get('https://qt.gtimg.cn/q=' + ','.join(symbols), timeout=15)
            response.raise_for_status()
            raw = response.content.decode('gbk')
        except Exception as exc:
            errors.append('腾讯封板价批次未取得：' + type(exc).__name__)
            continue
        for line in raw.split(';'):
            if '"' not in line:
                continue
            fields = line.split('"')[1].split('~')
            if len(fields) <= 48 or fields[2] not in batch:
                continue
            try:
                if _day(fields[30][:8]) != trade_date:
                    continue
                upper, lower = float(fields[47]), float(fields[48])
                if not math.isfinite(upper) or not math.isfinite(lower):
                    continue
            except (ValueError, TypeError):
                continue
            if upper <= 0 and lower <= 0:
                out[fields[2]] = (0, 0)
            elif upper > 0 and lower > 0:
                out[fields[2]] = (upper, lower)
    return out, errors


def market_breadth(verbose=False, *, trade_date=None, get=None):
    """每天至多一次；来源故障拒绝残页，不以名称猜ST涨跌幅限制。"""
    today = datetime.now(ZONE).date().isoformat()
    day = _day(trade_date or today)
    if day > today:
        raise ValueError('禁止未来行情数据日')
    if get is None:
        session = _new_session()
        last = None

        def get(url, **kwargs):
            nonlocal last
            if last is not None:
                time.sleep(max(0, 1.0 + random.uniform(0, 0.3) - (time.monotonic() - last)))
            last = time.monotonic()
            return session.get(url, **kwargs)

    rows, codes, total, page = [], set(), None, 1
    while total is None or len(rows) < total:
        response = get(BREADTH_URL, params={
            'pn': page, 'pz': 100, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2,
            'fid': 'f12', 'fs': BREADTH_FS, 'fields': 'f12,f14,f3,f2,f18,f297',
        }, headers={'Referer': 'https://quote.eastmoney.com/'}, timeout=25)
        response.raise_for_status()
        body = response.json()
        data = body.get('data') if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise ValueError('市场宽度返回空/畸形数据，不能解释为零家数')
        count = data.get('total')
        if type(count) is not int or not 0 < count <= 12000 or (total is not None and count != total):
            raise ValueError('市场宽度总数缺失或分页期间变化')
        total = count
        batch = data.get('diff')
        if isinstance(batch, dict):
            batch = list(batch.values())
        if not isinstance(batch, list) or not batch or len(batch) > 100 or len(rows) + len(batch) > total:
            raise ValueError('市场宽度分页不完整')
        if len(rows) + len(batch) < total and len(batch) != 100:
            raise ValueError('市场宽度非末页不足100条')
        for row in batch:
            code = row.get('f12') if isinstance(row, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r'[036][0-9]{5}', code) or code in codes:
                raise ValueError('市场宽度证券代码异常或重复')
            if _day(row.get('f297')) != day:
                raise ValueError('市场宽度源数据日不符')
            codes.add(code)
            rows.append(row)
        page += 1

    quoted = [r['f12'] for r in rows if _number(r.get('f2')) and r['f2'] > 0]
    limits, diagnostics = _tencent_limit_prices(quoted, day, get)
    quoted = set(quoted)
    up = down = flat = no_quote = 0
    rises, falls, missing, unlimited = [], [], [], []
    for row in rows:
        change = row.get('f3')
        if not _number(change):
            no_quote += 1
        elif change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
        code, name = row['f12'], str(row.get('f14') or '')
        if code not in quoted:
            if _number(change):
                missing.append((code, name))
            continue
        if code not in limits:
            missing.append((code, name))
            continue
        upper, lower = limits[code]
        if upper == lower == 0:
            unlimited.append((code, name))
        elif abs(row['f2'] - upper) < 0.005:
            rises.append((code, name, change))
        elif abs(row['f2'] - lower) < 0.005:
            falls.append((code, name, change))
    result = {
        'trade_date': day, 'complete': True, 'source': '东财完整沪深A股分页 + 腾讯当日封板价',
        'total': len(rows), 'up': up, 'down': down, 'flat': flat, 'no_quote': no_quote,
        'ratio': round(up / down, 3) if down else None,
        'limit_up': len(rises), 'limit_down': len(falls),
        'limit_up_st': sum('ST' in r[1].upper().replace(' ', '') for r in rises),
        'limit_price_missing': len(missing), 'limit_price_unlimited': len(unlimited),
        'detail': {'limit_up': rises, 'limit_down': falls, 'limit_price_missing': missing,
                   'limit_price_unlimited': unlimited}, 'diagnostics': diagnostics,
    }
    if verbose:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result


def breadth_reconcile(wencai_up, wencai_down, wencai_zt, wencai_dt, shadow):
    values = (wencai_up, wencai_down, wencai_zt, wencai_dt)
    if any(type(v) is not int or v < 0 for v in values):
        raise ValueError('问财家数必须是非负整数')
    delta = [shadow[k] - v for k, v in zip(('up', 'down', 'limit_up', 'limit_down'), values)]
    missing = shadow.get('limit_price_missing', 0)
    ok = shadow.get('complete') is True and missing == 0 and delta[:3] == [0, 0, 0] and abs(delta[3]) <= 1
    return {'ok': ok, 'd_up': delta[0], 'd_down': delta[1], 'd_zt': delta[2], 'd_dt': delta[3],
            'st_limit_up': shadow['limit_up_st'], 'limit_price_missing': missing,
            'note': '一致' if ok else '口径异常：需核对，不得静默写入'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', help='精确数据日 YYYY-MM-DD；接口不给历史时拒绝旧日冒充')
    args = parser.parse_args()
    print(json.dumps(market_breadth(trade_date=args.date), ensure_ascii=False, indent=2, allow_nan=False))
