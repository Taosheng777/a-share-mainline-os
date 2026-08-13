# 参与贡献

欢迎提交已脱敏的问题和改进。开始前请先运行：

```bash
python3 -u scripts/run_ci.py
```

提交内容必须满足以下边界：

- 不含 API Key、cookie、券商截图、持仓明细、真实纪律阈值或本机绝对路径。
- 行情数字必须带数据来源与数据日；取不到时明确写缺口，不补猜。
- 不把付费或许可证未知的外部组件源码复制进仓库。
- 平台无关业务规则先进入固定源，再通过 `scripts/export_from_source.py` 导出；只属于 Claude Code 或 Codex 的工具名留在窄适配层。
- 核心逻辑改动须带回归测试，并保持 Claude/Codex 两个平台的离线套件全绿。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中披露。
