# SENTINEL_SCREEN_C1
"""stock-screener 核心逻辑（stdlib-only）。MVP: 清洗 → rank百分位归一 → 加权 → TopN。
纯逻辑（parse/score/dedup/render）+ 数据访问（fetch 翻页/load_holdings）+ CLI 编排。"""

import json
import os
import re
import subprocess
import sys


# ---- 配置层（开源改造 2026-08-13）----
# 本文件不再内嵌任何绝对路径。vault 位置由用户配置提供，Claude 与 Codex 共用同一份：
#   1. 环境变量 ASM_CONFIG 指定的 JSON 文件
#   2. ~/.config/a-share-mainline/config.json
#   3. 环境变量 ASM_VAULT 可单独覆盖 vault_root
# 三者都没有 → fail closed，报明确错误与配置指引，绝不猜路径、不静默 fallback。

CONFIG_ENV = "ASM_CONFIG"
VAULT_ENV = "ASM_VAULT"
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/a-share-mainline/config.json")

# screen.py 位于 <skills_root>/stock-screener/scripts/screen.py，
# 由此反推 skills 根目录，可选依赖按相对位置自动发现（~/.claude 与 ~/.codex 两侧同样有效）。
SKILLS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

_CONFIG_CACHE = None


def compliance_notice(sources, data_date):
    """生成用户可见的统一运行时合规声明。"""
    source_text = str(sources or "未取得（运行时未报告）")
    date_text = str(data_date or "未取得")
    return (
        "本工具仅用于研究与决策支持；提供方非持牌证券投资咨询机构，"
        "本工具及其输出不构成投资建议，不代下单，使用者风险自担。"
        "数据来源：%s；数据日：%s，均以本次运行输出为准。"
        % (source_text, date_text)
    )

CONFIG_HINT = (
    "未找到 vault_root 配置。请任选其一：\n"
    "  1. 创建 %s，内容形如 {\"vault_root\": \"/path/to/your/vault\"}\n"
    "  2. 设环境变量 %s=/path/to/your/config.json 指向别处的配置\n"
    "  3. 设环境变量 %s=/path/to/your/vault 直接覆盖\n"
    "仓库根目录的 config.example.json 是可直接复制的模板。"
)


def reset_config_cache():
    """清空配置缓存。测试与配置变更后调用。"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def config_path():
    """返回本次使用的配置文件路径；未显式指定时用默认位置。"""
    return os.path.expanduser(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)


def load_config():
    """读配置文件。不存在时返回空 dict（留给取值处 fail closed）；
    但 ASM_CONFIG 显式指向的文件不存在、或文件存在却解析失败，一律立即报错——
    静默当空会让用户以为配置生效了，是最难查的一类故障。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    path = config_path()
    explicit = bool(os.environ.get(CONFIG_ENV))
    if not os.path.exists(path):
        if explicit:
            raise ScreenError("%s 指向的配置文件不存在：%s" % (CONFIG_ENV, path))
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise ScreenError("配置文件解析失败：%s（%s）" % (path, e))
    if not isinstance(doc, dict):
        raise ScreenError("配置文件解析失败：%s 顶层必须是对象" % path)
    _CONFIG_CACHE = doc
    return _CONFIG_CACHE


def _cfg(key, default=None):
    val = load_config().get(key)
    return os.path.expanduser(val) if isinstance(val, str) and val else default


def vault_root():
    """投资笔记 vault 根目录。未配置即 fail closed。"""
    env = os.environ.get(VAULT_ENV)
    if env:
        return os.path.expanduser(env)
    val = _cfg("vault_root")
    if not val:
        raise ScreenError(
            CONFIG_HINT % (DEFAULT_CONFIG_PATH, CONFIG_ENV, VAULT_ENV)
        )
    return val


def holdings_path():
    """极简持仓卡。默认 <vault>/03-持仓跟踪/持仓.md，可用 holdings_path 覆盖。"""
    return _cfg("holdings_path") or os.path.join(
        vault_root(), "03-持仓跟踪", "持仓.md")


def out_dir():
    """筛选产物落地目录。v3 的载体清单写回主线页，本目录只留给导出件。"""
    return _cfg("screener_out_dir") or os.path.join(
        vault_root(), "outputs", "screener")


def cli_path():
    """问财 CLI（可选依赖）。默认按 skill 目录相对位置自动发现，两侧运行时通用。"""
    return _cfg("wencai_cli") or os.path.join(
        SKILLS_ROOT, "hithink-market-query", "scripts", "cli.py")


def ifind_helper_path():
    """iFinD 本地证据 helper（可选适配器）。未配置返回 None，由调用方降级。"""
    return _cfg("ifind_evidence_helper")

