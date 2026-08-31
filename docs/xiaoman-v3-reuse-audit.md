# 小满 v3 迁移复用审计

审计日期：2026-08-17
来源：旧机 `macos-local-voice-agents` checkout（代码/模型不修改；部署时选择性运行）
目标：当前 `xiaoman-dsh-local` 仓库根目录

## 结论

### 2026-08-22 v3-only 收敛补充

本轮只以 Xiaoman v3 为复用权威。`bridge/xiaoman_v3_adapters/source-lock.json`
固定来源 commit、来源/本地 SHA-256、适配方式和生产状态，禁止两个仓库静默演进同一
算法。现在默认链路已经收敛为单一 v3 Voice Runtime：v3 在 `127.0.0.1:7860` 独占
MLX Whisper、Qwen3/OmniVoice TTS 和 LiveTalking 音频投递，DSH 的 `:8765` 保留原有
浏览器 API，但只通过 `xiaoman.voice-runtime.v1` 代理 STT/TTS/Avatar。DSH 继续拥有
Silero `/api/vad`、sentence assembly、播放、AgentLoop、Codex 和 React UI。

`bridge/xiaoman_v3_adapters/` 的 STT/TTS 复制件现为 explicit-local-fallback，只有显式
设置 `DSH_VOICE_RUNTIME_MODE=local` 才加载；v3 不可用时固定返回 503/502，不自动启动
第二套 MLX 模型。AudioBus、Python TTS segmentation 和 Energy VAD 仍为
compatibility/test-only。验证默认运行态稳定后，这些回滚复制件可以整体删除。

Avatar 仍直接复用 v3 LiveTalking runtime；DSH relay 只保留会话协议适配。relay 的
HTTP 上传已移出浏览器 PCM 关键路径，并按 DSH session 串行化，避免 Avatar 慢上传拖住
TTS 首包。启动默认重新对齐 v3 已验收的 `device=auto`、`inference_stride=4`；此前
`cpu + stride=6` 是已否决的实验档，不再作为 DSH 产品默认。

TTS 依赖差异已通过进程边界消歧：v3 的 `mlx-audio==0.4.7` 是默认运行权威；DSH 的
`0.5.0` 只服务显式 rollback，不再参与正常运行。真实首块延迟、连续性和音色仍需在
启动新 runtime 后做听感/30 分钟 soak，单元测试不替代这项运行态验收。

### 2026-08-24 覆盖性决定：不再做 renderer/media-sender 进程拆分

本节覆盖下文 2026-08-23 诊断结束时提出的“下一阶段做独立进程 A/B”。在该诊断之后，
v3 已复用 DSH continuity overlay 的核心语义：原生 audio track 在首个真实帧后维持
20 ms media cadence，renderer 短暂缺帧时只记录独立 fallback，并用 receiver wall
clock、media PTS 和 loop heartbeat 区分接收端卡顿、media timeline skip 与
sender/transport pause。没有新建第二套 TTS 或第二套 AvatarRelay。

更新后的权威 3 轮报告
`xiaoman-v3/benchmarks/results/2026-08-23-webrtc-dual-clock-authoritative-3x.json`
为 3/3 PASS：audio gap max 最差 29.458 ms，video gap p95 最差 42.021 ms，最低
video FPS 24.65，receiver loop lag max 最差 7.078 ms，media timeline skip 和
sender/transport pause 都为 0。随后同一实现完成两套独立 30 分钟门：

- `2026-08-23-strict-sync-subscription-soak-30m-final.json`：1800.013 秒、10/10
  WebRTC probe PASS；audio gap max 最差 32.787 ms，video gap p95 最差
  43.879 ms，最低 FPS 24.787，receiver loop lag max 最差 8.454 ms；underflow、
  media timeline skip 和 sender/transport pause 均为 0，全部 hard gate 为 true。
