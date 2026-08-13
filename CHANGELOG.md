# 变更日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。首个公开版本仍为 beta，接口和工作流可能在用户反馈后调整。

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

[0.1.0-beta.1]: https://github.com/Taosheng777/a-share-mainline-os/releases/tag/v0.1-beta
