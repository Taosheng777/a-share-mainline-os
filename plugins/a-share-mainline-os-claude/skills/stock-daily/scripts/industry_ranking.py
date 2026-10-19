"""东财行业全量排名：当日已验证完整快照优先；缺缓存时串行分页。

缓存只提供价格排名，不伪造上涨/下跌家数或领涨股；禁止换分类或取旧日顶替。
"""
import hashlib
import math
import random
import time
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


def _day(value):
    text = str(value)
    return datetime.strptime(text, '%Y%m%d' if len(text) == 8 else '%Y-%m-%d').date().isoformat()


def _digest(codes):
    return hashlib.sha256('\n'.join(sorted(codes)).encode()).hexdigest()


def record_snapshot_coverage(db_path, board_type, trade_date, codes, provenance):
    """仅供已通过全量分页检查的同步器调用；不改变任何行情数值。"""
    day = _day(trade_date)
    expected = set(codes)
    if not expected or len(expected) != len(codes):
        raise ValueError('完整快照须非空且代码唯一')
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError('待登记的快照数据库不存在')
    with closing(sqlite3.connect(path)) as con, con:
        got = {r[0] for r in con.execute(
            "SELECT board_code FROM observations WHERE taxonomy='eastmoney_dc' "
            "AND board_type=? AND trade_date=? AND provider='eastmoney_clist_snapshot'",
            (board_type, day))}
        if got != expected:
            raise RuntimeError('落盘代码与已验完整截面不一致，拒绝登记覆盖')
        con.execute('CREATE TABLE IF NOT EXISTS snapshot_coverage ('
                    'board_type TEXT, trade_date TEXT, row_count INTEGER, codes_sha256 TEXT, '
                    'provenance TEXT, verified_at TEXT, PRIMARY KEY(board_type, trade_date))')
        con.execute('INSERT OR REPLACE INTO snapshot_coverage VALUES (?,?,?,?,?,?)',
                    (board_type, day, len(codes), _digest(codes), provenance,
                     datetime.now(SHANGHAI).isoformat()))


def _cache_rows(db_path, day):
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError('本地板块缓存不存在')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        try:
            coverage = con.execute('SELECT * FROM snapshot_coverage WHERE board_type=? AND trade_date=?',
                                   ('industry', day)).fetchone()
        except sqlite3.OperationalError as exc:
            raise RuntimeError('旧缓存无完整截面凭证，不能推断完整') from exc
        if not coverage:
            raise RuntimeError(f'{day} 无完整行业截面凭证')
        rows = [dict(r) for r in con.execute(
            "SELECT board_code AS code,name,change_pct,trade_date FROM observations "
            "WHERE taxonomy='eastmoney_dc' AND board_type='industry' AND trade_date=? "
            "AND provider='eastmoney_clist_snapshot'", (day,))]
    if len(rows) != coverage['row_count'] or _digest([r['code'] for r in rows]) != coverage['codes_sha256']:
        raise RuntimeError('行业缓存覆盖与完整截面凭证不一致')
    for row in rows:
        row.update(up_count=None, down_count=None, leader=None, leader_change=None)
    return rows, {'mode': 'cache', 'source': '本地 SQLite（东财行业完整快照）',
                  'source_url': 'https://push2delay.eastmoney.com/api/qt/clist/get',
                  'coverage_provenance': coverage['provenance'],
                  'missing_fields': ['up_count', 'down_count', 'leader', 'leader_change']}


def _number(value):
    if isinstance(value, bool):
        raise RuntimeError('涨跌幅返回布尔值')
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError('涨跌幅缺失，不能完成全市场排名') from exc
    if not math.isfinite(value):
        raise RuntimeError('涨跌幅不是有限数值')
    return value


