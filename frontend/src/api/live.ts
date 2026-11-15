import { request } from './client'

export interface LiveMoment { id: number; t: number; score: number; reason: string; manual: boolean; event: string; state: string; clip?: number }
export interface LiveClip {
  rank: number; trigger: number; score: number; reason: string; title: string; duration: number; status: string
  captions: string; manual: boolean; hashtags: string[]; created_at: string; posted: Record<string, number>
  render: { seconds: number; encoder: string; cpu_avg: number; total_seconds: number }
}
export interface LiveSession {
  id: string; started_at: string; ended_at?: string; stream_url: string; live: boolean; simulated: boolean
  game: string; game_label: string; source: string; best_of?: string; clips: LiveClip[]
}
export interface LiveEngineStatus {
  session: string; game: string; game_label: string; facecam: boolean; paused: boolean; auto_post: boolean; clock: number
  meters: { mic: number; desktop: number; chat_rate: number; score: number; threshold: number }
  governor: { state: string; cpu: number; reason: string }
  hud_events: { t: number; event: string; note: string }[]
  moments: LiveMoment[]; rendering: number; queued: number; clips_made: number
}
export interface LiveStatus {
  enabled: boolean
  amf: boolean
  status: {
    engine_running: boolean; note?: string; source?: string
    obs?: { connected: boolean; streaming: boolean; replay: boolean }
    governor?: { state: string; cpu: number; reason: string }
    live?: LiveEngineStatus | null
  }
  settings: Record<string, unknown>
  gaming_accounts: { id: string; title: string; platform: string }[]
  sessions: LiveSession[]
}
export interface LiveAccount { id: string; title: string; platform: string; gaming: boolean; ok: boolean }

const j = (body: unknown) => ({ method: 'POST', body: JSON.stringify(body) })

export const live = {
  status: () => request<LiveStatus>('/api/live/status'),
  clip: (note = '') => request<{ ok: boolean }>('/api/live/clip', j({ note })),
  control: (action: 'pause' | 'resume' | 'auto_on' | 'auto_off') => request<{ ok: boolean }>('/api/live/control', j({ action })),
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
