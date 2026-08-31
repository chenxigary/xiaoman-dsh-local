# Xiaoman DSH Local

小满的 macOS 私有发行版：对话、听写、语音合成和数字人都在本机运行，不需要
OpenAI、Anthropic 或 DeepSeek API key。目标机器是 Apple Silicon，默认针对
M4 / 64GB 统一内存提供高性能档。

> 这是私人仓库。仓库包含个人参考声音和形象素材；私有 Release 还包含
> Wav2Lip、S3FD、预处理 Avatar 缓存和 Silero VAD 权重。不要改成 public。

## 5 分钟路径

新 Mac 先安装 Xcode Command Line Tools 和 [Homebrew](https://brew.sh/)，然后：

```bash
xcode-select --install
brew install gh
gh auth login
gh auth setup-git

gh repo clone chenxigary/xiaoman-dsh-local
cd xiaoman-dsh-local

# 安装公开依赖、固定源码、下载并校验私有素材、创建运行环境
./scripts/setup-macos-local.sh

# 第一次：允许下载缺失的 Qwen / Whisper / Qwen3-TTS 公开权重
./scripts/run-local.sh --online
```

以后日常启动默认严格使用本地缓存：

```bash
./scripts/run-local.sh
```

启动成功后会打开 <http://127.0.0.1:3080>。所有服务只绑定 loopback。

## “只用本地模型”的边界

| 能力 | 本地实现 | 默认端口/状态 |
|---|---|---|
| 对话 LLM | llama.cpp + Qwen3 GGUF | `127.0.0.1:8090` |
| ASR | Xiaoman v3 + MLX Whisper | Voice Runtime `:7860` |
| TTS | Xiaoman v3 + MLX Qwen3-TTS | Voice Runtime `:7860` |
| 数字人 | LiveTalking + Wav2Lip | `127.0.0.1:8010` |
| 浏览器桥 | FastAPI voice bridge | `127.0.0.1:8765` |
| UI / 会话 | 固定版本 DeepSeek Harness | `127.0.0.1:3080` |
| DeepSeek 云模型/搜索 | checked-in overlay 禁用 | 不可用 |
| Codex / ChatGPT Subscription | 配置、bridge 和 provider 三层禁用 | 不可用 |
| QQ / NapCat | 默认禁用，不属于 macOS 支持面 | 不启动 |

`--online` 只用于首次下载公开模型权重；生成仍由下载到本机的模型完成。日常不加
`--online` 时，llama.cpp 使用 `--offline`，Hugging Face/Transformers 也进入离线模式。

## M4 / 64GB 性能档

```bash
./scripts/run-local.sh --profile auto          # 推荐
./scripts/run-local.sh --profile efficient     # 4B / 4K / 0.6B TTS
./scripts/run-local.sh --profile balanced      # 8B / 8K / 0.6B TTS
./scripts/run-local.sh --profile performance   # 14B / 16K / 1.7B TTS
```

`auto` 读取 `sysctl hw.memsize`：低于 28GB 选 efficient，28–55GB 选 balanced，
56GB 及以上选 performance。因此 M4 / 64GB 会自动使用 Qwen3-14B Q4、16K context、
单并行槽和 Qwen3-TTS 1.7B 4-bit。

如果先想验证基础链路，不让 Wav2Lip 参与资源竞争：

```bash
./scripts/run-local.sh --online --profile efficient --no-avatar
```

也可以用 `LOCAL_LLM_MODEL_PATH=/absolute/model.gguf` 覆盖 GGUF；覆盖时仍只在本机加载。

## 安装脚本做什么

[`scripts/setup-macos-local.sh`](scripts/setup-macos-local.sh) 会：

1. 要求 macOS + Apple Silicon，并检查 Xcode CLI 与 Homebrew；
2. 安装缺失的 `gh`、`uv`、`llama.cpp`、`ffmpeg`、Node.js；
3. 确认本仓库仍是 GitHub private；
4. 按 [`xiaoman.local.lock.json`](xiaoman.local.lock.json) 固定
   `chenxigary/macos-local-voice-agents` 的 commit；
5. 从本仓库私有 Release 下载 `xiaoman-assets.zip`，先校验整体 SHA-256；
6. 导入参考声音、Avatar、Wav2Lip/S3FD、预处理缓存和 Silero VAD，并逐项校验；
7. 创建 Xiaoman v3 与本仓库 Python 环境；
8. 按 [`dsh.lock.json`](dsh.lock.json) 固定 DeepSeek Harness 并安装本地插件。

只读复查，不写入任何内容：

```bash
./scripts/setup-macos-local.sh --check
```

如果素材已经在移动硬盘解压，可避免重新下载：

```bash
./scripts/setup-macos-local.sh --assets-from /Volumes/Transfer/xiaoman-assets
```

该目录必须同时包含 `xiaoman-v3/` 和 `dsh-local/`。路径只是示例。

## 素材与模型放在哪里

- Git 仓库：`assets/xiaoman/` 的参考声音、idle 素材、manifest，以及
  `assets/bg-images/` 的现有背景视频。
- 私有 Release `xiaoman-dsh-assets-v1`：约 305MB 的个人/运行素材和小型推理权重。
- `.runtime/macos-local-voice-agents/`：固定的 Xiaoman v3 源码和其 Python 环境。
- `.runtime/deepseek-harness/`：固定的 DSH 源码与 `node_modules`。
- `models/silero-vad/`：从已校验私有 Release 导入，Git 忽略。
- Hugging Face / llama.cpp cache：首次 `--online` 下载的大型公开模型；不进 Git 和 Release。

Release 和缓存分开是刻意的：私人素材要跟随私有仓库；数 GB 的公开模型由各自官方
仓库下载，避免 GitHub 100MB object 限制，也不复制上游模型许可证责任。

## 日常操作

```bash
# 当前状态，并验证 bridge local_only=true、Codex disabled
./scripts/run-local.sh --status

# 不自动打开浏览器
./scripts/run-local.sh --no-open

# 释放整套本地进程；只处理本仓库记录且身份匹配的 PID
./scripts/stop-local.sh

# 只释放 llama.cpp 权重
./scripts/stop-local-llm.sh

# 空闲 5 分钟后让支持该能力的 llama.cpp 自动卸载模型
LOCAL_LLM_IDLE_SLEEP_SECONDS=300 ./scripts/run-local.sh
```

日志在 `logs/`，PID 记录在 `.run/`。二者都不进入 Git。

## 验证

不启动真实模型的回归：

```bash
./scripts/smoke-check.sh
```

运行中服务的真实状态：

```bash
./scripts/status-local.sh
curl -fsS http://127.0.0.1:8090/health
curl -fsS http://127.0.0.1:7860/api/voice-runtime/v1/health
curl -fsS http://127.0.0.1:8765/api/health
curl -fsS http://127.0.0.1:8765/api/codex/health
```

真实 WebRTC / 嘴型连续性：

```bash
./scripts/test-avatar-sync.sh --json-out logs/avatar-sync-latest.json
```

质量门不是“HTTP 能打开”这么简单：audio gap ≤100ms、video gap ≤200ms、A/V skew
p95 ≤120ms、lip onset 绝对偏差 ≤240ms、correlation ≥0.12，并且 underflow 与补静音为 0。

## 当前证据与未知项

- 本次打包前的无模型回归为 228 个 Python tests（2 skipped）、105 个 DSH 插件
  tests 和 49 个 Host tests，全部通过；固定 DSH checkout 的完整 install、typecheck、
  Host/Client bundle 也已通过。
- 旧 16GB 机器上的重复对照：Avatar-only 10/10；LLM + Voice Runtime + Avatar 全栈
  1/10。证据指向统一内存/MPS 调度争用，而不是单纯阈值问题。
- M4 / 64GB 的完整冷启动、真实麦克风、持续对话和 5× cold/hot Avatar 组还没有跑；
  README 中的 64GB 档是明确配置，不是已经验证的性能结论。

## Troubleshooting

### `gh auth` 或私有 Release 下载失败

```bash
gh auth status
gh auth login
gh auth setup-git
gh repo view chenxigary/xiaoman-dsh-local --json isPrivate,visibility
```

必须看到 `isPrivate: true` / `PRIVATE`。不要为了方便把仓库或 Release 公开。

### offline 模式找不到模型

首次运行或切换到新 profile 时执行一次：

```bash
./scripts/run-local.sh --online --profile performance
```

模型下载完成后停止，再用默认 offline 启动。

### 端口占用或启动到了别的 checkout

```bash
lsof -nP -iTCP:8090 -iTCP:8010 -iTCP:7860 -iTCP:8765 -iTCP:3080 -sTCP:LISTEN
./scripts/stop-local.sh
```

`stop-local.sh` 遇到 PID owner 或 command 不匹配会拒绝终止；先人工确认占用者，不要直接
批量 kill。

### UI 能打开但没有声音

先看 `logs/voice-runtime.log` 和 `logs/voice-bridge.log`，然后执行：

```bash
./scripts/setup-macos-local.sh --check
./scripts/status-local.sh
```

不要把一次 HTTP health PASS 当作真实语音链路通过。

## Recommended next steps

在 M4 / 64GB 新机上建议按这个顺序推进：

1. 运行 `setup-macos-local.sh --check`，保存硬件、版本和素材校验结果。
2. 用 `--profile efficient --no-avatar --online` 做最小冷启动；验证真实麦克风、ASR、
   首个 TTS PCM 和浏览器播放。
3. 切换 `--profile auto --online`，确认实际解析为 performance，并记录 14B/1.7B 的
   首 token、首音频、RTF、峰值内存和 swap。
4. 开启 Avatar，执行至少 5 组 cold/hot `test-avatar-sync.sh`，不要用单次 PASS 晋级。
5. 若全栈仍不稳，按 Avatar + LLM、Avatar + Voice Runtime、TTS-only、ASR-only 的顺序
   逐项加回；优先处理资源调度/模型生命周期，不放宽质量门。
6. 只有 14B/1.7B 全栈稳定后，再评估 30B-A3B 或更长 context；保持单并行槽。
7. 稳定性证据完成后，再把结果和推荐默认档更新到 README；当前不要宣称 M4/64GB 已验收。

## 项目结构

```text
bridge/                 loopback voice bridge 与 v3 adapter
scripts/                安装、启动、状态、停止、质量测试
config/                 local-Qwen DSH overlay 与小满 persona
assets/xiaoman/         私人参考声音/idle 素材与 hash manifest
dsh-plugin/             浏览器端语音/数字人插件
dsh-host-codex/         保留的历史协议代码；本发行版执行硬关闭
agents/codex/           保留的审计与测试代码；本发行版执行硬关闭
docs/                   架构、实验和历史设计证据
```

Windows/QQ/Codex 是来源项目的历史能力，不属于这个 local-only Mac 发行版的支持面。