def _live_rows(day, get):
    # 按代码稳定分页，避免按实时涨幅翻页发生名次移动；排名在全量获取后计算。
    url = 'https://push2.eastmoney.com/api/qt/clist/get'
    params = dict(pn=1, pz=100, po=1, np=1, fltt=2, invt=2, fid='f12', fs='m:90+t:2',
                  fields='f3,f12,f14,f104,f105,f136,f140,f297')
    rows, total, codes, source_urls = [], None, set(), []
    while total is None or len(rows) < total:
        response = get(url, params=dict(params), timeout=20)
        response.raise_for_status()
        actual_url = response.url if isinstance(response.url, str) else url
        if actual_url not in source_urls:
            source_urls.append(actual_url)
        payload = response.json()
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError('行业排名返回空/畸形数据，不能解释为没有行业')
        current = data.get('total')
        if isinstance(current, bool) or not isinstance(current, int) or not 0 < current <= 10000:
            raise RuntimeError('行业排名缺少有效总条数')
        if total is not None and current != total:
            raise RuntimeError('分页期间行业总数变化，拒绝部分排名')
        total = current
        batch = data.get('diff')
        if isinstance(batch, dict):
            batch = list(batch.values())
        if not isinstance(batch, list) or not batch:
            raise RuntimeError('行业分页中断/空页')
        if len(batch) > 100 or len(rows) + len(batch) > total:
            raise RuntimeError('行业分页数量超出总数')
        if len(rows) + len(batch) < total and len(batch) != 100:
            raise RuntimeError('行业非末页不足100条')
        for item in batch:
            if not isinstance(item, dict):
                raise RuntimeError('行业行格式异常')
            code = item.get('f12')
            if not isinstance(code, str) or not code.startswith('BK') or not code[2:].isdigit() or code in codes:
                raise RuntimeError('行业代码缺失/畸形/重复')
            codes.add(code)
            if _day(item.get('f297')) != day:
                raise RuntimeError('行业快照日期不符，拒绝旧日替代')
            rows.append(dict(code=code, name=item.get('f14'), change_pct=_number(item.get('f3')),
                             trade_date=day, up_count=item.get('f104'), down_count=item.get('f105'),
                             leader=item.get('f140'), leader_change=item.get('f136')))
        params['pn'] += 1
    missing = [key for key in ('up_count', 'down_count', 'leader', 'leader_change')
               if any(row.get(key) in (None, '', '-') for row in rows)]
    return rows, {'mode': 'live', 'source': '东财行业完整分页', 'source_url': source_urls[0],
                  'source_urls': source_urls,
                  'missing_fields': missing}


def industry_comparison(top_n=20, trade_date=None, *, db_path=None, prefer_cache=True, get=None):
    """保留 top/bottom/total，补充 rows、来源和精确数据日。默认当日缓存优先。

    非交易日/回看历史请显式给 trade_date。不含缓存缺失的宽度/领涨字段，不混用 THS 分类。
    """
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError('top_n 须为正整数')
    day = _day(trade_date or datetime.now(SHANGHAI).date().isoformat())
    if day > datetime.now(SHANGHAI).date().isoformat():
        raise ValueError('禁止未来数据日')
    if db_path is None:
        from board_fund_flow_cache import default_db_path
        db_path = default_db_path()
    errors = []
    rows = meta = None
    if prefer_cache:
        try:
            rows, meta = _cache_rows(db_path, day)
        except (RuntimeError, sqlite3.Error) as exc:
            errors.append(f'缓存：{exc}')
    if rows is None:
        if get is None:
            from board_fund_flow_cache import _new_session
            session = _new_session()
            last_request = None

            def get(url, **kwargs):
                nonlocal last_request
                if last_request is not None:
                    time.sleep(max(0, 1.0 + random.uniform(0, 0.3) - (time.monotonic() - last_request)))
                last_request = time.monotonic()
                return session.get(url, **kwargs)
        try:
            rows, meta = _live_rows(day, get)
        except Exception as exc:
            errors.append(f'在线：{type(exc).__name__}: {exc}')
            if not prefer_cache:
                try:
                    rows, meta = _cache_rows(db_path, day)
                except (RuntimeError, sqlite3.Error) as cache_exc:
                    errors.append(f'缓存：{cache_exc}')
            if rows is None:
                raise RuntimeError('行业全量排名未取得；' + '；'.join(errors)) from exc
    for row in rows:
        if not isinstance(row.get('name'), str) or not row['name'].strip():
            raise RuntimeError('行业名称缺失')
        row['change_pct'] = _number(row['change_pct'])
    rows.sort(key=lambda r: (-r['change_pct'], r['code']))
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return {**meta, 'trade_date': day, 'total': len(rows), 'top': rows[:top_n],
            'bottom': rows[-top_n:], 'rows': rows, 'complete': True, 'diagnostics': errors}
