# NAP-00 B0 实现验证记录

## 范围

- 分支：`codex/nap-00-integration`
- 施工基线：`c1b9519c7d25ce1fbef07984cdb548c89e7e1152`
- 允许写集：contracts / sdk / catalog / Anthropic + Gemini providers / integration tools / contract-provider-e2e tests / NAP-00 coordination / delivery docs
- 未修改 PlotPilot Core、Novel-Agent donor、桌面/EXE/安装包或 `dist`
- OpenAI-compatible provider 未实现；目录 disposition 保持 `not_planned_duplicate`

## 复用决策

直接复用已接受 PlotPilot 合同、golden、Python SDK 结构并作薄适配；供体 HEAD 为 `1b352be671e70a2ce443b10499060982e93d64b7`，合同 manifest SHA-256 为 `cbe9d02fc42409332151cd7905e387a46537b330b039a2d3d6798f48cbfcc024`。本地知识库无 scoped Novel-Agent 合同命中；工具库无 stack-matching validator；无额外 GitHub/npm 依赖。

## 受影响验证

| 命令 | 原始结果摘要 |
|---|---|
| `python -m compileall -q sdk/plotpilot_plugin_sdk tools/integration` | exit 0 |
| `python -m pytest -q tests/contracts` | 21 passed |
| `python -m pytest -q tests/providers` | 10 passed |
| `python -m pytest -q tests/e2e` | 3 passed |
| `python -m pytest -q tests/contracts tests/providers tests/e2e` | 34 passed |
| `python tools/integration/validate_b0_delivery.py --require-changes` | JSON status `ok`; branch correct; write-set check correct |
| TypeScript validation through `tests/contracts/test_typescript_parity.py` | 3 passed; Node strip-types parse and runtime parity passed |

## 自测验收矩阵

1. public SDK compiles/imports：PASS（Python compile/import；TypeScript Node parse/runtime parity）。
2. package/Skill/Data golden parity：PASS（hash、receipt、bundle 与 files.sha256 golden）。
3. demo Code/Data/Skill packages：PASS（同一 package/golden chain）。
4. Anthropic/Gemini descriptor-result-needs 与 stream/receipt parity：PASS（本地 deterministic mock；失败路径为 conditional diagnostic Bundle）。
5. no-ff merge gates：PASS（写集、catalog disposition、精确基线相对变更门；未执行合并）。

## 已知限制

未安装独立 `tsc` 二进制；因此 TypeScript 使用仓库既有 Node `--experimental-strip-types --check` 与运行时 parity 验证，而不是额外安装第三方编译器。该实现自测记录不构成 Sol 审查、Finding 关闭或 merge eligibility。