- `2026-08-23-direct-audio-browser-soak-30m-final.json`：1800.745 秒、10/10 浏览器
  direct-audio turn PASS；浏览器保持连接，heartbeat max lag 27.9 ms，测试 session
  在浏览器关闭后释放，全部 hard gate 为 true。

两份 30 分钟报告固定 top-level commit `42ba92d`、nested LiveTalking commit
`faef192`；2026-08-24 roadmap 又记录用户确认当前整体效果可接受，仅长句有轻微但不
明显的卡顿。当前代码复验为 v3 Avatar/media 38 项、benchmark/soak 31 项、DSH
双时钟/continuity 15 项全部通过。

因此现在拆分 renderer/media sender 没有尚未满足的产品门，只会新增 IPC、frame copy、
生命周期和故障恢复工作，违反本审计“避免 duplicate work”的目标。保留现有边界：v3
继续拥有 Wav2Lip/aiortc/continuity，DSH 继续拥有 AvatarRelay、session adaptation 和
独立质量 probe。只有在单一 LiveTalking 实例、无并行 soak 的干净基线中再次满足以下
任一条件，才重新开启进程隔离 spike：30 分钟 hard gate FAIL、media timeline skip 或
sender/transport pause 大于 0、video FPS 低于 20、video gap p95 高于 100 ms，或说话期
fallback 超出现有 hard limit。短暂 wall-arrival 抖动但 media timeline 连续，不再单独
触发架构重写。

### 2026-08-23 受控切换验证

默认 v3 链路已完成一次真实模型和浏览器验证。DSH Bridge 的 STT 请求经 Voice
Runtime 转写 24 kHz 参考 WAV，2.684 秒返回且文本与参考逐字一致；非流式 TTS 生成
3.92 秒、16 kHz 单声道 PCM，HTTP 总耗时约 5.00 秒。热路径流式 TTS 首 PCM 为
0.26--0.29 秒；较长文本生成 7.28 秒音频耗时 2.12 秒，生成期间 Runtime 和 Bridge
健康接口仍能返回 200。

真实 DSH 页面建立了 LiveTalking WebRTC 会话，文本回复经 6 个 PCM chunk 送入数字人，
并触发静音到说话状态。该次闲置后请求的首 PCM 为 2.915 秒，Avatar 记录 1 次音频队列
underflow；随后一分钟内 Avatar 约 25 FPS，6 次 Runtime/Bridge/UI 探针全部通过。
因此功能切换可保留，但冷/闲置首 PCM 和 underflow 仍是下一轮性能门槛，不能用热路径
数字替代。

切换时还复现并修复了一个流式死锁：worker 持有 TTS provider lock 等待异步队列时，
事件循环会因读取 provider `sample_rate` 的 eager default 再次等待同一把锁。Runtime
现在只读取结果自身的采样率，并把 TTS health 移到 worker thread；新增 lock-isolation
回归测试。验证结果为 v3 138 项通过（20 skipped）、DSH Bridge 40 项通过。DSH 全仓
200 项仍有原有的 `thread/start`、`thread/resume` 两个 Codex schema 错误，与 Voice
Runtime 变更无关。30 分钟连续性/听感 soak 尚未完成。

后续诊断确认，上述 Avatar underflow 不是 Qwen chunk 生成跟不上播放。该 turn 的终止
标记未送达，LiveTalking 在音频耗尽后仍保持 `active=true`，累计插入静音超过 34 分钟。
DSH 的句子级 `ttsStream` 现在默认将每次完整请求标为 `end=true`；需要跨多个 HTTP
请求维持同一连续 turn 的特殊调用仍可显式传 `end=false`。隔离端口上的真实 WebRTC
A/V 探针验证 `active=false`、underflow 0、插入静音 0，音频最大帧间隔 23.75 ms、视频
最大帧间隔 158.34 ms、A/V delivery skew p95 20.36 ms。该次整体仍未通过质量门：嘴型
lag 为 500 ms，高于 240 ms 门槛，因此不能据此宣称 30 分钟 soak 或完整 Avatar 质量
验收通过。

