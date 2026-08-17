# 变更日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。首个公开版本仍为 beta，接口和工作流可能在用户反馈后调整。

## [0.3.0-beta.1] - 2026-08-18

主题是主线判定结构：退潮从「单钥匙机械触发」改为「双钥匙 + 警戒态 + 回头验证」。

### 新增

- `警戒` 阶段与双钥匙清仓级死亡条件：资金键单独成立只冻结加仓并给出上移止损建议，不联动清仓纪律；预注册的纯价格条件仍可单键触发。
- `stock-daily/scripts/mainline_validation.py`：退潮判定真值表、提名影子观察、次日证伪与板块历史回放；缺数据返回 `unverified` 而非静默判「未触发」。
- 随仓库发布的结构化行为案例与双平台回放，纳入离线发布门。
- `stock-daily/scripts/board_fund_flow_cache.py`（本仓库自有代码）：板块逐日主力资金与收盘点位本地缓存，支撑 MA20 价格键、多日条件回溯、立项误报回测与 T+N 回填。
- 退潮复核判据预注册、退潮判定 T+N 台账与复活哨；vault 模板与契约测试同步承载。
- 正式提名的四项 `shadow_only` 观察与「A 股执行链未验证」高风险标注。
- 立项误报回测成为强制项；载体排序 T+N 跟踪把 Spearman 秩相关列为固定指标。

### 变更

- 主线生命周期由六段扩为七段：提名 → 启动 → 发酵 → 分歧 → 警戒 → 退潮 → 归档。
- 主线页 frontmatter 新增 `判定规则版本`；无该字段的旧页按 `legacy_any` 与页面原文执行，系统不代为升级或降级。
- 死亡条件写法卫生：极端值必须板块内标准化，禁止裸用全市场绝对额排名；锚点立项日取值必须与后续判定同源同接口。
- 板块逐日史统一以本地缓存为事实源，必须用判定交易日锁定末日；覆盖不足写「本项未验证」，不默认判「未触发」。

### 兼容性

- 不迁移、不覆写任何用户 vault 内容；新增区块只影响新建主线页。
- 升级仍只替换三个 Skill，用户 config 与 vault 永不进入覆盖范围。

## [0.2.0-beta.1] - 2026-08-14

### 新增

- 公开 canonical source `src/skills/` 与双平台逐字节再生检查。
- 统一版本事实源、安全升级/回滚安装器和只读环境 doctor。
- 升级与回滚文档、安装反馈/功能建议 Issue 表单、macOS 安装 smoke。

### 变更

- Git tag、插件、marketplace、冷启动与发行归档统一使用完整语义版本。
- 公开构建不再依赖作者私人 Skill 仓库或私人 Git revision。
- 用户 config 与 vault 明确采用永不自动覆盖策略。

## [0.1.0-beta.1] - 2026-08-13

### 新增

- 同时支持 Claude Code 与 Codex 的 `stock-daily`、`stock-screener`、`stock-buddy` 三个 Skill。
- 主线生命周期、死亡条件预注册、退潮独立复核和纪律—AI 建议—用户执行三层合同。
- 脱敏空 vault、示例配置、Claude/Codex 安装器和外部 `a-stock-data` 固定版本安装器。
- 双平台 marketplace 元数据、中文主 README 与英文 README。
- 确定性导出脚本：从固定的私人业务源 revision 按白名单生成两个公开平台树。
- 可重复冷启动测试、发布前隐私/凭据审计、Python 3.11/3.12 离线 CI。
- 每个工作日一次的非阻塞在线健康检查，验证固定上游和免费行情主干。

### 安全与合规

- 所有私人绝对路径、真实账户资料、历史运行日志、私人配置和未知许可证 helper 均排除在发行树外。
- 自有代码与文档采用 MIT；`a-stock-data` 作为 Apache-2.0 外部依赖，不复制、不修改、不再分发其源码。
- README、三个 Skill 的固定输出和脚本化 JSON/HTML 输出统一携带研究边界、非投顾、不构成投资建议、不代下单、风险自担及运行时数据口径。

[0.3.0-beta.1]: https://github.com/Taosheng777/a-share-mainline-os/releases/tag/v0.3.0-beta.1
[0.2.0-beta.1]: https://github.com/Taosheng777/a-share-mainline-os/releases/tag/v0.2.0-beta.1
[0.1.0-beta.1]: https://github.com/Taosheng777/a-share-mainline-os/releases/tag/v0.1-beta
