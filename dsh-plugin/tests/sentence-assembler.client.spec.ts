import test from 'node:test'
import assert from 'node:assert/strict'
import { SentenceAssembler } from '../src/client/voice/sentence-assembler.ts'

test('assembles Chinese and English terminal punctuation', () => {
  const assembler = new SentenceAssembler({ maxWaitMs: 0 })
  const chunks = assembler.push('你好。Hello!')
  assert.deepEqual(chunks.map((chunk) => chunk.text), ['你好。', 'Hello!'])
})

test('cleans markdown and does not speak fenced code by default', () => {
  const assembler = new SentenceAssembler({ maxWaitMs: 0 })
  const chunks = assembler.push('# 标题\n- 第一项。\n```ts\nconst secret = 1;\n```\n最后一句。')
  assert.deepEqual(chunks.map((chunk) => chunk.text), ['标题 第一项。', '最后一句。'])
})

test('bounds an overlong sentence at a readable soft break', () => {
  const assembler = new SentenceAssembler({ maxChars: 16, maxWaitMs: 0 })
  const chunks = assembler.push('这是一个没有终止标点但必须被切开的超长句子，用于测试。')
  assert.ok(chunks.length >= 2)
  assert.ok(chunks.every((chunk) => chunk.text.length <= 16))
})

test('final snapshot does not replay chunks already emitted by max_chars splitting', () => {
  const assembler = new SentenceAssembler({ maxChars: 16, maxWaitMs: 0 })
  const full = '这是一个没有终止标点但必须被切开的超长句子，用于测试。'
  const streamed = assembler.push(full)
  const flushed = assembler.finish(full)
  assert.equal(flushed.length, 0)
  assert.equal(streamed.length > 0, true)
})

test('finish flushes unfinished final text exactly once', () => {
  const assembler = new SentenceAssembler({ maxWaitMs: 0 })
  assembler.push('还没有句号')
  const chunks = assembler.finish('还没有句号')
  assert.deepEqual(chunks.map((chunk) => chunk.text), ['还没有句号'])
  assert.deepEqual(assembler.finish().map((chunk) => chunk.text), [])
})

test('flushExpired emits a stalled fragment at max_wait_ms', () => {
  const assembler = new SentenceAssembler({ maxWaitMs: 500, now: () => 0 })
  assembler.push('等待 flush')
  assert.deepEqual(assembler.flushExpired(499), [])
  assert.deepEqual(assembler.flushExpired(500).map((chunk) => chunk.reason), ['timeout'])
})

test('default sentence chunks stay within the server 512-character TTS boundary', () => {
  const assembler = new SentenceAssembler({ maxWaitMs: 0 })
  const chunks = [...assembler.push('长'.repeat(700)), ...assembler.finish()]
  assert.ok(chunks.length >= 2)
  assert.equal(chunks.every(chunk => chunk.text.length <= 512), true)
})
