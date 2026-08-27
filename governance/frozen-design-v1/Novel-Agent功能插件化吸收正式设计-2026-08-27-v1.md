# Novel-Agent 功能插件化吸收正式设计

- 文档版本：v1.0-rc1（一次受限修订）
- 日期：2026-08-27
- 设计状态：已按冻结 `NA-DESIGN-F-001`～`NA-DESIGN-F-011` 完成一次受限修订，等待同一位 Sol 定向复核
- 供体：`C:\Users\Administrator\Desktop\Novel-Agent- (2)\novel-agent`
- 供体分支：`codex/permanent-webui-f5-f6-integration`
- 供体 HEAD：`a2666dd722331c576445f57d4687922b19fd737c`
- 目标：`C:\Users\Administrator\Desktop\写作资料汇总\PlotPilot-Pluginized`
- 目标规范：`C:\Users\Administrator\Desktop\交接文档\PlotPilot-Pluginized正式设计规划-2026-08-25-v1.md` v1.2
- 目标规范 SHA-256：`e70f450b75cfa753148cf620d13855d0073f7d61294e7599ea9cc98f3e612b7b`
- 当前阶段：只做设计，不修改 Novel-Agent 产品源码，不修改 PlotPilot-Pluginized Core 或插件源码

---

## 1. 最终结论

Novel-Agent 不作为第二个应用继续演进，也不把它的后端、数据库、WebUI 或桌面壳嵌入
PlotPilot-Pluginized。它作为**业务能力供体**，按以下四种发布物吸收：

1. **Code Plugin**：导入解析、清洗执行、拆书、人物蒸馏、文风制造、文风应用、
   一致性、影响分析、受控修复、写作上下文等可执行能力。
2. **Data Plugin**：清洗规则、拆书分类表、文风包、剧情结构模板、人物原型、
   世界规则、质量量表和词典。
3. **Skill**：原子拆书方法、剧情单元、章/卷/书纲、人物线、伏笔线、文风分析与应用，
   以及战斗、心理、对话、钩子、节奏等可持续升级的写作方法。
4. **Plugin Plan/组合方案**：严格使用目标 v1.2 的 closed `plugin-plan/v1`，完整保存稳定 binding ID、exact release、`required`、`propagate_cancel`、参数 Asset、Data bundle 与 exact interpreter、Skill Preset、model profile、完整 synthesizer binding 和 UI defaults；方案不携带执行代码，不自动推荐或切换。

所有正式正文、原作规范文本、结构、人物/世界/伏笔事实、Revision、Asset、Candidate、
Publication、Job、Event 继续只由 PlotPilot Core 掌权。Novel-Agent 的相同基础设施不迁移。

### 1.1 不直接升级 Core 的原则

本设计没有授权修改 PlotPilot-Pluginized Core。插件合同确实无法承载的能力，全部进入
独立《Core 升级候选》；只有用户逐项批准后，才进入 Core 设计或施工。供体功能较强不等于
自动并入 Core。

### 1.2 与 PlotPilot 当前施工的关系

- 当前 P0—P6 继续完成 PlotPilot v4.6.0 功能插件化收口，优先级不变。
- 本设计属于 M8+ 的并行设计，不进入当前 P0 脏工作树，也不阻塞 P1—P6 集成。
- Novel-Agent 插件施工最早只能从 **P0 公共 Workspace/Document/Node/Revision/Asset/
  Candidate/Publication 合同通过 Sol 并形成干净集成 HEAD** 后开始。
- 本设计使用目标 v1.2 规范；若 P0 最终发布合同发生实质变化，必须先做 Contract Delta，
  不得让插件自行猜测 Core API。

---

## 2. 设计边界

### 2.1 必须保留

- 导入只支持 TXT、EPUB、复制文本三种入口。
- raw 输入不可变；清洗后的 canonical text 是后续坐标权威。
- 清洗规则可独立安装、启停、排序、组合、合并、更新、删除。
- 阈值由用户在软件中控制，不写死在规则包。
- 同一本书、同一能力只有一个正式 current Revision；历史 Revision 可回查。
- 新运行失败不替换旧 current；新 Candidate 被用户接受后才 Publication。
- Layer A Atom 必须绑定逐字证据；Layer B Claim 必须反链 current accepted Atom。
- 人物线、关系、发展、冲突、伏笔线、剧情单元、章/卷/书纲、总纲和整书综合。
- 文风拆解包含视角、人称、时态、词语习惯、句式、段落、断句、章节节奏、情绪曲线、
  章节结尾，以及动作/心理/景物比例。
- 多书组合：A 书剧情、B 书人物、C 书文笔可以在同一写作方案中并存。
- 多插件默认独立产生带来源结果；用户可比较、选择，或显式调用综合插件。
- Skill、Data Plugin、Code Plugin 和方案均保留历史 release/revision，可在软件中选择。
- 第一方随软件提供的包与普通包同权：可关闭、可删除；删除后没有“恢复内置插件”按钮。

### 2.2 明确不迁移

- Novel-Agent 自有 SQLite 事实库和 46 轮 migration。
- Novel-Agent 自有 Candidate、Job、Event、Gateway、API Key、备份、Capability Registry 权威。
- FastAPI 路由外形、旧 DTO、旧 Mock API、旧 Pinia 状态和旧页面布局。
- 桌面/Tauri/EXE/安装器、旧启动器和桌面兼容层。
- 动态一级导航、插件直连、插件直接读写 Core DB、插件直写正文或 Bible。
- 供体的 `.naplugin` 通用运行时假设；它当前只是 Core operation/UI projection 的声明包，
  不是 PlotPilot 所需的独立 Python Worker 平台。

### 2.3 延期或不宣称已有

