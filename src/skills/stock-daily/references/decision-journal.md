# 连续决策账本：运行合同 v1

这是标准库 CLI，只记录与校验有来源的研究判断。它不联网、不生成信号、不算账户绩效、不交易。`engineering_validation: passed` 只说明记录通过工程规则；`profitability: unverified` 始终表示赚钱能力尚未验证。

## 运行方式

```sh
python3 scripts/decision_journal.py append --journal /绝对路径/连续决策.jsonl --record /绝对路径/本次决策.json
python3 scripts/decision_journal.py validate --journal /绝对路径/连续决策.jsonl
python3 scripts/decision_journal.py status --journal /绝对路径/连续决策.jsonl
python3 scripts/decision_journal.py status --journal /绝对路径/连续决策.jsonl --theme-id 稳定主题编号
```

退出码 `0` 为通过，`2` 为拒绝/读取错误；stdout 均为 JSON。`append` 返回 `appended` 或 `already_present`。相同 ID 与完全相同记录的重试为幂等；相同 ID 内容不同则拒绝。只有 append 创建父目录和账本；validate/status 只读，文件不存在返回错误，不创建任何目录。不要把“尚无账本”写成“已有策略通过检验”。

每个 JSONL 行是 `{record, recorded_at, previous_hash, hash}`，由工具生成，不手写 envelope。`recorded_at` 在锁内读取系统时钟并纳入哈希，调用者不得提供。前瞻评价最早有效时间为 `max(decided_at, recorded_at)`；不得按调用者自报的较早 decided_at 提前认定信号可得。所有写入进程在同一账本文件上使用 `flock` 排他锁，检查、追加、flush/fsync 均在锁内；读取使用共享锁。并发进程以先取得锁者为先，过期 previous_decision_id 必须重读再形成新记录，不能覆盖。

shadow/live_advice 的 decided_at（换算 +08 日期）必须是**实际录入日（+08）**，旧决定补录只允许 historical_audit。data_date 可早于录入日，以支持午夜后/周末复盘，但不得在未来；旧行情仍须如实写其数据日和关键数据完整性。例：09-12 凌晨使用 09-11 行情可记录为新的前瞻判断，但不能把 decided_at 伪填 09-11，更不能模拟在 09-11 收盘前已经行动。执行最早使用 recorded_at 之后的真实可交易 session。CLI 不提供伪造系统日期参数；单元测试才注入内部时钟。后日 validate 按各行 recorded_at 检查当时约束，不会把昨天合法锁定的前瞻记录误判为今天补录。系统时钟错误会被保守拒绝，不能通过回拨时钟“修复”记录。

**锁只协调本机遵守协议的进程，不是跨设备 iCloud 分布式锁。**继续遵守前后段串行、单写者；禁止其他工具替换、截断、重命名正在使用的账本。哈希链只能发现未同步重算哈希的修改、中间断链及不完整末行；不能防止有权限者重写整条链或删除完整尾部。不要称作防篡改安全系统。

## 输入示例

下面是假设数据的格式样例，不能当真实主题或信号。时间戳必须换成证据真实可得时间与实际决策时间；历史材料用 historical_audit，不能改写日期伪造前瞻记录。

```json
{
  "schema_version": 1,
  "decision_id": "sample-shadow-20260901-01",
  "theme_id": "sample-theme",
  "data_date": "2026-09-01",
  "decided_at": "2026-09-01T18:00:00+08:00",
  "model": "gpt-6-astra",
  "method_version": "decision-journal-v1",
  "mode": "historical_audit",
  "evaluation_context": "retrospective",
  "theme_type": "unknown",
  "thesis": "格式样例：缺证据，仅观察，不形成进场判断",
  "evidence": [],
  "action": "observe",
  "previous_decision_id": null,
  "change_reason": {
    "kind": "initial",
    "text": "建立格式示例，尚未取得真实证据",
    "evidence_ids": []
  },
  "invalidation": [],
  "triggered_conditions": [],
  "discipline": {"1": "unknown", "2": "unknown", "3": "unknown"},
  "user_execution": {"status": "unconfirmed"},
  "confidence": "unknown",
  "key_data_status": "missing"
}
```

