# DSH 语音插件（ui-voice）

这是 DSH（deepseek-harness）的**客户端插件**：麦克风输入、⚡插话/排队开关、语音朗读开关、AI 女友动画窗、PCM 块级流式 TTS 朗读。它运行在 DSH 框架内（slot 系统、session prompt、locale），**不能独立运行**，必须装进 DSH 源码树。

## 它做了什么

| 组件 | 功能 |
|---|---|
| `MicButton` | 点一下连续聆听；静音 1.8s 端点；barge-in 打断监听（说话即停朗读） |
| `AgentModeToggle`（[DSH]/[Codex]） | 显式 agent 模式，默认 DSH；另选 `character=default|xiaoman`，不自动 Router |
| `AgentStatus` + durable `codex/*` nodes | 从 DSH session history 投影 Codex 状态；浏览器不直连 Codex 控制面 |
| `BusyToggle`（⚡） | 插话（steer）/ 排队（queue）投递模式开关，存 `s2s.voice.interrupt` |
| `VoiceToggle`（🔊） | 语音朗读开关，存 `s2s.voice.enabled` |
| `CompanionToggle`（🎬）+ `CompanionWindow` | 女友动画窗：`bg-images/` 空闲 / `task-videos/` 回复，30s 轮询素材 |
| `reply-listener` + `speaker` + `sentences` | 代理回复按句子切分 → PCM 流 → AudioContext 连续时间线；小满由 WebRTC 音轨播放 |

本部署不挂载 QQ toggle、QQ conversation bridge 或 QQ WebSocket；相关 legacy
源码仅保留作历史参考，不进入浏览器运行时 bundle。

桥接地址默认 `http://127.0.0.1:8765`；localStorage 的 `s2s.voice.bridge` 只接受
固定 loopback scheme/host/port 形状，任意远端地址会回退到默认值。
浏览器端 latency 事件默认开启；需要排查延迟时直接看 DevTools console，或用
`localStorage.setItem('s2s.voice.latency', '0')` 关闭。bridge 端对应配置为
`bridge-config.json` 的 `latency.enabled` / `latency.sample_rate`。

macOS/Linux 可从插件仓库根目录运行 `./scripts/start-bridge.sh` 或
`DSH_HARNESS=/absolute/path/to/deepseek-harness ./scripts/start-all.sh`；Windows
继续使用下方的 `.cmd` 步骤。完整数据流和依赖见
[`docs/upstream-architecture.md`](../docs/upstream-architecture.md)。

## Agent mode contract

- `[DSH]` is the default mode. STT text uses the session-scoped `sendText` path (DSH `session.prompt`).
- `[Codex]` dispatches through a typed DSH Remote to the Host-owned coordinator. The Host claims the Agent maintenance phase, persists `codex/user` + `codex/delegation-start`, and only then may reserve the bridge. It never enters the native AgentLoop or DSH tool loop.
- Each bridge STT/TTS request carries `X-DSH-Session-Id` (and JSON `session_id`) plus `character`. The client never handles Codex tokens. Signed-out identity may open only the exact `auth.openai.com/oauth/authorize` URL returned by pinned Codex; arbitrary hosts, subdomains and paths are rejected.
- On VAD `speech_start`, local order is `speaker.stop()` -> abort in-flight TTS/clear queue -> Codex `turn/interrupt` (Codex mode). The local stop metric never waits for STT.
- `assets/xiaoman/config/avatar.json` currently verifies idle only; bridge/UI fall back to idle when listening/thinking/speaking clips are missing.

The Host package owns the orchestration for the Host-only bridge endpoint `/api/codex/ws`; browser-origin Codex control requests are rejected. This client consumes only typed Remote methods and durable DSH conversation nodes. Mode switch/unmount disposes session-scoped listeners, AbortControllers and speaker jobs.

Codex turns are enabled in `read-only` mode through the ChatGPT Subscription backend. The bridge splits managed login/refresh from the execution App Server and gives the latter a credential-free HOME; no API key is required. Workspace-write and interactive approvals remain closed.

---

# 安装到 DSH（小白版）

## 推荐：一键准备 pinned DSH 并安装

从仓库根目录执行：

```bash
./scripts/bootstrap-dsh.sh
```

它读取根目录的 [`dsh.lock.json`](../dsh.lock.json)，把官方
`deepseek-harness` 固定到 lock 中的 commit，再把本目录完整同步到
`<HARNESS>/packages/client/ui-voice`，把 Host seam 放到
`<HARNESS>/packages/host/codex`，并管理七个精确注册锚点。默认目标是
`.runtime/deepseek-harness`；若已有干净且 pinned 的 checkout，可只执行：

```bash
./scripts/install-dsh-plugin.sh --harness "/absolute/path/to/deepseek-harness"
```

bootstrap 会在 checkout dirty 时停止；安装器在注册锚点漂移或插件文件被改动时会
停止。两条命令不会删除 manifest 外的用户源码；构建前只会按边界清理
`ui-voice`/`host-codex` 两个精确受控的 generated `lib`，随后由构建重新生成；构建后
仅清理有 canonical counterpart 且逐字节相同的、已校验的 macOS Finder ` 2`、` 3` 等
冲突副本；内容不同或无法识别的冲突会 fail closed。DSH checkout 的 `origin` 保持为官方 upstream；本目录是本项目源码，不是要
添加到 DSH 的 git remote。普通启动脚本不会自动联网，目标不存在时请先运行上述
bootstrap。

## 手动安装（仅用于排障）