2026-08-23 默认页面复测进一步发现，首次源码修复没有进入运行态：overlay 安装器按
设计只同步受管源码、排除 generated `lib`，而当时没有随后执行 Client bundle；从
`:3080/plugins/@deepseek-ai/dsh-client-ui-voice/client.js` 下载的实际代码仍是
`end: options?.end ?? false`。在 pinned harness 中重建 ui-voice 并重启 DSH 后，服务端
bundle revision 从 `a0e362e3ddf2` 更新为 `7f13c0f7064f`，反向下载确认运行代码已经是
`end: options?.end ?? true`。安装器现在会明确提示 generated `lib` 不受管理，README
也把“同步源码”和“完成 pinned build”分成两个不可省略的步骤，避免后续再次把源文件
状态误当成运行状态。后续页面生命周期修复再次重建后，当前实际服务 bundle 的
SHA-256 为 `a68e431502320b63ca5a2df24650ac44dedfb27f747a132152ed8a2a2250e056`，并反向确认同时
包含 `end=true` 默认值、`pagehide` 注销和 DELETE `keepalive`。

隔离 `:8012` 的早期探针曾报告 audio gap max 24.22 ms、video gap max 171.24 ms、
A/V delivery skew p95 18.55 ms、underflow 和插入静音均为 0。但是结果中的
`continuity.active=true`、`queued_audio_ms=4460` 暴露出探针缺陷：LiveTalking 的
`is_speaking` 会在推理 batch 间短暂变 false，旧探针因此在 turn 尚未结束时提前关闭
WebRTC。随后 30 分钟、60 轮得到的 44 pass / 16 fail 只能视为“中途强制 teardown”
压力数据，不能作为完整播放或 session-churn soak 的验收结果。该压力数据仍证明四个
服务全程存活并能回收 session，但其 gap/underflow 分布不得用于产品质量签字。

探针现要求 `continuity.active=false` 且 `queued_audio_ms=0` 持续 400 ms 后才判 idle。
修复后单轮完整播放耗时约 11 秒，采集 430 个音频帧和 209 个视频帧；终态为
`active=false`、queue 0、underflow 0、插入静音 0，证明 `end=true` 已经贯穿 bundle、
Voice Runtime 和 LiveTalking。该轮 audio gap max 24.62 ms、A/V skew p95 20.02 ms、
嘴型 onset 偏差 43.17 ms、相关性 0.164 均通过，但 video gap max 274.93 ms 超过
200 ms，故完整 A/V gate 仍失败。

恢复默认 `:8010` 后又完成一次真实 DSH 浏览器回归。页面实际显示“默认8010终包验证
成功。”；v3 LiveTalking 收到 turn `14:assistant-step4:1` 的 seq 0--5 音频块和 seq 6
终包，终包为 `pts_ms=3760, end=true, status=end`。播放终态为 `active=false`、queue 0、
underflow 0、插入静音 0，WebRTC fallback 也全部为 0。这证明默认端口上的实际生成
bundle、DSH Bridge、v3 Voice Runtime 和 LiveTalking 已完成端到端终包闭环；它不改变
上述 274.93 ms video gap 的质量门失败结论。

该页面回归还暴露出 Avatar binding 的卸载边界：普通 React cleanup 使用的 DELETE
fetch 在页面离开时可能被取消。客户端现在把同一个幂等 compare-and-delete `close()`
挂到 `pagehide`，并只为 DELETE 启用 `keepalive`。真实导航离开验证 Runtime 的
`avatar_sessions` 从 1 回到 0。自动化工具直接销毁 browser target 会绕过文档生命周期，
仍可留下 stale binding；浏览器崩溃/强杀也同属这一未覆盖边界。生产级彻底回收仍应在
Runtime 增加 lease/TTL，当前测试留下的 binding 通过受控重启 Runtime 清空。

