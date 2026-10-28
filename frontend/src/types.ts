export type Provider = {
  id: string
  label: string
  configured: boolean
}

export type ModelOption = Provider

export type DoubaoWebStatus = {
  enabled: boolean
  state: 'disabled' | 'stopped' | 'browser_stopped' | 'connecting' | 'ready' | 'working'
    | 'waiting_for_login' | 'challenge' | 'reconnecting' | 'timeout' | 'unavailable'
  detail: string
  current_company: string | null
}

export type CityProgress = {
  city: string
  total_units: number
  completed_units: number
  lead_count?: number
}

export type TaskStatus = 'draft' | 'queued' | 'running' | 'pausing' | 'paused' | 'waiting_for_login'
  | 'completed' | 'completed_with_errors' | 'terminating' | 'terminated'

export type DeliveryStatus = 'display_only' | 'pending' | 'sending' | 'succeeded' | 'failed'

export type RoundPhase = 'discovering' | 'enriching' | 'verifying' | 'delivering' | 'cooldown'

export type RoundMetrics = {
  candidates: number
  new_companies: number
  verification_matches: number
  verification_rejects: number
  erp_succeeded: number
  erp_failed: number
}

export type CollectionTask = {
  id: string
  name: string
  request_text?: string
  status: TaskStatus
  total_units: number
  completed_units: number
  lead_count: number
  cities: string[]
  city_progress: Record<string, CityProgress>
  created_at: string
  updated_at: string
  providers: string[]
  models: string[]
  keywords: string[]
  all_cities: boolean
  required_contacts: Array<'mobile' | 'email' | 'wechat'>
  contact_match: 'any' | 'all'
  limit_per_city: number | null
  unit_errors: number
  verification_errors: number
  verification_state: 'not_started' | 'running' | 'completed' | 'completed_with_errors'
  collection_phase: 'discovering' | 'verifying_company_names' | 'enriching' | 'completed'
  candidate_target: number
  candidate_count: number
  enrichment_total: number
  enrichment_completed: number
  company_name_verified_count?: number
  company_name_unverified_count?: number
  raw_contact_lead_count?: number
  contact_lead_count?: number
  strict_contact_lead_count?: number
  target_contact_lead_count?: number
  target_contact_mode?: 'strict' | 'evidence_backed'
  auto_deliver?: boolean
  continuous_mode?: boolean
  valid_lead_target?: number | null
  verified_auto_deliver?: boolean
  use_doubao_web_chat?: boolean
  web_chat_discovery?: boolean
  round_number?: number
  round_phase?: RoundPhase | null
  round_started_at?: string | null
  next_round_at?: string | null
  round_metrics?: RoundMetrics
  pause_reason?: 'model_account_blocked' | null
  last_blocking_error?: string | null
}

export type Lead = {
  city: string
  company_name: string
  mobile: string | null
  contact_person_name: string | null
  contact_person_role: string | null
  contact_verification_status: 'verified_public_web' | 'verified_official_page' | 'verified_cross_source' | 'pending_login_source' | 'unverified_model_only' | 'rejected_mismatch' | 'unverified' | 'rejected' | 'verification_failed'
  contact_evidence_url: string | null
  contact_evidence_quote: string
  contact_verified_at: string | null
  contact_verification_reason: string
  email: string | null
  wechat: string | null
  address: string | null
  service_intro: string | null
  saved_at: string
  discovery_channel: string | null
  source_url: string | null
  matched_keyword: string
  source_confidence: number
  confidence_score: number
  confidence_level: '待验证' | '已核验' | '较可信' | '低可信'
  verification_status: string
  verification_models: string[]
  verification_detail: Record<string, { relevant: boolean; confidence?: number; reason: string }>
  company_name_verification_status: 'verified_public_web' | 'verified_registry' | 'unverified' | 'rejected' | 'verification_failed'
  company_name_evidence_urls: string[]
  company_name_verification_reason: string
  company_name_verified_at: string | null
  company_name_verification_attempts: number
  enrichment_state?: 'pending' | 'completed' | 'failed' | 'blocked'
  enrichment_model?: string | null
  delivery_status?: DeliveryStatus
  delivery_error?: string | null
  delivery_started_at?: string | null
}

export type CreateTaskInput = {
  name: string
  request_text: string
  cities: string[]
  all_cities: boolean
  keywords: string[]
  providers: string[]
  models: string[]
  required_contacts: Array<'mobile' | 'email' | 'wechat'>
  auto_deliver?: boolean
  continuous_mode?: boolean
  verified_auto_deliver?: boolean
}

export type DeliveryResult = {
  status: string
  sent: number
  batches: number
  selected?: number
  failed?: number
}
