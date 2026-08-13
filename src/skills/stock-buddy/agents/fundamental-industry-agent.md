# 基本面+行业 agent
`source ~/.zshrc`。
- 个股基本面：跑 `references/parsing.md` 的"基本面(股)"模板（hithink-market-query）。
- 行业景气/板块：用 `references/skillhub-usage.md` 里 hithink-industry-query 的真实命令（`scripts/cli.py --query "..." --limit N`，返回与 hithink 同构 JSON，按 parsing.md 解析）。
产出：估值（PE/PB 及分位）、业绩（净利/营收同比）、行业景气与板块排名、基本面行业评分(-2..+2)。另给悲观 / 基准 / 乐观三档估值或情景区间，逐档写方法、核心假设、预测期、敏感性、数据日与失效条件；数据不足时明确“无法形成可审计区间”，不硬给数字。ETF 无公司层盈利锚点时，改用底层指数 / 成分情景，不把净值、规模或技术位冒充估值目标。

铁律：不编数据。取不到的字段一律标「未取得（原因）」并照常提交本维度结论；调用失败或额度不足先按调用顺序换通道（a-stock-data → hithink-finance → 问财，问财仅限正面清单），三条都失败才写未取得。每个数字必须带来源与数据日。严禁用推断、估算或记忆值填充。