- 未找到成熟的自由 Canvas/图谱编辑器；只吸收树、表、时间线、关系投影数据能力。
- 未找到可证明的“继续生成”完整纵切；施工时作为新 capability，不声称直接搬运即完成。
- 人物绑定已有存储和 UI，但不等于所有写作阶段都真实消费；必须做运行时 E2E。
- 分支已有 fork，没有 merge；本轮不设计自动 merge。
- Obsidian 直接写盘依赖尚未批准的 Core 外部目录 Broker；缺少 Broker 时降级为 Markdown Asset。

---

## 3. 目标 Core 语义

### 3.1 五个关键对象

| 对象 | 作用 | 插件权限 |
|---|---|---|
| Document | 稳定文档身份，如原作正文、总纲、人物卡、章节正文 | 只能引用或提出 Candidate |
| Revision | Document 的不可变版本；current pointer 才是正式真相 | 不能原地改写 |
| Asset | 大文本、JSON、报告、包、截图等不可变内容 | 经 Host API 读写，不能拿路径句柄 |
| Candidate | 插件、Skill 或 AI 提出的待审核变更 | 可生成，不能自行接受 |
| Publication | Core 对 Candidate 执行 CAS 后产生正式 Revision/关系 | 只能由 Core 原生按钮触发 |

`Workspace` 是可独立打开、备份和授权的空间；写作项目用 `WritingProject`，原作研究用
`SourceWorkspace`。两者共享同一 Core，不建立第二套“拆书数据库”。

### 3.2 单一正式 current

对 `(source_workspace_id, source_book_id, capability_family)`：

1. 任意时刻只有一个已发布 current Revision。
2. 重跑产生新的 Job 和 Candidate，不产生第二个正式 current。
3. 旧 current 在新运行期间仍可读，UI 标记“有待审替代结果”。
4. 新 Job 失败、取消或 partial 时，旧 current 不变。
5. 用户接受完整 Candidate 后，Publication 原子切换 current。
6. 局部 scope 运行必须输出完整快照或对 current 的显式 patch，不得另建局部 current。

### 3.3 Generation 术语隔离

PlotPilot 的 `Generation` 严格继承目标 v1.2 的完整 `plugin-generation/v1`，而不是 release ID 的缩减数组。顶层 closed 字段为 `schema,generation_id,core_api_version,members,created_reason,created_at,health_result_asset_id,parent_generation_id|null,base_generation_id|null`；每个 member closed 字段为 `plugin_id,release_id,package_hash,data_generation_id|null,ui_bundle_hash|null,global_settings_revision_id|null,settings_schema_hash|null,data_bundle_asset_id|null`。Generation 创建后不可变，current、LKG 和 qualification 状态不得回写其中。

Generation 不表示一本书有多份正式拆书结果。用户界面不显示“拆书 generation”；拆书只显示 current Revision、历史 Revision、待审 Candidate 和运行记录。

### 3.4 跨书复用

供体结果先发布到 SourceWorkspace 中的 Core Document/Asset。写作项目通过
`source_refs(workspace_id, source_type, source_id, revision_or_hash)` 引用 exact 版本：

- 剧情方案可引用 A 书 narrative synthesis Revision；
- 人物方案可引用 B 书 character card/template Revision；
- 文风方案可绑定由 C 书产出的 exact `style-pack/v1` Data release；
- Skill 仍是通用方法，不绑定某一本书；
- 每条输出 receipt 都保留 exact source revision/package/model/Skill 归因。

---

## 4. 吸收裁决

完整逐项裁决见 `capability-disposition-matrix-v1.json`，文件级证据见
`source-evidence-matrix-v1.json`。裁决口径如下：

| 裁决 | 含义 |
|---|---|
| `thin_adapt` | 算法和业务校验可复用，改成 PlotPilot Worker/Bundle/Receipt 接口 |
| `semantic_port` | 保留行为合同与测试思想，按目标合同重写持久化和编排 |
| `convert_data` | 转成 strict closed-schema Data Plugin |
| `convert_skill` | Prompt/方法转成不可变 Skill release |
| `target_core_only` | 目标 Core 已有权威，不迁移供体实现 |
| `core_candidate` | 插件合同无法承载，进入独立候选，不自动施工 |
| `defer` | 有源码但成熟度或依赖不足，后续单独纵切 |
| `drop` | 壳、旧 UI、兼容/调试或重复基础设施，不进入产品 |

### 4.1 可直接保留算法内核的高价值部分

- TXT 编码判断、EPUB 文本/层级解析、段落与结构识别。
- delete-only 清洗规则、native timeout、空匹配 fail-closed、survivor segment map。
- 四档证据重定位：`unchanged/rebound/needs_rerun/orphaned`。
- Atom/Claim 层级、accepted Atom authority、acceptance ordinal、证据反链。
- Narrative Unit/Plan/Synthesis 业务模型。
- 人物 Atom、人物卡、冲突裁决及可复用人物资产派生。
- 文风 Map/Synthesis/Qualification/Capsule/Review/Refine 流程。
- 一致性、影响分析、逐项受控修复和 Branch Canon preflight。
- Recipe、来源绑定、Skill 推荐、Skill 归因、写作上下文组装。
- Obsidian 投影模板和生命周期语义；直接文件系统写入不复用。

### 4.2 只保留测试思想、不保留实现权威

- SQLite repository、migration、CAS、Job/Attempt、Event/SSE。
- Provider Gateway、密钥存储、模型路由和用量统计。
- Candidate 接受、Publication、Revision current pointer。
- Capability Registry 安装/启停/删除。
- 备份恢复、项目/章节/事实/伏笔持久化。

这些能力由 PlotPilot Core 或其既有第一方插件提供。供体测试应改写成目标合同测试，不能把
供体 repo 类包装后继续当事实源。

---

## 5. Code Plugin 套件