当前 macOS 没有 thermal/performance warning，但 16 GB 系统有较大的
compression/swap 历史。压力轮的 stall 多次与 Avatar RSS 工作集变化同现；这是内存
回收/GPU 调度竞争的线索，不是已证明的唯一根因。continuity overlay 已消除无限静音
turn；上述有缺陷的 60 轮结果仍不得算作完成。

随后已使用修正探针完成一轮新的 30 分钟完整播放 soak：总时长 1806 秒，174 个独立
WebRTC session，每轮均等待 `active=false` 且 queue 0 后再关闭。严格门槛为 audio gap
40 ms、video gap 200 ms、A/V skew p95 80 ms、underflow 0、插入静音 0。结果为
168 pass / 6 fail（96.552%）；仓库原门槛 audio 100 ms、video 200 ms、A/V 120 ms 下
为 173 pass / 1 fail（99.425%），因此两套门槛都不能签字为全通过。

失败轮为 31、55、68、117、143、146。最差轮 31 同时出现 audio gap 203.78 ms 和
video gap 220.52 ms，两段时间完全重叠；但该轮及全部 174 轮的 underflow、插入静音、
WebRTC fallback 都为 0。全部 174 轮都完成 speaking-to-idle、嘴型相关性门槛、Avatar
session 回收、Runtime binding 归零；Runtime、Bridge、LLM、UI 健康探针也是
174/174。由此可签字的是终包、播放队列连续性和 session 生命周期；尚不能签字的是
接收端严格 gap 质量。双轨重叠停顿而发送侧队列不见底，优先指向 WebRTC transport、
探针接收事件循环或系统调度长尾，不应回头复制/重写 TTS。完整结果在
`logs/avatar-soak-corrected-20260823-124752/summary.json` 和同目录逐轮 JSON/TSV。

为消除上述多种解释，随后在 LiveTalking sender 和 DSH probe 两端加入同一
`time.monotonic_ns` 时钟的分段遥测。receiver gap 与 sender 返回 frame 的 gap 在失败
区间高度重叠，媒体 PTS 仍保持 audio 20 ms、video 40 ms；例如
`logs/avatar-timing-ab-20260823-170153/iteration-003.json` 的 receiver/sender audio
gap 分别为 40.44/36.30 ms，重叠 33.28 ms，video 分别为 83.04/82.42 ms，重叠
76.99 ms。这排除了“只在 DSH probe 接收端发生”的解释，也没有任何 TTS queue
underflow 或 WebRTC fallback。

进一步的 `recv()` phase 分解显示两类 sender 延迟：一类是 aiortc 已及时调用
`recv()`，但 audio pacing sleep 和 video queue wait 同时延长；另一类是上一帧返回后
aiortc 较晚再次调用 `recv()`。因此 `inter_call_gap` 不能单独解释成事件循环停顿，它还
包含 executor-backed codec 和 RTP send。新增独立 20 ms event-loop heartbeat 后，失败
轮可直接区分 loop 调度长尾与 renderer 缺帧，不再凭双轨重叠推断。

asyncio debug 同时确认 `/humanaudio` 原先在 aiohttp loop 内同步执行
`soundfile` decode、可能的 resample 和 20 ms chunk materialisation；一次带系统采样扰动
的诊断轮记录 RequestHandler 268 ms，因此该绝对数不作为性能基准，但源码中的同步边界
是确定的。路由现已按 session 用 `asyncio.Lock` 保序，并通过 `asyncio.to_thread` 把
读取/解码/切帧移出 aiohttp callback；28 项 LiveTalking interruption/continuity 测试
通过。该修复减少了一个明确的 loop blocker，但 thread 仍和 Wav2Lip、VP8 共享同一
Python 进程/GIL，不能当作进程隔离。

