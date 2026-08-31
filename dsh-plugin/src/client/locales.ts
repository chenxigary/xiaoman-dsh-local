/** `voice` namespace dictionaries. */

/** Simplified Chinese dictionary (the key-set source of truth). */
export const zh = {
  'mic.title': '语音输入',
  'mic.listening': '正在聆听…再点一下停止',
  'mic.transcribing': '识别中…',
  'mic.error': '语音输入不可用',
  'toggle.onHint': '开启语音朗读',
  'toggle.offHint': '关闭语音朗读',
  'companion.onHint': '显示女友窗',
  'companion.offHint': '隐藏女友窗',
  'interrupt.onHint': '插话模式：说话打断当前回复并立即发送（点击切换为排队）',
  'interrupt.offHint': '排队模式：当前回复结束后自动发送，连续对话（点击切换为插话）',
  'qqpush.onHint': '开启 QQ 推送（回复自动发到 QQ）',
  'qqpush.offHint': '关闭 QQ 推送（回复不再发到 QQ）',
  'model.model': '模型',
  'model.effort': '推理强度',
  'model.speed': '速度',
  'model.standard': '标准（省用量）',
  'model.fast': 'Fast（增加用量）',
  'model.loading': '加载模型…',
  'model.retry': '重试',
  'model.error': '模型目录不可用',
} satisfies Record<string, string>

/** The voice namespace key union. */
export type VoiceKey = keyof typeof zh

/** The fallback dictionary also keeps product-visible copy in Chinese. */
export const en = {
  'mic.title': '语音输入',
  'mic.listening': '正在聆听…再点一下停止',
  'mic.transcribing': '识别中…',
  'mic.error': '语音输入不可用',
  'toggle.onHint': '开启语音朗读',
  'toggle.offHint': '关闭语音朗读',
  'companion.onHint': '显示女友窗',
  'companion.offHint': '隐藏女友窗',
  'interrupt.onHint': '插话模式：说话打断当前回复并立即发送（点击切换为排队）',
  'interrupt.offHint': '排队模式：当前回复结束后自动发送，连续对话（点击切换为插话）',
  'qqpush.onHint': '开启 QQ 推送（回复自动发到 QQ）',
  'qqpush.offHint': '关闭 QQ 推送（回复不再发到 QQ）',
  'model.model': '模型',
  'model.effort': '推理强度',
  'model.speed': '速度',
  'model.standard': '标准（省用量）',
  'model.fast': 'Fast（增加用量）',
  'model.loading': '加载模型…',
  'model.retry': '重试',
  'model.error': '模型目录不可用',
} satisfies Record<VoiceKey, string>