| 字段 | 合同 |
|---|---|
| schema_version/model | 固定 `1` / `ALLOWED_MODELS` 之一（`gpt-6-astra`、`claude-opus-5-5`），如实写当轮执行模型，不互相冒充，不将历史旧模型输出假称原判断；historical_audit 的 model 指本次审计者，旧模型见 evidence.source |
| decision_id/theme_id | 非空稳定字符串。decision_id 全账本唯一；同一主题不要换名逃避前序；theme_id 的实际主题归一由调用者负责 |
| data_date/decided_at | `YYYY-MM-DD` / 带时区 ISO 时间。数据日不得晚于决策当地日期或当前日期，决策时刻不得在未来 |
| method_version | 本次方法版本。方法修改是实质变更，不能默默沿用旧效果结论 |
| mode/evaluation_context | `shadow/prospective`、`live_advice/prospective`、`historical_audit/retrospective`；三种模式分别续接，不能互相当做前序 |
| theme_type | `industrial/event/sentiment/unknown`：产业趋势/事件脉冲/情绪扩散/未知。不是收益权重，工具不自动分类 |
| thesis | 当时判断的具体可审计论点，非空；跨日更换论点须说明依据 |
| evidence | 列表：`{id, source, available_at, kind}`。source 为可追溯文件/接口结果/用户原话定位；kind 是 `observation/event/user_instruction`。available_at 是证据可得时间，必须不晚于决策时刻 |
| action | `observe/enter/hold/reduce/exit`；只是 AI 研究/建议状态，不是委托，也不等于成交 |
| previous_decision_id | 当前 theme_id + mode 最新 ID，初次为 null；不允许跳过前序、倒序日期或跨模式续接 |
| change_reason | `{kind, text, evidence_ids}`。kind 为 `initial/condition/user_instruction/new_event/continuation/discipline`；text 非空，引用 ID 必须存在于本次 evidence。discipline 用于实际纪律触发的防御动作，须本轮新 observation 证据，不要求伪造事件或原条件 ID |
| invalidation | 预写条件列表 `{id,text}`。同时可登记继续观察/持有、进入或退出的改判条件；文字须明确适用动作。工具不发明条件，不赋权重，不保证条件有效 |
| triggered_conditions | 本次已成立条件的 ID 列表，只能引用上一条已写的条件，不得用今天新增条件证明今天应改判 |
| discipline | 唯一真实阈值仍读纪律卡，此处只记录 1/2/3 的 `clear/triggered/unknown` 状态与证据，不保存第二套阈值 |
| user_execution | 默认 `{status:"unconfirmed"}`；确认后 `{status:"confirmed",source:"用户报告/截图定位",available_at:"带时区时间"}`，不得虚构。shadow 禁止 confirmed |
| confidence | `low/medium/high/unknown`；定性，不是校准过的获利概率 |
| key_data_status | `complete/missing`，调用者必须如实判断关键数据是否齐备；工具不能自动判断源内容真假或 completeness |
| research_target（可选） | 仅 shadow，有限数值 `[0,1]`，是标准化研究投入，**不是用户实际仓位或建议仓位**。enter 必须 >0，exit 必须 =0，hold 等于此前最近研究目标，reduce 小于此前最近研究目标；hold/reduce 无先前研究目标则拒绝。observe 不得带此字段，不视为研究清仓；研究模拟器另核 decision_id、录入时间与目标一致 |
| research_asset（与 target 成对） | 六位股票/ETF 代码字符串，例如 `111111`；有 research_target 必须有 asset，无 target 不得单独写 asset。同 theme_id + mode 链必须沿用前一个研究资产，不能在原持有链悄悄换载体；新载体用新的主题 episode ID，并在 thesis/source 关联原主题。工具只验代码格式，不联网核实资产存在 |

## 跨日延续及缺数处理

