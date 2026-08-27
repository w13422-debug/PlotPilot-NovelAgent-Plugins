# NAP-00 B0 复用与验证记录

## 基线与边界

- 实现分支：`codex/nap-00-integration`
- 施工基线：`c1b9519c7d25ce1fbef07984cdb548c89e7e1152`
- 接受的只读供体：`C:\Users\Administrator\Desktop\写作资料汇总\PlotPilot-Pluginized`
- 接受供体 HEAD：`1b352be671e70a2ce443b10499060982e93d64b7`
- 接受合同 manifest SHA-256：`cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024`

## 复用决策

采用“当前项目约定 + 已接受 PlotPilot 合同直接复用/薄适配”。供体合同目录和 `backend/plotpilot_plugin_sdk` 已通过合同审查证据，且 manifest SHA 与控制器释放证据一致，因此优先逐文件复用，不复制供体应用、Core、数据库或 UI。`C:\Program Files\ruanjian\gongju` selector 未发现匹配的 Python/TypeScript 合同 golden 工具；其候选均为视频、QML、遥测或 Git 救援工具，额外复用没有收益。未做无界 GitHub 搜索或引入第三方供应链。

本地知识库路由已执行两次；没有 scoped Novel-Agent/合同/SDK 命中，仅返回无关协议、支付、APK/PE 资料。为完成强制 `kb_router -> kb_read_file` 顺序读取了 `C:\Program Files\ruanjian\Open-tgtylab\kb\general\techniques\protocol\01-unknown-protocol-reverse.md`，该资料不影响 B0 实现。

## 验证范围

验证器 `tools/integration/validate_b0_delivery.py` 检查：当前分支/精确 HEAD（按参数）、相对基线的变更路径写集、catalog 的双向 Data interpreter/UI 三元绑定及 plan/result/rebind 规则、两个 NAP-00 planned provider 的 descriptor/needs/source owner、三类 demo package，以及失败 fixture 的 conditional 输入。Python/TypeScript 运行时合同、golden、provider mock 由对应 pytest/Node 测试负责；验证器不访问 Core 数据库、不联网、不合并分支。

本轮实现补强：Python 与 TypeScript SDK 均拒绝已废弃的 `result_mode=compare`；提供 Generation immutable/LKG rollback、release retiring/pin barrier、EvidenceSpan Unicode scalar offset + UTF-8 hash、ordered Atom -> `claim-input/v1` parameters Asset -> RunSnapshot hash 绑定，以及 catalog 的 exact `{plugin_id, capability_id}` Data interpreter 双向校验。`evidence-span/v1` 与 `claim-input/v1` 语义沿用冻结设计，由 SDK 语义层校验，未改变接受合同 manifest 的字节与 SHA。

## 已知限制

本文件是实现方证据，不是 Sol review、Finding 关闭或 merge eligibility 结论。最终 source HEAD 只有在提交后记录，并送 fresh Sol 独立审查。