完整机器目录见 `plugin-catalog-v1.json`。目录保留 22 个供体裁决项：21 个 `planned` Code
Plugin（含两个原生 Provider）和 1 个 `not_planned_duplicate` OpenAI-compatible 条目，共冻结
54 个 capability。每个插件可含多个强耦合 capability，但插件之间禁止 import，只能经 Core
Capability Broker 组合。

| Plugin ID | 主要 capability | 输出合同 | 优先级 |
|---|---|---|---|
| `com.plotpilot.novelagent.source-import` | TXT/EPUB/paste 解析、章节树候选 | candidate/artifact | P0 |
| `com.plotpilot.novelagent.source-cleaning-runtime` | 预览、应用、规则包合并 | artifact/candidate | P0 |
| `com.plotpilot.novelagent.source-structure` | 结构修订、搜索、证据重定位 | artifact/candidate | P0 |
| `com.plotpilot.novelagent.donor-analysis` | Atom、手工 Atom、Claim、重审 | candidate/diagnostic | P0 |
| `com.plotpilot.novelagent.narrative-analysis` | 剧情单元、计划、综合 | candidate | P1 |
| `com.plotpilot.novelagent.character-distillation` | 人物 Atom、卡片、关系、冲突 | candidate/diagnostic | P1 |
| `com.plotpilot.novelagent.asset-derivation` | 人物/世界/剧情模板派生与打包 | candidate/artifact | P1 |
| `com.plotpilot.novelagent.style-manufacturing` | 文风蒸馏、盲测资格、打包 | candidate/diagnostic/artifact | P1 |
| `com.plotpilot.novelagent.style-runtime` | 文风应用、审查、精修 | candidate/diagnostic | P1 |
| `com.plotpilot.novelagent.consistency-audit` | 独立一致性审计 | diagnostic | P1 |
| `com.plotpilot.novelagent.impact-repair` | 有界影响分析、受控修复 | diagnostic/candidate | P1 |
| `com.plotpilot.novelagent.branch-canon` | Canon preflight 与变更候选 | diagnostic/candidate | P2 |
| `com.plotpilot.novelagent.story-state` | 章后结算、事实/伏笔派生检查 | candidate/diagnostic | P2 |
| `com.plotpilot.novelagent.writing-context` | 上下文、记忆检索、Recipe 解析 | artifact-only | P1 |
| `com.plotpilot.novelagent.chapter-workflow` | 大纲、章纲、草稿、精修、选区改写 | candidate | P2 |
| `com.plotpilot.novelagent.skill-recommender` | Skill 推荐和归因投影 | artifact/diagnostic | P2 |
| `com.plotpilot.novelagent.analysis-io` | 分析交换导入/导出 | artifact/candidate | P2 |
| `com.plotpilot.novelagent.outline-projection` | 树/卡/时间线/关系投影数据 | artifact | P2 |
| `com.plotpilot.novelagent.knowledge-projection` | Markdown/知识投影 | artifact | P2 |
| `com.plotpilot.novelagent.provider-openai-compatible` | 已由 `com.plotpilot.provider.openai-compatible` 覆盖 | `not_planned_duplicate` | 不施工 |
| `com.plotpilot.novelagent.provider-anthropic` | Anthropic 原生模型 Provider | `artifact-bundle/v1`；stream 仅是 Host 提交行为 | P2 |
| `com.plotpilot.novelagent.provider-gemini` | Gemini 原生模型 Provider | `artifact-bundle/v1`；stream 仅是 Host 提交行为 | P2 |

Provider 裁决已经二分冻结：OpenAI-compatible 明确不施工、不进入任何写集；Anthropic 与 Gemini 为 planned headless provider，由 NAP-00 分别独占 `plugins/provider-anthropic/**` 和 `plugins/provider-gemini/**`。两者 descriptor 固定 input/output schema、`artifact-bundle/v1` result、`run|cancel`、空 Data formats，以及 Asset、Job、checkpoint、stream Host needs；streaming 是 `host.stream.commit/v1` 行为，不是第四种 result contract。

### 5.1 Capability 命名规则

- 采用 `domain.subject.action/v1`，例如 `analysis.book.atom.extract/v1`。
- 每个 capability descriptor 固定 input/output schema、result contract、determinism、支持的
  `run/resume/cancel/validate` 和 accepted Data formats。
- 分析结果修改 Core 正式资料时使用 `candidate-batch/v1`。
- 只读报告、导出包和投影使用 `artifact-bundle/v1`。
- 审计 Findings、资格判定、preflight 使用 `diagnostic-bundle/v1`。
- 一个 capability 不得依据参数临时改变 result contract。

### 5.2 Worker Host 权限

业务 Worker 只申请实际需要的 Host 方法：

- 读输入：`host.asset.read/v1`；
- 写大结果：`host.asset.create/v1` 与 upload status；
- 模型：`host.model.invoke/v1`；
- 子能力：`host.capability.invoke/poll/cancel/v1`；
- 候选：`host.candidate.stage/v1`；
- 恢复：`host.checkpoint.commit/v1`；
- 流式草稿：`host.stream.commit/v1`；
- 进度/等待用户/终态：`host.job.event/await_user/complete/v1`。

不得请求任意文件路径、网络、数据库、router、DOM、其他插件目录或其他插件私库。`host.candidate.stage/v1` 只允许 result profile 中至少存在 `candidate-batch/v1` 的插件申请；artifact/diagnostic-only 插件必须省略。

### 5.3 `plugin-plan/v1` closed 合同

Plan 顶层必需且仅允许：`schema,plan_id,revision,name,description,bindings,data_bindings,skill_preset_revision_id|null,result_mode,synthesizer|null,model_profile_revision_id|null,ui_defaults`。