修复后的首组 20 轮因 `cloudd` 和 `fileproviderd` 各接近一个 CPU 而被标记为高负载
压力数据，不与 30 分钟基线比较。随后把逐轮结果写到 `/tmp` 避免 iCloud 日志反馈，
再镜像到 `logs/avatar-humanaudio-offload-clean-20260823-193516/`：严格门槛下 10 轮
6 pass / 4 fail。四个失败轮中，receiver audio gap 为 49.10--403.05 ms，heartbeat
lag 为 13.26--376.84 ms；最差轮 sender audio pacing wait 401.6 ms、video queue wait
454.4 ms。另一个高负载轮还复现了 heartbeat 仅 1.9 ms、audio 正常而 video queue wait
228.3 ms 的独立 renderer 缺帧。这组短样本用于定位，不能覆盖或替代 30 分钟验收。

当时的根因边界收敛为两项：同进程 Wav2Lip/解码/codec 负载下的 event-loop/GIL/系统
调度长尾，以及 Wav2Lip renderer 偶发不能及时供给 video frame。DSH 接收探针、TTS
生成和播放音频队列不是这两类 gap 的来源。当时据此提出保留 v3 Wav2Lip 模型/素材和
DSH AvatarRelay/session/probe、只做 renderer/media-sender 独立进程 A/B；该建议已被
上方 2026-08-24 的权威双 30 分钟 PASS 覆盖，当前不执行。仍然不复制 TTS、不新建
第二套 relay，也不通过放宽质量阈值制造通过结果。

LiveTalking 启动边界也已增加 `LIVETALKING_HOST`，DSH 启动器只接受
`127.0.0.1`/`::1`；隔离实例通过 `lsof` 验证只监听 loopback。默认 `:8010` 浏览器
复测期间发生过一次 stall-detector WebRTC 重连，但重连后的会话完成了上述终包和零
underflow 终态；未用杀死外部进程或放宽质量阈值的方式制造通过结果。

来源工作区约 **13G**。其中 `xiaoman-v3` 约 **732M**，LiveTalking 本体和运行
产物约 **718M**；`server` 约 **5.1G**，`xiaoman-v2` 约 **2.4G**，并含约
2.0G 的 Python venv；客户端含约 545M `node_modules`。这些目录都没有迁移。

迁移范围限于：

1. 已在来源测试中验证、能以小依赖导入的 VAD、audio bus、STT/Whisper 和 TTS
   provider 逻辑；
2. 来源 manifest 明确标记用户已授权本项目使用的 idle 视频和参考声音；
3. 当前仓库的新隔离 Python 命名空间
   `bridge/xiaoman_v3_adapters/`，运行时不引用来源绝对路径。

原始审计阶段没有在来源目录运行测试、启动器、模型推理、格式化或 `chmod`。
2026-08-22 用户明确选择真实嘴型方案后，部署层新增 `scripts/start-avatar.sh`，复用
来源中现成的 LiveTalking/Wav2Lip 权重、venv 和预处理 avatar。该阶段没有修改来源
代码/模型；2026-08-23 的 continuity、session lifecycle 和 timing 诊断则在 v3
LiveTalking 工作树中加入了受控源码修改，但仍未复制或改写模型权重/预处理素材。
LiveTalking 自身也会更新来源目录现有的 `livetalking.log`。这是部署复用，不改变上表
“代码/权重未迁入当前仓库”的 provenance 结论。

## 已验证的可复用实现

