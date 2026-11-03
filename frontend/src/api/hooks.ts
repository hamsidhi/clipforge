import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './client'
import type { Job, JobEvent } from './types'

export const STAGES = ['download', 'transcribe', 'analyze', 'select', 'reframe', 'captions', 'render'] as const
export type StageName = (typeof STAGES)[number]

export const STAGE_LABELS: Record<string, string> = {
  download: 'Download',
  transcribe: 'Transcribe',
  analyze: 'Analyze',
  select: 'Select',
  reframe: 'Reframe',
  captions: 'Captions',
  render: 'Render',
}

export function useJobs() {
  return useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs,
    refetchInterval: (q) => ((q.state.data ?? []).some((j) => j.busy) ? 3000 : 15000),
  })
}

export function useJob(id: number) {
  return useQuery({
    queryKey: ['job', id],
    queryFn: () => api.job(id),
    refetchInterval: (q) => (q.state.data?.busy ? 4000 : false),
  })
}

export function useClips(jobId: number) {
  return useQuery({ queryKey: ['clips', jobId], queryFn: () => api.clips(jobId) })
}

export interface StageState {
  status: 'pending' | 'running' | 'done' | 'skipped' | 'error'
  progress: number
  eta: number | null
  message: string
}

export interface LogLine {
  seq: number
  level: string
  message: string
  ts: number
}

function initialStages(job?: Job): Record<string, StageState> {
  const out: Record<string, StageState> = {}
  const idx = job ? STAGES.indexOf(job.stage as StageName) : -1
  STAGES.forEach((s, i) => {
    let status: StageState['status'] = 'pending'
    if (job?.status === 'done' || job?.stage === 'metadata' || job?.stage === 'done') status = 'done'
    else if (idx > i) status = 'done'
    else if (idx === i) status = (job?.stage_status as StageState['status']) || 'running'
    out[s] = { status, progress: status === 'done' ? 1 : idx === i ? job?.progress ?? 0 : 0, eta: null, message: '' }
  })
  return out
}

/** Live job state from the SSE stream; also keeps react-query caches fresh. */
export function useJobEvents(jobId: number, onEvent?: (e: JobEvent) => void) {
  const qc = useQueryClient()
  const [stages, setStages] = useState<Record<string, StageState>>(() => initialStages())
  const [logs, setLogs] = useState<LogLine[]>([])
  const [connected, setConnected] = useState(false)
  const [current, setCurrent] = useState<string>('')
  const cb = useRef(onEvent)
  cb.current = onEvent

  useEffect(() => {
    const es = new EventSource(`/api/jobs/${jobId}/events`)
    let seen = new Set<number>()
    let bootId = ''
    // events up to replayUntil are history replayed on (re)connect: they rebuild the stepper/log,
    // but must not fire callbacks again (toasts like "Re-render finished" for old renders)
    let replayUntil = 0
    const handle = (ev: JobEvent) => {
      if (ev.type === 'snapshot') {
        if (ev.boot_id !== bootId) {
          seen = new Set() // server restarted: event numbers start again at 1
          bootId = ev.boot_id
        }
        replayUntil = ev.last_seq
      } else {
        if (seen.has(ev.seq)) return
        seen.add(ev.seq)
      }
      if (ev.type === 'snapshot' || ev.seq > replayUntil) cb.current?.(ev)
      switch (ev.type) {
        case 'snapshot':
          setStages(initialStages(ev.job))
          setCurrent(ev.job.stage)
          break
        case 'stage':
          if (!STAGES.includes(ev.stage as StageName)) break
          setCurrent(ev.stage)
          setStages((prev) => {
            const next = { ...prev }
            const i = STAGES.indexOf(ev.stage as StageName)
            if (ev.status === 'running') {
              // everything before a running stage is finished
              STAGES.slice(0, i).forEach((s) => {
                if (next[s].status === 'pending' || next[s].status === 'running') next[s] = { ...next[s], status: 'done', progress: 1 }
              })
            }
            next[ev.stage] = {
              status: ev.status as StageState['status'],
              progress: ev.status === 'done' || ev.status === 'skipped' ? 1 : 0,
              eta: null,
              message: ev.message,
            }
            return next
          })
          break
        case 'progress':
          if (!STAGES.includes(ev.stage as StageName)) break
          setStages((prev) => ({
            ...prev,
            [ev.stage]: { ...prev[ev.stage], status: 'running', progress: ev.progress, eta: ev.eta_sec, message: ev.message },
          }))
          break
        case 'log':
          setLogs((prev) => [...prev.slice(-400), { seq: ev.seq, level: ev.level, message: ev.message, ts: ev.ts }])
          break
        case 'status':
          qc.invalidateQueries({ queryKey: ['job', jobId] })
          qc.invalidateQueries({ queryKey: ['jobs'] })
          if (ev.status === 'done') {
            setStages((prev) => Object.fromEntries(Object.entries(prev).map(([k, v]) => [k, { ...v, status: v.status === 'skipped' ? 'skipped' : 'done', progress: 1 }])))
            qc.invalidateQueries({ queryKey: ['clips', jobId] })
          }
          break
        case 'clips':
        case 'clip_done':
        case 'render_done':
        case 'render_failed':
          qc.invalidateQueries({ queryKey: ['clips', jobId] })
          qc.invalidateQueries({ queryKey: ['job', jobId] })
          break
      }
    }
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    es.onmessage = (m) => handle(JSON.parse(m.data))
    es.addEventListener('snapshot', (m) => handle(JSON.parse((m as MessageEvent).data)))
    return () => es.close()
  }, [jobId, qc])

  return { stages, logs, connected, current }
}

export function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !isFinite(sec)) return '—'
  sec = Math.max(0, Math.round(sec))
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${s.toString().padStart(2, '0')}s`
  return `${s}s`
}

export function formatTime(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = sec - m * 60
  return `${m}:${s.toFixed(1).padStart(4, '0')}`
}
