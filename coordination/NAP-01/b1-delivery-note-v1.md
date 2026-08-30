# NAP-01 B1 Source-Import-Cleaning 候选交付

- 状态：Controller PASS，等待 fresh Sol/max 正式审查
- Source HEAD：`0a1ced6d47fb5c3622ff36f14bf09a727cb96a0b`
- Source tree：`ee3d0e8017068716310e114080dec8cf44261ca9`
- 基线：`2f0448cba177e298dbb00cef72d7401ccfe6a749`
- 全量测试：`84 passed, 40 subtests passed`
- Fresh checkout：`84 passed, 40 subtests passed`，compileall PASS，Git clean
- 确定性重建：三个插件各两次，0 differences
- 边界：未修改 PlotPilot Core、未修改 Novel-Agent donor、未构建 EXE/安装包

## 插件身份

- Source Import：`
c9bf87f728c3cff62aa0d251335d08fdc0000b4a05b2ad82623ac9a161699f16
` / `
1516d73072736a07f05ef854360eb86cedf5f9742dce4551d40689c116a2c73e
`
- Source Cleaning Runtime：`
1a142edaf589c048dfde0d0fcfa83c0c52c3abe0c47741a3b1c322b75ad627b5
` / `
3769203e651e8bc2169a00a5c7a01d82928ad3f2521aaf0b829f26f63d30eb78
`
- Source Structure：`
8c768a6b3693c344de1bebed4584391c64495d29c01f0cdd5929e711f12d7459
` / `
c050fd331a5d1dddb0451333fd478d764e81e7156f56e52a522c27a7009fb807
`

## 下一门禁

必须由 fresh `gpt-5.6-sol/max` 对精确 source/evidence 身份做一次完整 B1 审查。Sol PASS 前不得由 NAP-00 集成，也不得释放 NAP-02。
