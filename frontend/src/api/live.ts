import { request } from './client'

export interface LiveMoment { id: number; t: number; score: number; reason: string; manual: boolean; event: string; state: string; clip?: number }
export interface LiveClip {
  rank: number; trigger: number; score: number; reason: string; title: string; duration: number; status: string
  captions: string; manual: boolean; hashtags: string[]; created_at: string; posted: Record<string, number>
  render: { seconds: number; encoder: string; cpu_avg: number; total_seconds: number }
}
export interface LiveSession {
  id: string; started_at: string; ended_at?: string; stream_url: string; live: boolean; simulated: boolean
  game: string; game_label: string; source: string; best_of?: string; streamer?: string; platform?: string; title?: string
  folder: string
  clips: LiveClip[]
}
export interface LiveStream {
  session: string; streamer: string; platform: string; url: string; title: string; camera: string
  game: string; game_label: string; facecam: boolean; paused: boolean; auto_post: boolean; clock: number; ending: boolean
  meters: { audio: number; chat_rate: number; score: number; threshold: number }
  hud_events: { t: number; event: string; note: string }[]
  moments: LiveMoment[]; rendering: number; queued: number; clips_made: number
}
export interface LiveStatus {
  enabled: boolean
  amf: boolean
  status: {
    engine_running: boolean; note?: string
    governor?: { state: string; cpu: number; reason: string }
    streams?: LiveStream[]; starting?: string[]; errors?: string[]
  }
  settings: Record<string, unknown>
  gaming_accounts: { id: string; title: string; platform: string }[]
  sessions: LiveSession[]
}
export interface LiveSessionRow extends Omit<LiveSession, 'clips'> { n_clips: number }
export interface LiveAccount { id: string; title: string; platform: string; gaming: boolean; ok: boolean }

const j = (body: unknown) => ({ method: 'POST', body: JSON.stringify(body) })

export const live = {
  status: () => request<LiveStatus>('/api/live/status'),
  start: (url: string, folder = '') => request<{ message: string; folder: string }>('/api/live/streams', j({ url, folder })),
  sessions: () => request<LiveSessionRow[]>('/api/live/sessions'),
  moveSession: (sid: string, folder: string) =>
    request<{ ok: boolean; folder: string }>(`/api/live/sessions/${encodeURIComponent(sid)}/folder`, { method: 'PUT', body: JSON.stringify({ folder }) }),
  stop: (sid: string) => request<{ ok: boolean }>(`/api/live/streams/${encodeURIComponent(sid)}`, { method: 'DELETE' }),
  clip: (sid: string, note = '') => request<{ ok: boolean }>('/api/live/clip', j({ sid, note })),
  control: (sid: string, action: 'pause' | 'resume' | 'auto_on' | 'auto_off') =>
    request<{ ok: boolean }>('/api/live/control', j({ sid, action })),
  saveSettings: (values: Record<string, unknown>) =>
    request<{ ok: boolean }>('/api/live/settings', { method: 'PUT', body: JSON.stringify({ values }) }),
  post: (sid: string, rank: number, mode: 'now' | 'queue') =>
    request<{ lines: string[] }>(`/api/live/sessions/${sid}/clips/${rank}/post`, j({ mode })),
  discard: (sid: string, rank: number) => request<{ ok: boolean }>(`/api/live/sessions/${sid}/clips/${rank}/discard`, j({})),
  bestof: (sid: string) => request<{ message: string }>(`/api/live/sessions/${sid}/bestof`, j({})),
  accounts: () => request<LiveAccount[]>('/api/live/accounts'),
  setGaming: (id: string, gaming: boolean) =>
    request<{ ok: boolean }>(`/api/live/accounts/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify({ gaming }) }),
  video: (sid: string, rank: number) => `/api/live/sessions/${sid}/clips/${rank}/video`,
  thumb: (sid: string, rank: number) => `/api/live/sessions/${sid}/clips/${rank}/thumb`,
}
