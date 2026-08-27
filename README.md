# PlotPilot Novel-Agent Plugins

Novel-Agent 功能吸收的独立插件单仓库。PlotPilot Core 与 Novel-Agent donor 均为只读外部基线，不在本仓库混写。

- 架构：一个 Git monorepo、九个隔离 Worktree、九个 Codex Project。
- 唯一集成者：NAP-00。
- 施工顺序：B0 → B1 → B2 → B3（NAP-03/04/05 并发）→ B4（NAP-06/07 并发）→ B5 → B6。
- Core 接受基线：`1b352be671e70a2ce443b10499060982e93d64b7`。
- 正式设计与机器矩阵：`governance/frozen-design-v1/`。

当前阶段只启动 NAP-00 B0；其余项目在各自依赖门通过前保持停止。
