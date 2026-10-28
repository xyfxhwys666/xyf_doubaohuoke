import type { CollectionTask, CreateTaskInput, DeliveryResult, DoubaoWebStatus, Lead, ModelOption, Provider } from './types'

const API_ROOT = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, init)
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `请求失败 (${response.status})`)
  }
  const body = await response.text()
  return body.trim() ? JSON.parse(body) as T : (undefined as T)
}

export const api = {
  providers: () => request<{ items: Provider[] }>('/providers'),
  models: () => request<{ items: ModelOption[] }>('/models'),
  createTask: (input: CreateTaskInput) => request<CollectionTask>('/collection-tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  }),
  createNaturalLanguageTask: (
    requestText: string,
    models: string[],
    options: { continuousMode: boolean; verifiedAutoDeliver: boolean; useDoubaoWebChat: boolean },
  ) => request<CollectionTask>(
    '/collection-tasks/from-natural-language',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        request_text: requestText,
        models,
        continuous_mode: options.continuousMode,
        verified_auto_deliver: options.verifiedAutoDeliver,
        use_doubao_web_chat: options.useDoubaoWebChat,
        web_chat_discovery: options.useDoubaoWebChat,
      }),
    },
  ),
  startTask: (taskId: string) => request<CollectionTask>(`/collection-tasks/${taskId}/start`, { method: 'POST' }),
  retryErrors: (taskId: string) => request<void>(`/collection-tasks/${taskId}/retry-errors`, { method: 'POST' }),
  verifyTask: (taskId: string) => request<void>(`/collection-tasks/${taskId}/verify`, { method: 'POST' }),
  tasks: () => request<{ items: CollectionTask[] }>('/collection-tasks'),
  pauseTask: (taskId: string) => request<CollectionTask>(`/collection-tasks/${taskId}/pause`, { method: 'POST' }),
  resumeTask: (taskId: string) => request<CollectionTask>(`/collection-tasks/${taskId}/resume`, { method: 'POST' }),
  terminateTask: (taskId: string) => request<CollectionTask>(`/collection-tasks/${taskId}/terminate`, { method: 'POST' }),
  deliveryConfig: () => request<{ configured: boolean }>('/delivery-config'),
  doubaoWebStatus: () => request<DoubaoWebStatus>('/collection-tasks/doubao-web/status'),
  startDoubaoWeb: () => request<DoubaoWebStatus>('/collection-tasks/doubao-web/start', { method: 'POST' }),
  reconnectDoubaoWeb: () => request<DoubaoWebStatus>('/collection-tasks/doubao-web/reconnect', { method: 'POST' }),
  task: (taskId: string) => request<CollectionTask>(`/collection-tasks/${taskId}`),
  leads: async (taskId: string, city?: string) => {
    const items: Lead[] = []
    const limit = 1000
    let offset = 0
    let total = 0
    do {
      const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
      if (city) query.set('city', city)
      const page = await request<{ items: Lead[]; total: number }>(
        `/collection-tasks/${taskId}/leads?${query}`,
      )
      total = page.total
      items.push(...page.items)
      offset += page.items.length
    } while (items.length < total && offset > 0)
    return { items, total }
  },
  exportUrl: (taskId: string, format: 'xlsx' | 'csv' | 'json') => `${API_ROOT}/collection-tasks/${taskId}/export?format=${format}`,
  deliver: (taskId: string, targetUrl: string, bearerToken: string) => request<DeliveryResult>(
    `/collection-tasks/${taskId}/deliver`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_url: targetUrl, bearer_token: bearerToken || null, batch_size: 100 }),
    },
  ),
  deliverLead: (
    taskId: string,
    lead: Pick<Lead, 'city' | 'company_name'>,
    targetUrl: string,
    bearerToken: string,
  ) => request<{ status: string; lead: Lead; error?: string }>(
    `/collection-tasks/${taskId}/leads/deliver`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        city: lead.city,
        company_name: lead.company_name,
        target_url: targetUrl,
        bearer_token: bearerToken || null,
      }),
    },
  ),
}
