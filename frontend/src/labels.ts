const modelNames: Record<string, string> = {
  deepseek: 'DeepSeek',
  doubao: '豆包',
}

export const displayModelName = (modelId: string) => modelNames[modelId] || modelId

export const displayModelNames = (modelIds: string[]) => (
  modelIds.length ? modelIds.map(displayModelName).join(' + ') : '未验证'
)

const doubaoWebStateNames: Record<string, string> = {
  disabled: '未启用',
  stopped: '尚未连接',
  browser_stopped: '专用浏览器未启动',
  connecting: '正在连接',
  waiting_for_login: '等待人工验证，完成后自动继续',
  challenge: '检测到可见安全验证，完成后自动继续',
  ready: '已就绪',
  working: '正在查询',
  reconnecting: '正在重新连接',
  timeout: '普通超时，自动继续',
  unavailable: '连接不可用',
}

export const doubaoWebStateLabel = (state: string | undefined, detail?: string) => {
  if (state === 'working' && detail?.startsWith('等待豆包网页查询间隔')) {
    return detail.replace('等待豆包网页查询间隔', '查询冷却中')
  }
  return state ? doubaoWebStateNames[state] || state : '正在检查'
}

export const isDoubaoWebReady = (state: string | undefined) => (
  state === 'ready' || state === 'working'
)

export const createSequentialPoller = (
  operation: () => Promise<unknown>,
  schedule: (callback: () => void, delayMs: number) => number,
  cancel: (handle: number) => void,
  delayMs: number,
) => {
  let active = true
  let timer: number | undefined

  const run = async () => {
    try {
      await operation()
    } finally {
      if (active) timer = schedule(() => { void run() }, delayMs)
    }
  }

  void run()
  return () => {
    active = false
    if (timer !== undefined) {
      cancel(timer)
      timer = undefined
    }
  }
}

export const shouldResetLeadRows = (
  previousTaskId: string | null | undefined,
  previousCity: string,
  nextTaskId: string | null | undefined,
  nextCity: string,
) => previousTaskId !== nextTaskId || previousCity !== nextCity

export const leadResultRefreshKey = (meta: {
  taskId: string | null | undefined
  city: string
  status: string | null | undefined
  candidateCount: number | null | undefined
  enrichmentCompleted: number | null | undefined
  leadCount: number | null | undefined
  rawContactLeadCount: number | null | undefined
  contactLeadCount: number | null | undefined
  verificationState: string | null | undefined
  roundPhase: string | null | undefined
  updatedAt?: string | null
}) => [
  // A timestamp changes on every progress write; result rows only need data-affecting fields.
  meta.taskId ?? '',
  meta.city,
  meta.status ?? '',
  meta.candidateCount ?? 0,
  meta.enrichmentCompleted ?? 0,
  meta.leadCount ?? 0,
  meta.rawContactLeadCount ?? 0,
  meta.contactLeadCount ?? 0,
  meta.verificationState ?? '',
  meta.roundPhase ?? '',
].join(':')

export const leadCountSummary = (
  rawCount: number | null | undefined,
  verifiedCount: number | null | undefined,
  fallbackCount = 0,
  verifiedLabel = '核验有效',
) => {
  const raw = rawCount ?? verifiedCount ?? fallbackCount
  const verified = verifiedCount ?? 0
  return `${raw} 有联系方式 · ${verified} ${verifiedLabel}`
}

export const targetContactLabel = (mode: string | null | undefined) => (
  mode === 'evidence_backed' ? '有来源联系方式' : '核验有效'
)

export const hasDeliveryContact = (lead: { mobile?: string | null; email?: string | null }) => (
  Boolean(lead.mobile?.trim() || lead.email?.trim())
)

export const DELIVERY_STALE_AFTER_MS = 5 * 60 * 1000

export const isDeliveryInFlight = (
  lead: { delivery_status?: string; delivery_started_at?: string | null },
  now = Date.now(),
) => {
  if (lead.delivery_status !== 'sending') return false
  const startedAt = Date.parse(lead.delivery_started_at || '')
  return Number.isFinite(startedAt) && now - startedAt < DELIVERY_STALE_AFTER_MS
}

export const visibleLeadColumnLabels = [
  '城市',
  '公司名称',
  '公司名称核验',
  '手机',
  '联系人',
  '职务',
  '联系证据',
  '邮箱',
  '微信',
  '地址',
  '服务简介',
  'ERP 推送',
  '人工推送',
  '保存时间',
  '发现渠道',
  '来源链接',
  '核验模型',
]