| 能力 | 来源路径 | 依赖/验证证据 | 复用判断 |
| --- | --- | --- | --- |
| ASR 核心 | `xiaoman-v3/voice/asr/whisper_asr.py` | `numpy`、运行时惰性导入 `mlx_audio.stt.load`，16 kHz 输入，锁保护模型，来源 gateway 在 `asyncio.to_thread` 中调用；模型 `mlx-community/whisper-large-v3-turbo-asr-fp16` | 默认直接由 v3 Voice Runtime 调用；DSH `MacSTTProvider` 只保留显式 rollback。 |
| SenseVoice STT | `server/sensevoice_stt.py`、`xiaoman-v2/xiaoman/sensevoice_handler.py` | `sherpa-onnx` + SenseVoice-small int8 ONNX（来源注记 M5 实测约 0.103s p50、中文 CER 2.2%，CPU 推理）；同时绑定 Pipecat 或 `speech_to_speech` frame/handler 类型，模型目录约 239M | 审计确认性能价值，但不是本次自包含轻量迁移；未复制 sherpa runtime、模型、Pipecat frame 层。可在后续以同一 `STTProvider` 边界增加 `SenseVoiceProvider`。 |
| Neural VAD | `server/bot.py` 的 `SileroVADAnalyzer`/`LocalSmartTurnAnalyzerV2` | Pipecat + Silero ONNX（来源 venv 内约 3.0M `silero_vad.onnx`）和 Smart Turn 权重；WebRTC pipeline 依赖多层 Pipecat transport | 只记录已验证组合；不迁移 venv、ONNX 权重或 Smart Turn 模型。本次 adapter 采用来源 v3 已有的确定性 energy VAD，事件形状可兼容未来 Silero。 |
| Legacy ASR | `bridge/voice_bridge.py`（现有目标代码）及来源文档/测试中的 legacy Whisper 调用形状 | `speech_to_speech.STT.whisper_stt_handler.WhisperSTTHandler`、`VADAudio`；来源验证了取消后旧 ASR 结果须丢弃、单 token 结果须按空文本处理 | 适合作为兼容 adapter；`LegacyWhisperProvider` 采用惰性导入和可注入 factory，避免 macOS 环境强制安装 legacy 包。 |
| Energy VAD | `xiaoman-v3/voice/vad/energy_vad.py` | 仅 `numpy`；来源 `tests/test_vad.py` 覆盖 audio-clock 阈值、soft endpoint/reopen、短段合并、预滚、硬上限和 reset | 适合直接迁移；复制后只增加 activation event，原状态机语义保留。 |
| Audio fan-out | `xiaoman-v3/voice/audio_bus/bus.py` | 仅 stdlib；来源 `tests/test_audio_bus.py` 覆盖 generation stale-drop、required/optional sink 隔离和 close | 适合直接迁移；用于 browser required、avatar best-effort 的同包边界。 |
| TTS boundary | `xiaoman-v3/voice/tts/base.py` | `numpy`；`TTSProvider`、`TTSResult`、`VoiceProfile`；来源 `tests/test_tts_provider.py` 覆盖 prewarm、reference prompt 只缓存一次、并发串行、empty output、health/unload | 唯一生产权威；由 v3 Voice Runtime 暴露 versioned HTTP contract。 |
| OmniVoice | `xiaoman-v3/voice/tts/omnivoice.py` | `numpy`、运行时惰性导入 `mlx_audio.tts` 和 `scipy.io.wavfile`；参考音频只在 prewarm 构建 `ref_tokens`，generate 不重复传 `ref_audio`；来源测试覆盖这一点 | v3 内可选 provider；DSH 复制件仅显式 rollback。 |
| Qwen3 TTS | `xiaoman-v3/voice/tts/qwen3.py` | `numpy`、运行时惰性导入 `mlx_audio.tts`；来源测试覆盖 stream chunk/final 标记和非流式合并 | v3 默认 provider；DSH 不再维护生产副本。 |
| TTS segmentation | `xiaoman-v3/voice/tts/text_segmentation.py` | 仅 stdlib；来源测试覆盖强/软标点、短片段合并和长度上限 | 适合直接迁移；未绑定任何模型路径。 |

### 依赖边界

