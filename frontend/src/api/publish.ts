import { useQuery } from '@tanstack/react-query'
import { request } from './client'
import type { AuditStatus, Batch, Channel, ChannelSettings, Dashboard, Health, PublishingStatus, QueueItem, Reminder, SyncResult } from './publishTypes'

const json = (body: unknown) => JSON.stringify(body)

export type BulkAction =
  | 'unschedule' | 'delete' | 'shift' | 'regenerate' | 'reschedule' | 'retry' | 'unbatch' | 'to_batch'
  | 'reschedule_missed' | 'retry_failed'

export const pub = {
  status: () => request<PublishingStatus>('/api/publishing/status'),
  uploadClientSecret: (file: File) =>
    request<{ type: string; client_id: string }>('/api/publishing/client-secret', { method: 'POST', body: file }),
  runScheduler: () => request<{ queued: boolean }>('/api/publishing/run', { method: 'POST' }),
  health: () => request<Health>('/api/publishing/health'),
  setAudit: (status: AuditStatus, applied_on?: string) =>
    request<PublishingStatus>('/api/publishing/audit', { method: 'POST', body: json({ status, applied_on }) }),

  channels: () => request<Channel[]>('/api/channels'),
  oauthStart: () => request<{ url: string }>('/api/channels/oauth/start', { method: 'POST' }),
  pickOptions: (pid: string) =>
    request<{ id: string; title: string; handle: string; thumbnail: string }[]>(`/api/channels/pick/${pid}`),
  pick: (pid: string, channel_id: string) =>
    request<Channel>(`/api/channels/pick/${pid}`, { method: 'POST', body: json({ channel_id }) }),
  patchChannel: (id: string, body: { paused?: boolean; settings?: Partial<ChannelSettings> }) =>
    request<Channel>(`/api/channels/${id}`, { method: 'PATCH', body: json(body) }),
  confirmChannel: (id: string) => request<Channel>(`/api/channels/${id}/confirm`, { method: 'POST' }),
  checkAuth: (id: string) => request<{ ok: boolean; channel: Channel }>(`/api/channels/${id}/check-auth`, { method: 'POST' }),
  deleteChannel: (id: string) => request<{ ok: boolean; revoked: string }>(`/api/channels/${id}`, { method: 'DELETE' }),

  queue: (channelId?: string, status?: string) =>
    request<QueueItem[]>(`/api/queue?${channelId ? `channel_id=${channelId}&` : ''}${status ? `status=${status}` : ''}`),
  add: (body: { channel_id: string; job_id?: number; video_id?: string; ranks?: number[] | null }) =>
    request<{ added: QueueItem[] }>('/api/queue', { method: 'POST', body: json(body) }),
  patch: (id: number, body: Partial<Pick<QueueItem, 'title' | 'description' | 'tags' | 'slot_local' | 'category_id' | 'ig_caption' | 'privacy' | 'ig_thumb_offset_ms'>>) =>
    request<QueueItem>(`/api/queue/${id}`, { method: 'PATCH', body: json(body) }),
  regenerate: (id: number) => request<QueueItem>(`/api/queue/${id}/regenerate`, { method: 'POST' }),
  unschedule: (id: number) => request<QueueItem>(`/api/queue/${id}/unschedule`, { method: 'POST' }),
  remove: (id: number) => request<{ ok: boolean }>(`/api/queue/${id}`, { method: 'DELETE' }),
  bulk: (body: { ids?: number[]; action: BulkAction; days?: number; channel_id?: string }) =>
    request<{ done: number[]; errors: { id: number | null; error: string }[]; batches?: { relative: string; url: string }[] }>(
      '/api/queue/bulk', { method: 'POST', body: json(body) }),
  reschedule: (id: number) => request<QueueItem>(`/api/queue/${id}/reschedule`, { method: 'POST' }),
  retry: (id: number) => request<QueueItem>(`/api/queue/${id}/retry`, { method: 'POST' }),
  unbatch: (id: number) => request<QueueItem>(`/api/queue/${id}/unbatch`, { method: 'POST' }),
  publishNow: (id: number) => request<QueueItem>(`/api/queue/${id}/publish-now`, { method: 'POST' }),
  autoschedule: (channelId: string, item_ids?: number[]) =>
    request<{ scheduled: QueueItem[]; still_draft: { id: number; title: string; meta_status: string; problems: string[] }[] }>(
      `/api/channels/${channelId}/autoschedule`, { method: 'POST', body: json({ item_ids }) }),
  freeSlots: (channelId: string) =>
    request<{ slot_utc: string; slot_local: string; peak: boolean }[]>(`/api/channels/${channelId}/free-slots?n=21`),

  prepareBatch: (channelId: string, n?: number) =>
    request<Batch & { items: unknown[] }>(`/api/channels/${channelId}/batch`, { method: 'POST', body: json({ n }) }),
  batches: () => request<Batch[]>('/api/batches'),
  sync: (channelId: string) => request<SyncResult>(`/api/channels/${channelId}/sync`, { method: 'POST' }),
  link: (id: number, video_id: string) =>
    request<QueueItem>(`/api/queue/${id}/link`, { method: 'POST', body: json({ video_id }) }),

  dashboard: (channelId: string) => request<Dashboard>(`/api/channels/${channelId}/dashboard`),
  applySlots: (channelId: string, slots: string[]) =>
    request<Channel>(`/api/channels/${channelId}/apply-slots`, { method: 'POST', body: json({ slots }) }),
  reminders: () => request<Reminder[]>('/api/posts/reminders'),
}

export function useChannels() {
  return useQuery({ queryKey: ['channels'], queryFn: pub.channels })
}

export function usePublishing() {
  return useQuery({
    queryKey: ['publishing'],
    queryFn: pub.status,
    refetchInterval: (q) => (q.state.data?.worker.current || q.state.data?.worker.pending.length ? 2500 : 20000),
  })
}

export function useQueue(channelId?: string) {
  return useQuery({
    queryKey: ['queue', channelId],
    queryFn: () => pub.queue(channelId),
    enabled: !!channelId,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((i) => i.meta_status === 'pending' || i.status === 'uploading') ? 2500 : false,
  })
}

export function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: pub.health, refetchInterval: 30000 })
}

export const STATUS_STYLE: Record<string, string> = {
  draft: 'bg-muted text-muted-foreground',
  ready: 'bg-primary/15 text-primary',
  uploaded: 'bg-violet-500/15 text-violet-300',
  missed: 'bg-rose-500/15 text-rose-300',
  queued: 'bg-sky-500/15 text-sky-300',
  uploading: 'bg-primary/15 text-primary',
  scheduled: 'bg-violet-500/15 text-violet-300',
  published: 'bg-success/15 text-success',
  failed: 'bg-destructive/15 text-destructive',
  locked_private: 'bg-orange-500/15 text-orange-300',
  needs_manual: 'bg-amber-500/15 text-amber-300',
}

export const STATUS_LABEL: Record<string, string> = {
  draft: 'Draft', queued: 'Queued', uploading: 'Uploading', ready: 'Uploaded, publishes at slot',
  scheduled: 'Scheduled on YouTube', uploaded: 'On YouTube (private/unlisted)', published: 'Published',
  failed: 'Failed', locked_private: 'Locked private', needs_manual: 'Needs you', missed: 'Missed slot',
}

export function istLabel(slotLocal: string | null): string {
  if (!slotLocal) return '—'
  const d = new Date(slotLocal + ':00')
  return d.toLocaleString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' })
}

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
