import assert from 'node:assert/strict'
import test from 'node:test'

import { DELIVERY_STALE_AFTER_MS, createSequentialPoller, displayModelName, displayModelNames, doubaoWebStateLabel, hasDeliveryContact, isDeliveryInFlight, isDoubaoWebReady, leadCountSummary, leadResultRefreshKey, shouldResetLeadRows, targetContactLabel, visibleLeadColumnLabels } from './labels.ts'

test('displays configured model ids with user-facing names', () => {
  assert.equal(displayModelName('doubao'), '豆包')
  assert.equal(displayModelName('deepseek'), 'DeepSeek')
  assert.equal(displayModelName('custom-model'), 'custom-model')
})

test('displays multiple model names without exposing internal ids', () => {
  assert.equal(displayModelNames(['doubao', 'deepseek']), '豆包 + DeepSeek')
  assert.equal(displayModelNames([]), '未验证')
})

test('hides internal verification columns from the lead table', () => {
  for (const hidden of ['命中关键词', '可信度', '等级', '手机核验', '核验状态']) {
    assert.equal(visibleLeadColumnLabels.includes(hidden), false)
  }
  assert.deepEqual(visibleLeadColumnLabels.slice(0, 5), [
    '城市', '公司名称', '公司名称核验', '手机', '联系人',
  ])
})

test('starts the web-chat collection path when Doubao can accept queued work', () => {
  assert.equal(isDoubaoWebReady('ready'), true)
  assert.equal(isDoubaoWebReady('working'), true)
  assert.equal(isDoubaoWebReady('waiting_for_login'), false)
  assert.equal(isDoubaoWebReady(undefined), false)
})

test('shows dedicated browser connection states in Chinese', () => {
  assert.equal(doubaoWebStateLabel('browser_stopped'), '专用浏览器未启动')
  assert.equal(doubaoWebStateLabel('connecting'), '正在连接')
  assert.equal(doubaoWebStateLabel('waiting_for_login'), '等待人工验证，完成后自动继续')
  assert.equal(doubaoWebStateLabel('challenge'), '检测到可见安全验证，完成后自动继续')
  assert.equal(doubaoWebStateLabel('reconnecting'), '正在重新连接')
  assert.equal(doubaoWebStateLabel('ready'), '已就绪')
})

test('distinguishes a recoverable timeout from manual verification', () => {
  assert.equal(doubaoWebStateLabel('timeout'), '普通超时，自动继续')
})

test('shows pacing cooldown instead of implying an active query', () => {
  assert.equal(
    doubaoWebStateLabel('working', '等待豆包网页查询间隔，剩余约 14 分钟'),
    '查询冷却中，剩余约 14 分钟',
  )
})

test('waits for a task refresh to settle before scheduling the next poll', async () => {
  const scheduled: Array<() => void> = []
  let releaseFirstPoll: (() => void) | undefined
  let calls = 0
  const stop = createSequentialPoller(
    () => {
      calls += 1
      if (calls === 1) return new Promise<void>((resolve) => { releaseFirstPoll = resolve })
      return Promise.resolve()
    },
    (callback) => { scheduled.push(callback); return scheduled.length },
    () => undefined,
    1500,
  )

  await Promise.resolve()
  assert.equal(calls, 1)
  assert.equal(scheduled.length, 0)
  releaseFirstPoll?.()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(scheduled.length, 1)
  scheduled[0]()
  await Promise.resolve()
  assert.equal(calls, 2)
  stop()
})

test('keeps lead rows during progress refreshes and resets only when the query changes', () => {
  assert.equal(shouldResetLeadRows('task-1', '深圳市', 'task-1', '深圳市'), false)
  assert.equal(shouldResetLeadRows('task-1', '深圳市', 'task-2', '深圳市'), true)
  assert.equal(shouldResetLeadRows('task-1', '深圳市', 'task-1', '广州市'), true)
})

test('does not reload lead rows for an updated task timestamp alone', () => {
  const before = leadResultRefreshKey({
    taskId: 'task-1', city: '', status: 'running', candidateCount: 16,
    enrichmentCompleted: 4, leadCount: 4, rawContactLeadCount: 4,
    contactLeadCount: 1, verificationState: 'not_started', roundPhase: 'enriching',
    updatedAt: '2026-08-01T01:00:00Z',
  })
  const after = leadResultRefreshKey({
    taskId: 'task-1', city: '', status: 'running', candidateCount: 16,
    enrichmentCompleted: 4, leadCount: 4, rawContactLeadCount: 4,
    contactLeadCount: 1, verificationState: 'not_started', roundPhase: 'enriching',
    updatedAt: '2026-08-01T01:01:00Z',
  })
  assert.equal(after, before)
})

test('distinguishes collected mobile rows from evidence-verified rows', () => {
  assert.equal(leadCountSummary(55, 24), '55 有联系方式 · 24 核验有效')
  assert.equal(leadCountSummary(undefined, undefined, 3), '3 有联系方式 · 0 核验有效')
  assert.equal(targetContactLabel('evidence_backed'), '有来源联系方式')
  assert.equal(targetContactLabel('strict'), '核验有效')
})

test('treats an email-only lead as ready for manual delivery', () => {
  assert.equal(hasDeliveryContact({ mobile: null, email: 'sales@example.com' }), true)
  assert.equal(hasDeliveryContact({ mobile: null, email: '  ' }), false)
  assert.equal(hasDeliveryContact({ mobile: null, email: null }), false)
})

test('lets a stale sending row be retried instead of blocking the button forever', () => {
  const now = Date.parse('2026-08-05T08:00:00Z')
  assert.equal(isDeliveryInFlight({ delivery_status: 'sending', delivery_started_at: '2026-08-05T07:59:00Z' }, now), true)
  assert.equal(isDeliveryInFlight({ delivery_status: 'sending', delivery_started_at: '2026-08-05T07:54:00Z' }, now), false)
  assert.equal(isDeliveryInFlight({ delivery_status: 'sending', delivery_started_at: null }, now), false)
  assert.equal(DELIVERY_STALE_AFTER_MS, 5 * 60 * 1000)
})