来源 v3 `pyproject.toml` 声明 `fastapi`、`uvicorn`、`websockets`、`httpx`、
`mlx-audio>=0.4.7`、`sounddevice`、`numpy>=2.0`、`scipy>=1.14.0`。默认部署直接使用
v3 已有 `.venv-v3-tts312` 和模型缓存；DSH Bridge 只新增轻量 `httpx` 客户端，不导入
`mlx_audio`。`scripts/start-voice-runtime.sh` 可用 `XIAOMAN_V3_ROOT` 和
`XIAOMAN_VOICE_RUNTIME_PYTHON` 覆盖来源 checkout/环境。

## VAD 事件和取消语义

目标包提供 `VADEventStream`/`EnergyVADAdapter`，将来源内部的
`start`、`soft_end`、`reopen`、`commit` 映射为稳定事件：

- `speech_start`：达到最小活动语音时长；
- `speech_end` + `soft=true`：来源 1200ms（可配置）的软端点，仍允许 reopen；
- `speech_reopen`：在 speculative grace 内恢复同一 utterance；
- `speech_end` + `final=true`：最终 commit，附带防御性 audio snapshot。

`CancellationToken`/`CancellationRequested` 是 provider 共用的协作取消协议。
native MLX/legacy 一次推理不能安全地从另一线程强杀，因此 adapter 在加载/调用
前、调用后和流式 chunk 之间检查 token；网关还应使用 generation 检查丢弃迟到
结果。来源 `gateway/app.py` 的 generation invalidation 和 `audio_bus` stale-drop
是该设计的依据。

## 数字人和声音素材

### 已迁入（许可证据清楚）

| 目标 | 来源 | 来源大小 | 证据 |
| --- | --- | ---: | --- |
| `assets/xiaoman/idle/idle.mp4` | `xiaoman-v3/profiles/avatars/xiaoman-v3-original-idle/idle.mp4` | 5.5M | `manifest.json` 的 `usage_rights=user-confirmed-authorized-for-this-project`，确认日期 2026-08-10；704×896、25fps、127 帧、无音频 |
| `assets/xiaoman/idle/poster.png` | 同 profile `poster.png` | 332K | 同一 profile；仅作人工/前端 poster |
| `assets/xiaoman/voice/ref.wav` | `xiaoman-v3/profiles/voices/original-ref-v1/ref.wav` | 440K | `manifest.json` 的 `production_release_status=confirmed_for_this_project` 和用户授权证据；24kHz 单声道 PCM16 |
| `assets/xiaoman/voice/ref.txt` | 同 profile `ref.txt` | <4K | manifest 标记逐字人工确认；作为 voice-clone reference transcript |

目标 manifest 已去掉来源绝对路径，只保留 profile、规格和授权摘要；每个迁入文件
都有独立 SHA-256，并固定来源 manifest 自身的 SHA-256 与本次审计版本。公开再分发
仍受本文“未能迁移项与后续边界”中的来源许可证复核要求约束。

### 候选但未迁入

| 来源 | 大小 | 原因 |
| --- | ---: | --- |
| `xiaoman-v3/profiles/avatars/xiaoman-v3-real/idle.mp4` + poster | 1.9M + 336K | manifest 明确写着来自上游本地样例，发布前必须确认再分发权；未复制。 |
| `xiaoman-v3/profiles/avatars/xiaoman-v3-real-backup/{source.webp,master.png,idle.mp4,idle-static.mp4,poster.png}` | 56K + 728K + 512K + 120K + 568K | manifest 写明 user-provided、usage rights not verified；未复制。 |
| `voice/ref.wav` + `voice/ref.txt` | 508K + <4K | 根目录候选没有对应 voice profile manifest/授权摘要；已选择有完整 manifest 的 `original-ref-v1`，未复制此候选。 |
| `client/public/models/xiaoman.vrm` | 27M | 未找到与目标迁移相关的授权/provenance manifest；体积也明显超过本次“小型素材”范围。 |
| `xiaoman-v3/avatar/livetalking/data/avatars/*` | 约 330M | Wav2Lip 预处理缓存/生成物，依赖来源目录和模型，不是可独立发布素材。 |

