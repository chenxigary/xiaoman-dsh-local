# 数字人卡顿与嘴型同步测试

## 目标与测试分层

这组测试判断用户真正听到、看到的输出，不把 HTTP 200、创建 WebRTC session 或“收到过一帧”当成质量通过。

| 层级 | 测试 | 覆盖 | 运行条件 |
|---|---|---|---|
| 单元 | `tests/test_av_quality.py`、`tests/test_livetalking_{video,warmup}.py` | 帧停顿、A/V 到达偏差、嘴型 lag/correlation、codec 顺序、会话预热、缺数据 fail-closed | 普通 Python |
| 客户端单元 | `dsh-plugin/tests/reply-speaker.client.spec.ts` | 连续 PCM 零间隙调度、晚到 PCM 的精确静音时长、打断 fencing、队列上限 | Node 26 的 TS strip |
| Relay 集成 | `bridge/tests/test_avatar_relay.py` | 跨分段 seq/PTS 连续、串行上传、end marker、重注册隔离、有界队列 | bridge Python |
| 真实 E2E | `scripts/test-avatar-sync.sh` | `/offer` WebRTC 双轨、接收端帧卡顿、真实嘴部像素响应、LiveTalking 播放 underflow | 已启动的 LiveTalking + Avatar venv |

## 一键运行真实 E2E

先启动服务，再运行探针：

```bash
cd /path/to/xiaoman-dsh-local
./scripts/start-all.sh
./scripts/test-avatar-sync.sh --json-out logs/avatar-sync-latest.json
```

探针不会启动、停止或重配服务。它会：

1. 创建独立 LiveTalking WebRTC session，同时消费 audio/video track；
2. 先录制 1 秒真正的 idle 基线，再通过生产 `AvatarRelay` 注入仓库内固定人声
   `assets/xiaoman/voice/ref.wav`（7.2 秒、分成 400 ms 块）；
3. 用同一个接收时钟记录音频能量，并以“嘴部逐帧变化减去同尺寸上脸控制区变化”
   提取可见口型信号；像素转换在工作线程执行，避免探针自己阻塞音频接收；
4. 等待 `/is_speaking` 完成 speaking → idle；
5. 输出 JSON 并按结果返回 exit code。

退出码是稳定接口：

- `0`：所有质量阈值通过；
- `1`：真实运行完成，但出现质量回归；
- `2`：依赖、服务或媒体证据不完整，不能声称通过。

为避免 MPS/统一内存争用污染结果，正式基线应只运行一个 LiveTalking 实例；
已有浏览器会话也会各自创建一套 Wav2Lip pipeline。多实例结果可用于压力测试，
但不能替代单实例同步基线。

## 默认质量门槛

| 指标 | 默认门槛 | 含义 |
|---|---:|---|
| `audio_gap_max` | ≤ 100 ms | WebRTC 接收端不能出现更长的音频帧停顿 |
| `video_gap_max` | ≤ 200 ms | 25 fps 视频不能连续缺失 5 帧以上 |
| `av_delivery_skew_p95` | ≤ 120 ms | 音频帧到达时附近应有视频帧 |
| `lip_onset_offset_abs` | ≤ 240 ms | 持续可见口型启动相对首个有效人声帧的时间差 |
| `lip_correlation` | ≥ 0.12 | 嘴部像素响应必须确实跟随测试音频变化 |
| `underflow_events` | 0 | LiveTalking 实际播放队列不能见底 |
| `inserted_silence_ms` | 0 ms | 播放侧不能为缺帧补静音 |

`lip_lag_abs` 仍会输出，但在真实人声 E2E 中只作诊断：音频能量与逐帧嘴部运动
不是同一条音素时间线，不能把两者的相关峰当成绝对嘴型延迟。确定性单元测试会继续
验证已知同形信号的 lag 恢复；真实 E2E 的硬同步门槛是持续口型 onset。
在已收到有效语音和足够视频样本时，检测不到持续嘴部活动会明确判为 `fail`；只有
缺少语音、视频或 idle 基线等证据不足情形才判为 `incomplete`。

默认嘴部 ROI 是针对当前 `xiaoman-v3-original-idle` 素材校准的归一化矩形
`0.51 0.47 0.69 0.60`，上脸控制区为 `0.51 0.29 0.69 0.42`。更换数字人
素材时，应基于脸部位置显式覆盖，例如：

```bash
./scripts/test-avatar-sync.sh --mouth-roi 0.42 0.48 0.62 0.60
```

