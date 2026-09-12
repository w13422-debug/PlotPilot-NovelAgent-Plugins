# PlotPilot Novel-Agent Plugins

这是一个为 **PlotPilot-Pluginized** 开发的 Novel-Agent 插件化适配项目。

本项目以我自己原有的 Novel-Agent 软件为能力来源，在保留原有小说创作、拆书、分析和写作能力的基础上，将功能拆分为一组可独立安装、独立运行、独立升级和独立验收的插件，适配到另一个软件：[PlotPilot-Pluginized](https://github.com/w13422-debug/PlotPilot-Pluginized)。

对应的插件化仓库地址：

<https://github.com/w13422-debug/PlotPilot-NovelAgent-Plugins>

> 当前项目仍处于持续开发阶段。已经完成的插件可以作为开发和集成基线；NAP-06 及后续阶段尚未全部完成，不能把当前仓库理解为最终完整版发布包。

## 项目关系

```text
Novel-Agent（原有软件）
        │
        │ 能力提取、边界重构、协议适配
        ▼
PlotPilot-NovelAgent-Plugins（本仓库）
        │
        │ 独立插件协议、Data Plugin、Skill、Host API
        ▼
PlotPilot-Pluginized（承载插件的目标软件）
```

- **Novel-Agent**：原始功能和算法能力的来源。
- **PlotPilot-NovelAgent-Plugins**：本仓库，负责把 Novel-Agent 的能力拆分、封装并适配为插件。
- **PlotPilot-Pluginized**：目标宿主软件，负责提供 Core、Host API、运行时和用户界面。

本仓库不是另一个完整桌面软件，也不是把所有功能重新合并成一个“大插件”。最终交付目标是多个具有独立身份和版本的 Code Plugin、Data Plugin 以及 Skill。

## 开发思路

### 1. 能力拆分，而不是整体搬运

先从原 Novel-Agent 中识别可复用能力，再按职责拆分为来源处理、供体分析、叙事分析、角色资产、文风套件、质量校验和写作工作流等插件。插件之间不直接互相调用，通过 PlotPilot-Pluginized 提供的 Host 能力、不可变 Asset、Result Bundle 和标准协议进行组合。

### 2. 保留 PlotPilot Core 的权威性

插件不创建第二套正文、Revision、Asset、Candidate、Publication、事实或伏笔权威。插件只读取 Host 提供的上下文和 Asset，并返回诊断结果、候选内容或投影结果，由 PlotPilot Core 决定是否接受和发布。

### 3. Code、Data、Skill 分离

- **Code Plugin**：提供实际运行能力。
- **Data Plugin**：提供规则、词典、模板、文风包等可版本化数据。
- **Skill**：提供提示词、方法和写作操作定义。

三者保持独立身份、独立版本和独立哈希，便于替换、回滚、审计和复用。

### 4. 每个插件独立验收

每个插件都需要具备自己的 manifest、descriptor、schema、runtime、package identity、rebuild 逻辑和测试。只有通过源码检查、定向测试和独立 Sol 审计后，才允许由 NAP-00 集成。

## 已完成的开发工作

当前 NAP-00 接受的集成基线为：

```text
192584a3b458ec0bbc4a1b10385217ae7688cc24
```

已完成并集成的 Code Plugin 共 12 个：

### B0：模型供应商

- `com.plotpilot.novelagent.provider-anthropic`
- `com.plotpilot.novelagent.provider-gemini`

### B1：来源导入与清洗

- `com.plotpilot.novelagent.source-import`
- `com.plotpilot.novelagent.source-cleaning-runtime`
- `com.plotpilot.novelagent.source-structure`

覆盖 TXT/EPUB 导入、清洗预览与应用、规则合并、结构处理、证据搜索和重绑定。

### B2：供体分析与原子拆书

- `com.plotpilot.novelagent.donor-analysis`

覆盖书籍 Atom 提取、人工 Atom、Claim 生成和重新审查。

### B3：叙事与大纲

- `com.plotpilot.novelagent.narrative-analysis`
- `com.plotpilot.novelagent.outline-projection`

覆盖叙事单元、叙事计划、叙事综合和树状/卡片/时间线等大纲投影。

### B3：角色与资产

- `com.plotpilot.novelagent.character-distillation`
- `com.plotpilot.novelagent.asset-derivation`

覆盖角色 Atom、角色卡、角色冲突审查、冲突应用和可复用资产派生。

### B3：文风套件

- `com.plotpilot.novelagent.style-manufacturing`
- `com.plotpilot.novelagent.style-runtime`

覆盖文风制造、资格审查、文风包、文风应用、文风审查和文风优化。

另外已经集成：

- 7 个独立 Data Plugin
- 10 个独立 Skill
- PlotPilot Plugin SDK、标准 Schema、Result Bundle、Checkpoint、Provenance 和重建验证工具

## 已完成的验证

B3 集成基线已经通过：

- 四域插件测试：`236 passed`
- E2E 测试：`37 passed`
- TypeScript 类型检查：通过
- 跟踪 Python 源码编译：通过
- `git diff --check`：通过
- Fresh Sol 最终独立审计：`PASS`
- Open Findings：`0`

## 尚未完成的工作

### B4：NAP-06 Quality-Canon

尚未完成的四个插件：

- `com.plotpilot.novelagent.consistency-audit`
- `com.plotpilot.novelagent.impact-repair`
- `com.plotpilot.novelagent.branch-canon`
- `com.plotpilot.novelagent.story-state`

同时尚未完成：

- Quality Rubric Data Plugin
- Quality Consistency Skill
- `tests/quality/**` 全套测试
- NAP-06 的最终 Sol 审计和 NAP-00 集成

当前 NAP-06 工作树中仍有早期未提交内容，不能视为正式完成，也不能把其中的模板文件当作已发布插件。

### B4：NAP-07 Writing-Skills

计划开发：

- 写作上下文
- 章节工作流
- Skill 推荐
- 草稿、续写、改写、润色和流式检查点

### B5：NAP-08 Exchange-Projection

计划开发：

- 分析导入/导出
- Markdown/ZIP 交换格式
- 知识投影
- 可移植组合配置

### B6：最终交付

计划进行：

- 浏览器端 E2E
- 恢复与故障测试
- 性能验证
- 新鲜环境重建
- 全部插件的最终交付审计

## 仓库结构

```text
plugins/       独立 Code Plugin
data/          独立 Data Plugin
skills/        独立 Skill
contracts/     PlotPilot Plugin 公共协议
sdk/           Python/TypeScript SDK
catalog/       插件目录和示例包
tests/         各插件及集成测试
governance/    冻结设计、施工矩阵和验收记录
coordination/  各项目施工与集成记录
```

施工组织采用一个 Git monorepo、九个隔离 Worktree 和九个 Codex Project；NAP-00 是唯一集成者，但这不改变最终插件的独立身份。

## 参与交流

这是一个持续开发和实践中的项目。如果你对小说创作软件、Novel-Agent、AI 写作工作流或插件化架构感兴趣，欢迎加入 QQ 群交流：

**QQ群：663844122**

## 许可证与第三方声明

请同时阅读：

- [`LICENSE`](./LICENSE)
- [`NOTICE`](./NOTICE)
- [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)

本项目中的插件、依赖、示例包和第三方组件应分别遵循其对应的许可证和使用条件。