- 每个运行 binding 必需且仅允许 `binding_id,capability_id,plugin_id,release_requirement,order,enabled,required,propagate_cancel,parameters_asset_id|null`；`binding_id` 在 revision 内稳定唯一，`release_requirement` 是 exact SemVer，`order` 唯一，两个控制字段都是必需 boolean。
- 每个 Data binding 必需且仅允许 `data_binding_id,data_plugin_id,release_requirement,format_id,interpreter_binding_id,order,enabled,parameters_asset_id|null`；必须同时解析 exact Data release/bundle 和接受该 format 的 exact `{plugin_id,capability_id}` interpreter binding。
- Skill 只通过 exact `skill_preset_revision_id` 绑定；模型只通过 exact `model_profile_revision_id` 绑定。
- `result_mode` 只允许 `separate|synthesize`。`synthesize` 时 `synthesizer` 必须是非空 closed `{binding_id,capability_id,plugin_id,release_requirement}` 并唯一指向 enabled binding；`separate` 时必须为 null。
- 每个 UI default 必需且仅允许 `slot,expanded`；它只控制显示投影，不进入 request key。除 UI defaults 外的全部运行字段进入 RunSnapshot。
- Plan 序列化、读取和 JCS 重序列化必须保持规范 bytes；Core 用 current Generation 解析冲突，冲突显式进入 `blocked_release_conflict`，不得静默改版、推荐或切换。

---

## 6. Data Plugin

### 6.1 冻结格式

| format ID | 内容 | 解释器 |
|---|---|---|
| `source-cleaning-rules/v1` | delete-only 正则、scope、target、输入类型、顺序 | source-cleaning-runtime |
| `book-analysis-taxonomy/v1` | Atom 类型、字段、度量、审核 rubric | donor-analysis |
| `style-pack/v1` | 可执行文风约束、禁忌、句段统计、证据与资格 | style-runtime |
| `plot-structure-template/v1` | 剧情结构、节拍、卷章模板 | narrative/chapter workflow |
| `character-archetype/v1` | 人物原型、弧线、冲突维度 | character/writing-context |
| `world-rule-template/v1` | 世界规则与一致性约束 | writing-context/consistency |
| `quality-rubric/v1` | 一致性、张力、节奏、文风漂移量表 | consistency/style review |
| `lexicon/v1` | 词语、惯用表达、禁用词、专名词典 | style/writing/quality |

### 6.2 清洗规则要求

1. 规则包本质是 Data Plugin，不允许 Python、JS、wheel、SQL、远程 URL 或模型调用。
2. 用户可同时绑定一个或多个规则包；只按用户排序执行，不自动重排。
3. 可用“合并规则包”生成新的 `.ppplugin` artifact；合并保存来源 release/hash/order。
4. 合并不修改原包；用户安装新包后自行选择方案。
5. 阈值、最大命中比例、预览数量、是否逐条确认属于用户 settings/Plan 参数，不在包内写死。
6. 所有随软件提供的第一方规则包均是普通 release；可删除，删除后不自动恢复。
7. release 状态只允许 `installed -> retiring -> retired`。retire 必须以同一事务将 installed CAS 为 retiring 并递增 retire epoch；从该事务起禁止所有新 executable pin。
8. executable pin 必须覆盖 current/LKG/pending Generation、enabled Plan binding、非终态或可恢复 Job/Attempt、live worker、install/settings/data transition 和正在使用 package 的 backup；取 pin 与 retire CAS 原子竞争。
9. 只有 pin 清零且未发布 Candidate 已保存完整 publication identity 时才能删除 package/venv/private data；失败保持 retiring 并在启动时对账。物理删除不得删除 tombstone、package hash、RunSnapshot、receipt、Candidate 或正式文档。

### 6.3 文风不是 Skill

- `style-pack/v1` 是可版本化的**风格数据**；
- “如何分析文风”“如何把风格用于草稿”是 Skill；
- 真正执行约束、调用模型、产出 Candidate 的是 style Code Plugin；
- 三者分别版本化并在 RunSnapshot 中冻结，不能合并成一个含糊的“文风插件”。

---

## 7. Skill 套件

建议首版发布以下通用 Skill，全部由 `plotpilot.prompt-skill-runtime` 管理，不绑定某本书：

### 7.1 拆书

- `com.plotpilot.skill.donor.atomic-breakdown`
- `com.plotpilot.skill.donor.claim-synthesis`
- `com.plotpilot.skill.donor.narrative-unit`
- `com.plotpilot.skill.donor.character-line`
- `com.plotpilot.skill.donor.foreshadow-line`
- `com.plotpilot.skill.donor.style-analysis`

### 7.2 大纲

- `com.plotpilot.skill.outline.book`
- `com.plotpilot.skill.outline.volume`
- `com.plotpilot.skill.outline.chapter`
- `com.plotpilot.skill.outline.plot-unit`

### 7.3 写作与复核

- `com.plotpilot.skill.writing.combat`
- `com.plotpilot.skill.writing.psychology`
- `com.plotpilot.skill.writing.dialogue`
- `com.plotpilot.skill.writing.scenery`
- `com.plotpilot.skill.writing.hook`
- `com.plotpilot.skill.writing.pacing`
- `com.plotpilot.skill.writing.refine`
- `com.plotpilot.skill.quality.consistency`
- `com.plotpilot.skill.style.apply`

多 Skill 只按用户 Preset 顺序执行。每步必须产生独立 Skill receipt，并分别表达
`frozen/participated/model_claimed/verified_patch`；模型自称“使用了某 Skill”不能升级为
`verified_patch`。

---

## 8. 原作导入与清洗纵切

### 8.1 输入

固定 UI 只提供：

1. 选择 TXT；
2. 选择 EPUB；
3. 粘贴文本。

