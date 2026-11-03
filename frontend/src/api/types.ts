export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

export interface JobSettings {
  clips?: number
  min_sec?: number
  max_sec?: number
  caption_script?: 'roman' | 'devanagari'
  caption_preset?: string
  layout?: Layout
  provider?: Provider
  model?: string
  whisper_model?: string
  language?: string
  section?: [number, number]
}

export type Layout = 'auto' | 'track' | 'split' | 'blur'
export type Provider = 'ollama' | 'gemini' | 'groq' | 'mock'

export interface Job {
  id: number
  source: string
  video_id: string | null
  title: string
  channel: string
  duration: number
  status: JobStatus
  stage: string
  stage_status: string
  progress: number
  eta_sec: number | null
  message: string
  error: string
  settings: JobSettings
  n_clips: number
  n_rendered: number
  busy: boolean
  thumbnail_url: string | null
  created_at: string
  updated_at: string
  finished_at: string | null
}

export interface Scores {
  hook: number
  standalone: number
  emotion: number
  value: number
  controversy: number
}

export interface CaptionStyle {
  preset?: string | null
  font?: string | null
  font_size?: number | null
  highlight_color?: string | null
  keyword_color?: string | null
  position_pct?: number | null
}

export interface CropOverride {
  shot_index: number
  cx: number
  cy?: number
}

export interface ClipMetadata {
  rank: number
  file: string
  start: number
  end: number
  duration: number
  score: number
  title: string
  hook: string
  yt_title: string
  yt_description: string
  ig_caption: string
  hashtags: string[]
  credit: string
}

export interface Clip {
  id: string
  job_id: number
  rank: number
  start: number
  end: number
  duration: number
  title: string
  hook: string
  topic: string
  why: string
  score: number
  scores: Scores
  enabled: boolean
  approved: boolean | null
  layout: Layout | null
  caption_style: CaptionStyle
  word_edits: Record<string, string>
  crop_overrides: CropOverride[]
  rendered: boolean
  stale: boolean
  file: string | null
  video_url: string | null
  thumbnail_url: string | null
  has_previous: boolean
  metadata: ClipMetadata | null
}

export interface ClipWord {
  index: number
  start: number
  end: number
  text: string
  edited: string | null
}

export interface ClipDetail extends Clip {
  words: ClipWord[]
  script: 'roman' | 'devanagari'
  source: { duration: number; width: number; height: number; title: string }
}

export interface Shot {
  index: number
  start: number
  end: number
  frame_url: string
  mode: string | null
  cx: number | null
}

export interface ShotsResponse {
  shots: Shot[]
  crop_width_frac: number
  src_width: number
  src_height: number
}

export interface Preset {
  key: string
  label: string
  builtin: boolean
  font: string
  font_size: number
  text_color?: string
  highlight_color: string
  keyword_color: string
  uppercase: boolean
}

export interface DoctorCheck {
  name: string
  ok: boolean
  detail: string
  fix: string
  required: boolean
}

export interface Post {
  id: number
  job_id: number | null
  clip_rank: number | null
  clip_title: string
  platform: 'instagram' | 'youtube'
  url: string
  posted_at: string | null
  campaign: string
  rate_per_1k: number
  views: number
  notes: string
  earnings: number
  campaign_fee_pct: number
  submission_status: 'not_submitted' | 'submitted' | 'approved' | 'rejected' | 'paid'
  submission_link: string
  approved_views: number | null
  queue_id: number | null
  youtube_video_id: string
}

export interface PostsResponse {
  posts: Post[]
  totals: { posts: number; views: number; earnings: number }
}

export interface SettingsResponse {
  config: Record<string, Record<string, unknown>>
  secrets: Record<string, string | null>
}

export type JobEvent =
  | { type: 'snapshot'; job: Job; last_seq: number; boot_id: string }
  | { type: 'stage'; stage: string; status: string; message: string; seq: number }
  | { type: 'progress'; stage: string; progress: number; eta_sec: number | null; message: string; seq: number }
  | { type: 'log'; level: string; message: string; ts: number; seq: number }
  | { type: 'status'; status: JobStatus; error?: string; kind?: string; seq: number }
  | { type: 'clip_done'; rank: number; seq: number }
  | { type: 'clips'; count: number; seq: number }
  | { type: 'render_done'; ranks: number[] | null; seq: number }
  | { type: 'render_failed'; error: string; ranks: number[] | null; seq: number }
