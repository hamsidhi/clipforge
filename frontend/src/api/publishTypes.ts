// Phase 12: publishing types

export type QueueStatus =
  | 'draft' | 'queued' | 'uploading' | 'processing' | 'ready' | 'scheduled' | 'uploaded' | 'published'
  | 'failed' | 'locked_private' | 'needs_manual' | 'missed' | 'cancelled' | 'skipped_duplicate' | 'waiting_capacity'

export type Platform = 'youtube' | 'instagram'
export type Privacy = 'scheduled' | 'public' | 'unlisted' | 'private'
export type FixAction =
  | 'sync' | 'link' | 'open_batch' | 'reschedule' | 'publish_now' | 'retry' | 'reconnect' | 'audit_info'
  | 'studio' | 'unbatch' | 'open_app' | 'edit' | 'confirm' | 'switch_assisted' | 'switch_auto' | 'studio_instead'

export type AccountMode = 'auto' | 'assisted'
/** Server-side wording of an item's state (same text as Telegram): publish/states.py status_text */
export interface StatusText { state: string; label: string; detail: string; actions: FixAction[] }
export interface AccountSummary {
  id: string; title: string; platform: Platform; paused: boolean; token: { ok: boolean; text: string }; attention: number
  mode: AccountMode; mode_source: string; mode_problem: string; audit_status?: AuditStatus; api_audited?: boolean
  last_published: { title: string; at: string | null; url: string | null } | null
  next_post: { title: string; at: string | null; status: string } | null
  upcoming: number
  auto_ready?: boolean
  connected?: boolean
  limits?: { uploads_24h: number; safety_limit?: number; cooldown_until: string | null; recent_failures: string[] }
  plan?: { day: string; label: string; slots: string[]; count: number }[]
  eligibility?: { auto_ready: boolean; connected: boolean; summary: string }
}

export interface ChannelSettings {
  timezone: string
  slots: string[]
  peak_slots: string[]
  posts_per_day: number
  min_gap_hours: number
  days: number[]
  start_date: string | null
  privacy: string
  category_id: string
  language: string
  title_language: 'hinglish' | 'english'
  description_template: string
  credit_template: string
  niche_tag: string
  default_playlist: string
  whop_campaign: string
  campaign_rate_per_1k: number
  campaign_fee_pct: number
  hours_after_youtube: number
  share_to_feed: boolean
  cta: string
}

export interface Channel {
  id: string
  title: string
  handle: string
  thumbnail: string
  auth_status: 'ok' | 'needs_reauth' | 'missing'
  auth_error: string
  paused: boolean
  confirmed: boolean
  settings: ChannelSettings
  counts: Record<string, number>
  last_sync_at: string | null
  token_present: boolean
  platform: Platform
  token?: { ok: boolean; text: string }
  mode?: AccountMode
  mode_source?: string
  mode_problem?: string
  mode_setting?: 'auto' | 'assisted' | 'default'
  extra: {
    username?: string; page_name?: string; followers?: number; token_expires_at?: string; ig_user_id?: string
    profile_picture_url?: string; manual?: boolean
  }
}

export interface TitleVariant {
  title: string
  score: number
  reasons: string[]
  duplicate: boolean
}

export interface QueueItem {
  id: number
  channel_id: string
  video_id: string
  rank: number
  episode_title: string
  topic: string
  score: number
  duration: number
  slot_utc: string | null
  slot_local: string | null
  status: QueueStatus
  youtube_video_id: string
  youtube_url: string | null
  attempts: number
  error: string
  title: string
  description: string
  tags: string[]
  category_id: string
  variants: TitleVariant[]
  meta_status: 'pending' | 'ready' | 'failed'
  ig_caption: string
  batch: string
  problems: string[]
  uploading_progress: number | null
  platform: Platform
  privacy: Privacy
  reason: string
  reason_label: string
  actions: FixAction[]
  status_text?: StatusText
  yt_state: {
    uploadStatus?: string; privacyStatus?: string; publishAt?: string; processingStatus?: string
    failureReason?: string; rejectionReason?: string; checked_at?: string
  }
  url: string | null
  permalink: string
  ig_media_id: string
  ig_thumb_offset_ms: number | null
}

export type AuditStatus = 'not_applied' | 'applied' | 'approved' | 'rejected'

