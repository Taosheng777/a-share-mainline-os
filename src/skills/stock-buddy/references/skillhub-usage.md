# SkillHub 三技能调用（现场探明，2026-06-16 实测）

> 专家模式用。三技能与 `hithink-market-query` 同厂商（同花顺问财），共用 `~/.zshrc` 里的 `IWENCAI_API_KEY`。
> **额度警告：三技能与问财共用同一个额度池**——每次调用都计入问财当日额度，受 SKILL.md §问财额度纪律 与 `00-系统/AI操作规则.md` §问财额度纪律 约束（额度池由 `stock-buddy`、`stock-daily`、`stock-screener` 共用，无法预查余额）。专家模式 5 个 sub-agent 各调 1–2 次，量级等于复盘全天预算，交易日 15:00 前启动必须先告知"会占用当日复盘额度"并等用户确认。
> 跑前必须 `source ~/.zshrc`。第三方依赖 `requests`（已确认 2.33.1 可用）。

## 安装与发现

这些组件许可证尚未确认，不进入本仓库。用户如自行安装，必须按实际安装目录设置环境变量；未配置时明确降级，不猜私人路径：

- `ASM_NEWS_SEARCH_CLI=/absolute/path/to/news-search/scripts/__main__.py`
- `ASM_REPORT_SEARCH_CLI=/absolute/path/to/report-search/scripts/__main__.py`
- `ASM_INDUSTRY_QUERY_CLI=/absolute/path/to/hithink-industry-query/scripts/cli.py`

## 1. news-search（消息面 / 新闻）
**入口**：环境变量 `ASM_NEWS_SEARCH_CLI`
```bash
source ~/.zshrc
python3 "$ASM_NEWS_SEARCH_CLI" -q "<标的简称>" -l 5 -f json
```
- 参数：`-q/--query` 关键词｜`-l/--limit` 条数(默认10)｜`-f/--format {csv,json,text}`(默认text)｜`-d/--days` 最近N天(默认30)｜`-i/--input` 批量文件。
- 返回（text）：标题/摘要/发布时间/链接，逐条；`-f json` 给结构化。底层 POST `openapi.iwencai.com/v1/comprehensive/search`，`channels=["news"]`。
- 用途：催化、利空、政策、公司业务进展。

## 2. report-search（研报 / 券商观点）
**入口**：环境变量 `ASM_REPORT_SEARCH_CLI`
```bash
source ~/.zshrc
python3 "$ASM_REPORT_SEARCH_CLI" -q "<标的简称>" -l 5 -f text --sort-by date
```
- 参数：`-q/--query`｜`-l/--limit`｜`-f/--format {csv,json,text,markdown}`｜`-d/--days`｜`--date-from/--date-to YYYY-MM-DD`｜`--sort-by {date,relevance}`｜`--sort-order {asc,desc}`｜`--test` 测连通。
- 返回：研报标题/发布时间/原文链接/摘要/**提取信息（评级、行业）**。
- ⚠️ 实测会**返回同一篇研报多条重复**（API 行为，非 bug），汇总时按标题去重。
- 用途：券商评级、目标价、一致预期。

## 3. hithink-industry-query（行业 / 板块）
**入口**：环境变量 `ASM_INDUSTRY_QUERY_CLI`
```bash
source ~/.zshrc
python3 "$ASM_INDUSTRY_QUERY_CLI" --query "<板块>估值 市盈率 主力资金净流入" --limit 10
```
- 参数：`--query/-q`(必填)｜`--page`(默认1)｜`--limit`(默认10)｜`--call-type {normal,retry}`｜`--timeout`(默认30)。
- 返回：**与 hithink-market-query 同构 JSON**——`success/query/code_count/returned_count/has_more/datas[]`。解析同 `parsing.md`（日期后缀模糊匹配等）。
- ⚠️ 像"涨跌幅排名"这类问句，可能只回指数代码/简称而不带数值；要数值就把字段写进 query（如"…涨跌幅 主力资金净流入额"），必要时改用龙头个股代理（见 parsing.md）。
- 用途：行业景气、板块估值分位、板块资金轮动、行业排名。

## 给 sub-agent 的统一约定
- 每个 sub-agent 自己先 `source ~/.zshrc`。
- 透明传递：news/report 的原始返回不要再加工成自定义结构（问财网关规范条件六）；摘取关键字段汇报即可，但标注数据来源「同花顺问财」。
- 缺数据按铁律标缺口，不臆造。**单个技能调用失败或额度耗尽 ≠ 整轮熔断**：该数据项写 `未取得（<技能名> 失败/额度耗尽）`，其余维度与首席裁决照常完成；整轮停止只在关键数据项全部熔断或数据日异常两种情况。