FIELD_NAME_ALIASES = {
    "归属于母公司所有者的净利润同比增长率": "归母净利润同比增长率",
    "归属母公司股东的净利润(同比增长率)": "归母净利润同比增长率",
    "加权净资产收益率": "净资产收益率",
    "净资产收益率roe(加权,公布值)": "净资产收益率",
}


class ScreenError(Exception):
    """取数/编排阶段的可预期错误（额度不足、cli.py 非 JSON 输出等）。"""


def parse_value(v):
    """问财字段值 → float。裸数字直接用；'万/亿' 换算；'%' 保留量级；无法解析 → None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace(",", "").replace("，", "")
    if not s:
        return None
    mult = 1.0
    if s.endswith("%"):
        s = s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    elif s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def normalize_field_name(key):
    """剥掉问财字段的 '@' 前缀、'[日期]' 后缀、':子标记'，得到标准字段名。"""
    if not isinstance(key, str):
        return key
    k = key.strip()
    if k.startswith("@"):
        k = k[1:]
    i = k.find("[")
    if i != -1:
        k = k[:i]
    k = k.split(":")[0].strip()
    return FIELD_NAME_ALIASES.get(k, k)


_MARKET_DATE_FIELD_MARKERS = (
    "涨跌", "换手", "成交", "振幅", "资金流",
    "流通市值", "总市值", "放量", "最新价", "现价",
)
_FIELD_DATE_RE = re.compile(r"\[(\d{8})\]")


def infer_market_data_date(pool):
    """从问财原始行情字段日期推断数据日；财务报告期与非法日期不参与。"""
    import datetime

    counts = {}
    for record in pool or []:
        if not isinstance(record, dict):
            continue
        for key in record:
            if not isinstance(key, str):
                continue
            field = normalize_field_name(key)
            if not any(marker in field for marker in _MARKET_DATE_FIELD_MARKERS):
                continue
            for raw_date in _FIELD_DATE_RE.findall(key):
                try:
                    data_date = datetime.datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
                except ValueError:
                    continue
                counts[data_date] = counts.get(data_date, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda data_date: (counts[data_date], data_date))


def pick_value(record, stdname):
    """在同名多变体（@版/无@版/带子标记）里取第一个非空数值。"""
    for k, v in record.items():
        if normalize_field_name(k) == stdname:
            pv = parse_value(v)
            if pv is not None:
                return pv
    return None


def rank_percentile(values):
    """截面排序百分位 → [0,1]；None 保持 None；并列取平均；单值记 0.5（中性）。"""
    idx = [i for i, v in enumerate(values) if v is not None]
    out = [None] * len(values)
    n = len(idx)
    if n == 0:
        return out
    if n == 1:
        out[idx[0]] = 0.5
        return out
    order = sorted(idx, key=lambda i: values[i])
    j = 0
    while j < n:
        k = j
        while k + 1 < n and values[order[k + 1]] == values[order[j]]:
            k += 1
        pct = ((j + k) / 2.0) / (n - 1)
        for t in range(j, k + 1):
            out[order[t]] = pct
        j = k + 1
    return out


def score(pool, fields):
    """fields: [(stdname, weight, direction)]，direction ∈ {'higher','lower'}。
    返回 [{'record','score','parts'}]；某只缺某字段→按已用权重重归一；整列缺失自然不计。"""
    cols = {}
    for stdname, weight, direction in fields:
        raw = [pick_value(rec, stdname) for rec in pool]
        pct = rank_percentile(raw)
        if direction == "lower":
            pct = [(1.0 - p) if p is not None else None for p in pct]
        cols[stdname] = pct
    results = []
    for i, rec in enumerate(pool):
        wsum = 0.0
        applied = 0.0
        parts = {}
        for stdname, weight, direction in fields:
            p = cols[stdname][i]
            parts[stdname] = p
            if p is not None:
                wsum += p * weight
                applied += weight
        sval = (wsum / applied * 100.0) if applied > 0 else None
        results.append({"record": rec, "score": sval, "parts": parts})
    return results


def _code6(c):
    """取 6 位代码前缀用于匹配：'002415.SZ' → '002415'。"""
    if not isinstance(c, str):
        return None
    return c.split(".")[0].strip() or None


# 问财按标的类型下发两套代码/名称列（见 stock-buddy/references/parsing.md §1）：
# 股票 = 股票代码/股票简称；基金 ETF = 基金代码/基金简称/基金扩位简称。
# 全链路（去重 / 排除持仓 / 行情 enrich / 渲染 / 投影）一律经 record_code、record_name 取值，
# 不得再写死单列——写死 '股票代码' 会让 ETF 池整条链路拿到 None（2026-08-05 实测：
# 3 只 ETF 去重后只剩 1 只、已持仓 ETF 永远排不掉、名单代码名称列全空）。
CODE_KEYS = ("股票代码", "基金代码", "证券代码", "代码")
NAME_KEYS = ("股票简称", "基金简称", "基金扩位简称", "证券简称", "简称", "名称")


def _pick_labeled(record, keys):
    """按 keys 的优先级取第一个非空字符串值；键名先 normalize（容忍 '@' 前缀 / '[日期]' 后缀）。"""
    if not isinstance(record, dict):
        return None
    for want in keys:
        for k, v in record.items():
            if normalize_field_name(k) == want and isinstance(v, str) and v.strip():
                return v.strip()
    return None


def record_code(record, code_key=None):
    """取记录代码原值（可能带 .SZ/.SH 后缀），兼容股票与 ETF 两套列名。
    code_key 显式给定时只认这一列。取不到 → None（调用方必须当"未知"处理，
    不得把 None 当成一个可比较的代码值）。"""
    return _pick_labeled(record, (code_key,) if code_key else CODE_KEYS)


def record_name(record, name_key=None):
    """取记录简称，兼容股票简称 / 基金简称 / 基金扩位简称。取不到 → None。"""
    return _pick_labeled(record, (name_key,) if name_key else NAME_KEYS)


def dedup(pool, code_key=None):
    """按 6 位代码去重，保留首次出现；兼容股票与 ETF 两套代码列。
    取不到代码的记录一律保留：None 不是可比较的代码，一旦塞进 seen，后面所有
    「无代码」记录都会被当成彼此的重复静默吞掉。去重只删已证明重复的，
    宁可多留一条待人工判断，也不静默丢候选。"""
    seen = set()
    out = []
    for rec in pool:
        c = _code6(record_code(rec, code_key))
        if c is not None:
            if c in seen:
                continue
            seen.add(c)
        out.append(rec)
    return out


def exclude_holdings(pool, holding_codes, code_key=None):
    """剔除已持仓（6 位代码匹配，兼容带/不带交易所后缀，兼容股票与 ETF 两套代码列）。
    取不到代码的记录保留：无法证明它是持仓，剔除等于静默丢候选；留着还能被人眼发现。"""
    hold = {_code6(c) for c in (holding_codes or [])}
    hold.discard(None)
    out = []
    for rec in pool:
        c = _code6(record_code(rec, code_key))
        if c is not None and c in hold:
            continue
        out.append(rec)
    return out


def render_html(results, meta):
    """渲染可排序 HTML 名单。results: [{'record','score','parts'}]；外部值一律 escape。"""
    import html as H

    title = H.escape(str(meta.get("title", "选股名单")))
    report_label = H.escape(str(meta.get("report_label", "")))
    notice = H.escape(compliance_notice(meta.get("sources"), meta.get("data_date")))
    ordered = sorted(
        results,
        key=lambda r: (r.get("score") is not None, r.get("score") or 0.0),
        reverse=True,
    )
    rows = []
    for rank, item in enumerate(ordered, 1):
        rec = item.get("record", {})
        code = H.escape(record_code(rec) or "")
        name = H.escape(record_name(rec) or "")
        sc = item.get("score")
        sc_txt = "-" if sc is None else "%.1f" % sc
        tmpl = H.escape(str(rec.get("命中模板", meta.get("title", ""))))
        parts = item.get("parts", {}) or {}
        parts_txt = H.escape(
            " ".join("%s=%.2f" % (k, v) for k, v in parts.items() if v is not None)
        )
        rows.append(
            "<tr><td>%d</td><td>%s</td><td>%s</td><td class='score'>%s</td>"
            "<td>%s</td><td class='parts'>%s</td></tr>"
            % (rank, code, name, sc_txt, tmpl, parts_txt)
        )
    rows_html = "\n".join(rows)
    return """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>%s · 选股名单 · 报告标签：%s</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:24px;color:#1a1a1a}