1. 每次先 status，按主题和模式找到最新记录；在 previous_decision_id 续接。不要因日期刷新而重置论点和条件。
2. action、thesis、theme_type、method_version 或 invalidation 有实质变化，须声明：原预写条件触发并有新证据；或有来源的用户指示；或有来源的新事件。`enter/reduce→hold`、`exit→observe` 在论点/方法/条件与研究目标均未变时可用 continuation，不要求新交易理由。`new_event` 必须引用 kind=event 的新证据；不能把重复旧新闻换个 ID 当新事件。纪律触发单独使用 discipline，见第 6 条。
3. “新证据”要求该主题模式内新 ID，且 available_at 晚于上一条 decided_at；已有 ID 不能改 source、时间或类型。证据确实是否新发生仍需人工/AI 源文核查，校验器只检验申报结构。
4. `hold` 必须有完整关键数据和新证据，不能仅复制旧结论。资料不足可转 `observe` + key_data_status=missing + 原因，即使没有新事件也允许，但不能顺便更换 thesis/方法/类型/条件；这表示**无法形成持有安全判断**，不代表卖出指令或自动退仓。①②硬纪律已触发时仍要求 exit，缺数不绕过纪律。
5. `enter` 必须关键数据齐全、主题类型已知、纪律三项全 clear、有预写条件和新证据。未知或③触发不得 enter。即使初次 enter 通过结构检查，也绝不代表获得验证或真实进场授权。
6. ①②任一 triggered 必须 exit。可用 `change_reason.kind=discipline`，引用本轮新的 observation 证据，triggered_conditions 可留空：纪律本来就生效，不要求在前条账本重复注册，更不应伪造 new_event。③ triggered 可同型用 discipline 给 observe/reduce/exit；不能 enter。此 reason 不允许同时更换 thesis/方法/类型/条件。保留用户既有个案须显式引用 override，不能由 AI 新造豁免；override 不允许任何纪律非 clear 时 enter。
7. 每个新条件使用新 ID，不改变既有条件 ID 的文字。历史建议撤销/纠错以新的 decision_id 追加，说明改判依据并保留 previous_decision_id；不删除旧条目。若误录造成整个账本校验失败，工具会拒绝继续追加，需保留原件并由有授权者审阅修复，不能自动“清洗”历史。

可选个案引用示例（evidence 里必须存在该用户指示）：

```json
{
  "override": {
    "source": "user-instruction-20260818-01",
    "available_at": "2026-08-18T18:00:00+08:00",
    "conditions": ["2"]
  }
}
```

source 是用户指示 evidence 的 ID，时间须与其 available_at 相同。只有显式列出的纪律被该记录的个案引用覆盖；这不是修改纪律卡。

## 日报读取与验收

status 输出 `latest`，每个主题每种 mode 一项：数据日、行动、论点、方法版本、预写条件、纪律状态、关键数据完整性、定性置信度、执行确认与原因，以及系统 recorded_at、evaluation_effective_at 和可选 research_target/research_asset。日报只能把 live_advice 作为已登记的最新建议；shadow 是模拟，historical_audit 是事后审计。检查日期，旧记录只作前序，不能伪装今日数据；空列表仅表示尚未记录。

独立基线与 AI 方法各用独立影子 JSONL；可有相同 theme_id，但各文件只锁定自己的 method_version。比较器须检查同资产、同锁定方法和完整的研究信号集合，不允许从账本挑选有利交易；此账本不承担收益评价。hold 是继续持有，不是每天把浮动占比重新交易回 research_target。

```sh
python3 -m unittest discover -s tests -p test_decision_journal.py -v
```

测试包含真实多进程并发、幂等/冲突、跨日续接、跨主题/模式隔离、事后条件、硬纪律、个案来源、未知关键数据、时间与未来证据、旧日伪前瞻拒绝、系统录入时间、研究目标与行动一致性、执行确认、哈希与残缺行、只读命令不创建路径。收益优势必须另做预注册的历史诊断和前瞻模拟验证，此 CLI 通过不等于策略通过。