浏览器文件对象或粘贴内容先由 Core-owned Intake Broker 物化为 immutable raw Asset。插件 UI
Worker 无 DOM/file picker 权限，不能自行读取本机文件。

### 8.2 流程

```text
Core 文件/粘贴入口
  -> raw Asset
  -> source.import.parse/v1
  -> 解析预览 + 结构 Candidate
  -> source.clean.preview/v1（可选多个 Data release）
  -> 用户调整阈值/例外/规则顺序
  -> source.clean.apply/v1
  -> cleaned canonical Document + Node structure Candidate
  -> Core Publication
  -> canonical Revision 成为全部 EvidenceSpan 的坐标权威
```

清洗确认前可重复预览；确认发布后不原地修改 canonical text。后续改动只能重新导入或创建
新的 Source Revision，并执行证据重定位。

### 8.3 清洗回执

至少冻结：raw Asset/hash、decoder policy、parser release、规则包 release/hash/order、settings
revision、命中清单 hash、例外清单、survivor segment map、canonical text hash、结构 hash、
RunSnapshot、模型回执（通常为空）和 Publication receipt。

### 8.4 失败规则

- 任一规则超时、空匹配、无效 span 或输出 hash 不一致：整个 apply fail-closed，不发布部分文本。
- 99% 失败后 Job 必须可从 checkpoint 恢复或以同一参数新 Attempt 重试。
- 失败不会锁死导入会话，也不会替换已有 canonical Revision。
- `failed` 或 `skipped` result item 永不创建 Candidate；failed Attempt 只能返回 null Bundle 或 `diagnostic-bundle/v1`。事务内曾 staging 的普通 Candidate rows 在失败/reconciliation 时必须废弃并保持不可见，不能靠“失败 Candidate 不可 Publication”替代这一规则。

---

## 9. 证据与拆书纵切

### 9.1 EvidenceSpan

冻结 `evidence-span/v1`：

```json
{
  "schema": "evidence-span/v1",
  "workspace_id": "source-workspace-id",
  "document_id": "source-document-id",
  "revision_id": "cleaned-canonical-revision-id",
  "node_id": "chapter-node-id",
  "start_codepoint": 100,
  "end_codepoint": 128,
  "quote": "逐字原文",
  "quote_hash": "sha256",
  "canonical_text_hash": "sha256"
}
```

- 坐标是对应 canonical Revision **Document-global Unicode scalar value** offset；`start_codepoint` inclusive，`end_codepoint` exclusive。不得使用 UTF-8 byte、UTF-16 code unit、grapheme cluster 或 node-local offset。
- `node_id` 是包含关系约束：该 Node 在同一 Revision 的全局范围必须完整包含 `[start_codepoint,end_codepoint)`。
- `quote` 必须等于 exact scalar slice，不做 trim、NFC/NFKC、换行或标点替换。
- `quote_hash = SHA256(UTF8(quote))`，输出 lowercase hex；`canonical_text_hash` 必须等于该 Core Revision exact canonical content Asset 的内容 SHA-256。
- Core/插件在接受前按相同跨语言公式重算 quote/hash/span、Revision hash 与 Node containment；仅 UI 高亮不能成为证据。

### 9.2 Layer A Atom

一个 Atom 只表达一个最小可判断事实或技法，至少包含：

- `atom_kind`；
- EvidenceSpan 数组；
- 适用主体/章节；
- 可复核的结构化 payload；
- 生成 Skill、模型、插件、RunSnapshot 和来源 Revision。

首版 taxonomy 必须覆盖：

- 描写类型：景色、人物外貌、动作、打斗、心理、环境、物品；
- 叙述方式：白描、细描、意识流、象征、隐喻、对比；
- 比例：动作、心理、景物；
- 节奏与情绪：章节节奏、情绪曲线、高潮/缓冲；
- 语言：词语和惯用表达、视角、人称、时态；
- 结构：章节结尾方式、句式长短、段落长度、断句和标点习惯。

Atom 先成为 Candidate。用户接受后才获得 Core Publication receipt 和单调
`acceptance_ordinal`。拒绝或被替代的 Atom 仍可历史回查，但不得作为新 Claim 的 current
authority。

### 9.3 Layer B Claim

Claim 的有序依据不新增 RunSnapshot 字段，而是写入 immutable `claim-input/v1` parameters Asset。该 closed Asset 至少包含 `schema,source_revision_id,ordered_atoms`；每个 ordered item 必需且仅允许 `ordinal,atom_id,payload_hash,acceptance_ordinal,evidence_spans`，其中 ordinal 从 0 连续。

Core 将该 Asset ID 写入 RunSnapshot `parameters_asset_id`，将 exact Asset SHA-256 写入 `asset_hashes`，并把该 hash 纳入 request key。因此每条依据冻结 current accepted Atom ID、payload hash、acceptance ordinal、EvidenceSpan 和输入顺序，同时保持目标 v1.2 RunSnapshot closed。

缺失、重复、顺序断裂、非 current、未接受、跨错 Source Revision、Asset/hash 不匹配或 payload hash 漂移时，Claim Job fail-closed。Claim 不能只引用一段原文而绕过 Layer A。

### 9.4 Layer C 派生结果

剧情单元、人物卡、人物关系、伏笔线、卷纲、书纲、文风包和综合结论属于派生结果。
它们可以引用 Atom 和 Claim，但必须保留完整反链。综合插件不得把多个来源合成无来源的
“真相”；每个字段保留 source refs 和 child receipt。

### 9.5 接受、替换与“继位”

旧软件“接受并继位”不再是插件自定义按钮，而是 Core 原生 Candidate compare/accept：

1. Core 检查 target/base/write-set；
2. 验证 EvidenceSpan 和 Atom/Claim authority；
3. 以 `publication_operation_key` 幂等 Publication；
4. 创建新 Revision 并切换 current pointer；
5. 保留 predecessor/successor lineage；
6. ACK 丢失重试返回同一 Revision，不重复接受。