h1{font-size:20px}
table{border-collapse:collapse;width:100%%;font-size:14px}
th,td{border:1px solid #ddd;padding:6px 10px;text-align:left}
th{background:#f5f5f5;cursor:pointer}
.score{font-weight:600;color:#c0392b}
.parts{color:#666;font-size:12px}
footer{margin-top:16px;color:#888;font-size:12px}
</style></head><body>
<h1>%s · 选股名单 <small>报告标签：%s</small></h1>
<table id="t">
<thead><tr><th>#</th><th>代码</th><th>名称</th><th>综合分</th><th>命中模板</th><th>分项贡献(rank)</th></tr></thead>
<tbody>
%s
</tbody></table>
<footer>%s</footer>
<script>
document.querySelectorAll('#t th').forEach((th,i)=>th.addEventListener('click',()=>{
 const tb=th.closest('table').tBodies[0], rows=[...tb.rows], num=(i===0||i===3);
 rows.sort((a,b)=>{const x=a.cells[i].innerText,y=b.cells[i].innerText;
  return num?(parseFloat(y)||0)-(parseFloat(x)||0):x.localeCompare(y,'zh');});
 rows.forEach(r=>tb.appendChild(r));
}));
</script>
</body></html>""" % (title, report_label, title, report_label, rows_html, notice)


# ---- 策略模板库（命名 + 问财问句 + 打分字段权重）。字段名为 normalize 后的标准名 ----

PRESETS = {
    "放量突破": {
        "title": "放量突破",
        # 行情类展示词缀（换手率/市盈率/市净率）已移除：打分列由腾讯 enrichment 覆盖
        "query": (
            "今日放量突破20日新高 近5日涨幅小于15% 近一年归母净利润同比增长率 "
            "主力资金流向 净资产收益率 非ST 非创业板 非北交所"
        ),
        "fields": [
            ("最新涨跌幅", 0.20, "higher"),
            ("换手率", 0.15, "higher"),
            ("主力资金流向", 0.25, "higher"),
            ("归母净利润同比增长率", 0.15, "higher"),
            ("净资产收益率", 0.15, "higher"),
            ("最新市盈率ttm", 0.05, "lower"),
            ("最新市净率", 0.05, "lower"),
        ],
    },
    "低位反转": {
        "title": "低位反转",
        "query": (
            "近一年涨跌幅小于-20% 股价站上5日均线 "
            "主力资金流向 非ST 非北交所"
        ),
        "fields": [
            ("最新涨跌幅", 0.30, "higher"),
            ("换手率", 0.20, "higher"),
            ("主力资金流向", 0.30, "higher"),
            ("最新市净率", 0.20, "lower"),
        ],
    },
    "业绩成长": {
        "title": "业绩成长",
        "query": (
            "近一年归母净利润同比增长率大于30% 营业收入同比增长率 净资产收益率大于10% "
            "市盈率小于40 非ST 非北交所"
        ),
        "fields": [
            ("归母净利润同比增长率", 0.30, "higher"),
            ("营业收入同比增长率", 0.25, "higher"),
            ("净资产收益率", 0.20, "higher"),
            ("最新市盈率ttm", 0.15, "lower"),
            ("最新市净率", 0.10, "lower"),
        ],
    },
    "主力异动": {
        "title": "主力异动",
        "query": (
            "主力资金连续3日净流入 今日放量 "
            "主力资金流向 资金流入 非ST 非北交所"
        ),
        "fields": [
            ("主力资金流向", 0.40, "higher"),
            ("资金流入", 0.20, "higher"),
            ("换手率", 0.20, "higher"),
            ("最新涨跌幅", 0.20, "higher"),
        ],
    },
}

# 自由口述（无模板）时的通用打分字段：动量 + 资金 + 质量
DEFAULT_FIELDS = [
    ("最新涨跌幅", 0.25, "higher"),
    ("换手率", 0.15, "higher"),
    ("主力资金流向", 0.25, "higher"),
    ("归母净利润同比增长率", 0.15, "higher"),
    ("净资产收益率", 0.20, "higher"),
]


def get_preset(name):
    """按名取模板；未知名抛 KeyError 并列出可用模板。"""
    if name not in PRESETS:
        raise KeyError("未知模板：%s。可用：%s" % (name, "、".join(PRESETS)))
    return PRESETS[name]


# ---- 数据访问层 ----

# 6 位 A 股/ETF/北交所代码，可带 .SZ/.SH/.BJ 后缀；前后不接数字、前不接小数点（避免命中小数尾数）
_HOLDING_CODE_RE = re.compile(
    r"(?<![\d.])(\d{6})(?:\.(?:SZ|SH|BJ|sz|sh|bj))?(?!\d)"
)


def load_holdings(path):
    """只读 stock-buddy 的持仓 md，抽出 6 位代码（去重保序）。文件缺失 → []。"""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    seen = set()
    out = []
    for m in _HOLDING_CODE_RE.finditer(text):
        c = m.group(1)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _brief(doc):
    try:
        return json.dumps(doc, ensure_ascii=False)[:200]
    except (TypeError, ValueError):
        return str(doc)[:200]


def _subprocess_runner(query, page, page_limit, cli=None, timeout=45):
    """调 hithink cli.py 取一页，解析其 stdout JSON。API Key 由 cli.py 从 env 读，本层不碰。"""
    cli = cli or cli_path()
    proc = subprocess.run(
        [sys.executable, cli, "--query", query,
         "--page", str(page), "--limit", str(page_limit)],
        capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise ScreenError(
            "cli.py 未返回 JSON（rc=%d）：%s"
            % (proc.returncode, (proc.stderr or out or "<空>")[:300])
        )


def fetch_pool(query, max_records=100, page_limit=50, runner=None):
    """按 has_more 翻页抓全池子（带上限）。runner 可注入用于离线测试。
    返回 record 列表；某页缺 datas（额度不足等）→ ScreenError。"""
    run = runner or _subprocess_runner
    pool = []
    page = 1
    max_pages = max(1, (max_records + page_limit - 1) // page_limit) + 2
    while len(pool) < max_records and page <= max_pages:
        doc = run(query, page, page_limit)
        if not isinstance(doc, dict) or "datas" not in doc:
            raise ScreenError(
                "问财返回缺少 datas（可能额度不足/查询超限）：%s" % _brief(doc)
            )
        datas = doc.get("datas") or []
        pool.extend(datas)
        if not datas or not doc.get("has_more"):
            break
        page += 1
    return pool[:max_records]


# ---- 行情打分列 enrichment（腾讯批量 · T3 增强层 · 2026-07-15 加）----
# 目的：换手率/涨跌幅/PE/PB 等行情打分列不再依赖问财"词缀下发"（漏列即综合分落空），
# 改由腾讯行情批量覆盖（零额度、全池一两次请求）。腾讯失败→保留问财原值并标记，
# 打分按既有"缺列重归一"口径降级，不熔断、不编数据。资金/财务列仍走问财。

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
# 腾讯 ~ 分隔载荷字段索引 → 打分标准字段名（索引实测校准 2026-07）
TENCENT_FIELD_INDEX = {
    "最新涨跌幅": 32,
    "换手率": 38,
    "最新市盈率ttm": 39,
    "最新市净率": 46,
}
ENRICH_FIELDS = tuple(TENCENT_FIELD_INDEX)
# 腾讯对亏损股/ETF 的比率字段给 0.00 作"无值"哨兵；直接采用会把亏损股按
# 'lower' 方向排成"估值最优"，必须转 None（打分层按缺失重归一）。
TENCENT_ZERO_IS_NONE = ("最新市盈率ttm", "最新市净率")

_TENCENT_CODE_RE = re.compile(r"^(\d{6})(?:\.(SZ|SH|BJ))?$", re.IGNORECASE)


def tencent_prefix(code):
    """'002415.SZ'→'sz002415'。优先用问财返回的交易所后缀；裸码按首位判：
    6/9/5→sh（5 开头是沪市基金/ETF，判 sz 是已知坑）、8/4→bj、其余→sz。无法识别→None。"""
    if not isinstance(code, str):
        return None
    m = _TENCENT_CODE_RE.match(code.strip())
    if not m:
        return None
    six, suf = m.group(1), (m.group(2) or "").lower()
    if suf:
        return suf + six
    if six[0] in "695":
        return "sh" + six
    if six[0] in "84":
        return "bj" + six
    return "sz" + six


def parse_tencent_payload(text):
    """腾讯 GBK 载荷 → {6位代码: {标准字段名: float}}。短行/坏行跳过。"""
    out = {}
    for line in (text or "").strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code6 = vals[2].strip()
        if not code6:
            continue
        row = {}
        for std, idx in TENCENT_FIELD_INDEX.items():
            v = parse_value(vals[idx])
            if v == 0.0 and std in TENCENT_ZERO_IS_NONE:
                v = None
            row[std] = v
        out[code6] = row
    return out


def fetch_tencent_quotes(prefixed_codes, timeout=10, chunk=50):
    """批量拉腾讯行情（stdlib urllib，GBK）。prefixed_codes 为 tencent_prefix 结果。"""
    import urllib.request

    got = {}
    codes = [c for c in prefixed_codes if c]
    for i in range(0, len(codes), chunk):
        url = TENCENT_QUOTE_URL + ",".join(codes[i: i + chunk])
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            got.update(parse_tencent_payload(resp.read().decode("gbk", "replace")))
    return got


def enrich_records(pool, code_key=None, fetcher=None):
    """行情打分列改源：腾讯值覆盖问财同名列（逐列删旧变体再写标准名）。
    代码列经 record_code 取，股票/ETF 两套列名都认（ETF 的 '基金代码' 必须能命中）。
    腾讯缺某票→该票保留问财原值；整体失败→原样返回并在 info 标记（T3 增强层，降级不熔断）。
    返回 (pool, info)，info={'enriched','hit','note'}。"""
    fetch = fetcher or fetch_tencent_quotes
    prefixed = [tencent_prefix(record_code(rec, code_key)) for rec in pool]
    try:
        quotes = fetch([p for p in prefixed if p])
    except Exception as e:
        return pool, {"enriched": False, "hit": 0,
                      "note": "腾讯行情获取失败，打分退回问财返回列：%s" % e}
    hit = 0
    for rec in pool:
        row = quotes.get(_code6(record_code(rec, code_key)))
        if not row:
            continue
        hit += 1
        for std in ENRICH_FIELDS:
            v = row.get(std)
            if v is None:
                continue
            for k in [k for k in list(rec) if normalize_field_name(k) == std]:
                del rec[k]
            rec[std] = v
    return pool, {"enriched": True, "hit": hit,
                  "note": "行情打分列（%s）由腾讯批量覆盖 %d/%d 只"
                          % ("/".join(ENRICH_FIELDS), hit, len(pool))}


# ---- 编排：dedup → 排除持仓 → 打分 → 排序 → TopN ----

def screen_pool(pool, fields, holding_codes=None, top=15):
    pool = dedup(pool)
    pool = exclude_holdings(pool, holding_codes)
    scored = score(pool, fields)
    ordered = sorted(
        scored,
        key=lambda r: (r["score"] is not None, r["score"] or 0.0),
        reverse=True,
    )
    return ordered[: top] if top and top > 0 else ordered


def build_listing(pool, fields, holding_codes=None, top=15, meta=None):
    """跑完整管线，返回 {meta, results(精简投影), html}。results 供对话内回名单。"""
    meta = meta or {}
    ordered = screen_pool(pool, fields, holding_codes, top)
    results = []
    for item in ordered:
        rec = item["record"]
        sc = item["score"]
        parts = {
            k: (round(v, 3) if v is not None else None)
            for k, v in (item.get("parts") or {}).items()
        }
        results.append({
            "code": record_code(rec) or "",
            "name": record_name(rec) or "",
            "score": (round(sc, 1) if sc is not None else None),
            "parts": parts,
        })
    return {"meta": meta, "results": results, "html": render_html(ordered, meta)}


# ---- iFinD 本地历史证据（opt-in 接入层 · 2026-07-23 加）----
# 默认关闭。仅当 --ifind-local-evidence 时，在已形成的 Top 名单之后附加一个
# 独立的「本地历史覆盖体检」区块，绝不参与排名/评分/候选准入，也不改动任何既有键。
# 数据源由用户配置的离线只读 helper 提供；数据截止日必须由 helper 在每次输出中声明，
# 不在本脚本固化。失败/无匹配一律 fail-closed（core 名单照常输出）。

# helper 路径来自配置键 ifind_evidence_helper；未配置即本适配器不可用（见 attach_ifind_evidence）。


def _ifind_subprocess_runner(codes, as_of, helper_path, html_out=None, timeout=60):
    """调离线 Node helper 取本地历史证据，解析其 stdout JSON。仅本地 Parquet，不联网。
    安全：生产只允许 canonical helper——helper_path 的 realpath 必须等于配置里
    ifind_evidence_helper 的 realpath，否则拒绝（阻断任意 Node helper 执行）。"""
    configured = ifind_helper_path()
    if not configured:
        raise ValueError("iFinD 本地证据未配置（config 键 ifind_evidence_helper）")
    canonical = os.path.realpath(configured)
    if os.path.realpath(helper_path) != canonical:
        raise ValueError("helper 路径非法：仅允许配置中的 canonical helper")
    cmd = ["node", canonical, "--codes", ",".join(codes)]
    if as_of:
        cmd += ["--as-of", as_of]
    if html_out:
        cmd += ["--html-out", html_out]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    return json.loads(out)


def _validate_ifind_contract(doc):
    """fail-closed 契约校验：任一不符即拒绝（返回 (False, 原因)）。
    要求 ok=true、数据截止日为非未来 YYYY-MM-DD、声明含非实时/冻结边界、
    候选证据与市场状态为预期数组结构。"""
    import datetime

    if not isinstance(doc, dict) or doc.get("ok") is not True:
        return False, "helper 未返回 ok=true"
    cutoff = doc.get("数据截止日")
    try:
        cutoff_date = datetime.date.fromisoformat(cutoff)
    except (TypeError, ValueError):
        return False, "数据截止日必须为 YYYY-MM-DD（拒绝：%r）" % cutoff
    if cutoff_date.isoformat() != cutoff:
        return False, "数据截止日必须为 YYYY-MM-DD（拒绝：%r）" % cutoff
    if cutoff_date > datetime.date.today():
        return False, "数据截止日不得为未来日期（拒绝：%s）" % cutoff
    disc = doc.get("声明") or ""
    if "非实时" not in disc or cutoff not in disc:
        return False, "声明缺少冻结/非实时边界"
    cov = doc.get("候选证据")
    ms = doc.get("市场状态")
    if not isinstance(cov, list) or not isinstance(ms, list):
        return False, "候选证据/市场状态 非数组"
    if cov and not all(isinstance(r, dict) and "匹配状态" in r for r in cov):
        return False, "候选证据 结构不符（缺 匹配状态）"
    if ms and not all(isinstance(r, dict) and "综合状态" in r for r in ms):
        return False, "市场状态 结构不符（缺 综合状态）"
    return True, ""


def attach_ifind_evidence(top_results, as_of=None, helper_path=None, runner=None, html_out=None):
    """opt-in：为 Top 名单附加 iFinD 本地历史覆盖体检（独立区块）。
    - 不修改 top_results；只读其 code 字段。
    - fail-closed：helper 缺失/超时/非 JSON/契约不符 → {'available': False, 'reason': ...}。
    - 契约校验见 _validate_ifind_contract（数据截止日必须可审计且不得晚于运行日）。
    - html_out 非空时，helper 旁路写覆盖体检 HTML；写成则在区块返回 html_path。
    - runner 可注入 (codes, as_of, helper_path)->dict 用于离线测试（不走生产 canonical 校验）。
    返回一个 dict，供调用方作为独立键附加，绝不并入排名或综合分。"""
    codes = [r.get("code") for r in (top_results or []) if r.get("code")]
    base = {"opt_in": True,
            "note": "独立历史覆盖体检，不参与排名/评分/候选准入；数据截止日以 helper 本次输出为准。",
            "source": "iFinD 本地历史证据 helper（离线只读）"}
    if not codes:
        return dict(base, available=False, reason="Top 名单无可查代码")
    if runner is None:
        try:
            helper = helper_path or ifind_helper_path()
        except ScreenError as e:
            return dict(base, available=False, reason="本地证据配置不可用：%s" % e)
        if not helper:
            return dict(base, available=False,
                        reason="iFinD 本地证据未配置（可选适配器，config 键 "
                               "ifind_evidence_helper）；core 名单不受影响")
        run = lambda c, a, h: _ifind_subprocess_runner(
            c, a, h, html_out=html_out)
    else:
        helper = helper_path
        run = runner
    try:
        doc = run(codes, as_of, helper)
    except FileNotFoundError:
        return dict(base, available=False, reason="未找到 node 或 helper，跳过本地证据（core 名单不受影响）")
    except Exception as e:  # 超时/JSON 解析失败/路径非法/其它 → 一律降级
        return dict(base, available=False, reason="本地证据接口调用失败：%s" % e)
    ok, why = _validate_ifind_contract(doc)
    if not ok:
        return dict(base, available=False, reason="本地证据契约校验不通过：%s" % why)
    cutoff = doc.get("数据截止日")
    out = dict(
        base,
        available=True,
        note="独立历史覆盖体检，不参与排名/评分/候选准入。数据截止日 %s。" % cutoff,
        data_cutoff=cutoff,
        market_state_asof=doc.get("市场状态对齐日"),
        coverage_caliber=doc.get("覆盖统计口径"),
        coverage=doc.get("候选证据"),
        audit=doc.get("口径校验"),
        market_state=doc.get("市场状态"),
        disclaimer=compliance_notice(base["source"], cutoff),
    )
    if html_out and os.path.exists(os.path.expanduser(html_out)):
        out["html_path"] = html_out
    return out


def assemble_output(title, query, report_label, pool, listing, holdings,
                    holdings_missing, enrich_info, html_path, ifind_evidence=None):
    """组装 CLI JSON；固定含运行时声明，iFinD 证据仅在 opt-in 时追加。"""
    meta = listing.get("meta") or {}
    sources = meta.get("sources")
    data_date = meta.get("data_date")
    out = {
        "ok": True,
        "title": title,
        "query": query,
        "date": data_date or "未取得",
        "report_label": report_label,
        "disclaimer": compliance_notice(sources, data_date),
        "fetched": len(pool),
        "returned": len(listing["results"]),
        "holdings_excluded": len(holdings),
        "holdings_missing": holdings_missing,
        "enrich": enrich_info,
        "html_path": html_path,
        "top": listing["results"],
    }
    if ifind_evidence is not None:
        out["ifind_local_evidence"] = ifind_evidence
    return out


# ---- CLI ----

def _safe_filename(s):
    return re.sub(r"[^\w一-鿿-]+", "", str(s)) or "选股"


def main(argv=None):
    import argparse
    import datetime

    ap = argparse.ArgumentParser(
        description="问财选股：筛池子 + 数据化打分排序（只筛不分析，深度研判交 stock-buddy）"
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preset", choices=list(PRESETS), help="命中模板名")
    g.add_argument("--query", help="自由口述的问财问句")
    ap.add_argument("--top", type=int, default=15, help="输出 Top-N（默认 15）")
    ap.add_argument("--max-records", type=int, default=100, help="抓取上限（默认 100）")
    ap.add_argument("--page-limit", type=int, default=50, help="每页条数（默认 50）")
    ap.add_argument("--no-enrich", action="store_true",
                    help="跳过腾讯行情打分列覆盖（降级：打分退回问财返回列）")
    ap.add_argument("--no-exclude-holdings", action="store_true", help="不排除已持仓")
    # 默认值留空，运行时从配置解析；这样 --help 与导入本模块都不要求先配好 vault。
    ap.add_argument("--holdings-path", default=None,
                    help="极简持仓卡路径（默认 <vault_root>/03-持仓跟踪/持仓.md）")
    ap.add_argument("--out-dir", default=None,
                    help="导出目录（默认 <vault_root>/outputs/screener）")
    ap.add_argument(
        "--report-date", "--date", dest="report_date", default=None,
        help=("报告/文件标签，不改变取数时点（--date 为 legacy alias；"
              "默认使用实际数据日，无法推断时使用本地运行日）"),
    )
    ap.add_argument("--ifind-local-evidence", action="store_true",
                    help="opt-in：Top 名单后附加 iFinD 本地历史覆盖体检（独立区块，不影响排名/评分/准入）")
    ap.add_argument("--ifind-as-of", default=None,
                    help="配合 --ifind-local-evidence：市场状态对齐日 YYYY-MM-DD（默认不传→市场状态返回不适用）")
    # 注：不提供任意 helper 路径参数——生产只允许 canonical helper（IFIND_EVIDENCE_HELPER）。
    args = ap.parse_args(argv)

    if args.preset:
        preset = get_preset(args.preset)
        title, query, fields = preset["title"], preset["query"], preset["fields"]
    else:
        title, query, fields = "自选", args.query, DEFAULT_FIELDS

    exclude = not args.no_exclude_holdings
    # 路径在此处才解析：未配置 vault 时给出配置指引，而不是抛裸异常。
    try:
        holdings_file = args.holdings_path or holdings_path()
        target_dir = args.out_dir or out_dir()
    except ScreenError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    holdings_file = os.path.expanduser(holdings_file)
    holdings = load_holdings(holdings_file) if exclude else []
    holdings_missing = exclude and not os.path.exists(holdings_file)

    try:
        pool = fetch_pool(query, max_records=args.max_records, page_limit=args.page_limit)
    except (ScreenError, subprocess.TimeoutExpired) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    # enrichment 会删除并覆盖带日期的同名行情键，必须先从问财原始记录取证。
    data_date = infer_market_data_date(pool)
    report_label = args.report_date or data_date or datetime.date.today().isoformat()

    if args.no_enrich:
        enrich_info = {"enriched": False, "hit": 0, "note": "--no-enrich 跳过"}
    else:
        pool, enrich_info = enrich_records(pool)
    sources = ("同花顺问财（筛选/资金/财务）+ 腾讯行情（行情打分列）"
               if enrich_info.get("hit", 0) > 0 else "同花顺问财")

    listing = build_listing(
        pool, fields, holding_codes=holdings, top=args.top,
        meta={"title": title, "report_label": report_label,
              "data_date": data_date, "sources": sources},
    )

    target_dir = os.path.expanduser(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    html_path = os.path.join(
        target_dir, "%s-%s.html" % (report_label, _safe_filename(title)))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(listing["html"])

    ifind_evidence = None
    if args.ifind_local_evidence:
        ifind_html_path = os.path.join(
            target_dir, "%s-%s-ifind覆盖体检.html"
            % (report_label, _safe_filename(title)))
        ifind_evidence = attach_ifind_evidence(
            listing["results"], as_of=args.ifind_as_of, html_out=ifind_html_path)

    print(json.dumps(assemble_output(
        title, query, report_label, pool, listing, holdings,
        holdings_missing, enrich_info, html_path, ifind_evidence,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