阈值也能用对应 CLI 参数覆盖，但基线变更必须保存多次同机测量证据，不能为了让失败变绿而放宽。

## 2026-08-23 本机 codec A/B 与修复快照

首轮失败最初看起来像 Wav2Lip 冷启动，但额外时间线显示 0.6–1.4 秒停顿会随机
出现在说话中段。独立模型基准的推理 p95 为 94 ms、max 为 149 ms，不足以解释
该停顿；真正的共同变量是 aiortc 协商到的 H.264 编码路径。

同一台机器、单一 Wav2Lip 实例、`l=10/r=10/inference_stride=4`、每轮均为全新
Avatar 进程：

| codec | 独立冷进程 | 音频最大 gap | 视频最大 gap | 结果 |
|---|---:|---:|---:|---:|
| H.264 | 3 | 552–675 ms | 609–1393 ms | **0/3 pass** |
| VP8 | 4 | 23–26 ms | 93–144 ms | **4/4 pass** |

DSH 启动脚本因此默认设置 `XIAOMAN_AVATAR_VIDEO_CODEC=VP8`。最终默认路径的
冷启动 + 同进程热态两轮均通过：视频最大 gap 为 `141.9/144.8/71.5 ms`，
音频最大 gap 为 `23.9/24.0/23.7 ms`，A/V p95 为 `14.1/14.9/17.9 ms`，
口型 onset 偏差绝对值为 `52.7/145.1/158.0 ms`，underflow 均为 `0`。
运行产物是 `logs/avatar-sync-vp8-default-{cold,hot-1,hot-2}.json`。

需要诊断回退时必须重启 Avatar，并显式运行：

```bash
XIAOMAN_AVATAR_VIDEO_CODEC=H264 ./scripts/start-avatar.sh
```

这只是 A/B 逃生开关，不是当前推荐的产品默认值。

### 满载冷页入修复

VP8 消除了编码器同时阻塞双轨的问题，但在完整服务栈（32K llama.cpp、Qwen3 TTS、
Whisper ASR 同时驻留）下还捕获到另一种独立冷启动：Avatar 权重被统一内存换出后，
首次说话推理在媒体开始后阻塞视频 `2124.6 ms`，而音频最大 gap 仍只有 `33.9 ms`。

启动器现在默认启用 `XIAOMAN_AVATAR_SESSION_WARMUP=1`。每个 Wav2Lip session
在 `/offer` 返回前执行一次真实脸帧推理，把可能的页入放在媒体时钟启动前。通过重载
TTS/ASR 将 Avatar RSS 压到约 12 MB 后复测，offer 前预热实际耗时 `1151.6 ms`；
媒体开始后视频最大 gap 为 `131.2 ms`，音频为 `23.9 ms`，A/V p95 为
`18.5 ms`，口型 onset 偏差为 `26.6 ms`，underflow 为 `0`，结果通过。
紧接着的热 session 预热只需 `80.7 ms`，视频/音频最大 gap 为
`78.6/23.5 ms`，同样通过。

失败基线与修复证据分别保存在：

- `logs/avatar-sync-vp8-full-stack.json`
- `logs/avatar-sync-vp8-session-warmup-evicted-full-stack.json`
- `logs/avatar-sync-vp8-session-warmup-hot-full-stack.json`

诊断时可以通过重启 Avatar 并设置
`XIAOMAN_AVATAR_SESSION_WARMUP=0` 暂时关闭，但产品默认应保持开启。

更早报告的 `560 ms` 来自“纯正弦音量包络 ↔ 嘴部平均暗度”的错误相关峰；
用人声、真实 idle 基线及上脸控制区复核后，不再把该伪相关作为生产回归。

## 快速回归命令

```bash
# 纯指标测试
python3 -m unittest \
  tests.test_av_quality \
  tests.test_livetalking_video \
  tests.test_livetalking_warmup -v

# Avatar relay 协议/队列测试
cd bridge
../.venv/bin/python -m unittest tests.test_avatar_relay -v
cd ..

# 浏览器 PCM 调度测试
node --experimental-strip-types --test \
  dsh-plugin/tests/reply-speaker.client.spec.ts
```

真实 E2E 是本机/预发布质量门，不应在没有 LiveTalking 和 Avatar venv 的普通 CI
中伪造通过。普通 CI 运行前三类确定性测试；具备模型的 runner 再运行
`test-avatar-sync.sh` 并保存 JSON artifact。