### 9.6 重新导入四档

重定位拆成两个固定 capability/result profile：`source.evidence.rebind.inspect/v1` 返回 `diagnostic-bundle/v1` 四档报告，不 staging mutation；`source.evidence.rebind.propose/v1` 只把确定性的 `rebound` successor 写为 `candidate-batch/v1`。

| 分类 | 合法条件 | 动作 |
|---|---|---|
| `unchanged` | 仅限 **same Revision/no-op**，exact 坐标、quote 与 hash 均一致 | 直接沿用，不产生 Candidate |
| `rebound` | 新 Revision 中可确定性映射且 quote/hash 一致 | propose 显式 successor Candidate，用户确认 |
| `needs_rerun` | 新 Revision 中语义可能存在但不能确定性定位 | inspect 报告并重新运行原 capability，不伪造 mutation |
| `orphaned` | 新 Revision 中内容已不存在或无合法目标 | inspect 报告，仅保留历史，不进入 current |

同一本书重新导入不迁移供体 SQLite 数据；它创建新 Source Revision，因此跨 Revision 只允许 `rebound|needs_rerun|orphaned`，绝不允许 `unchanged` 沿用旧证据。

---

## 10. 人物、文风、质量与写作

### 10.1 人物

```text
accepted Atom/Claim
  -> character atoms
  -> character card Candidate
  -> conflict diagnostics / user ruling
  -> character/world/relation Publication
  -> optional reusable character-archetype Data Plugin
```

人物卡至少保存：身份、目标、动机、恐惧、能力、缺陷、关系、冲突、弧线阶段、关键事件、
说话习惯、行为证据和来源。人物绑定只有被 writing-context/chapter-workflow 的 RunSnapshot
冻结并真实读取，才能宣称参与写作。

### 10.2 文风

```text
source refs
  -> style manufacture map/synthesis Candidate
  -> independent qualification diagnostic
  -> user approve
  -> style-pack/v1 Data Plugin
  -> style runtime apply Candidate
  -> independent review diagnostic
  -> optional refinement Candidate
```

制造插件与资格/审查路径必须可由不同 release 或不同模型承担；不得由同一模型自评后自动
通过。资格失败保留报告，不发布 style pack current。

### 10.3 一致性和影响修复

- 一致性审计只生成 diagnostic Bundle，不直接修文。
- 影响分析按章节/事实/人物/伏笔/时间线给出有界影响项和 EvidenceSpan。
- 用户逐项选择“修复、忽略、稍后”；修复插件只对已选项生成 Candidate patch。
- 每章修复单独 Publication；失败项不伪装成功，已发布项不回滚。
- Canon preflight 在文风或章节 Candidate 接受前运行，但最终接受仍由 Core。

### 10.4 写作工作台

Novel-Agent 的 Creation Workbench 行为拆成 capability，而不是复制页面：

- source binding：选择 A/B/C 等 exact 来源 Revision；
- recipe：冻结来源、人物、文风、Skill、模型、结构、质量规则和顺序；
- context assembly：只读 Core current 与 exact cross-workspace refs；
- chapter workflow：章纲、草稿、精修、选区改写均输出 Candidate；
- skill recommendation：只提出建议，用户确认后创建新 Skill Preset Revision；
- attribution：由 Core receipt 计算，不信任前端标签。

“继续生成”单独定义为 `writing.chapter.continue/v1`，必须从 Core durable stream high-water 或
已发布/已接受的 exact base Revision 开始；不复用旧软件未被证明的隐式继续逻辑。

---

## 11. 固定 WebUI

### 11.1 导航

继续使用固定浏览器导航，不允许插件注册 route：

- 全局：`/library`、`/donors`、`/assets`、`/capabilities`、`/tasks`、`/settings`；
- 写作项目：`overview`、`story-bible`、`structure`、`writing`、`review`、`output`。

`/donors` 是 Core 固定页面候选，不复用 Novel-Agent `DonorResearchView.vue`。页面采用单轨状态，
用户可直接进入任意工作区，不做强制向导。

### 11.2 Donor 页面布局

```text
┌─────────────────────────────────────────────────────────────┐
│ 原作库 / 搜索 / 导入 / 当前书 / 当前 canonical Revision     │
├──────────────┬──────────────────────────────┬───────────────┤
│ 书与结构树    │ 导入清洗 | 结构 | 拆书 | 人物 | 文风 | 资产 │
│ current 状态  │ 当前动作、结果、对比、审核                   │ 证据/任务/回执 │
└──────────────┴──────────────────────────────┴───────────────┘
```

中栏是标签工作区，不是一步步锁死的 Wizard。默认只显示当前动作所需信息；模型、hash、release、
RunSnapshot、receipt 等工程字段进入“运行依据”抽屉。

### 11.3 新固定 Slot 候选

| Slot | 用途 |
|---|---|
| `donors.import.panel` | 导入解析与清洗配置；文件/粘贴按钮仍由 Core 渲染 |
| `donors.structure.panel` | 结构预览、修订和四档重定位 |
| `donors.analysis.configure` | 选择拆解深度、Skill、模型、scope 和方案 |
| `donors.analysis.review` | Atom/Claim/剧情/人物/文风结果投影 |
| `donors.evidence.inspector` | 原文 span、反链和 stale/rebind 状态 |
| `donors.asset.derivation` | 生成可复用资产/Data Plugin |
| `donors.history.detail` | Revision、Job、RunSnapshot、receipt 历史 |

已有 Slot 继续用于写作侧：`workbench.reference.panel`、`workbench.writing-assets.panel`、
`workbench.generate.context`、`workbench.story-state.panel`、`workbench.quality.panel`、
`outline.visualization.panel`、`job.drawer.detail`。

