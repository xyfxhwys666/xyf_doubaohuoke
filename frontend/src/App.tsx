import { useCallback, useEffect, useRef, useState } from 'react'
import { Download, MonitorUp, Pause, Play, RefreshCw, Search, Send, Settings2, ShieldCheck, Square } from 'lucide-react'
import { api } from './api'
import { DEFAULT_ERP_DELIVERY_URL } from './config'
import { createSequentialPoller, displayModelNames, doubaoWebStateLabel, hasDeliveryContact, isDeliveryInFlight, isDoubaoWebReady, leadCountSummary, leadResultRefreshKey, shouldResetLeadRows, targetContactLabel, visibleLeadColumnLabels } from './labels'
import type { CollectionTask, DeliveryStatus, DoubaoWebStatus, Lead, ModelOption, TaskStatus } from './types'

const statusLabel: Record<TaskStatus, string> = { draft: '待启动', queued: '排队中', running: '采集中', pausing: '正在暂停', paused: '已暂停', waiting_for_login: '等待人工验证', completed: '已完成', completed_with_errors: '完成（有异常）', terminating: '正在终止', terminated: '已终止' }
const deliveryLabel: Record<DeliveryStatus, string> = { display_only: '仅展示', pending: '待推送', sending: '推送中', succeeded: '推送成功', failed: '推送失败' }
const companyNameVerificationLabel: Record<string, string> = { verified_public_web: '公开网页已核验', verified_registry: '工商已核验', unverified: '证据不足', rejected: '不符合', verification_failed: '核验失败' }
const sourceLabel: Record<string, string> = { web_search: '公开网页', deepseek: 'DeepSeek 候选', doubao: '豆包候选' }
const contactLabel: Record<string, string> = { mobile: '手机', email: '邮箱', wechat: '微信' }
const phaseLabel: Record<string, string> = { discovering: '正在发现候选公司', verifying_company_names: '正在核验公司名称', enriching: '正在逐家公司补全', completed: '采集完成' }
const roundPhaseLabel: Record<string, string> = { discovering: '发现候选', enriching: '豆包补全联系方式', verifying: '模型核验', delivering: '推送 ERP', cooldown: '等待下一轮' }
const canTerminate = (status: TaskStatus) => !['completed', 'completed_with_errors', 'terminating', 'terminated'].includes(status)
const isHttpUrl = (value: string | null | undefined) => /^https?:\/\//i.test(value || '')
type ExportFormat = 'xlsx' | 'csv' | 'json'
type ControlAction = 'pause' | 'resume' | 'terminate'
const formatCreatedAt = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }

export default function App() {
  const [requestText, setRequestText] = useState('我是做家具的，寻找全国三线及以上城市的全案软装公司、设计公司和整装公司，需要手机号码，尽量多找。')
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([])
  const [selectedModels, setSelectedModels] = useState<string[]>([])
  const [exportFormat, setExportFormat] = useState<ExportFormat>('xlsx')
  const [tasks, setTasks] = useState<CollectionTask[]>([])
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [leads, setLeads] = useState<Lead[]>([])
  const [cityFilter, setCityFilter] = useState('')
  const [error, setError] = useState('')
  const [isStarting, setIsStarting] = useState(false)
  const [controlActions, setControlActions] = useState<Record<string, ControlAction>>({})
  const [continuousMode, setContinuousMode] = useState(false)
  const [verifiedAutoDeliver, setVerifiedAutoDeliver] = useState(false)
  const [deliveryConfigured, setDeliveryConfigured] = useState(false)
  const [deliveryUrl, setDeliveryUrl] = useState(DEFAULT_ERP_DELIVERY_URL)
  const [deliveryToken, setDeliveryToken] = useState('')
  const [deliveryMessage, setDeliveryMessage] = useState('')
  const [isDelivering, setIsDelivering] = useState(false)
  const [manualDeliveryBusy, setManualDeliveryBusy] = useState<Record<string, boolean>>({})
  const [doubaoWebStatus, setDoubaoWebStatus] = useState<DoubaoWebStatus | null>(null)
  const [isOpeningDoubao, setIsOpeningDoubao] = useState(false)
  const [isReconnectingDoubao, setIsReconnectingDoubao] = useState(false)
  const taskRequestToken = useRef(0)
  const leadRequestToken = useRef(0)
  const leadQueryRef = useRef<{ taskId: string | null; city: string } | null>(null)
  const task = tasks.find((item) => item.id === selectedTaskId) ?? null
  const progress = task?.total_units ? Math.round(task.completed_units / task.total_units * 100) : 0
  const candidateProgress = task?.candidate_target ? Math.min(100, Math.round(task.candidate_count / task.candidate_target * 100)) : 0
  const enrichmentProgress = task?.enrichment_total ? Math.round(task.enrichment_completed / task.enrichment_total * 100) : 0
  const cityRows = task ? Object.values(task.city_progress) : []
  const resultCities = task?.cities ?? []
  const leadRefreshKey = leadResultRefreshKey({
    taskId: task?.id,
    city: cityFilter,
    status: task?.status,
    candidateCount: task?.candidate_count,
    enrichmentCompleted: task?.enrichment_completed,
    leadCount: task?.lead_count,
    rawContactLeadCount: task?.raw_contact_lead_count,
    contactLeadCount: task?.contact_lead_count,
    verificationState: task?.verification_state,
    roundPhase: task?.round_phase,
  })
  const selectedModelLabels = selectedModels.map((modelId) => modelOptions.find((item) => item.id === modelId)?.label || modelId)
  const hybridPathReady = isDoubaoWebReady(doubaoWebStatus?.state)
  const replaceTask = useCallback((nextTask: CollectionTask) => setTasks((current) => current.map((item) => item.id === nextTask.id ? nextTask : item)), [])
  const refreshTasks = useCallback(async () => {
    const token = ++taskRequestToken.current
    try {
      const { items } = await api.tasks()
      if (token !== taskRequestToken.current) return false
      setTasks(items)
      setSelectedTaskId((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id ?? null)
      return true
    } catch (reason) {
      if (token === taskRequestToken.current) throw reason
      return false
    }
  }, [])

  useEffect(() => {
    api.models().then(({ items }) => { setModelOptions(items); setSelectedModels((current) => { const configured = new Set(items.filter((item) => item.configured).map((item) => item.id)); return current.filter((id) => configured.has(id)) }) }).catch((reason: Error) => setError(reason.message))
    api.deliveryConfig().then(({ configured }) => setDeliveryConfigured(configured)).catch((reason: Error) => setError(reason.message))
    api.doubaoWebStatus().then(setDoubaoWebStatus).catch((reason: Error) => setError(reason.message))
  }, [])

  const startDedicatedDoubao = async () => {
    setIsOpeningDoubao(true)
    setError('')
    try {
      setDoubaoWebStatus(await api.startDoubaoWeb())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法启动豆包专用浏览器')
    } finally {
      setIsOpeningDoubao(false)
    }
  }
  const reconnectDoubao = async () => {
    setIsReconnectingDoubao(true)
    setError('')
    try {
      setDoubaoWebStatus(await api.reconnectDoubaoWeb())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法重新连接豆包专用浏览器')
    } finally {
      setIsReconnectingDoubao(false)
    }
  }
  useEffect(() => {
    let active = true
    const pollBrowser = () => api.doubaoWebStatus()
      .then((status) => { if (active) setDoubaoWebStatus(status) })
      .catch(() => undefined)
    const timer = window.setInterval(pollBrowser, 3000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  useEffect(() => {
    let active = true
    const stopPolling = createSequentialPoller(
      async () => {
        try {
          const applied = await refreshTasks()
          if (active && applied) setError((current) => current === '连接暂时中断，正在自动重试…' ? '' : current)
        } catch {
          if (active) setError('连接暂时中断，正在自动重试…')
        }
      },
      (callback, delayMs) => window.setTimeout(callback, delayMs),
      (handle) => window.clearTimeout(handle),
      1500,
    )
    return () => { active = false; stopPolling() }
  }, [refreshTasks])
  useEffect(() => {
    let active = true
    const token = ++leadRequestToken.current
    const nextQuery = { taskId: task?.id ?? null, city: cityFilter }
    const previousQuery = leadQueryRef.current
    if (
      shouldResetLeadRows(
        previousQuery?.taskId,
        previousQuery?.city ?? '',
        nextQuery.taskId,
        nextQuery.city,
      )
    ) {
      setLeads([])
    }
    leadQueryRef.current = nextQuery
    if (!task) return () => { active = false }
    api.leads(task.id, cityFilter || undefined).then(({ items }) => {
      if (active && token === leadRequestToken.current) setLeads(items)
    }).catch((reason: Error) => {
      if (active && token === leadRequestToken.current) setError(reason.message)
    })
    return () => { active = false }
  }, [task?.id, leadRefreshKey, cityFilter])

  const selectTask = (taskId: string) => { leadRequestToken.current += 1; setLeads([]); setSelectedTaskId(taskId); setCityFilter('') }
  const start = async () => {
    setError('')
    if (!requestText.trim()) return setError('请用一句话说明要找什么客户。')
    if (!selectedModels.length) return setError('请至少选择一个已配置的大模型。')
    if (!hybridPathReady) return setError('豆包聊天网页未就绪，请先启动专用浏览器并完成登录。')
    taskRequestToken.current += 1
    setIsStarting(true)
    try {
      const created = await api.createNaturalLanguageTask(requestText.trim(), selectedModels, {
        continuousMode,
        verifiedAutoDeliver,
        useDoubaoWebChat: true,
      })
      const started = await api.startTask(created.id)
      taskRequestToken.current += 1
      const nextTask = started || { ...created, status: 'queued' as TaskStatus }
      setTasks((current) => [nextTask, ...current.filter((item) => item.id !== nextTask.id)])
      selectTask(nextTask.id)
      void refreshTasks().catch(() => setError('连接暂时中断，正在自动重试…'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '任务启动失败')
    } finally {
      setIsStarting(false)
    }
  }
  const control = async (taskId: string, action: ControlAction) => {
    taskRequestToken.current += 1
    setControlActions((current) => ({ ...current, [taskId]: action }))
    setError('')
    try {
      const nextTask = action === 'pause' ? await api.pauseTask(taskId) : action === 'resume' ? await api.resumeTask(taskId) : await api.terminateTask(taskId)
      taskRequestToken.current += 1
      if (nextTask) replaceTask(nextTask)
      void refreshTasks().catch(() => setError('连接暂时中断，正在自动重试…'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '任务控制失败')
    } finally {
      setControlActions((current) => { const remaining = { ...current }; delete remaining[taskId]; return remaining })
    }
  }
  const retryErrors = async () => { if (!task) return; setError(''); await api.retryErrors(task.id); const nextTask = await api.task(task.id); if (nextTask) replaceTask(nextTask) }
  const verifyTruth = async () => { if (!task || !leads.length) return; setError(''); await api.verifyTask(task.id); const nextTask = await api.task(task.id); if (nextTask) replaceTask({ ...nextTask, verification_state: 'running' }) }
  const deliver = async () => { if (!task || !deliveryUrl.trim()) return setDeliveryMessage('请填写目标系统接收 URL。'); setIsDelivering(true); setDeliveryMessage(''); try { const result = await api.deliver(task.id, deliveryUrl.trim(), deliveryToken.trim()); setDeliveryMessage(`已推送 ${result.sent} 条，失败 ${result.failed ?? 0} 条。`); const nextTask = await api.task(task.id); if (nextTask) replaceTask(nextTask); const nextLeads = await api.leads(task.id, cityFilter || undefined); setLeads(nextLeads.items) } catch (reason) { setDeliveryMessage(reason instanceof Error ? reason.message : 'API 推送失败') } finally { setIsDelivering(false) } }
  const deliverLead = async (lead: Lead) => {
    if (!task || !deliveryUrl.trim() || (!lead.mobile && !lead.email)) return
    const key = `${lead.city}|${lead.company_name}`
    setManualDeliveryBusy((current) => ({ ...current, [key]: true }))
    setError('')
    try {
      const result = await api.deliverLead(task.id, lead, deliveryUrl.trim(), deliveryToken.trim())
      setLeads((current) => current.map((item) => item.city === lead.city && item.company_name === lead.company_name ? result.lead : item))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '单条推送失败')
    } finally {
      setManualDeliveryBusy((current) => { const next = { ...current }; delete next[key]; return next })
    }
  }

  return <main className="workspace">
    <header className="topbar"><div><p className="eyebrow">NATURAL LANGUAGE LEAD SEARCH</p><h1>全国城市线索采集</h1></div><div className="connection"><span className="dot" /> 公开网页证据</div></header>
    <section className="task-grid"><div className="form-panel">
      <div className="section-heading"><RefreshCw size={18} className={tasks.some((item) => item.status === 'running') ? 'spin' : ''} /><div><h2>任务队列</h2><p>创建、切换和控制采集任务。</p></div></div>
      <div className="task-queue" aria-label="采集任务队列">{tasks.length ? tasks.map((item) => <button key={item.id} className={`task-row ${item.id === selectedTaskId ? 'selected' : ''}`} onClick={() => selectTask(item.id)}><span className={`task-rail status-${item.status}`} /><span className="task-row-main"><strong>{item.name || item.request_text}</strong><small>{formatCreatedAt(item.created_at)} · {item.completed_units}/{item.total_units} 城市单元</small></span><span className="task-row-meta"><span className={`status-tag task-status status-${item.status}`}>{statusLabel[item.status]}</span><small>{item.candidate_count} 候选 · {leadCountSummary(item.raw_contact_lead_count, item.contact_lead_count, item.lead_count, targetContactLabel(item.target_contact_mode))}</small></span></button>) : <p className="queue-empty">尚未创建任务。</p>}</div>
      <div className="composer-divider" />
      <div className="section-heading"><Settings2 size={18} /><div><h2>描述客户需求</h2><p>自然语言只需说明城市、客户类型、联系方式和数量。</p></div></div>
      <label>用一句话说明要找什么客户<textarea className="request-box" rows={7} value={requestText} onChange={(event) => setRequestText(event.target.value)} /></label>
      <div className="doubao-web-row"><div><strong>豆包聊天网页查公司和联系方式</strong><span className={`status-tag ${hybridPathReady ? '' : 'warning'}`} title={doubaoWebStatus?.detail}>{doubaoWebStateLabel(doubaoWebStatus?.state, doubaoWebStatus?.detail)}</span></div><div className="doubao-browser-actions"><button type="button" onClick={startDedicatedDoubao} disabled={!doubaoWebStatus?.enabled || isOpeningDoubao}><MonitorUp size={16} />{isOpeningDoubao ? '正在启动…' : '启动专用浏览器'}</button>{['unavailable', 'timeout', 'reconnecting', 'stopped'].includes(doubaoWebStatus?.state || '') && <button type="button" title="重新连接现有专用浏览器" onClick={reconnectDoubao} disabled={isReconnectingDoubao || doubaoWebStatus?.state === 'reconnecting'}><RefreshCw size={16} className={isReconnectingDoubao ? 'spin' : ''} />{isReconnectingDoubao ? '连接中…' : '重新连接'}</button>}</div></div>
      <fieldset className="capability-group"><legend>选择核验模型</legend><div className="model-mode"><strong>{selectedModels.length > 1 ? '多模型交叉核验' : '单模型核验'}</strong><span>{selectedModelLabels.length ? selectedModelLabels.join(' + ') : '请选择已配置模型'}</span></div><div className="provider-list">{modelOptions.map((model) => <label className={`provider model-provider ${model.configured ? '' : 'unavailable'}`} key={model.id}><input type="checkbox" checked={selectedModels.includes(model.id)} disabled={!model.configured} onChange={(event) => setSelectedModels((current) => event.target.checked ? [...current, model.id] : current.filter((item) => item !== model.id))} /><span>{model.label}</span><small>{model.configured ? '已配置' : '未配置'}</small></label>)}</div></fieldset>
      <label className="toggle-row"><input type="checkbox" checked={continuousMode} onChange={(event) => setContinuousMode(event.target.checked)} /><span>持续采集</span><small>{continuousMode ? '默认达到 100 条严格有效信息后停止' : '完成一轮后结束'}</small></label>
      <label className={`toggle-row ${deliveryConfigured ? '' : 'disabled'}`}><input type="checkbox" checked={verifiedAutoDeliver} disabled={!deliveryConfigured} onChange={(event) => setVerifiedAutoDeliver(event.target.checked)} /><span>验证通过后自动推送 ERP</span><small>{deliveryConfigured ? (verifiedAutoDeliver ? '仅推送有手机号、有来源且核验通过的数据' : '只保存并展示') : 'ERP 未配置'}</small></label>
      {error && <p className="error" role="alert">{error}</p>}<button className="start-button" onClick={start} disabled={isStarting}><Play size={17} fill="currentColor" />{isStarting ? '正在创建任务…' : '创建并开始采集'}</button>
    </div><div className="progress-panel">
      <div className="section-heading"><RefreshCw size={18} className={task?.status === 'running' ? 'spin' : ''} /><div><h2>执行进度</h2><p>{task ? `${statusLabel[task.status]} · ${task.lead_count} 条线索` : '尚未选择任务'}</p></div></div>
      {task && <div className="control-row"><button title="暂停任务" disabled={task.status !== 'running' || Boolean(controlActions[task.id])} onClick={() => control(task.id, 'pause')}><Pause size={16} />暂停</button><button title={task.status === 'running' ? '检查并接管后台执行器，不会重复提交正在运行的任务' : '继续任务'} disabled={!['paused', 'running'].includes(task.status) || Boolean(controlActions[task.id])} onClick={() => control(task.id, 'resume')}><Play size={16} />{task.status === 'running' ? '恢复执行' : '继续'}</button><button title="终止任务" disabled={!canTerminate(task.status) || Boolean(controlActions[task.id])} onClick={() => control(task.id, 'terminate')}><Square size={15} fill="currentColor" />终止</button></div>}
      {task && <div className="parsed-plan"><span><b>城市</b>{task.all_cities ? `全国三线及以上（${task.cities.length} 城）` : task.cities.join('、')}</span><span><b>关键词</b>{task.keywords.join('、')}</span><span><b>模型</b>{displayModelNames(task.models)}</span>{task.use_doubao_web_chat && <span><b>采集路径</b>{task.web_chat_discovery ? '豆包网页发现公司 + 逐家查联系方式' : '候选发现 + 豆包网页查联系方式'}</span>}<span><b>联系方式</b>{task.required_contacts.map((item) => contactLabel[item]).join('、')}（{task.contact_match === 'all' ? '全部具备' : '任一具备'}）</span><span><b>数量</b>{task.limit_per_city ? `每城最多 ${task.limit_per_city} 家` : '尽量多找'}</span>{task.continuous_mode && <span><b>采集进度</b>{leadCountSummary(task.raw_contact_lead_count, task.target_contact_lead_count ?? task.contact_lead_count, task.lead_count, targetContactLabel(task.target_contact_mode))} / 目标 {task.valid_lead_target ?? 100}</span>}{task.continuous_mode && <span><b>持续任务</b>第 {task.round_number || 1} 轮 · {roundPhaseLabel[task.round_phase || ''] || '准备中'}</span>}</div>}
      {task?.status === 'waiting_for_login' && <p className="partial-note">请在当前专用 Chrome 完成豆包登录或安全验证；系统检测恢复后会从当前公司自动继续。</p>}
      {task?.pause_reason === 'model_account_blocked' && <p className="error">豆包账号欠费或接口无权限，任务已自动暂停。处理账号后点击“继续”即可从当前任务恢复。{task.last_blocking_error ? `（${task.last_blocking_error}）` : ''}</p>}
      {task && <div className="phase-progress"><div><span><b>候选公司</b><small>{task.candidate_count} / {task.candidate_target}</small></span><i><em style={{ width: `${candidateProgress}%` }} /></i></div><div><span><b>信息补全</b><small>{task.enrichment_completed} / {task.enrichment_total}</small></span><i><em style={{ width: `${enrichmentProgress}%` }} /></i></div><p>{phaseLabel[task.collection_phase] || '准备采集'} · 名称通过 {task.company_name_verified_count ?? 0}，未通过 {task.company_name_unverified_count ?? 0}</p></div>}
      {task?.continuous_mode && <div className="round-summary"><span><b>有手机号或邮箱</b>{task.raw_contact_lead_count ?? 0}</span><span><b>{targetContactLabel(task.target_contact_mode)}</b>{task.target_contact_lead_count ?? task.contact_lead_count ?? 0} / {task.valid_lead_target ?? 100}</span><span><b>本轮候选</b>{task.round_metrics?.candidates ?? 0}</span><span><b>新增公司</b>{task.round_metrics?.new_companies ?? 0}</span><span><b>核验通过</b>{task.round_metrics?.verification_matches ?? 0}</span><span><b>ERP 成功</b>{task.round_metrics?.erp_succeeded ?? 0}</span><span><b>ERP 失败</b>{task.round_metrics?.erp_failed ?? 0}</span>{task.round_phase === 'cooldown' && task.next_round_at && <span><b>下一轮</b>{new Date(task.next_round_at).toLocaleString('zh-CN', { hour12: false })}</span>}</div>}
      <div className="overall"><strong>{progress}%</strong><span>{task ? `${task.completed_units} / ${task.total_units} 个单元` : '等待任务开始'}</span></div><div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
      <div className="city-progress">{cityRows.length ? cityRows.map((row) => { const percent = row.total_units ? Math.round(row.completed_units / row.total_units * 100) : 0; return <div className="city-row" key={row.city}><span>{row.city}</span><div className="mini-track"><i style={{ width: `${percent}%` }} /></div><small>{row.completed_units}/{row.total_units || '—'}</small></div> }) : <p className="city-placeholder">启动后显示全国城市进度</p>}</div>
      {!!task?.verification_errors && <p className="partial-note">{task.verification_errors} 次模型核验未完成，已保留公开证据达到 50 分的线索。</p>}
      {task && leads.length > 0 && task.status !== 'running' && <div className="verification-panel"><div><strong>真实性验证</strong><span>候选已先展示。验证只更新状态，不删除任何数据。</span></div><button onClick={verifyTruth} disabled={task.verification_state === 'running'}><ShieldCheck size={16} />{task.verification_state === 'running' ? '正在验证…' : task.verification_state === 'not_started' ? '开始真实性验证' : '重新验证'}</button></div>}
      {task?.status === 'completed_with_errors' && <button className="retry-button" onClick={retryErrors}><RefreshCw size={16} />仅重试失败单元</button>}
      {task && <><div className="export-row"><select aria-label="下载格式" value={exportFormat} onChange={(event) => setExportFormat(event.target.value as ExportFormat)}><option value="xlsx">XLSX</option><option value="csv">CSV</option><option value="json">JSON</option></select><a href={api.exportUrl(task.id, exportFormat)}><Download size={16} />下载 ZIP</a></div><div className="delivery-panel"><h3>推送到另一个系统 API</h3><label>接收 URL<input placeholder="https://your-system.example/api/leads" value={deliveryUrl} onChange={(event) => setDeliveryUrl(event.target.value)} /></label><label>Bearer Token（可选）<input type="password" value={deliveryToken} onChange={(event) => setDeliveryToken(event.target.value)} /></label><button onClick={deliver} disabled={isDelivering}><Send size={16} />{isDelivering ? '正在推送…' : '批量推送'}</button>{deliveryMessage && <p>{deliveryMessage}</p>}<small>每行也可单独人工推送；有手机号或邮箱的线索才会发送，成功记录不会重复发送。</small></div></>}
    </div></section>
      <section className="results-section"><div className="results-header"><div className="section-heading"><Search size={18} /><div><h2>采集结果</h2><p>{task ? `当前显示 ${leads.length} 条线索` : '选择任务后显示客户候选'}</p></div></div><select aria-label="按城市筛选" value={cityFilter} onChange={(event) => setCityFilter(event.target.value)} disabled={!task}><option value="">全部城市</option>{resultCities.map((city) => <option key={city}>{city}</option>)}</select></div><div className="table-wrap"><table><thead><tr>{visibleLeadColumnLabels.map((label) => <th key={label}>{label}</th>)}</tr></thead><tbody>{leads.length ? leads.map((lead, index) => { const rowKey = `${lead.city}|${lead.company_name}`; const busy = Boolean(manualDeliveryBusy[rowKey]); const inFlight = isDeliveryInFlight(lead); const staleSending = lead.delivery_status === 'sending' && !inFlight; const alreadySent = lead.delivery_status === 'succeeded' || inFlight; const hasContact = hasDeliveryContact(lead); const displayDeliveryStatus = staleSending ? 'pending' : (lead.delivery_status || 'display_only'); return <tr key={`${lead.company_name}-${index}`}><td>{lead.city}</td><td className="company">{lead.company_name}</td><td><div className="evidence-cell"><span className={`status-tag ${['unverified', 'rejected', 'verification_failed'].includes(lead.company_name_verification_status) ? 'warning' : ''}`} title={lead.company_name_verification_reason}>{companyNameVerificationLabel[lead.company_name_verification_status] || lead.company_name_verification_status}</span>{lead.company_name_evidence_urls?.length ? <a href={lead.company_name_evidence_urls[0]} target="_blank" rel="noreferrer">证据 {lead.company_name_evidence_urls.length}</a> : null}</div></td><td>{lead.mobile || '未公开'}</td><td>{lead.contact_person_name || '未公开'}</td><td>{lead.contact_person_role || '未公开'}</td><td>{isHttpUrl(lead.contact_evidence_url) ? <a href={lead.contact_evidence_url || undefined} target='_blank' rel='noreferrer' title={lead.contact_evidence_quote || lead.contact_verification_reason}>查看证据</a> : '未公开'}</td><td>{lead.email || '未公开'}</td><td>{lead.wechat || '未公开'}</td><td>{lead.address || '未公开'}</td><td className="intro">{lead.service_intro || '未公开'}</td><td><span className={`status-tag delivery-${displayDeliveryStatus}`} title={lead.delivery_status === 'failed' ? lead.delivery_error || 'ERP 推送失败' : staleSending ? '上一次推送已超时，可以重新推送' : undefined}>{deliveryLabel[displayDeliveryStatus as DeliveryStatus]}</span></td><td><button className="manual-delivery-button" title={!deliveryUrl.trim() ? '请先填写接收 URL' : !hasContact ? '无手机号或邮箱' : staleSending ? '上一次推送超时，点击重新推送' : undefined} onClick={() => void deliverLead(lead)} disabled={busy || alreadySent || !hasContact || !deliveryUrl.trim()}><Send size={13} />{busy ? '推送中…' : alreadySent ? (lead.delivery_status === 'succeeded' ? '已推送' : '推送中…') : staleSending ? '重新推送' : !hasContact ? '无手机号或邮箱' : '人工推送'}</button></td><td>{lead.saved_at}</td><td>{lead.discovery_channel ? sourceLabel[lead.discovery_channel] || lead.discovery_channel : '未公开'}</td><td>{lead.source_url ? <a href={lead.source_url} target="_blank" rel="noreferrer">查看来源</a> : '未公开'}</td><td>{displayModelNames(lead.verification_models || [])}</td></tr> }) : <tr><td colSpan={visibleLeadColumnLabels.length} className="empty">{task ? '当前筛选下尚无线索。' : '选择或创建任务后，候选数据会显示在这里。'}</td></tr>}</tbody></table></div></section>
  </main>
}
