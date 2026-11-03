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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      headers: init?.body && !(init.body instanceof Blob) ? { 'Content-Type': 'application/json', ...init?.headers } : init?.headers,
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the ClipForge server. Is `clipforge ui` still running?')
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
  createJob: (body: { url?: string; upload_id?: string; settings: JobSettings }) =>
    request<Job>('/api/jobs', { method: 'POST', body: json(body) }),
  deleteJob: (id: number, deleteFiles = false) =>
    request<{ ok: boolean }>(`/api/jobs/${id}?delete_files=${deleteFiles}`, { method: 'DELETE' }),
  cancelJob: (id: number) => request<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryJob: (id: number) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),
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
  const { upload_id } = await request<{ upload_id: string }>('/api/uploads', {
    method: 'POST',
    body: json({ filename: file.name, size: file.size }),
  })
  const CHUNK = 16 * 1024 * 1024
  for (let offset = 0; offset < file.size; offset += CHUNK) {
    const blob = file.slice(offset, offset + CHUNK)
    let tries = 0
    for (;;) {
      try {
        await request(`/api/uploads/${upload_id}?offset=${offset}`, { method: 'PUT', body: blob })
        break
      } catch (e) {
        if (++tries >= 3) throw e
      }
    }
    onProgress(Math.min(1, (offset + blob.size) / file.size))
  }
  await request(`/api/uploads/${upload_id}/complete`, { method: 'POST' })
  return upload_id
}