机器目录为每个有 UI 的 capability 冻结唯一 `contribution_id + slot + capability_id` 三元组，并且三者绑定同一 release；headless capability 显式使用空 `ui_contributions`。插件级 `ui_slots` 只是这些三元组 slot 的去重投影，不能代替 contribution identity。

### 11.4 Core 原生控制

以下控件不允许由插件模拟：

- 文件选择、粘贴、raw Asset 创建；
- Candidate 接受/拒绝、批量预览、Publication；
- current Revision、Plugin Generation、Plan 和 release 切换；
- retry/resume/cancel/等待用户；
- 插件删除、版本回退、备份恢复；
- 跨 Workspace 来源选择器。

### 11.5 稳定性

- 每个 Slot 独立 Worker、Error Boundary、timeout 和 abort。
- 插件 UI 只返回 `plugin-ui-tree/v1`；未知 component/prop/event 拒绝。
- 某个拆书/故事圣经插件死循环或抛错，只替换该 Slot 为错误卡，不阻塞路由。
- 项目切换递增 UI freshness/epoch；迟到响应不得覆盖新项目。
- Job 99% 失败、partial、stale、needs_attention 都有显式恢复动作。

---

## 12. Core 升级候选

机器清单见 `core-upgrade-candidates-v1.json`。当前只列出，不授权施工。

### 12.1 必需前置门

1. **公共 Workspace/Document/Node/Revision/Relation/Asset/Candidate/Publication 合同**：
   P0 当前正在发布与 Sol 复核；未 PASS 前不启动插件施工。
2. **SourceWorkspace 与通用来源身份**：Core 能创建原作工作区、稳定 Source Document、
   canonical Revision 和跨 Workspace source refs。
3. **浏览器 Intake Broker**：Core 固定文件/粘贴入口创建 raw Asset，再启动插件 Job。
4. **Donor 固定页面与 Slot**：复杂导入、Evidence 审核不能挤入现有右栏。
5. **Evidence query/coordinate 合同**：按 exact Revision 读取 span/search/node，使用 Unicode
   code point 与 quote/hash；插件不能拿数据库或路径。
6. **登记 donor mutation payload schema**：Atom、Claim、Narrative、Character、Style 等
   Candidate payload 必须命中 Core 已登记 schema，不能靠 `item_kind` 猜。
7. **跨 Workspace 来源选择器**：Core UI 选择 exact SourceWorkspace/Document/Revision/Asset，
   并生成规范 source ref Asset。

### 12.2 可选候选

8. `workspace.fork/v1`：吸收 Novel-Agent 分支 fork；不含 merge。
9. 外部知识投影 Broker：用户显式选择 root、root-bound、只接收 Markdown Asset；否则下载。
10. 从 Core Asset 安装生成的 Data Plugin：没有时先“导出→用户重新选择安装”。

### 12.3 不应升级 Core 的供体强项

- Atom/Claim authority、acceptance ordinal：放在 donor-analysis plugin 的业务 schema/validator。
- 清洗 regex 与阈值：Data Plugin＋cleaning runtime＋settings。
- 人物/文风/一致性算法：Code Plugin/Skill/Data Plugin。
- Narrative Plan、Recipe、综合：Plugin Plan/业务 capability。
- Obsidian Markdown 模板：knowledge-projection plugin。

---

## 13. 九项目并发施工方案

施工机器书见 `construction-plan-9-projects-v1.json`。未来新建独立仓库
`PlotPilot-NovelAgent-Plugins`，再创建九个**独立 Codex Project＋九个 Worktree**；不在
PlotPilot-Pluginized Core 仓库里混写。

| 项目 | 范围 | 依赖 |
|---|---|---|
| NAP-00 Integration | SDK 适配、共享 schema、包、集成、唯一合并写者 | P0 Core 合同 PASS |
| NAP-01 Source | 导入、清洗、结构、重定位、清洗 Data | NAP-00 |
| NAP-02 Evidence | Atom、Claim、Evidence、人工/外部拆书 | NAP-00/01 |
| NAP-03 Narrative | 剧情单元、计划、综合、大纲投影 | NAP-02 |
| NAP-04 Character | 人物、关系、世界、模板、资产派生 | NAP-02 |
| NAP-05 Style | 文风制造、资格、应用、审查 | NAP-02 |
| NAP-06 Quality | 一致性、影响、修复、Canon、Story State | NAP-00；部分依赖 04/05 |
| NAP-07 Writing | Context、Recipe、Skill、章节工作流 | NAP-00；依赖 03/04/05 |
| NAP-08 Exchange | Analysis I/O、知识投影、组合方案、发布工具 | NAP-02—07 稳定 schema |

### 13.1 并发上限

- 最多 6 个 Luna Max 源码施工单元；
- 最多 2 个 Sol Max 复核单元；
- NAP-00 是唯一集成写者；
- Terra 仅作只读证据检索，不得作裁决；
- 所有 review、re-review、Finding 关闭、验收和合并资格均为 Sol-only。

### 13.2 写集

每个项目只写自己 `plugins/<family>/**`、`skills/<family>/**`、`data/<family>/**` 和定向测试。
共享 `contracts/**`、`sdk/**`、`catalog/**`、集成 fixture 和 release manifest 只由 NAP-00 写。
任何公共字段缺口必须提交 Contract Delta，业务项目不得自行扩展共享 schema。

### 13.3 集成批次

1. B0：SDK/golden/演示 plugin；
2. B1：Import→Cleaning→Source Publication；
3. B2：Atom→accept→Claim→re-review；
4. B3：Narrative＋Character＋Style 并行；
5. B4：Quality＋Writing；
6. B5：Exchange/Projection/Profile；
7. B6：真实浏览器十流 E2E、备份恢复、性能、独立总验收。