export interface Health {
  last_run_at: string | null
  last_source: string | null
  last_result: Record<string, unknown>
  runs: Record<string, string>
  next_run_at: string | null
  ticker: boolean
  mode: 'assisted' | 'api'
  quota: { inserts: number; units: number; inserts_limit: number; units_limit: number; day: string }
  audit_status: AuditStatus
  api_audited: boolean
  accounts: AccountSummary[]
  plan_today?: string
  db?: { ok: boolean; text: string }
  heartbeat?: {
    ok: boolean; last_run: string | null; last_run_minutes_ago: number | null; source: string | null
    items_processed: number; next_run: string | null; in_app_ticker: boolean; windows_task: boolean; text: string
  }
  keep_awake: { mode: string; holding: boolean; reason?: string; supported?: boolean }
  tasks: { name: string; status: string; last_run: string; last_result: string; next_run: string }[]
  server_time: string
}

export interface PublishingStatus {
  audit_status: AuditStatus
  audit_applied_on: string
  late_policy: 'next_slot' | 'publish_now' | 'ask'
  mode: 'assisted' | 'api'
  api_audited: boolean
  unverified_lock: boolean
  banner: { level: 'info' | 'warning'; text: string; link: string | null } | null
  audit_form: string
  quota: { inserts: number; units: number; inserts_limit: number; units_limit: number; day: string }
  uploads_left_today: number
  client_secret: boolean
  channels: number
  timezone: string
  worker: { current: string | null; pending: string[]; history: { task: string; ok: boolean; at: number; error?: string }[] }
  scope_help: { scope: string; why: string }[]
}

export interface Batch {
  relative: string
  folder: string
  channel: string
  channel_id: string
  count: number
  name: string
  url: string
}

export interface SyncResult {
  matched: { queue_id: number; video_id: string; title: string; score: number; by: string }[]
  unmatched_items: { queue_id: number; title: string }[]
  unmatched_videos: {
    video_id: string; title: string; duration: number; file_name: string
    suggestions: { queue_id: number; title: string; score: number }[]
  }[]
}

export interface YppTier {
  subs_pct: number
  views_pct: number
  subs_eta: string | null
  views_eta: string | null
  needed_views_per_day: number
}

export interface Group {
  key: string
  avg_views: number
  n: number
}

export interface Dashboard {
  channel: Channel
  ypp: {
    subscribers: number
    shorts_views_90d: number
    uploads_90d: number
    subs_per_day: number
    shorts_views_per_day: number
    targets: {
      ads: { label: string; subs: number; shorts_views_90d: number }
      fan: { label: string; subs: number; shorts_views_90d: number; uploads_90d: number }
      rule_note: string
    }
    ads: YppTier
    fan: YppTier
  }
  working: {
    top: { title: string; views: number; id: string }[]
    by_topic: Group[]
    by_episode: Group[]
    by_length: Group[]
    by_title: Group[]
    avg_views: number
    shorts: number
  }
  heatmap: {
    samples: number
    cells: { weekday: number; hour: number; avg: number; n: number }[]
    hours: { hour: number; avg: number; n: number }[]
  }
  recommendation: { ready: boolean; rule: string; message: string; slots: string[] }
  daily: { day: string; subscribers: number; shorts_views_28d: number | null; shorts_views_90d: number | null }[]
  reminders: Reminder[]
  last_sync_at: string | null
}

export interface Reminder {
  queue_id: number
  title: string
  channel_id: string
  youtube_url: string
  url?: string
  platform?: Platform
  published_at: string | null
}

export interface MetaApp {
  app_id: string
  app_secret: string | null
  login_config_id: string
  configured: boolean
  mode: 'api' | 'assisted'
  graph_version: string
  redirect_uri: string
  scopes: { scope: string; why: string }[]
}

export interface IgChoice {
  ig_user_id: string
  username: string
  profile_picture_url: string
  followers: number
  page_id: string
  page_name: string
}

export interface IgDashboard {
  account: { id: string; title: string; extra: Channel['extra']; last_sync_at: string | null }
  followers: number
  followers_daily: { day: string; followers: number }[]
  reels: number
  reels_7d: number
  totals: { views: number; reach: number; likes: number; comments: number; shares: number; saves: number }
  top: { title: string; views: number; likes: number; shares: number; saves: number; permalink: string }[]
  heatmap: Dashboard['heatmap']
  recommendation: Dashboard['recommendation']
  working: Dashboard['working']
  reminders: Reminder[]
}

export interface Overview {
  youtube: { accounts: number; followers: number; posts: number; views: number }
  instagram: { accounts: number; followers: number; posts: number; views: number }
  upcoming: { id: number; platform: Platform; title: string; slot_local: string }[]
}