日常流程不要手工复制或编辑 managed overlay；先用上面的 installer。Host/Remote、
persistence catalog、workspace lock 和七个锚点必须作为一个事务更新。以下只列出
排障目标，不提供容易造成半安装状态的手工复制步骤。

### 前置条件（已完成主 README「一、前置准备」的前提下）

- ✅ 一份 commit 与 `dsh.lock.json` 完全一致的 **deepseek-harness 源码树**
- ✅ Node.js 与 npm 可用；pnpm 由 bootstrap 按 pinned 版本调用

> 没做过？先回主 README 的「一、前置准备」第 6 步。

安装器管理根 `tsconfig.client.json`、根 `tsconfig.host.json`、
`packages/api/remotes` 的 client assembly/package/reference，以及 web-app 的 Cordis
mount 和 package dependency。任一锚点、lock、catalog 或已安装文件发生未记录漂移时
都会 fail closed；应先查看 installer 的固定错误，而不是手工覆盖。

### 构建验证

在 `<HARNESS>` 目录打开终端，依次执行：

```powershell
cd <HARNESS>
npm exec --yes --package=pnpm@11.7.0 -- pnpm install --frozen-lockfile
npm exec --yes --package=pnpm@11.7.0 -- pnpm run build:lib:host
npm exec --yes --package=pnpm@11.7.0 -- pnpm run build:lib:client
npm exec --yes --package=pnpm@11.7.0 -- pnpm --filter @deepseek-ai/dsh-client-ui-voice bundle
```

> - 四行都要跑，**前一行成功后再跑下一行**（任何一行报错先看文末 FAQ）。
> - Windows 下不要用项目里某些脚本自带的 `rm` 命令，按上面顺序手动执行即可。

### 重启并验证

**重启 dsh web**（新增插件必须重启，插件清单启动时确定；只刷页面不行）。

启动后按 `F12` 打开浏览器控制台，应看到：

```
[ui-voice] loaded, bridge = http://127.0.0.1:8765
```

输入栏工具行出现：🔊 🎬 ⚡ 🎙️（顺序：朗读、女友窗、插话开关、麦克风）。

> 看到这条日志但麦克风点不了 → 检查桥接是否已启动（`bridge\start-bridge.cmd`）。

---

# 构建 FAQ

| 现象 | 原因 / 解决 |
|---|---|
| `tsc` 报 `TS6133: 'xxx' is declared but its value is never read` | 某处 import 没用到，删掉那个 import 再跑 |
| `tsc` 报找不到 `@deepseek-ai/dsh-client-ui-conversation/client` 等 | `pnpm install` 没跑，或 workspace 链接没建好，重跑 `pnpm install` |
| `bundle` 报 `rm: command not found` | Windows 没有 `rm`，手动先跑 `tsc` 再跑 bundle 那行 |
| 重启后没有麦克风按钮 | managed overlay/七个注册锚点未完整安装；重跑 installer/bootstrap 并重启 dsh web |
| 控制台报 CORS / fetch 失败 | 桥接没启动，或 `bridge-config.json` 的 `cors_origins` 没包含 `http://127.0.0.1:3080` |

---

# 源码结构

```
src/client/
├── AgentModeToggle.tsx      # explicit DSH/Codex selector
├── AgentStatus.tsx           # observable event-bus projection
├── agent-mode.ts             # session-scoped mode、owner 与 hydration fence
├── codex-conversation.ts     # exact durable codex/* reducer
├── codex-remote-client.ts    # typed DSH Remote client；不持有 Codex WS
├── CodexComposer.tsx         # 旧版独立 composer（保留源码，不再注册）
├── index.ts                  # 插件入口：原生 composer 路由、DSH sendText 与 Remote owner
├── bridge.ts                # 桥接 HTTP 封装（stt/tts/media）
├── contract.ts              # 注入给组件的接口（sendText/speaker/companion/…）
├── MicButton.tsx            # 麦克风 + 连续聆听 + barge-in
├── BusyToggle.tsx           # ⚡ 插话/排队开关
├── VoiceToggle.tsx          # 🔊 朗读开关
├── CompanionToggle.tsx      # 🎬 女友窗开关
├── locales.ts               # zh/en 文案
└── voice/
    ├── native-composer-route.ts # 在原生输入框内切换 DSH/Codex 后端
    ├── recorder.ts          # 采集 + 静音端点 + 打断检测
    ├── reply-listener.tsx   # 监听回复 → 句子级 TTS 流式
    ├── speaker.ts           # AudioContext 播放队列（可打断）
    ├── sentence-assembler.ts # streaming final-answer assembler/max-wait/code fence
    ├── sentences.ts         # 中文句子切分 + 纯标点过滤
    ├── companion.tsx        # 女友动画窗（拖宽/换边）
    └── companion-controller.ts
```

## Lightweight tests

纯 Python provider/asset/bridge registry tests 不下载模型：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest bridge.tests.test_xiaoman_v3_adapters -v
```

插件 TypeScript 测试位于 `dsh-plugin/tests/`，需要在实际 DSH tree 里使用其现有
TypeScript/Vitest 工具链运行完整 package typecheck；本独立仓库不安装 node_modules。
源码树测试使用 pinned DSH 已安装的 `tsx` 与本包 tsconfig；裸
`node --experimental-strip-types` 不能处理本包的 TypeScript/Workspace imports：

```bash
.runtime/deepseek-harness/node_modules/.bin/tsx \
  --tsconfig dsh-plugin/tsconfig.client.json \
  --test dsh-plugin/tests/*.client.spec.ts
```

性能报告模板和无模型 benchmark 见 [`docs/performance-report.md`](../docs/performance-report.md)。
