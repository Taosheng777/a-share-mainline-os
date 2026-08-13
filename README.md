# A 股主线研究与决策支持系统

[![CI](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/ci.yml/badge.svg)](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/ci.yml)
[![Live smoke](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/live-smoke.yml/badge.svg)](https://github.com/Taosheng777/a-share-mainline-os/actions/workflows/live-smoke.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README_EN.md)

`a-share-mainline-os` 是面向 Claude Code 与 Codex 的 A 股研究 Skill 套件。它围绕“主线”组织复盘、载体筛选和持仓分析，把纪律事实、AI 建议与用户执行分开，并保留可审计的数据来源、数据日、反方理由和改判条件。

> 本项目仅用于研究与决策支持；提供方非持牌证券投资咨询机构，本项目及其输出不构成投资建议，不代下单，使用者风险自担。数据来源与数据日以每次运行输出为准。

## 三个 Skill

| Skill | 做什么 | 不做什么 |
|---|---|---|
| `stock-daily` | 最新交易日五区复盘、环境判定、主线状态与持仓纪律对照 | 不把机械候选池冒充正式提名，不补写断更日记 |
| `stock-screener` | 为已立项主线筛选 ETF 与龙头股，做相关度校验和四维排序 | 不替代账户适配与买卖判断 |
| `stock-buddy` | 主线答辩、体检、退潮复核、持仓分析；显式开启时运行五维专家模式 | 不代下单，不在输入不足时硬造目标区间或个性化仓位 |

## 核心设计

- **主线生命周期与退潮复核**：提名、启动、发酵、分歧、退潮、归档全程保留证据链；机械触发后仍有独立复核闸门。
- **三层决策合同**：纪律层只读用户事实；AI 建议层给明确倾向、依据、反方与改判条件；用户执行层由用户确认并自行交易。
- **防锚定接力**：机械候选池与正式提名分角色生成；后段先独立扫描，再看候选池取并集，避免候选先验锁死判断。
- **多源降级与宽度对账**：低成本通道优先，单源失败只降级；涨跌家数要求写入源与影子源对账，冲突不得静默吸收。
- **死亡条件预注册**：立项前写明可判定条件、标的或指数代码、基线点位和基线数据日，禁止事后追着行情改规则。
- **断更零债务**：恢复运行时只处理最新有效交易日与当前事实，不伪造中断期间的日报。

## 五分钟上手

### 1. Clone 并安装

```bash
git clone https://github.com/Taosheng777/a-share-mainline-os.git
cd a-share-mainline-os
```

Codex marketplace：

```bash
codex plugin marketplace add Taosheng777/a-share-mainline-os
codex plugin add a-share-mainline-os@a-share-mainline-os
```

Claude Code marketplace：

```text
/plugin marketplace add Taosheng777/a-share-mainline-os
/plugin install a-share-mainline-os@a-share-mainline-os
```

也可用已测试的本地安装器：

```bash
bash adapters/codex/install.sh   # 或 bash adapters/claude/install.sh
```

安装器默认拒绝覆盖同名 Skill。确需升级时，先确认自动备份位置，再设置 `ASM_FORCE_INSTALL=1`。

### 2. 建立空 vault 与配置

```bash
cp -R vault-template "$HOME/a-share-mainline-vault"
mkdir -p "$HOME/.config/a-share-mainline"
cp config/config.example.json "$HOME/.config/a-share-mainline/config.json"
```

把配置中的 `__VAULT_ROOT__` 改为刚复制的绝对路径，然后打开 `01-纪律卡.md` 自行填写三条线。阈值为空时系统会明确写“不可判定”，不会从旧记录或模型记忆猜补。

### 3. 安装免费数据 Skill

```bash
python3 adapters/shared/install_a_stock_data.py \
  --target "$HOME/.codex/skills/a-stock-data"
```

Claude Code 用户把目标改为 `$HOME/.claude/skills/a-stock-data`。安装器固定并校验上游 `v3.6.1` 的 commit、`SKILL.md` 指纹和 Apache-2.0 许可证；本仓库不复制或重新许可上游源码。

### 4. 开始使用

在新任务中说：

- `执行最新交易日复盘`
- `为我已立项的主线筛选 ETF 和龙头股`
- `用 stock-buddy 体检这条主线`

更完整的逐步说明见 [五分钟上手](docs/五分钟上手.md)。本地可重复冷启动检查：

```bash
python3 scripts/cold_start.py
```

该脚本在临时 HOME 中验证 Codex marketplace、安装器、空 vault、复盘前置读取和离线载体筛选，不读取现有用户目录。

## 数据源与降级

- `a-stock-data`：默认免费外部依赖，覆盖腾讯、东财等公开数据通道。
- `hithink-finance`：可选的结构化数据层，由用户自行安装、认证并遵守服务条款。
- 问财 `hithink-market-query`：可选适配器，用于正面清单内查询；凭据只从本机环境读取。
- iFinD helper、`news-search`、`report-search` 等：不随发行包提供；只有用户自行安装并显式配置时才启用。

任一可选组件不可用时必须在输出中明示降级，不得猜作者目录、复用过期数据或编造字段。详见 [配置与可选数据源](docs/配置与可选数据源.md)和[系统设计](docs/系统设计.md)。

## 仓库结构

```text
.agents/                         Codex marketplace
.claude-plugin/                  Claude Code marketplace
plugins/
  a-share-mainline-os-codex/     Codex 插件根与 skills/
  a-share-mainline-os-claude/    Claude Code 插件根与 skills/
adapters/                        安装器与外部数据薄适配
vault-template/                  脱敏空壳与契约测试
docs/                            设计、生命周期、配置与许可证说明
examples/                        纯虚构输出样例
scripts/                         冷启动、固定源导出与发行构建
tests/                           产品发布契约
```

两个平台版本由同一个已提交业务源 revision 和同一份白名单确定性导出；平台工具名只在导出适配层变化。

## 许可证与第三方边界

本仓库自有代码与文档采用 [MIT License](LICENSE)。数据能力通过 Simon Lin 的 [simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) 接入；该项目采用 Apache-2.0，本仓库不复制、不修改、不再分发其源码。完整归属和审计快照见 [NOTICE](NOTICE) 与 [依赖许可证清单](docs/依赖许可证清单.md)。

本项目不附带行情、公告、研报、新闻、账户凭据、API Key 或私人数据。公开网页接口并不等于获得官方 API 或数据再分发授权；接口可能变更、限流或失效。使用者须自行遵守各服务条款、授权范围和频率限制。

## 参与贡献

提交问题时请附复现命令、脱敏输入、实际输出和数据日，不要上传 API Key、券商截图或账户明细。贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题见 [SECURITY.md](SECURITY.md)。

离线发布门可用 `python3 scripts/run_ci.py` 一次复现。定时 `Live smoke` 只读探测固定的 `a-stock-data` 上游 revision 与腾讯免费行情主干；失败会保留日志并开 Issue，但不阻断离线 CI 或发布。
