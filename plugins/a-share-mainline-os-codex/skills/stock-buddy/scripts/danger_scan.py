#!/usr/bin/env python3
"""stock-buddy 常规档 · 个股危险信号三层扫描

用法:
    python3 danger_scan.py 002070 600323 [--date 2026-07-15] [--deep-news]

- 仅扫 A 股个股(0/3/6 开头 6 位代码);ETF/指数自动跳过(三层情报不适用)。
- 第一层硬风险:解禁/公告/融资融券/互动易;第二层免费快讯:财联社最新电报;
  第三层深度新闻仅在显式 --deep-news 时调用问财 news-search,不自动耗额度。
- --date 是风险扫描基准日/窗口锚点,不是所有子结果的统一数据日。
- 数据源为 T3(东财/巨潮/腾讯/财联社爬取,a-stock-data V3.4 口径):数字不入台账,
  与 T1(hithink-finance)冲突时以 T1 为准;引用须标注来源+日期。
- 东财请求内置 ≥1s 串行节流,勿并发改造。
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from functools import lru_cache

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
NEWS_SEARCH_CLI_ENV = "ASM_NEWS_SEARCH_CLI"
RISK_KW = ["减持", "质押", "冻结", "问询", "关注函", "监管", "立案", "处罚",
           "辞职", "离任", "预亏", "下修", "诉讼", "仲裁", "违规", "警示", "退市"]
SOURCE_TIER = "第一/二层为T3公开爬取情报,deep-news为T2问财;数字不入台账,与T1冲突以T1为准"
RUNTIME_SOURCE_ORDER = (
    "东方财富（解禁/两融）",
    "巨潮资讯（公告/互动易）",
    "腾讯行情（公司名）",
    "财联社电报",
    "同花顺问财 news-search",
)


def compliance_notice(sources, anchor_date):
    """生成用户可见的统一运行时合规声明。"""
    source_text = "、".join(sources) if sources else "未取得（运行时无成功来源）"
    return (
        "本工具仅用于研究与决策支持；提供方非持牌证券投资咨询机构，"
        "本工具及其输出不构成投资建议，不代下单，使用者风险自担。"
        "数据来源：%s；数据日：无单一数据日；风险扫描基准日 %s；"
        "各子结果日期/窗口见 data_caliber 与正文，均以本次运行输出为准。"
        % (source_text, anchor_date)
    )


def collect_runtime_sources(stocks, deep_news_runs=None):
    """按固定顺序列出本次扫描中实际调用成功的数据源。"""
    succeeded = set()
    for code, item in stocks.items():
        if any(item.get(key) is not None and not isinstance(item.get(key), str)
               for key in ("解禁_未来90天", "两融")):
            succeeded.add("东方财富（解禁/两融）")
        if any(item.get(key) is not None and not isinstance(item.get(key), str)
               for key in ("公告_60天", "互动易_90天")):
            succeeded.add("巨潮资讯（公告/互动易）")

        news = item.get("新闻")
        if not isinstance(news, dict):
            continue
        if news.get("公司名来源") == "腾讯行情":
            succeeded.add("腾讯行情（公司名）")
        if news.get("状态") == "已检索":
            succeeded.add("财联社电报")

    if any(isinstance(run, dict) and run.get("状态") == "已运行"
           for run in (deep_news_runs or {}).values()):
        succeeded.add("同花顺问财 news-search")
    return [source for source in RUNTIME_SOURCE_ORDER if source in succeeded]


def build_meta(anchor_date, data_sources, executed_at=None):
    """构造扫描结果元数据，确保终端与落盘 JSON 使用同一声明。"""
    anchor = datetime.strptime(anchor_date, "%Y-%m-%d")
    since60 = (anchor - timedelta(days=60)).strftime("%Y-%m-%d")
    since90 = (anchor - timedelta(days=90)).strftime("%Y-%m-%d")
    through90 = (anchor + timedelta(days=90)).strftime("%Y-%m-%d")
    return {
        "scan_anchor_date": anchor_date,
        "executed_at": executed_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data_sources": list(data_sources),
        "source_tier": SOURCE_TIER,
        "data_caliber": {
            "解禁_未来90天": f"自风险扫描基准日 {anchor_date} 起未来90天（至 {through90}）",
            "公告_60天": f"自风险扫描基准日前60日 {since60} 起至运行时可得最新；日期见子项 date",
            "互动易_90天": f"自风险扫描基准日前90日 {since90} 起至运行时可得最新；日期见子项 t",
            "两融": "最近可得交易日及5/20日变化；数据日见子项 date",
            "新闻": "运行时最新50条全市场快讯；命中时间见子项 t",
            "deep_news": "由上游输出自带口径",
        },
        "disclaimer": compliance_notice(data_sources, anchor_date),
        "news_layers": {
            "第一层": "解禁/公告/两融/互动易硬风险",
            "第二层": "财联社最新50条免费快讯,零命中不等于无新闻",
            "第三层": "仅 --deep-news 显式调用问财 news-search",
        },
    }

EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]))
    EM_SESSION.mount("https://", _adapter)
    EM_SESSION.mount("http://", _adapter)
except Exception:
    pass
EM_MIN_INTERVAL = 1.0
_last = [0.0]


def em_get(url, params=None, headers=None, timeout=15):
    wait = EM_MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout)
    finally:
        _last[0] = time.time()


def datacenter(report, filter_str="", page_size=50, sort_columns="", sort_types="-1"):
    r = em_get(DATACENTER_URL, params={
        "reportName": report, "columns": "ALL", "filter": filter_str,
        "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB"})
    d = r.json()
    return d["result"]["data"] if d.get("result") and d["result"].get("data") else []


def lockup(code, anchor_date, forward_days=90):
    end = (datetime.strptime(anchor_date, "%Y-%m-%d")
           + timedelta(days=forward_days)).strftime("%Y-%m-%d")
    rows = datacenter("RPT_LIFT_STAGE",
        f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{anchor_date}\')(FREE_DATE<=\'{end}\')',
        20, "FREE_DATE", "1")
    return [{"date": str(r.get("FREE_DATE", ""))[:10],
             "type": r.get("FREE_SHARES_TYPE", ""),
             "shares_wan": r.get("FREE_SHARES", 0),
             "ratio_pct": (r.get("FREE_RATIO") or 0) * 100} for r in rows]


_ORGID = {}


def _orgid(code):
    global _ORGID
    if not _ORGID:
        try:
            r = requests.get("http://www.cninfo.com.cn/new/data/szse_stock.json",
                             headers={"User-Agent": UA}, timeout=15)
            _ORGID = {s["code"]: s["orgId"] for s in r.json().get("stockList", [])}
        except Exception as e:
            print(f"[WARN] 巨潮 orgId 映射失败,回退硬编码: {e}", file=sys.stderr)
    if code in _ORGID:
        return _ORGID[code]
    return ("gssh0" if code.startswith("6") else
            "gsbj0" if code.startswith(("8", "4")) else "gssz0") + code


def announcements(code, since_date, page_size=30):
    r = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
        data={"stock": f"{code},{_orgid(code)}", "tabName": "fulltext",
              "pageSize": str(page_size), "pageNum": "1", "column": "", "category": "",
              "plate": "", "seDate": "", "searchkey": "", "secid": "",
              "sortName": "", "sortType": "", "isHLtitle": "true"},
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://www.cninfo.com.cn/new/disclosure",
                 "Origin": "https://www.cninfo.com.cn"}, timeout=15).json()
    out = []
    for i in r.get("announcements", []) or []:
        ts = i.get("announcementTime")
        date = (datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                if isinstance(ts, (int, float)) else str(ts)[:10])
        if date < since_date:
            continue
        title = i.get("announcementTitle", "")
        out.append({"date": date, "title": title,
                    "flags": [k for k in RISK_KW if k in title]})
    return out


def margin(code, page_size=25):
    rows = datacenter("RPTA_WEB_RZRQ_GGMX", f'(SCODE="{code}")',
                      page_size, "DATE", "-1")
    rows = [{"date": str(r.get("DATE", ""))[:10], "rzye": r.get("RZYE", 0)} for r in rows]
    if len(rows) < 21 or not rows[5]["rzye"] or not rows[20]["rzye"]:
        return {"rows": rows[:3], "trend": None}
    return {"rows": rows[:3], "trend": {
        "date": rows[0]["date"], "rzye_yi": rows[0]["rzye"] / 1e8,
        "chg5_pct": (rows[0]["rzye"] - rows[5]["rzye"]) / rows[5]["rzye"] * 100,
        "chg20_pct": (rows[0]["rzye"] - rows[20]["rzye"]) / rows[20]["rzye"] * 100}}


def irm(code, since_date, page_size=15):
    try:
        d1 = requests.post("https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo",
            data={"keyWord": code}, headers={"User-Agent": UA}, timeout=10
            ).json().get("data") or []
        if not d1:
            return []
        rows = requests.post("https://irm.cninfo.com.cn/newircs/company/question",
            params={"_t": 1, "stockcode": code, "orgId": d1[0].get("secid"),
                    "pageSize": page_size, "pageNum": 1, "keyWord": "",
                    "startDay": "", "endDay": ""},
            headers={"User-Agent": UA}, timeout=10).json().get("rows") or []
    except Exception as e:
        print(f"[WARN] 互动易 {code}: {e}", file=sys.stderr)
        return None
    out = []
    for it in rows:
        ts = it.get("pubDate")
        t = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
        if t and t < since_date:
            continue
        out.append({"t": t, "q": (it.get("mainContent") or "")[:90],
                    "a": (it.get("attachedContent") or "")[:130] or None})
    return out


def stock_name(code):
    market = "sh" if code.startswith("6") else "sz"
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={market}{code}",
                         headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
                         timeout=10)
        r.raise_for_status()
        text = r.content.decode("gbk", errors="replace")
        fields = text.split('"', 2)[1].split("~")
        return fields[1].strip() or None
    except Exception:
        return None


@lru_cache(maxsize=1)
def cls_telegraph(page_size=50):
    page_size = min(max(int(page_size), 1), 50)
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
              "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{key}={params[key]}" for key in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    r = requests.get(f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}",
                     headers={"User-Agent": UA, "Referer": "https://www.cls.cn/"},
                     timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("errno") not in (None, 0):
        raise RuntimeError(f"CLS errno={data.get('errno')}")

    rows = []
    for item in data.get("data", {}).get("roll_data", []) or []:
        timestamp = item.get("ctime")
        published = (datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
                     if timestamp else "")
        title = item.get("title", "") or item.get("brief", "") or ""
        content = item.get("content", "") or item.get("brief", "") or ""
        rows.append({"time": published,
                     "title": re.sub(r"<[^>]+>", "", title),
                     "content": re.sub(r"<[^>]+>", "", content)})
    return rows


def breaking_news(code):
    name = stock_name(code)
    name_source = "腾讯行情" if name else None
    terms = [term for term in (name, code) if term]
    try:
        rows = cls_telegraph(50)
    except Exception as exc:
        return {"状态": "来源不可用", "来源": "财联社电报", "公司": name or code,
                "公司名来源": name_source,
                "覆盖": "未取得", "检索词": terms, "命中": [],
                "说明": f"未形成媒体新闻结论({type(exc).__name__})"}

    matches = []
    for row in rows:
        text = row["title"] + " " + row["content"]
        if any(term in text for term in terms):
            matches.append({"t": row["time"], "title": row["title"][:60]})
    note = ("仅为财联社快讯命中,需回到原文或公告核实"
            if matches else
            "该范围内零命中,不等于无新闻;完整公司新闻需显式运行 --deep-news")
    return {"状态": "已检索", "来源": "财联社电报", "公司": name or code,
            "公司名来源": name_source,
            "覆盖": f"最新{len(rows)}条全市场快讯", "检索词": terms,
            "命中": matches[:6], "说明": note}


def run_deep_news(query):
    if not os.environ.get("IWENCAI_API_KEY"):
        return {"状态": "未运行", "原因": "IWENCAI_API_KEY 未配置"}
    cli = os.environ.get(NEWS_SEARCH_CLI_ENV)
    if not cli:
        return {"状态": "未运行", "原因": "%s 未配置" % NEWS_SEARCH_CLI_ENV}
    cli = os.path.realpath(os.path.expanduser(cli))
    if not os.path.isfile(cli):
        return {"状态": "未运行", "原因": "news-search CLI 不存在"}
    print(f"\n== 深度新闻 {query} · 同花顺问财原始输出 ==")
    try:
        completed = subprocess.run(
            [sys.executable, cli, "-q", query,
             "-l", "20", "-d", "30", "-f", "text"],
            check=False)
    except Exception as exc:
        return {"状态": "运行失败", "原因": type(exc).__name__}
    return {"状态": "已运行" if completed.returncode == 0 else "运行失败",
            "returncode": completed.returncode, "来源": "同花顺问财 news-search",
            "说明": "结果由上游 CLI 直接输出,本扫描器不重组"}


def scan_stock(code, anchor_date):
    since60 = (datetime.strptime(anchor_date, "%Y-%m-%d")
               - timedelta(days=60)).strftime("%Y-%m-%d")
    since90 = (datetime.strptime(anchor_date, "%Y-%m-%d")
               - timedelta(days=90)).strftime("%Y-%m-%d")
    item, red = {}, []

    try:
        lk = lockup(code, anchor_date)
        item["解禁_未来90天"] = lk
        if lk:
            red.append(f"未来90天解禁 {len(lk)} 批(最近 {lk[0]['date']},"
                       f"占总股本 {lk[0]['ratio_pct']:.1f}%)")
    except Exception as e:
        item["解禁_未来90天"] = f"未跑({e})"

    try:
        anns = announcements(code, since60)
        flagged = [a for a in anns if a["flags"]]
        item["公告_60天"] = {"总数": len(anns), "命中": flagged[:8],
                             "近3条": [f"{a['date']} {a['title'][:40]}" for a in anns[:3]]}
        for a in flagged[:5]:
            red.append(f"公告[{'/'.join(a['flags'])}] {a['date']} {a['title'][:44]}")
    except Exception as e:
        item["公告_60天"] = f"未跑({e})"

    try:
        mg = margin(code)
        item["两融"] = mg["trend"] or {"说明": "样本不足或非两融标的", "rows": mg["rows"]}
        t = mg["trend"]
        if t and abs(t["chg5_pct"]) >= 8:
            red.append(f"融资余额5日 {t['chg5_pct']:+.1f}%(急变,{t['date']})")
    except Exception as e:
        item["两融"] = f"未跑({e})"

    got = irm(code, since90)
    if got is None:
        item["互动易_90天"] = "未跑(请求失败)"
    else:
        unanswered = sum(1 for x in got if not x["a"])
        item["互动易_90天"] = {"条数": len(got), "未回复": unanswered, "样例": got[:5]}
        if len(got) >= 5 and unanswered / len(got) > 0.6:
            red.append(f"互动易近90天 {len(got)} 问仅回复 {len(got) - unanswered}(沟通弱)")

    item["新闻"] = breaking_news(code)

    item["红字信号"] = red
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+", help="6位A股个股代码")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                    help="风险扫描基准日/窗口锚点（非统一数据日，YYYY-MM-DD）")
    ap.add_argument("--json", help="结果另存 JSON 路径")
    ap.add_argument("--deep-news", action="store_true",
                    help="显式调用问财 news-search 深搜公司新闻(消耗问财额度)")
    args = ap.parse_args()

    stocks = {}
    for code in args.codes:
        if not (len(code) == 6 and code.isdigit() and code[0] in "036"):
            print(f"[SKIP] {code}: 非 A 股个股代码(ETF/指数不适用三层情报)")
            continue
        print(f"== 扫描 {code} ==")
        stocks[code] = scan_stock(code, args.date)
        for r in stocks[code]["红字信号"]:
            print(f"  🔴 {r}")
        if not stocks[code]["红字信号"]:
            print("  (无红字信号)")

    deep_news_runs = None
    if args.deep_news:
        deep_news_runs = {}
        for code, item in stocks.items():
            query = item["新闻"].get("公司") or code
            deep_news_runs[code] = {
                "query": query, **run_deep_news(query)}

    data_sources = collect_runtime_sources(stocks, deep_news_runs)
    meta = build_meta(args.date, data_sources)
    if deep_news_runs is not None:
        meta["deep_news_runs"] = deep_news_runs
    results = {"meta": meta, "stocks": stocks}

    print("\n" + json.dumps(results, ensure_ascii=False, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print(f"\nSAVED {args.json}")


if __name__ == "__main__":
    main()
