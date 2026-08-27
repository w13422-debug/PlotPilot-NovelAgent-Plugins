# NAP-00 B0 Candidate Delivery

## Delivery status

实现与合同级自测已完成；已提交 source candidate commit：`27a8320de05319745dbf7f87db5ea64a0b75dec3`。该候选只表示可送审，不表示 Sol PASS 或 merge eligibility。

## Delivered

- 公共 JSON Schema、manifest/golden/fixtures、Python SDK 与 TypeScript parity surface。
- Code/Data/Skill demo package，含 deterministic package hash、files.sha256、bundle/receipt fixtures。
- `plugin-plan/v1` 语义与完整 Generation/LKG、retiring/pin/rollback 语义验证；废弃 `compare` mode fail-closed。
- catalog exact Data interpreter 双向映射、capability/UI contribution 三元绑定、headless/UI exclusion 与 rebind/result rules 验证。
- EvidenceSpan 跨语言 Unicode scalar offset、exact quote、UTF-8 hash、Node containment。
- ordered Atom -> immutable `claim-input/v1` parameters Asset -> RunSnapshot asset/hash/request-key 绑定。
- Anthropic/Gemini headless provider descriptor、local mock stream/checkpoint/receipt、conditional diagnostic failure。
- 最小集成门：写集、catalog、demo、provider owner/needs/descriptor 与精确基线变更检查。

## Self-test evidence

详见 [`coordination/NAP-00/b0-verification.md`](../../coordination/NAP-00/b0-verification.md)。当前结果：`34 passed`（contracts/providers/e2e），integration gate `status=ok`。

## Review handoff

`candidate ready for fresh Sol review`。不得将本文件或实现方自测解释为 Sol 审查结论。
