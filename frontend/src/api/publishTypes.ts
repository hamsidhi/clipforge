// Phase 12: publishing types

export type QueueStatus = 'draft' | 'queued' | 'uploading' | 'scheduled' | 'published' | 'failed' | 'locked_private' | 'needs_manual'

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
}

export interface PublishingStatus {
  mode: 'assisted' | 'api'
  api_audited: boolean
  unverified_lock: boolean
  banner: { level: 'info' | 'warning'; text: string; link: string } | null
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
  matched: { queue_id: number; video_id: string; title: string; score: number }[]
  unmatched_items: { queue_id: number; title: string }[]
  unmatched_videos: { video_id: string; title: string; duration: number }[]
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
  published_at: string | null
}