`listening/`、`thinking/`、`speaking/` 目前只放状态说明，运行时应回退到已授权
idle；来源没有对应的许可清楚独立片段。

## macOS helper 审计

| 来源路径 | 作用 | 依赖/副作用 | 迁移决定 |
| --- | --- | --- | --- |
| `server/bot.py` | Pipecat `SmallWebRTCTransport`、Silero VAD、Smart Turn、MLX Whisper、隔离 TTS、可打断 pipeline | `server/pipecat`、`aiortc`、FastAPI、多个 MLX/Pipecat 包；服务器目录约 5.1G，另有约 2.0G `.venv` | 只复用接口/取消经验，不复制整套 pipeline。 |
| `xiaoman-v3/start-v3.sh` | macOS 上编排 llama-server、LiveTalking、gateway；PID/命令/启动时间校验、健康等待、隔离清理 | Homebrew/llama-server、两个 venv、LiveTalking、运行时 `.run-v3` 日志/PID；会启动外部服务并写生成物 | 不整套复制；DSH 分别用 `start-avatar.sh` 和 `start-voice-runtime.sh` 复用现有 Avatar/Gateway，并共用 DSH 的 8090 LLM。 |
| `xiaoman-v3/start-v3-candidate.sh` | 固定原项目 idle + original-ref-v1，再进入启动器 | 依赖上行脚本和 v3 profile 绝对结构 | 不迁移；目标只使用本地 `assets/xiaoman` manifest。 |
| `xiaoman-v3/stop-v3.sh` | 安全停止已记录的 macOS 服务 | `ps`/`kill`/PID 目录，具破坏性 | 不迁移；保留为后续平台启动器设计参考。 |
| `xiaoman-v3/avatar/setup_avatar_env.sh` | 建 venv、pip 安装、clone LiveTalking | 会 `rm -rf` venv、联网 clone、下载依赖 | 严禁执行/复制；不是自包含实现。 |

## 迁移清单

### 代码

- `bridge/xiaoman_v3_adapters/cancel.py`
- `bridge/xiaoman_v3_adapters/stt/{__init__.py,providers.py}`
- `bridge/xiaoman_v3_adapters/tts/{__init__.py,base.py,omnivoice.py,qwen3.py,text_segmentation.py}`
- `bridge/xiaoman_v3_adapters/vad/{__init__.py,energy_vad.py,events.py}`
- `bridge/xiaoman_v3_adapters/audio/{__init__.py,bus.py}`

这些文件只使用包内相对 import 或已安装第三方包；默认运行态不 import 它们。来源绝对
路径只存在于部署启动器的可覆盖默认值，不存在于 DSH Python import graph。

### 资产/配置

- `assets/xiaoman/idle/{idle.mp4,poster.png,manifest.json}`
- `assets/xiaoman/voice/{ref.wav,ref.txt,manifest.json}`
- `assets/xiaoman/{README.md,listening/README.md,thinking/README.md,speaking/README.md}`
- `assets/xiaoman/config/{providers.json,avatar.json}`

## 未能迁移项与后续边界

- Silero `.jit`、Whisper/Qwen/OmniVoice 权重、LiveTalking Wav2Lip 权重、预处理
  cache、任何 venv/node_modules 均不迁移；目标环境须按部署流程单独获取。
- `MacSTTProvider` 是基于已验证 `WhisperASR` 的新增 provider；来源本身没有
  同名类，也没有在本次审计中运行真实模型。测试使用 fake loader。
- `LegacyWhisperProvider` 只在安装 `speech_to_speech` 后可运行；没有把该第三方
  包或来源 `server/pipecat` 搬进来。
- 来源根目录未检出独立 `LICENSE` 文件；代码迁移适用于当前用户工作区，若要向外
  发布来源实现/素材，需先完成来源代码许可证和上游素材再分发权复核。
- 目前无独立 listening/thinking/speaking 数字人片段；按 manifest 回退 idle。