每批只合并 Sol PASS 的**源码 HEAD**；evidence-only commit 不作为源码合并对象。

---

## 14. 测试与验收

### 14.1 合同测试

- 每个 JSON object closed schema；未知字段拒绝。
- package/files/JCS/hash/golden 跨 Python/TypeScript 一致。
- capability descriptor、RunSnapshot、Data/Skill binding、result contract 完全匹配。
- ACK-loss、同 key 同 payload 幂等、同 key 不同 payload `1008`。
- Worker terminal 只能经 `host.job.complete/v1`，Step/Job 由 Core 聚合。
- 插件删除后历史 Revision/receipt 可读；需要执行的旧结果明确不可重跑。

### 14.2 Source/Evidence 测试

- TXT：UTF-8/UTF-16/GBK、长行、混合换行、章节误判、空书。
- EPUB：多 spine、目录缺失、嵌套章节、实体、脚注、广告段。
- paste：Unicode、emoji、组合字符、超长文本。
- 多规则顺序、合并来源、timeout、空匹配、命中阈值、用户例外。
- raw/canonical hash、segment map、Unicode code-point span、quote/hash。
- 四档重定位全覆盖；Atom successor 一对一；Claim 的全部 Atom successor 完整。
- 99% kill、Core 重启、SSE gap、checkpoint resume、重复 terminal、取消竞态。

### 14.3 业务测试

- Atom 分类覆盖用户冻结的全部描写/叙述/文风指标。
- Claim 无 accepted Atom 时 fail-closed。
- 人物卡每个高置信字段可反查证据。
- 文风资格由独立路径执行；未通过不发布。
- A 剧情+B 人物+C 文风的 source refs 和 receipts 保持独立。
- `separate` 不投票、不自动合并；`synthesize` 只在用户选择综合插件时执行。
- 一致性只报 Finding；修复逐项选择并只生成 Candidate。
- 人物绑定必须在写作 RunSnapshot/receipt 中证明真正参与。
- 继续生成从 durable high-water/exact base 开始，迟到流不覆盖新 Revision。

### 14.4 浏览器十流

1. 三种导入；
2. 多规则清洗预览/确认；
3. 结构修订与证据重定位；
4. Atom 运行、99%失败、恢复、接受；
5. Claim、剧情单元、综合；
6. 人物蒸馏/冲突/资产；
7. 文风制造/资格/应用/审查；
8. A/B/C 多来源写作；
9. 一致性→影响→受控修复；
10. 导出/知识投影/删除插件后历史回查。

任何 HTTP >=400、未知 console warning/error、pageerror、全局路由冻结或共享截图冒充两个流程，
均阻止交付。

### 14.5 性能

- 大文本以 Asset 分块读取，不把整本书塞进 Worker UI message。
- 清洗和拆书按章节/checkpoint；UI 只取窗口化列表和局部 span。
- 插件私有索引可重建；丢失索引不丢 Core 正式资料。
- 不在中间里程碑构建桌面端、EXE 或安装器。

---

## 15. 迁移和回滚

本轮不迁移用户旧数据。用户重新导入 TXT/EPUB/文本，生成新的 SourceWorkspace 和 canonical
Revision。供体源码只作为实现参考；不读取或复制 Novel-Agent 现有 SQLite 作为正式数据。

每个 capability 独立纵切：

1. 先安装 Novel-Agent 来源的 plugin release；
2. 在用户选定 Plan 中启用，不自动切换；
3. 运行真实小样并比较结果；
4. 通过后才把该 capability 的 active binding 切到新 release；
5. runtime/install/settings/data 失败时，Core 恢复 **exact LKG Generation**（含 package、data generation/bundle、settings revision/schema、UI bundle 等完整 member），不删除 Candidate、Revision、Asset 或 receipt；
6. Plan 回退是用户独立选择旧 Plan revision 的动作，不等于也不隐式触发 Generation/LKG 回滚；
7. active Job 期间不切 writer；不让新旧 runtime 接管同一个 Attempt。

---

## 16. 供体中开发脚本、兼容层和调试工具的处理

### 16.1 开发脚本

指构建、测试、fixture 生成、数据库检查、源码验证和一次性迁移辅助脚本。只选择能验证新插件
合同的脚本思路；不作为用户插件安装，也不随意复制整套脚本目录。

### 16.2 旧兼容层

指旧路由别名、旧 DTO、legacy reset、旧数据 migration、desktop launcher、旧 Mock/real 双轨和
历史页面跳转。由于本轮明确不迁移用户数据、不做桌面端，默认删除或不吸收。

### 16.3 内部调试工具

指开发者诊断、fake provider、测试服务器、fixture、手工 DB 修复、日志检查和临时实验入口。
它们只能进入 `devtools/` 或测试依赖，不能获得产品 Core 权限，也不能出现在普通用户 UI。

---

## 17. 设计完成门

本设计只有满足以下条件才从 draft 变为 accepted：

- [x] 供体 HEAD、tracked manifest 和产品源码 blob manifest 已冻结；
- [x] 本地知识库 reviewed 证据已路由；
- [x] 后端、前端、Skill/Data/能力源码已形成文件级矩阵；
- [x] 全功能吸收/淘汰/延期裁决已形成；
- [x] Code Plugin、Data Plugin、Skill、Plan、UI Slot 和 Core gap 已形成；
- [x] 九项目写集、依赖、并发和验收已形成；
- [ ] fresh Sol Max 完整独立复核 PASS；
- [ ] 如有 Findings，只做一次受限修订，并由同一 Sol reviewer 逐项关闭；
- [ ] 用户逐项裁决 Core 升级候选；
- [ ] 用户另行授权九项目施工。

在最后三项之前，本设计不构成修改 PlotPilot Core 或创建插件施工项目的授权。

