# NAP 单仓库强制协议

1. 开工前读取 `governance/frozen-design-v1/construction-plan-9-projects-v1.json`、当前 Worktree 的 `AGENTS.override.md` 与 `.codex/active-work.json`。
2. 只写所属项目在冻结施工矩阵中的路径前缀；未知公共字段提交 Contract Delta，不得由业务项目修改 `contracts/**`、`sdk/**` 或 `catalog/**`。
3. NAP-00 是唯一集成者，也是 `contracts/**`、`sdk/**`、`catalog/**`、`tests/contracts/**`、`tests/providers/**`、`tests/e2e/**`、`docs/deliveries/**`、`coordination/NAP-00/**` 与 `coordination/integration/**` 的唯一写者。
4. 禁止修改 `C:\Users\Administrator\Desktop\写作资料汇总\PlotPilot-Pluginized` 与 `C:\Users\Administrator\Desktop\Novel-Agent- (2)\novel-agent`；只能读取冻结提交或证据。
5. 禁止插件直连 Core 数据库、创建第二正文/Revision/Asset/Candidate/Publication 权威、插件间直接 import、动态新增路由、桌面/EXE/安装包构建。
6. Luna 可实现和测试；Terra 只读搜证；只有 Sol 可以审查、关闭 Finding、授予 merge eligibility。只有精确 Sol-PASS source HEAD 可由 NAP-00 `--no-ff` 集成。
7. 开发过程只运行受影响源码检查、单元/集成测试和必要前端构建；最终 B6 前不执行 Windows 最终产物构建或 GUI smoke。
8. 不得 reset/clean/checkout 覆盖其他工作；检测到未知脏项先停止并记录。
9. 所有 package、Skill、Data、RunSnapshot、receipt 与 hash 语义以冻结设计和接受的 PlotPilot 合同为准；不得从旧 donor UI 或 SQLite 复制权威模型。
