# 小满迁移素材命名空间

这是从已验证的 macOS 本地语音项目迁入 DSH 的隔离素材目录。运行时代码
只应从当前仓库读取这里的文件，不得回退到来源工作区的绝对路径。

已迁入：

- `idle/idle.mp4`、`idle/poster.png`：原项目默认 idle，来源清单标记为用户确认可用于本项目。
- `voice/ref.wav`、`voice/ref.txt`：`original-ref-v1` 已确认的 24 kHz 单声道参考声音和逐字文本。

`listening/`、`thinking/`、`speaking/` 当前只保留状态说明；来源没有对应的、许可清楚的
独立数字人片段，因此不伪造占位二进制。具体迁移证据和未迁入候选见
[`docs/xiaoman-v3-reuse-audit.md`](../../docs/xiaoman-v3-reuse-audit.md)。

两个目标 manifest 对每个迁入文件记录独立 SHA-256，并固定来源 manifest 的哈希与
审计版本。该授权摘要只覆盖当前用户项目使用；来源仓库未检出独立 LICENSE，公开
再分发前仍须完成来源代码许可证和上游素材权利复核。
