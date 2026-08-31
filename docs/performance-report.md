# Voice / Agent performance report

状态：可复现实验模板；本仓库没有伪造硬件测量值。

## 目的与边界

性能记录拆开语音、本地 bridge、DSH/Codex backend 和可感知停音。`t9` 是浏览器
实际开始播放的时间，不能用远程 interrupt response 代替。Codex 模式还要记录
`turn/start`、first event、first final-answer delta、tool/approval 等子阶段。

事件默认只包含 trace/correlation、阶段、字符/字节计数、状态和耗时；不要把 prompt、
音频、命令全文、diff、thread id 或 credential 写入日志。浏览器事件来自
`dsh-plugin/src/client/latency.ts`，bridge 事件来自 `bridge/latency.py`。

## 计划批次

| 批次 | 计划次数 | 输入/场景约束 | 完成次数 | 备注 |
|---|---:|---|---:|---|
| short question | 20 | 短中文/英文问句，固定输入表另存，不进日志 | 0 | 待实机 |
| ordinary QA | 10 | 普通问答，覆盖中英混合和多句回答 | 0 | 待实机 |
| coding | 10 | Codex 工具/文件审批；必须在隔离 workspace | 0 | 待 approval UI |
| interruption/race | 10 | STT 中、backend thinking/tool、delta、TTS、approval、mode switch | 0 | 待实机 |

只有执行实验后才填写“完成次数”和结果。不要用本地单测、CPU import 时间或
`turn/interrupt` RPC 时间冒充真实硬件听感。

## 时间线与指标

```text
t0 speech_start
  -> t1 final VAD endpoint/commit
  -> t2 STT request start -> t3 STT result
  -> t4 top-level queue accepted -> t5 backend started
  -> t6 first speakable sentence -> t7 TTS start
  -> t8 audio ready -> t9 audio actually played
```

派生指标：`endpoint_latency=t2-t1`、`agent_ttft=t5-t4`、
`tts_latency=t8-t7`、`perceived_latency=t9-t1`。

Codex 另记：thread ensure/resume、turn start、first event、first final delta、每个
tool activity/approval wait、interrupt ack、authoritative terminal 和下一 turn start。
Barge-in 必须独立记录 `speech_start → local audible stop`（发布目标 p95 `<150 ms`）
以及远程 ack/terminal；后两者不能替代本地停音。

## 复现命令

无模型时仅验证脚本和报告格式：

```bash
python3 scripts/benchmark-voice.py
python3 scripts/benchmark-voice.py --log /path/to/captured-latency.jsonl \
  --out /tmp/xiaoman-voice-benchmark.json
```

运行真实实验时，先按 README 启动 bridge、DSH 和浏览器；在浏览器 console 保存
`[ui-voice] {"event":"voice.latency",...}` 记录，在 bridge 日志中保留同一 trace
的 JSON 行，然后交给脚本。结果文件只接受已捕获的事件，缺失指标保持 `null`，不会
填入默认数字。

建议每次实验同时保存：commit/配置摘要、macOS/CPU/内存、Apple Silicon 型号、
Python/Node/pnpm/Codex CLI 版本、bridge/DSH 端口、批次完成数、失败/取消数，以及
是否启用 MPS。不要保存模型权重、音频或登录材料。

## 当前未验证项

- 真实 macOS 麦克风、AudioWorklet、AEC 和 Apple MPS STT/TTS 延迟；
- 真实 Codex App Server streaming、审批、thread resume 和 interrupt terminal；
- `speech_start → audible stop` p95 与重复播放/跨 tab 行为；
- 20/10/10/10 批次的任何实际数值。
