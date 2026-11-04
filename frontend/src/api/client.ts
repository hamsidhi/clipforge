import type {
  Clip, ClipDetail, DoctorCheck, Job, JobSettings, Post, PostsResponse, Preset, SettingsResponse, ShotsResponse,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|;\s*)cf_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

/** True when the page isn't opened on the laptop itself (phone via Tailscale, LAN, …). */
export const isRemote = () => !['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname)

/** Phones / data saver: play the 540×960 preview instead of the full 1080×1920 clip. */
export function dataSaver(): boolean {
  try {
    const v = localStorage.getItem('cf-data-saver')
    return v == null ? isRemote() : v === '1'
  } catch {
    return isRemote()
  }
}

export function videoSrc(url: string | null | undefined): string | null {
  if (!url) return null
  return dataSaver() ? `${url}${url.includes('?') ? '&' : '?'}preview=1` : url
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  const method = (init?.method ?? 'GET').toUpperCase()
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) }
  if (init?.body && !(init.body instanceof Blob)) headers['Content-Type'] = 'application/json'
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const t = csrfToken()
    if (t) headers['X-CSRF-Token'] = t
  }
  try {
    res = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  } catch {
    throw new ApiError(0, isRemote() ? 'Cannot reach your laptop. Is it on, online, and is Tailscale connected on both devices?'
      : 'Cannot reach the ClipForge server. Is `clipforge ui` still running?')
  }
  if (res.status === 401 && !path.startsWith('/api/auth/') && window.location.pathname !== '/login') {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`)
    throw new ApiError(401, 'Please log in.')
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (typeof data.detail === 'string') msg = data.detail
      else if (Array.isArray(data.detail)) msg = data.detail.map((d: { msg: string }) => d.msg).join('; ')
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg)
  }
  return (await res.json()) as T
}

const json = (body: unknown) => JSON.stringify(body)

export const api = {
  jobs: () => request<Job[]>('/api/jobs'),
  job: (id: number) => request<Job>(`/api/jobs/${id}`),
  createJob: (body: { url?: string; upload_id?: string; settings: JobSettings; transcript?: string }) =>
    request<Job>('/api/jobs', { method: 'POST', body: json(body) }),
  deleteJob: (id: number, deleteFiles = false) =>
    request<{ ok: boolean }>(`/api/jobs/${id}?delete_files=${deleteFiles}`, { method: 'DELETE' }),
  cancelJob: (id: number) => request<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryJob: (id: number) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),
  useWhisper: (id: number) => request<Job>(`/api/jobs/${id}/transcript/whisper`, { method: 'POST' }),
  clips: (jobId: number) => request<Clip[]>(`/api/jobs/${jobId}/clips`),
  renderAll: (jobId: number, body: { ranks?: number[] | null; force?: boolean; only_approved?: boolean; only_stale?: boolean } = {}) =>
    request<{ ok: boolean }>(`/api/jobs/${jobId}/render-all`, { method: 'POST', body: json(body) }),
  bulkApprove: (jobId: number, minScore: number, approve = true) =>
    request<{ changed: number }>(`/api/jobs/${jobId}/approve`, { method: 'POST', body: json({ min_score: minScore, approve }) }),
  waveform: (jobId: number, start: number, end: number) =>
    request<{ start: number; end: number; per_sec: number; peaks: number[] }>(
      `/api/jobs/${jobId}/source-waveform?start=${start}&end=${end}&per_sec=50`),
  transcript: (jobId: number, start: number, end: number, script: string) =>
    request<{ start: number; end: number; text: string }[]>(
      `/api/jobs/${jobId}/transcript?start=${start}&end=${end}&script=${script}`),

  clip: (id: string) => request<ClipDetail>(`/api/clips/${id}`),
  patchClip: (id: string, body: Record<string, unknown>) =>
    request<Clip>(`/api/clips/${id}`, { method: 'PATCH', body: json(body) }),
  renderClip: (id: string) => request<{ ok: boolean }>(`/api/clips/${id}/render`, { method: 'POST' }),
  undoClip: (id: string) => request<Clip>(`/api/clips/${id}/undo`, { method: 'POST' }),
  shots: (id: string) => request<ShotsResponse>(`/api/clips/${id}/shots`),

  settings: () => request<SettingsResponse>('/api/settings'),
  saveSettings: (body: { config?: Record<string, unknown>; secrets?: Record<string, string | null> }) =>
    request<SettingsResponse>('/api/settings', { method: 'PUT', body: json(body) }),
  ollamaModels: () => request<{ models: string[]; recommended: string | null; running: boolean }>('/api/settings/ollama-models'),
  doctor: () => request<DoctorCheck[]>('/api/doctor'),
  doctorFix: () => request<DoctorCheck[]>('/api/doctor/fix', { method: 'POST' }),
  estimate: (duration: number, clips: number, provider?: string) =>
    request<{ total_sec: number; parts: Record<string, number> }>(
      `/api/estimate?duration=${duration}&clips=${clips}${provider ? `&provider=${provider}` : ''}`),
  probe: (url: string) =>
    request<{ title: string; channel: string; duration: number | null; thumbnail: string | null; chapters: number }>(
      `/api/probe?url=${encodeURIComponent(url)}`),
  system: () => request<{ cuda: boolean; cpu_count: number; whisper_model: string; provider: string }>('/api/system'),

  presets: () => request<Preset[]>('/api/presets'),
  createPreset: (body: Omit<Preset, 'key' | 'builtin'>) => request<Preset>('/api/presets', { method: 'POST', body: json(body) }),
  deletePreset: (key: string) => request<{ ok: boolean }>(`/api/presets/${key}`, { method: 'DELETE' }),

  posts: () => request<PostsResponse>('/api/posts'),
  createPost: (body: Partial<Post>) => request<Post>('/api/posts', { method: 'POST', body: json(body) }),
  updatePost: (id: number, body: Partial<Post>) => request<Post>(`/api/posts/${id}`, { method: 'PATCH', body: json(body) }),
  deletePost: (id: number) => request<{ ok: boolean }>(`/api/posts/${id}`, { method: 'DELETE' }),
}

/** Chunked upload of a (possibly multi-GB) local video. */
export async function uploadFile(file: File, onProgress: (fraction: number) => void): Promise<string> {
  // resumable: the upload id is remembered per file, so a dropped connection (mobile data, a tunnel) or a reload
  // continues from the bytes the laptop already has
  const key = `cf-upload:${file.name}:${file.size}:${file.lastModified}`
  let upload_id = ''
  let offset = 0
  try {
    const saved = localStorage.getItem(key)
    if (saved) {
      const st = await request<{ received: number; size: number; complete: boolean }>(`/api/uploads/${saved}`)
      if (st.size === file.size && !st.complete) {
        upload_id = saved
        offset = st.received
      }
    }
  } catch {
    /* start fresh */
  }
  if (!upload_id) {
    upload_id = (await request<{ upload_id: string }>('/api/uploads', {
      method: 'POST',
      body: json({ filename: file.name, size: file.size }),
    })).upload_id
    try {
      localStorage.setItem(key, upload_id)
    } catch {
      /* private mode */
    }
  }
  const CHUNK = (isRemote() ? 4 : 16) * 1024 * 1024
  let tries = 0
  while (offset < file.size) {
    const blob = file.slice(offset, offset + CHUNK)
    try {
      const r = await request<{ received: number }>(`/api/uploads/${upload_id}?offset=${offset}`, { method: 'PUT', body: blob })
      offset = r.received
      tries = 0
      onProgress(Math.min(1, offset / file.size))
    } catch (e) {
      if (++tries >= 8) throw e
      await new Promise((r) => setTimeout(r, Math.min(30000, 1000 * 2 ** tries)))
      try {  // ask the laptop how much arrived, continue from there
        offset = (await request<{ received: number }>(`/api/uploads/${upload_id}`)).received
      } catch {
        /* still offline: retry the same chunk */
      }
    }
  }
  await request(`/api/uploads/${upload_id}/complete`, { method: 'POST' })
  try {
    localStorage.removeItem(key)
  } catch {
    /* ignore */
  }
  return upload_id
}
